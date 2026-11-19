"""Leg interface: the coordinator's view of one OKX account.

`LocalLeg` wraps the OKX broker in-process. Every method speaks plain
dicts, so the coordinator, the executor and the UI never depend on the
broker's own objects.

One convention in here is load-bearing and easy to lose in a rewrite:
`positions()` and `pending_orders()` return None for "unknown (OKX did
not answer)", which is NOT the same as "flat" / "no orders".
"""

import logging
import threading
import time

from .models import OrderSide


class LocalLeg:
    def __init__(self, broker):
        self.broker = broker
        self.name = broker.account.name

    def connect(self):
        return self.broker.initialize()

    def close(self):
        self.broker.shutdown()

    def ping(self):
        return self.broker.is_alive()

    def account_info(self):
        """Full margin picture per account — with two brokers, margin is
        posted per account, so every field the positions monitor needs
        comes from here rather than being aggregated away. Cached by the
        caller: this is an IPC round trip, not a free read."""
        info = self.broker.account_info()
        if not info:
            return None
        equity = getattr(info, 'equity', 0.0) or 0.0
        margin = getattr(info, 'margin', 0.0) or 0.0
        return {
            'account': self.name,
            'login': info.login, 'server': info.server,
            # The broker's own name for itself ("CFI Financial Group"),
            # where the server is a machine's name ("CFI3-Real").
            'company': getattr(info, 'company', None) or None,
            'name': getattr(info, 'name', ''),
            'currency': getattr(info, 'currency', 'USD'),
            'leverage': getattr(info, 'leverage', None),
            'balance': getattr(info, 'balance', 0.0),
            # Brokers often fund a demo with CREDIT rather than balance,
            # which makes balance alone read as an empty account —
            # 0.00 against 5,000 of equity, live.
            'credit': getattr(info, 'credit', 0.0),
            'equity': equity,
            'margin': margin,
            'margin_free': getattr(info, 'margin_free', 0.0),
            'margin_level': (getattr(info, 'margin_level', None)
                             or (100 * equity / margin if margin else None)),
            'margin_so_call': getattr(info, 'margin_so_call', None),
            'margin_so_so': getattr(info, 'margin_so_so', None),
            'profit': getattr(info, 'profit', 0.0),
            # OKX's fee tier: 'Lv1', 'Lv2'... the VIP level the fees
            # on every ladder are priced at.
            'fee_level': getattr(info, 'fee_level', None),
        }

    def ensure_symbol(self, symbol):
        info = self.broker.ensure_symbol(symbol)
        if not info:
            return {'ok': False, 'error': f'Symbol {symbol} not found'}
        return {
            'ok': True,
            'volume_min': getattr(info, 'volume_min', 0.01),
            'volume_max': getattr(info, 'volume_max', 1000.0),
            'volume_step': getattr(info, 'volume_step', 0.01),
            'point': getattr(info, 'point', 0.01),
            'tick_size': (getattr(info, 'trade_tick_size', 0)
                          or getattr(info, 'point', 0.01)),
        }

    def tick(self, symbol):
        t = self.broker.symbol_tick(symbol)
        if not t:
            return None
        return {'bid': t.bid, 'ask': t.ask, 'last': t.last,
                'time': getattr(t, 'time', time.time()),
                # The BROKER's own millisecond stamp, and whether the
                # symbol was in Market Watch when this was read. A leg
                # that looks frozen is one of two faults — not
                # subscribed, or subscribed and receiving nothing — and
                # these two fields are what tell them apart.
                'time_msc': getattr(t, 'time_msc', None),
                'visible': self.broker.last_visible.get(symbol)}

    def session_stats(self, symbol):
        return self.broker.session_stats(symbol)

    def depth(self, symbol):
        return self.broker.depth(symbol)

    def rates(self, symbol, timeframe_min, count):
        """History bars, oldest first, or None when there are none."""
        reader = getattr(self.broker, 'rates', None)
        return reader(symbol, timeframe_min, count) if reader else None

    def margin_for(self, symbol, side, volume, price=None):
        return self.broker.margin_for(symbol, side, volume, price)

    def resubscribe(self, symbol):
        tick = self.broker.resubscribe(symbol)
        if not tick:
            return None
        return {'bid': tick.bid, 'ask': tick.ask, 'last': tick.last,
                'time': getattr(tick, 'time', time.time())}

    def order(self, symbol, side, volume, slippage_points=1.0, comment=""):
        """Send a market order and report WHAT IS AT THE BROKER.

        `ok` is the broker's verdict on the request. `filled_volume`
        and `position_tickets` are the answer to a different and more
        important question - is anything of ours on? - and they are
        filled in EITHER WAY.

        They used to be zeroed whenever `ok` was false, on the
        assumption that a request the broker did not accept left
        nothing behind. A partial fill (10010), and a rejection that
        arrives after part of the order has already dealt, both break
        that assumption, and the caller then has no ticket to unwind
        and no volume to notice. That is how a leg went on, was
        reported as a refusal, and sat at the broker until the
        reconciler listed it as unclaimed.
        """
        result = self.broker.send_market_order(
            symbol, OrderSide(side), volume,
            slippage_points=slippage_points, comment=comment)
        filled = float(result.volume or 0.0) if result.success else 0.0
        price = result.executed_price
        position_tickets = []
        if result.ticket:
            # Resolve which position(s) the fill created (hedging mode)
            state = self.broker.order_fill_state(result.ticket)
            measured = float(state.get('filled_volume') or 0.0)
            tickets = list(state.get('position_tickets') or [])
            if result.success:
                filled = measured or filled
                position_tickets = tickets or [result.ticket]
            elif measured or tickets:
                # REFUSED, AND ON ANYWAY. The refusal stands - it is
                # the broker's own word and it reaches the screen - but
                # the position it left behind is now visible to the
                # unwind instead of being discovered by the reconciler.
                logging.critical(
                    "%s: the broker refused this order (%s) but %s lots are "
                    "ON, tickets %s - it is not a clean refusal",
                    symbol, result.error, measured,
                    ', '.join(str(t) for t in tickets) or 'unknown')
                filled = measured
                position_tickets = tickets
                price = state.get('price') or price
        return {
            'ok': result.success,
            'filled_volume': filled,
            'price': price,
            # The touch the order was SENT at, as MT5 was quoting it:
            # beside `price` it says how far this one leg slipped.
            'requested_price': result.requested_price,
            'ticket': result.ticket,
            'position_tickets': position_tickets,
            'error': result.error,
        }

    def place_limit(self, symbol, side, volume, price, comment="",
                    post_only=None, position_ticket=None):
        extra = {}
        if post_only is not None:
            extra['post_only'] = post_only
        if position_ticket is not None:
            extra['position_ticket'] = position_ticket
        return self.broker.place_pending_limit(
            symbol, OrderSide(side), volume, price, comment=comment, **extra)

    def pending_orders(self, symbol=None):
        return self.broker.pending_orders_by_magic(symbol)

    def modify_order(self, ticket, price):
        return self.broker.modify_pending(ticket, price)

    def cancel_order(self, ticket):
        return self.broker.cancel_pending(ticket)

    def order_state(self, ticket):
        return self.broker.order_fill_state(ticket)

    def close_ticket(self, symbol, ticket, volume, entry_side,
                     slippage_points=1.0, comment=""):
        result = self.broker.close_position_ticket(
            symbol, ticket, volume, OrderSide(entry_side),
            slippage_points=slippage_points, comment=comment)
        return {
            'ok': result.success,
            'filled_volume': result.volume if result.success else 0.0,
            'price': result.executed_price,
            'requested_price': result.requested_price,
            'error': result.error,
        }

    def positions(self, symbol=None):
        return self.broker.positions_by_magic(symbol)

    def order_log(self, hours=24):
        """This account's recent MT5 order/deal activity, each row
        stamped with the account it came from so the order log
        can show both accounts in one table."""
        return [dict(row, account=self.name)
                for row in (self.broker.order_log(hours) or ())]

    def terminal_report(self):
        return dict(self.broker.terminal_report(), account=self.name)

    def server_offset(self):
        """Seconds the BROKER's clock runs ahead of ours, or None.

        Everything on a session clock — the DAY cancel, the overnight
        rule — has to be measured against the broker's day, not this
        machine's. A box in one time zone and a broker in another is the
        normal case, not the exception, and a cutoff on the wrong clock
        fires hours early or late.

        None when it cannot be established. A guess here would be worse
        than an honest blank.
        """
        return self.broker.server_time_offset_sec()

    def symbol_report(self, symbol):
        return dict(self.broker.symbol_report(symbol), account=self.name)

    def find_symbols(self, pattern, limit=40):
        return self.broker.find_symbols(pattern, limit)

    def verify_order(self, ticket):
        """What MT5 itself holds for this ticket — proof the order
        reached the broker, not just that order_send returned ok."""
        return dict(self.broker.verify_ticket(ticket), account=self.name)
