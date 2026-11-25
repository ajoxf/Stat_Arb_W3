"""Fakes. The whole desk is testable with nothing in here needing OKX,
a network or a clock (the OKX broker itself is tested against the
simulated exchange, okx_sim).

`FakeBroker` keeps a real long/short book: an opposite market order
opens a SECOND position and does not close the first. That is the
behaviour the executor's ticket-based closes exist for, so faking it
any other way would make the tests agree with a bug.
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from okxtrader.okx_broker import OrderResult                        # noqa: E402
from okxtrader.config import PairConfig, TraderConfig           # noqa: E402
from okxtrader.legs import LocalLeg                             # noqa: E402
from okxtrader.models import MAGIC_NUMBER, OrderSide            # noqa: E402


class FakeSymbol:
    def __init__(self, name, bid, ask, contract_size=100.0, volume_min=0.01,
                 volume_step=0.01, volume_max=100.0, tick_size=0.01,
                 trade_allowed=True, swap_long=None, swap_short=None,
                 swap_mode=None, expiry=0):
        self.name = name
        self.bid = bid
        self.ask = ask
        self.contract_size = contract_size
        self.volume_min = volume_min
        self.volume_step = volume_step
        self.volume_max = volume_max
        self.tick_size = tick_size
        self.trade_allowed = trade_allowed
        #: What the broker charges to hold this symbol overnight, and
        #: WHAT UNITS that figure is in. None on both by default: most
        #: symbols on a fixture have no swap, and a fixture that always
        #: reported one would make the "unconvertible is not zero" test
        #: pass on anything.
        self.swap_long = swap_long
        self.swap_short = swap_short
        self.swap_mode = swap_mode
        self.expiry = expiry
        self.time = 1_700_000_000
        #: {'open','high','low','volume'} when this broker publishes a
        #: session for the symbol, None when it does not.
        self.session = None

    def quote(self, bid, ask):
        self.bid, self.ask = bid, ask
        self.time += 1


class FakeBroker:
    """One account's book, in memory."""

    def __init__(self, name='fake', symbols=None, timeline=None, login=None):
        self.account = SimpleNamespace(name=name)
        #: One MT5 login per broker, DIFFERENT per account by default —
        #: two legs reporting the same login is a real fault (both
        #: runners attached to one terminal), and a fixture that always
        #: modelled it would make the test for it pass on anything.
        self.login = login or (100000 + (abs(hash(name)) % 8999))
        #: What this account is geared at. None models a broker that
        #: reports none — where a margin the terminal cannot price has
        #: no fallback either, and the target must simply not appear.
        self.leverage = 100
        self.symbols = {s.name: s for s in (symbols or [])}
        #: ticket -> position dict. Hedging mode: one per fill.
        self.positions = {}
        self.pendings = {}
        #: Every request that reached the broker, in order, so a test can
        #: assert that an "unwind" never sent an opposite market order.
        self.sent = []
        #: Shared across both accounts when the fixture passes one, so a
        #: test can assert which leg reached its broker FIRST.
        self.timeline = [] if timeline is None else timeline
        self.fail_symbols = set()
        #: Fails CLOSES only, leaving entries working — the shape of a
        #: leg that goes on and then cannot be taken off again.
        self.fail_closes = set()
        self.reject_orders = {}          # symbol -> error string
        #: symbol -> price points a market order fills WORSE than the
        #: touch it was sent at: a broker that executes off its quote.
        self.fill_worse = {}
        #: symbol -> (lots that DEAL anyway, the broker's refusal).
        #: The shape the real fault had: order_send answers with a
        #: retcode that is not DONE, and a position is on regardless -
        #: 10010 DONE_PARTIAL, or a rejection that lands after the
        #: first deal. A fake that can only reject cleanly cannot see
        #: it, which is why nothing caught this until a desk did.
        self.refuse_but_fill = {}
        #: Margin per lot this terminal reports, or None for a broker
        #: that cannot price it.
        self.margin_per_lot = None
        #: symbol -> was it in Market Watch when a tick was last read?
        #: The real one records this on every read; the fake says every
        #: symbol it knows is watched.
        self.last_visible = {}
        self.next_ticket = 1000
        self.next_deal = 500000
        self.connected = False
        #: MT5's own deal history — what `order_log()` reads back, and
        #: what the journal is built from. The fake records a deal on
        #: every fill, exactly as the terminal does, including for
        #: positions opened outside this system.
        self.deals = []
        #: Seconds the broker's clock runs ahead of ours.
        self.server_offset_sec = 3 * 3600
        #: Our own clock, so a test can put a fill in the past. The real
        #: one stamps every deal and position with the SERVER's wall
        #: clock; the fake does too (`_stamp`), because the whole point
        #: of the naked-window measurement is that those two clocks are
        #: hours apart and the offset has to come off before they can be
        #: subtracted.
        self.now = time.time
        #: symbol -> MT5 history bars, oldest first, as `rates` returns
        #: them. A symbol with none answers None — the terminal had no
        #: history — which is NOT an empty list.
        self.bars = {}
        #: (symbol, timeframe in minutes, count) for every history read.
        self.rates_asked = []

    # -- helpers for tests -------------------------------------------------

    def quote(self, symbol, bid, ask):
        self.symbols[symbol].quote(bid, ask)

    def open_positions(self, symbol=None):
        return [p for p in self.positions.values()
                if symbol is None or p['symbol'] == symbol]

    def _record(self, entry):
        entry = dict(entry, account=self.account.name)
        self.sent.append(entry)
        self.timeline.append(entry)
        return entry

    def _stamp(self):
        """The broker's wall clock, as MT5 encodes it on a deal."""
        return int(self.now() + self.server_offset_sec)

    def back_date(self, ticket, seconds):
        """This position filled `seconds` ago and nobody noticed.

        The shape of 2026-09-16, in one line a test can read.
        """
        self.positions[int(ticket)]['filled_at'] -= int(seconds)

    def _ticket(self):
        self.next_ticket += 1
        return self.next_ticket

    # -- the BrokerSession interface --------------------------------------

    def initialize(self):
        self.connected = True
        return True

    def shutdown(self):
        self.connected = False

    def is_alive(self):
        return self.connected

    def account_info(self):
        return SimpleNamespace(
            login=self.login, server='FakeServer', name=self.account.name,
            currency='USD', leverage=self.leverage, balance=100_000.0,
            equity=100_000.0, margin=0.0, margin_free=100_000.0,
            margin_level=None, margin_so_call=50.0, margin_so_so=30.0,
            profit=sum(p.get('profit', 0.0) for p in self.positions.values()))

    def ensure_symbol(self, symbol):
        info = self.symbols.get(symbol)
        if info is not None:
            self.last_visible[symbol] = True
        return info

    def resubscribe(self, symbol):
        """Dropped from Market Watch and taken back. The fake counts
        it, because the point of the button is that it HAPPENS."""
        self.resubscribed = getattr(self, 'resubscribed', [])
        self.resubscribed.append(symbol)
        info = self.symbols.get(symbol)
        if info is None:
            return None
        info.time += 1                    # a fresh tick, as a real one is
        from types import SimpleNamespace
        return SimpleNamespace(bid=info.bid, ask=info.ask, last=info.bid,
                               time=info.time)

    def margin_for(self, symbol, side, volume, price=None):
        """What this terminal says the margin is. None when it cannot
        price it — the case the take-profit target has to survive."""
        if getattr(self, 'margin_per_lot', None) is None:
            return None
        return float(self.margin_per_lot) * float(volume)

    def depth(self, symbol):
        """The DOM this broker publishes, or None.

        None by default: most CFD accounts publish no depth at all, and
        that is the case the ladder has to handle without inventing a
        size.
        """
        return getattr(self, 'depth_book', None)

    def session_stats(self, symbol):
        """What the terminal publishes for this symbol's own session.

        None when the broker publishes nothing, which is the case the
        ladder has to fall back from — several brokers fill in no
        session fields at all for CFDs.
        """
        info = self.symbols.get(symbol)
        if info is None or getattr(info, 'session', None) is None:
            return None
        return dict(info.session, symbol=symbol)

    def symbol_info(self, symbol):
        return self.symbols.get(symbol)

    def symbol_tick(self, symbol):
        info = self.symbols.get(symbol)
        if info is None:
            return None
        return SimpleNamespace(bid=info.bid, ask=info.ask,
                               last=(info.bid + info.ask) / 2, time=info.time)

    def find_symbols(self, pattern, limit=40):
        needle = (pattern or '').upper()
        return [{'symbol': s.name, 'description': s.name,
                 'contract_size': s.contract_size, 'volume_min': s.volume_min,
                 'volume_max': s.volume_max, 'volume_step': s.volume_step}
                for s in self.symbols.values() if needle in s.name.upper()]

    def symbol_report(self, symbol):
        info = self.symbols.get(symbol)
        if info is None:
            return {'symbol': symbol, 'found': False,
                    'error': f'{symbol} does not exist on this broker'}
        return {
            'symbol': symbol, 'found': True, 'description': symbol,
            'visible': True, 'bid': info.bid, 'ask': info.ask,
            'digits': 2, 'point': info.tick_size, 'tick_size': info.tick_size,
            'tick_value': info.contract_size * info.tick_size,
            'contract_size': info.contract_size,
            'volume_min': info.volume_min, 'volume_max': info.volume_max,
            'volume_step': info.volume_step, 'currency': 'USD',
            'filling_mode': 1, 'trade_mode': 4,
            'trade_allowed': info.trade_allowed,
            'expiry': getattr(info, 'expiry', 0),
            'swap_long': info.swap_long, 'swap_short': info.swap_short,
            'swap_mode': info.swap_mode, 'swap_rollover3days': 3,
        }

    def rates(self, symbol, timeframe_min, count):
        self.rates_asked.append((symbol, timeframe_min, count))
        bars = self.bars.get(symbol)
        return None if bars is None else [dict(b) for b in bars[-count:]]

    def server_time_offset_sec(self):
        """Seconds this broker's clock runs ahead of ours — measured
        from a tick, as the real one does."""
        return self.server_offset_sec

    def terminal_report(self):
        return {'library': True, 'terminal': True, 'logged_in': True,
                'algo_trading': True, 'hedging': True,
                'login': self.login, 'server': 'FakeServer'}

    def verify_ticket(self, ticket, attempts=3, delay=0.0):
        position = self.positions.get(int(ticket))
        if position is None:
            return {'ticket': ticket, 'confirmed': False,
                    'error': 'not found'}
        return {'ticket': ticket, 'confirmed': True, 'position_open': True,
                'symbol': position['symbol'], 'volume': position['volume'],
                'price': position['price_open'], 'source': 'open position'}

    def send_market_order(self, symbol, side, volume, slippage_points=1.0,
                          comment=""):
        self._record({'action': 'market', 'symbol': symbol,
                      'side': side.value, 'volume': volume,
                      'comment': comment})
        if symbol in self.fail_symbols:
            return OrderResult(False, error='forced failure')
        if symbol in self.reject_orders:
            return OrderResult(False, error=self.reject_orders[symbol])
        info = self.symbols[symbol]
        price = info.ask if side is OrderSide.BUY else info.bid
        ticket = self._ticket()
        if symbol in self.refuse_but_fill:
            dealt, error = self.refuse_but_fill[symbol]
            dealt = min(float(dealt), float(volume))
            magic = MAGIC_NUMBER if comment != 'by hand' else 0
            self.positions[ticket] = {
                'ticket': ticket, 'symbol': symbol, 'side': side.value,
                'volume': dealt, 'price_open': price, 'magic': magic,
                'comment': comment, 'profit': 0.0,
                'filled_at': self._stamp()}
            self._record_deal(symbol, side.value, dealt, price, 'open',
                              comment, ticket, magic)
            # The TICKET comes back with the refusal, exactly as
            # BrokerSession does now: without it nothing upstream can
            # find out that the refusal left a position on.
            return OrderResult(False, requested_price=price, ticket=ticket,
                               error=error)
        # HEDGING: this OPENS a position. It never closes another one.
        magic = MAGIC_NUMBER if comment != 'by hand' else 0
        worse = float(self.fill_worse.get(symbol, 0.0))
        filled = price + worse if side is OrderSide.BUY else price - worse
        self.positions[ticket] = {
            'ticket': ticket, 'symbol': symbol, 'side': side.value,
            'volume': volume, 'price_open': filled, 'magic': magic,
            'comment': comment, 'profit': 0.0, 'filled_at': self._stamp()}
        self._record_deal(symbol, side.value, volume, filled, 'open',
                          comment, ticket, magic)
        return OrderResult(True, requested_price=price,
                           executed_price=filled, ticket=ticket,
                           volume=volume)

    def order_fill_state(self, ticket):
        position = self.positions.get(int(ticket))
        pending = self.pendings.get(int(ticket))
        if position is not None:
            return {'ok': True, 'filled_volume': position['volume'],
                    'price': position['price_open'],
                    'position_tickets': [position['ticket']],
                    'filled_at': position.get('filled_at'),
                    'server_offset_sec': self.server_offset_sec,
                    'still_open': pending is not None, 'error': None}
        # A CLOSING pending leaves no position behind — only a deal, as
        # the real one does. Read it the same way the broker does.
        deals = [d for d in self.deals
                 if str(d.get('order_id')) == str(int(ticket))]
        volume = sum(d['fill_qty'] for d in deals)
        if volume > 0:
            price = (sum(d['fill_price'] * d['fill_qty'] for d in deals)
                     / volume)
            return {'ok': True, 'filled_volume': volume, 'price': price,
                    'position_tickets': [d['position_id'] for d in deals
                                         if d.get('position_id')],
                    'filled_at': max(int(d['filled_at'] / 1000)
                                     for d in deals),
                    'server_offset_sec': self.server_offset_sec,
                    'still_open': pending is not None, 'error': None}
        # No fill, so no fill time. None, never a zero - the reader has
        # to be able to tell "not filled" from "filled at the epoch".
        return {'ok': True, 'filled_volume': 0.0, 'price': None,
                'position_tickets': [], 'still_open': pending is not None,
                'filled_at': None, 'server_offset_sec': None,
                'error': None}

    def close_position_ticket(self, symbol, ticket, volume, entry_side,
                              slippage_points=1.0, comment=""):
        self._record({'action': 'close', 'symbol': symbol,
                      'ticket': ticket, 'volume': volume,
                      'entry_side': entry_side.value, 'comment': comment})
        position = self.positions.get(int(ticket))
        if position is None:
            return OrderResult(False, error=f'no position {ticket}')
        if symbol in self.fail_symbols or symbol in self.fail_closes:
            return OrderResult(False, error='forced failure')
        info = self.symbols[symbol]
        close_side = entry_side.opposite
        quoted = info.ask if close_side is OrderSide.BUY else info.bid
        worse = float(self.fill_worse.get(symbol, 0.0))
        price = quoted + worse if close_side is OrderSide.BUY \
            else quoted - worse
        move = price - position['price_open']
        if entry_side is OrderSide.SELL:
            move = -move
        profit = move * volume * info.contract_size
        self._record_deal(symbol, close_side.value, volume, price, 'close',
                          comment, int(ticket), position['magic'], profit)
        if volume >= position['volume'] - 1e-9:
            del self.positions[int(ticket)]
        else:
            position['volume'] -= volume
        return OrderResult(True, requested_price=quoted,
                           executed_price=price, ticket=int(ticket),
                           volume=volume)

    def positions_by_magic(self, symbol=None):
        """Magic-scoped, so the trader's own terminal positions are
        never touched — and never mistaken for ours."""
        return [{'ticket': p['ticket'], 'symbol': p['symbol'],
                 'side': p['side'], 'volume': p['volume'],
                 'price_open': p['price_open'],
                 # The COMMENT, because the real broker reports it and
                 # the reconciler needs it to tell one of OUR legs from
                 # a stray position. This double omitted it exactly as
                 # the real one did, which is why no test caught the
                 # day a trader's own spread was closed sixty seconds
                 # after they put it on.
                 'comment': p.get('comment', '')}
                for p in self.positions.values()
                if p['magic'] == MAGIC_NUMBER
                and (symbol is None or p['symbol'] == symbol)]

    def place_pending_limit(self, symbol, side, volume, price, comment="",
                            position_ticket=None):
        if symbol in self.fail_symbols:
            return {'ok': False, 'ticket': None, 'error': 'forced failure'}
        ticket = self._ticket()
        self.pendings[ticket] = {'ticket': ticket, 'symbol': symbol,
                                 'side': side.value, 'volume': volume,
                                 'price': price, 'comment': comment,
                                 # With a position on the request, this
                                 # limit CLOSES that position when it
                                 # executes. Without one, on a hedging
                                 # account, it OPENS a second position —
                                 # and the fake has to model the
                                 # difference or the test proves
                                 # nothing.
                                 'position_ticket': position_ticket}
        self._record({'action': 'pending', 'symbol': symbol,
                      'side': side.value, 'volume': volume, 'price': price,
                      'position_ticket': position_ticket})
        return {'ok': True, 'ticket': ticket, 'error': None, 'price': price}

    def part_fill_pending(self, ticket, volume):
        """A partial fill: some volume executes, the rest of the pending
        keeps resting — which is what MT5 does, and what a naive 'the
        pending is gone' model hides.

        A pending carrying a POSITION closes that much OF IT and opens
        nothing, exactly as a full fill of one does. Modelling a partial
        close as an OPEN made the fake disagree with the broker in the
        one case the closing path is hardest to get right, and a test
        run against that fake proves nothing about a live account.
        """
        pending = self.pendings[int(ticket)]
        pending['volume'] -= volume
        closes = pending.get('position_ticket')
        if closes:
            self._close_against(pending, int(closes), volume, int(ticket))
            return int(ticket)
        self.positions[int(ticket)] = {
            'ticket': int(ticket), 'symbol': pending['symbol'],
            'side': pending['side'], 'volume': volume,
            'price_open': pending['price'], 'magic': MAGIC_NUMBER,
            'comment': pending['comment'], 'profit': 0.0,
            'filled_at': self._stamp()}
        return int(ticket)

    def _close_against(self, pending, closes, filled, order_ticket):
        """A closing pending executing: the deal belongs to the closing
        ORDER and points at the position it reduced."""
        target = self.positions.get(int(closes))
        if target is None:
            return
        entry = OrderSide(target['side'])
        self._record_deal(pending['symbol'], entry.opposite.value,
                          filled, pending['price'], 'close',
                          pending['comment'], int(closes), MAGIC_NUMBER,
                          order_ticket=order_ticket)
        if filled >= target['volume'] - 1e-9:
            del self.positions[int(closes)]
        else:
            target['volume'] -= filled

    def fill_pending(self, ticket, volume=None):
        """The broker fills a resting order.

        MT5 turns a filled pending into a POSITION carrying the ORDER's
        ticket, and `positions_get` shows it BEFORE deal history does —
        so the fake does the same, because reading deals alone once
        called a real fill "no fill".

        A pending carrying a POSITION is the other case: it closes that
        position instead, and opens nothing.
        """
        pending = self.pendings.pop(int(ticket))
        filled = pending['volume'] if volume is None else volume
        closes = pending.get('position_ticket')
        if closes:
            self._close_against(pending, int(closes), filled, int(ticket))
            return int(ticket)
        self.positions[int(ticket)] = {
            'ticket': int(ticket), 'symbol': pending['symbol'],
            'side': pending['side'], 'volume': filled,
            'price_open': pending['price'], 'magic': MAGIC_NUMBER,
            'comment': pending['comment'], 'profit': 0.0,
            'filled_at': self._stamp()}
        return int(ticket)

    def pending_orders_by_magic(self, symbol=None):
        return [dict(p) for p in self.pendings.values()
                if symbol is None or p['symbol'] == symbol]

    def modify_pending(self, ticket, price):
        pending = self.pendings.get(int(ticket))
        if pending is None:
            return {'ok': False, 'error': 'order not open'}
        pending['price'] = price
        self._record({'action': 'modify', 'ticket': ticket, 'price': price})
        return {'ok': True, 'error': None}

    def cancel_pending(self, ticket):
        pending = self.pendings.pop(int(ticket), None)
        state = self.order_fill_state(ticket)
        state['cancelled'] = pending is not None
        if pending is None:
            state['error'] = f'no pending {ticket}'
        return state

    def _record_deal(self, symbol, side, volume, price, entry, comment,
                     position_ticket, magic, profit=0.0, order_ticket=None):
        self.next_deal += 1
        self.deals.append({
            # Usually the same number — MT5 gives a filled market order's
            # position the order's ticket. A CLOSING pending is the case
            # where they differ: the deal belongs to the closing ORDER
            # and points at the position it closed.
            'order_id': str(order_ticket if order_ticket is not None
                            else position_ticket),
            'deal_id': str(self.next_deal),
            'symbol': symbol, 'inst_type': 'DEAL', 'side': side.lower(),
            'pos_side': entry, 'order_type': 'market',
            'quantity': volume, 'fill_qty': volume, 'fill_price': price,
            # THE SAME KEYS THE REAL order_log EMITS, NO MORE AND NO
            # FEWER. This double used to be RICHER than the thing it
            # doubles: it carried commission, swap and magic while
            # BrokerSession.order_log emitted only their merged `fee`.
            # Tests asserting `fill['commission'] < 0` passed against
            # the fake and could never have failed, while every real
            # fill wrote NULL. A double that supplies what production
            # does not manufactures confidence, which is worse than a
            # gap. test_journal_row_shape pins the two key sets equal.
            'commission': -0.7 * volume, 'swap': 0.0,
            'fee': -0.7 * volume, 'fee_ccy': 'USD', 'pnl': profit,
            'state': 'filled',
            'filled_at': int((time.time() + self.server_offset_sec) * 1000),
            'position_id': position_ticket, 'magic': magic,
            'is_bot': magic == MAGIC_NUMBER, 'comment': comment,
            'server_offset_sec': self.server_offset_sec,
        })
        return self.deals[-1]

    def order_log(self, hours=24):
        """Everything this account did, as MT5 reports it — ours and the
        trader's own terminal clicks alike."""
        return [dict(deal, account=self.account.name) for deal in self.deals]


class FakeLeg(LocalLeg):
    """The real LocalLeg over a FakeBroker — the wiring is under test too."""

    def __init__(self, broker):
        super().__init__(broker)
        self.broker.initialize()


@pytest.fixture
def gold_symbols():
    #: Spot 0.01-lot minimum against a future's 0.10 — CFI's real shape,
    #: and the reason a click is quoted in spreads rather than leg lots.
    return (FakeSymbol('XAUUSD_', 4292.00, 4292.20, contract_size=100.0,
                       volume_min=0.01, volume_step=0.01),
            FakeSymbol('GC1226', 4351.00, 4351.40, contract_size=100.0,
                       volume_min=0.10, volume_step=0.10))


@pytest.fixture
def timeline():
    """Every request that reached EITHER broker, in the order it did."""
    return []


@pytest.fixture
def legs(gold_symbols, timeline):
    spot, future = gold_symbols
    a = FakeLeg(FakeBroker('acct_a', [spot], timeline))
    b = FakeLeg(FakeBroker('acct_b', [future], timeline))
    return {'acct_a': a, 'acct_b': b}


@pytest.fixture
def pair():
    return PairConfig(
        'XAUUSD_|GC1226', name='Gold basis',
        leg_a={'account': 'acct_a', 'symbol': 'XAUUSD_'},
        leg_b={'account': 'acct_b', 'symbol': 'GC1226'},
        hedge_ratio=1.0, hedge_ratio_for='XAUUSD_|GC1226',
        increment=0.01, default_quantity=1.0,
        # What ONE Qty is on each leg. Typed now, not derived — and set
        # to what this pair's matched minimum used to resolve to, so
        # every test written against that size goes on testing what it
        # was written to test. The future's minimum is 0.10 against the
        # spot's 0.01, which is CFI's real shape and the reason a click
        # is quoted in Qty rather than in lots.
        clip_lots_a=0.10, clip_lots_b=0.10)


@pytest.fixture
def config(pair):
    cfg = TraderConfig(pairs={pair.key: pair})
    cfg.settings['LEG_DEADLINE_SEC'] = 0.05     # tests do not wait 2s
    return cfg


# -- which kind of working order --------------------------------------------------


def pytest_configure(config):
    config.addinivalue_line(
        'markers', 'quote: run with WORKING_ORDERS = QUOTE, the one-leg '
                   'pending the desk used before the spread trigger')


@pytest.fixture(autouse=True, scope='session')
def _fill_style():
    """Mirror's engine tests drive the TAKER crossing (both legs at
    market), which stays available as FILL_STYLE = taker. The W3 maker
    fill, the OKX default, has its own tests against the simulated
    exchange (test_maker.py, test_maker_desk.py)."""
    from okxtrader import config as cfg
    was = (cfg.DEFAULT_SETTINGS['FILL_STYLE'],
           cfg.DEFAULT_SETTINGS['MARKET_HOURS'])
    # ...and Mirror's session cutoff, which its session tests exercise;
    # the OKX default is 24/7 (test_crypto_hours.py).
    cfg.DEFAULT_SETTINGS['FILL_STYLE'] = 'taker'
    cfg.DEFAULT_SETTINGS['MARKET_HOURS'] = 'SESSION'
    yield
    cfg.DEFAULT_SETTINGS['FILL_STYLE'], \
        cfg.DEFAULT_SETTINGS['MARKET_HOURS'] = was


@pytest.fixture(autouse=True)
def _no_coingecko(monkeypatch):
    """BTC.D is read from CoinGecko on the trading PC. Never from a
    test: a coordinator built here gets a stand-in that refuses, so no
    test waits on - or depends on - the internet."""
    from okxtrader import dominance

    def offline(url, timeout=10.0):
        raise ConnectionError('no network in tests')
    monkeypatch.setattr(dominance, '_http_get', offline)


@pytest.fixture(autouse=True)
def _no_economic_calendar(monkeypatch):
    """The calendar reads BLS, DOL and EIA on the trading PC. A
    coordinator built in a test has it off; the tests of the calendar
    itself switch it on, with agencies of their own."""
    from okxtrader import config as cfg, econcal

    def offline(*args, **kwargs):
        raise ConnectionError('no network in tests')
    monkeypatch.setitem(cfg.DEFAULT_SETTINGS, 'CALENDAR_ON', False)
    monkeypatch.setattr(econcal, '_http_get', offline)
    monkeypatch.setattr(econcal, '_http_post_json', offline)


@pytest.fixture(autouse=True)
def _working_orders(request, monkeypatch):
    """Working orders are held and crossed at the level by default. The
    tests of the old one-leg quote - its fills, hedges, pulls and the
    naked legs it can leave - still run against it, marked `quote`, so
    the desk setting that brings it back stays safe."""
    if request.node.get_closest_marker('quote'):
        from okxtrader import config as cfg
        monkeypatch.setitem(cfg.DEFAULT_SETTINGS, 'WORKING_ORDERS', 'QUOTE')
