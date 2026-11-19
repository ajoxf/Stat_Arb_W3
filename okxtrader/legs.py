"""Leg interface: the coordinator's view of one account's execution.

Two implementations with identical methods:
- LocalLeg  — wraps a BrokerSession in-process (same-terminal setups).
- RemoteLeg — talks to a leg_runner process over localhost TCP
  (required when spot and futures live on different MT5 terminals,
  because one process can hold only one MT5 connection).

All methods speak plain dicts so results are identical whether the
leg is local or remote — the coordinator, the executor and the UI must
never know which they are holding.

Ported from the stat-arb system. Two conventions in here are
load-bearing and easy to lose in a rewrite: `positions()` and
`pending_orders()` return None for "unknown (IPC failure)", which is
NOT the same as "flat"/"no orders"; and RemoteLeg dedups its
unreachable warning per (account, endpoint) because the UI opens a
short-lived leg on every poll.
"""

import logging
import threading
import time

from . import ipc
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


class RemoteLeg:
    # One line per (account, endpoint, error) instead of one per
    # attempt. The webapp opens a short-lived RemoteLeg every time the
    # Accounts page polls — every 15s, two legs at a time — so an
    # account whose runner is not up wrote hundreds of identical
    # WARNING lines and buried the coordinator's own output. Live
    # 2026-08-11 while a second account was being added.
    #
    # Class-level, because the offending clients are short-lived
    # objects: dedup on the instance would never match twice.
    _reported = set()

    def __init__(self, name, endpoint, timeout=10.0):
        self.name = name
        self.host, self.port = ipc.parse_endpoint(endpoint)
        # Normalised, so "what is this process actually talking to" can
        # be published and compared against the saved config.
        self.endpoint_label = f'{self.host}:{self.port}'
        self.timeout = timeout
        self.conn = None
        #: ONE request at a time on this connection, whichever thread
        #: sends it. The poll, the command thread and the reconciler all
        #: talk to the same leg; without this, two of them read each
        #: other's replies. It happened: a chart's 200 KB of bars was
        #: half-read by the price poll, every reply after it was read out
        #: of step, and the leg read as dark until the engine restarted.
        self._io = threading.RLock()
        self._last_reconnect = None

    #: How often a connection that failed is tried again, at most.
    RECONNECT_SEC = 2.0

    def connect(self, retries=5, delay=2.0):
        for attempt in range(1, retries + 1):
            try:
                self.conn = ipc.connect(self.host, self.port, self.timeout)
                reply = self.conn.request({'cmd': 'ping'})
                if reply and reply.get('ok'):
                    logging.info("Connected to leg runner '%s' at %s:%s",
                                 self.name, self.host, self.port)
                    # A leg that comes back is news, and so is it going
                    # away again later.
                    RemoteLeg._reported.discard(
                        (self.name, self.host, self.port))
                    return True
            except OSError as e:
                key = (self.name, self.host, self.port)
                if key not in RemoteLeg._reported:
                    RemoteLeg._reported.add(key)
                    logging.warning(
                        "Leg '%s' not reachable at %s:%s: %s — is its leg "
                        "runner started? (further attempts logged at debug)",
                        self.name, self.host, self.port, e)
                else:
                    logging.debug(
                        "Leg '%s' still not reachable at %s:%s (attempt "
                        "%d/%d): %s", self.name, self.host, self.port,
                        attempt, retries, e)
                time.sleep(delay)
        return False

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None

    def _request(self, msg):
        with self._io:
            if not self.conn and not self._reconnect():
                return None
            try:
                return self.conn.request(msg)
            except (OSError, ValueError) as e:
                # The connection is closed, not reused: whatever is left
                # of a reply in it would be read as the answer to the
                # next question.
                logging.error("IPC failure to leg '%s': %s", self.name, e)
                self.close()
                return None

    def _reconnect(self):
        """A connection that failed is opened again by itself, once
        every RECONNECT_SEC at most - the leg runner and its MT5 session
        are still there. It used to stay closed until the engine was
        restarted, with the leg reading as unknown all that time.

        Nothing is re-sent: a request that failed has already answered
        None (unknown), and the caller has treated it as that."""
        now = time.monotonic()
        if self._last_reconnect is not None and \
                now - self._last_reconnect < self.RECONNECT_SEC:
            return False
        self._last_reconnect = now
        if self.connect(retries=1, delay=0.0):
            logging.warning("leg '%s': reconnected after an IPC failure",
                            self.name)
            return True
        return False

    def ping(self):
        reply = self._request({'cmd': 'ping'})
        return bool(reply and reply.get('ok'))

    def account_info(self):
        reply = self._request({'cmd': 'account_info'})
        return reply.get('account') if reply and reply.get('ok') else None

    def ensure_symbol(self, symbol):
        reply = self._request({'cmd': 'ensure_symbol', 'symbol': symbol})
        return reply if reply else {'ok': False, 'error': 'IPC failure'}

    def tick(self, symbol):
        reply = self._request({'cmd': 'tick', 'symbol': symbol})
        if reply and reply.get('ok'):
            return reply['tick']
        return None

    def session_stats(self, symbol):
        reply = self._request({'cmd': 'session_stats', 'symbol': symbol})
        if reply and reply.get('ok'):
            return reply.get('stats')
        return None

    def depth(self, symbol):
        reply = self._request({'cmd': 'depth', 'symbol': symbol})
        if reply and reply.get('ok'):
            return reply.get('depth')
        return None

    def rates(self, symbol, timeframe_min, count):
        reply = self._request({'cmd': 'rates', 'symbol': symbol,
                               'timeframe_min': timeframe_min,
                               'count': count})
        if reply and reply.get('ok'):
            return reply.get('rates')
        return None       # unknown, which is NOT empty

    def resubscribe(self, symbol):
        reply = self._request({'cmd': 'resubscribe', 'symbol': symbol})
        if reply and reply.get('ok'):
            return reply.get('tick')
        return None

    def margin_for(self, symbol, side, volume, price=None):
        reply = self._request({'cmd': 'margin', 'symbol': symbol,
                               'side': side, 'volume': volume,
                               'price': price})
        if reply and reply.get('ok'):
            return reply.get('margin')
        return None

    def order(self, symbol, side, volume, slippage_points=1.0, comment=""):
        reply = self._request({
            'cmd': 'order', 'symbol': symbol, 'side': side,
            'volume': volume, 'slippage_points': slippage_points,
            'comment': comment,
        })
        if not reply:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'ticket': None, 'position_tickets': [],
                    'error': 'IPC failure during order'}
        return reply

    def place_limit(self, symbol, side, volume, price, comment=""):
        reply = self._request({
            'cmd': 'place_limit', 'symbol': symbol, 'side': side,
            'volume': volume, 'price': price, 'comment': comment,
        })
        return reply or {'ok': False, 'ticket': None, 'error': 'IPC failure'}

    def pending_orders(self, symbol=None):
        reply = self._request({'cmd': 'pending_orders', 'symbol': symbol})
        if reply and reply.get('ok'):
            return reply['orders']
        return None    # None = unknown (IPC failure), NOT "no orders"

    def modify_order(self, ticket, price):
        reply = self._request({'cmd': 'modify_order', 'ticket': ticket,
                               'price': price})
        return reply or {'ok': False, 'error': 'IPC failure'}

    def cancel_order(self, ticket):
        reply = self._request({'cmd': 'cancel_order', 'ticket': ticket})
        # `readable` FALSE: we did not reach the broker, so we know
        # NOTHING about this pending - not that it is gone, and not that
        # it is still there. `_pull` keeps the ticket on this answer
        # rather than forgetting a pending that is still live.
        return reply or {'ok': False, 'readable': False,
                         'cancelled': False, 'filled_volume': 0.0,
                         'price': None, 'position_tickets': [],
                         'filled_at': None, 'server_offset_sec': None,
                         'still_open': True, 'error': 'IPC failure'}

    def order_state(self, ticket):
        reply = self._request({'cmd': 'order_state', 'ticket': ticket})
        # `readable` FALSE, not a filled volume of zero.
        #
        # The caller has to tell "this order has not filled" from "I
        # could not ask". They are opposite instructions: one says carry
        # on, the other says stop and pull. This dict carried 0.0 and
        # the reader looked no further, so an unreachable leg read as a
        # quiet one - see quoter._check_fill.
        return reply or {'ok': False, 'readable': False,
                         'filled_volume': 0.0, 'price': None,
                         'position_tickets': [],
                         'filled_at': None, 'server_offset_sec': None,
                         'still_open': True, 'error': 'IPC failure'}

    def close_ticket(self, symbol, ticket, volume, entry_side,
                     slippage_points=1.0, comment=""):
        reply = self._request({
            'cmd': 'close_ticket', 'symbol': symbol, 'ticket': ticket,
            'volume': volume, 'entry_side': entry_side,
            'slippage_points': slippage_points, 'comment': comment,
        })
        return reply or {'ok': False, 'filled_volume': 0.0, 'price': None,
                         'error': 'IPC failure during close'}

    def positions(self, symbol=None):
        reply = self._request({'cmd': 'positions', 'symbol': symbol})
        if reply and reply.get('ok'):
            return reply['positions']
        return None    # None = unknown (IPC failure), NOT "flat"

    def order_log(self, hours=24):
        reply = self._request({'cmd': 'order_log', 'hours': hours})
        if reply and reply.get('ok'):
            return [dict(row, account=self.name)
                    for row in (reply.get('orders') or ())]
        return None    # None = unknown (IPC failure), NOT "no activity"

    def terminal_report(self):
        reply = self._request({'cmd': 'terminal_report'})
        if reply and reply.get('ok'):
            return dict(reply['report'], account=self.name)
        return {'account': self.name, 'library': None, 'terminal': False,
                'error': 'leg runner not reachable'}

    def server_offset(self):
        reply = self._request({'cmd': 'server_offset'})
        if reply and reply.get('ok'):
            return reply.get('offset')
        return None       # unknown, which is NOT zero

    def symbol_report(self, symbol):
        reply = self._request({'cmd': 'symbol_report', 'symbol': symbol})
        if reply and reply.get('ok'):
            return dict(reply['report'], account=self.name)
        return {'account': self.name, 'symbol': symbol, 'found': False,
                'error': 'leg runner not reachable'}

    def find_symbols(self, pattern, limit=40):
        reply = self._request({'cmd': 'find_symbols', 'pattern': pattern,
                               'limit': limit})
        if reply and reply.get('ok'):
            return reply['symbols']
        return None

    def verify_order(self, ticket):
        reply = self._request({'cmd': 'verify_order', 'ticket': ticket})
        if reply and reply.get('ok'):
            return dict(reply['verification'], account=self.name)
        return {'account': self.name, 'ticket': ticket, 'confirmed': False,
                'error': 'leg runner not reachable'}
