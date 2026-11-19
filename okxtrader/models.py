"""Domain models: the vocabulary the whole system speaks.

A ladder trades a SPREAD. A spread order has a side (BUY or SELL the
spread) and becomes two leg orders, one per account. Nothing here knows
about MetaTrader5 — that lives in broker.py alone.
"""

import itertools
import time
import uuid
from enum import Enum

from . import sizing


class OrderSide(Enum):
    """A side on ONE leg — what an MT5 order does."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self):
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY


class SpreadSide(Enum):
    """A side on the SPREAD, which is the thing the trader clicks.

    `spread = P_B - beta * P_A`, so buying the spread buys leg B and
    sells leg A. Keeping this distinct from OrderSide is deliberate: a
    'BUY' that means leg A on one line and the spread on the next is
    exactly how a hedge gets inverted.
    """

    BUY = "BUY"      # buy leg B, sell leg A   -> lifts long_spread
    SELL = "SELL"    # sell leg B, buy leg A   -> hits short_spread

    @property
    def opposite(self):
        return SpreadSide.SELL if self is SpreadSide.BUY else SpreadSide.BUY

    def leg_sides(self):
        """(leg A side, leg B side) for this spread side."""
        if self is SpreadSide.BUY:
            return OrderSide.SELL, OrderSide.BUY
        return OrderSide.BUY, OrderSide.SELL


class OrderType(Enum):
    """What a click on the ladder does. Both are first-class (spec §4)."""

    LIMIT = "LIMIT"      # rest a synthetic order; quote one leg, cross the other
    MARKET = "MARKET"    # cross both legs now, clicked price as the guard


class TimeInForce(Enum):
    """Working-order lifetime (spec §3.1).

    Neither survives this process stopping — nothing at the broker knows
    what a spread is, so an order that "survived" would be a promise
    nothing could keep. The UI says so beside the selector.
    """

    DAY = "DAY"      # cancelled at the session cutoff
    GTC = "GTC"      # until the trader cancels — or until this system stops


class OvernightMode(Enum):
    """What happens to a POSITION at the session cutoff (spec §3.2)."""

    ALLOW = "ALLOW"                      # keep it and pay the swap
    EXIT_IF_PROFIT = "EXIT_IF_PROFIT"    # flatten only if NET P&L > 0
    EXIT_ALWAYS = "EXIT_ALWAYS"          # flatten regardless


class OrderState(Enum):
    WORKING = "WORKING"        # resting, backed by a real pending (LIMIT)
    FILLING = "FILLING"        # quoting leg filled; crossing leg going on
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


#: Stamped on every MT5 order this application sends, so a sweep can
#: scope itself to our own orders and never touch the trader's own
#: terminal clicks (spec §6).
MAGIC_NUMBER = 24680

_uid = itertools.count(1)


def new_id(prefix):
    """Short, unique, and readable in a log line beside an MT5 ticket."""
    return f"{prefix}{next(_uid):04d}-{uuid.uuid4().hex[:4]}"


class SyntheticOrder:
    """One click: one working order on one ladder at one level.

    Three clicks at 58.40 are THREE of these, individually cancellable
    (spec §3). They may share a single real pending at the broker — the
    executor aggregates; this object does not know or care.
    """

    def __init__(self, pair_key, side, level, quantity, order_type,
                 time_in_force, clock=time.time, position_id=None,
                 auto_armed=False):
        self.order_id = new_id('SO')
        #: Did AUTOMATION arm this, or the trader?
        #:
        #: Both are closing orders and both carry a position_id, so
        #: nothing else can tell them apart. It matters because
        #: standing AutoRouting down pulls what AutoRouting armed —
        #: and it must NOT pull a close the trader placed by hand. A
        #: trader who clicks to get out and finds their order swept by
        #: a switch they did not touch is a trader who believes they
        #: are covered and is not.
        self.auto_armed = bool(auto_armed)
        #: The position this order CLOSES, when it is a closing order.
        #: None on an ordinary click. It is what makes an AutoRouting
        #: take-profit a different order from an entry at the same
        #: level on the same side — they must never be merged into one
        #: pending, or one of them silently changes meaning.
        self.position_id = position_id
        self.pair_key = pair_key
        self.side = SpreadSide(side)
        self.level = float(level)           # the SPREAD level clicked
        self.quantity = float(quantity)     # in SPREADS, never leg lots
        self.order_type = OrderType(order_type)
        self.time_in_force = TimeInForce(time_in_force)
        self.state = OrderState.WORKING
        self.created_at = clock()
        self.filled_quantity = 0.0
        #: The real pending backing this order, when one exists. Several
        #: synthetics can point at the same ticket.
        self.pending_ticket = None
        #: Set when the order stops working, in the broker's own words
        #: where the broker is the one who refused (spec §11).
        self.reason = None

    @property
    def remaining(self):
        return max(0.0, self.quantity - self.filled_quantity)

    @property
    def is_working(self):
        return self.state in (OrderState.WORKING, OrderState.FILLING)

    def to_dict(self):
        return {
            'order_id': self.order_id,
            'pair_key': self.pair_key,
            'side': self.side.value,
            'level': self.level,
            'quantity': self.quantity,
            'filled_quantity': self.filled_quantity,
            'order_type': self.order_type.value,
            'time_in_force': self.time_in_force.value,
            'state': self.state.value,
            'created_at': self.created_at,
            'pending_ticket': self.pending_ticket,
            'position_id': self.position_id,
            'intent': 'CLOSE' if self.position_id else 'OPEN',
            'auto_armed': self.auto_armed,
            'reason': self.reason,
        }


class LegFill:
    """What one leg actually did, at the broker, as MT5 reported it."""

    def __init__(self, account, symbol, side, volume, price,
                 order_ticket=None, position_tickets=None,
                 contract_size=None, at=None, clock=time.time):
        self.account = account
        self.symbol = symbol
        self.side = OrderSide(side)
        self.volume = float(volume)
        self.price = price
        self.order_ticket = order_ticket
        #: Hedging-mode accounts REQUIRE closes to target these (spec §4).
        self.position_tickets = list(position_tickets or [])
        self.contract_size = contract_size
        self.at = clock() if at is None else at
        #: What MT5 was quoting this leg at when the order was SENT, and
        #: the same for the close: beside the fills they say which leg
        #: slipped and by how much. None when it was not reported (a
        #: limit that filled, a fill from before this was kept).
        self.sent_price = None
        self.exit_sent_price = None
        self.exit_price = None

    def to_dict(self):
        return {
            'account': self.account, 'symbol': self.symbol,
            'side': self.side.value, 'volume': self.volume,
            'price': self.price, 'order_ticket': self.order_ticket,
            'position_tickets': list(self.position_tickets),
            'contract_size': self.contract_size, 'at': self.at,
            'sent_price': self.sent_price,
            'exit_sent_price': self.exit_sent_price,
            'exit_price': self.exit_price,
        }

    @classmethod
    def from_dict(cls, raw):
        if not raw:
            return None
        fill = cls(raw['account'], raw['symbol'], raw['side'], raw['volume'],
                   raw['price'], order_ticket=raw.get('order_ticket'),
                   position_tickets=raw.get('position_tickets'),
                   contract_size=raw.get('contract_size'), at=raw.get('at'))
        fill.sent_price = raw.get('sent_price')
        fill.exit_sent_price = raw.get('exit_sent_price')
        fill.exit_price = raw.get('exit_price')
        return fill


#: Who opened a position. See `SpreadPosition.source`.
MANUAL = 'MANUAL'
ALGO_SOURCE = 'ALGO'


class SpreadPosition:
    """A pair that is ON: leg A and leg B, both filled, both ours.

    Entered at a real fill and marked at the touches it would actually
    CLOSE at (spec §5), which is why it shows a loss the instant it
    opens — that is what closing immediately would cost.
    """

    def __init__(self, pair_key, side, quantity, leg_a_fill, leg_b_fill,
                 entry_spread, order_type, spread_units, clock=time.time):
        self.position_id = new_id('POS')
        self.pair_key = pair_key
        self.side = SpreadSide(side)
        # TIDIED HERE, once, for every path that builds a position.
        #
        # A LIMIT fill computes this as `leg_b.volume / clip_lots_b`,
        # and binary floats do not divide cleanly: 0.01 / 0.1 is
        # 0.09999999999999999, which is what the Net column printed -
        # seventeen digits where the trader typed two. Nothing was
        # wrong with the trade; the number was just never swept.
        #
        # Doing it in the constructor covers the market path, the limit
        # path, the adopt path and a position rebuilt from the database
        # at restart, so no future caller can reintroduce it. It
        # changes no quantity anybody could type (9 decimal places).
        self.quantity = sizing.tidy(quantity)  # in spreads
        self.leg_a = leg_a_fill
        self.leg_b = leg_b_fill
        #: Anchored on the EXECUTED fills, never on the mid the decision
        #: was taken at (spec §11).
        self.entry_spread = entry_spread
        self.order_type = OrderType(order_type)
        #: `k` — dollars per 1.00 of spread. The ONE multiplier (spec §2).
        self.spread_units = spread_units
        self.opened_at = clock()
        self.closed_at = None
        self.close_reason = None
        self.realized_pnl = None
        self.exit_spread = None
        #: Measured against the executable touch at decision time, and
        #: unmeasured stays None — never 0.0 (spec §5, §11).
        self.entry_slippage = None
        self.exit_slippage = None
        self.click_to_on_ms = None
        #: How long ONE leg was on by itself, measured from the
        #: BROKER'S stamp on the fill to the hedge coming back.
        #:
        #: `click_to_on_ms` cannot answer this on the LIMIT path: its
        #: clock starts when this process NOTICED the fill, so a fill
        #: nobody looked at for twelve minutes still reports a
        #: one-second hedge. That is exactly what the report said on
        #: 2026-09-16. None when the broker's stamp could not be read -
        #: unmeasured is not zero, and a naked window shown as 0ms is
        #: the report saying nothing happened.
        self.naked_ms = None
        #: True when this came back from the database at startup rather
        #: than being watched happen.
        self.recovered = False
        #: Set once the reconciler has seen both legs at the broker.
        self.confirmed = False
        #: Who put it on: MANUAL (a click) or ALGO. Every position is
        #: MANUAL today — nothing trades by itself — but the tag is
        #: stored from now, so when execution arrives an algo re-adopts
        #: only what IT opened after a restart, and never a trader's.
        self.source = MANUAL
        #: The margin ONE spread tied up, read once — the first time this
        #: position is priced after its fill — and kept. TP and SL are a
        #: % of it, so they stay where they were set instead of drifting
        #: as the oil price moves the terminals' margin. None until read.
        self.entry_margin = None
        #: The spread's ATR (spread points) when this position was first
        #: priced, and kept - an ATR stop or target is sized from it and
        #: stays where it was set. None until read.
        self.entry_atr = None
        #: The best and worst net P&L while open, and the minute (from
        #: the open) each was reached — the trade's MFE and MAE. None
        #: until measured: a trade never marked has no extremes, not 0.
        self.peak_pnl = None
        self.peak_min = None
        self.trough_pnl = None
        self.trough_min = None
        #: The levels this position was being JUDGED against on the last
        #: pass that priced it: break-even, take-profit, stop, and which
        #: setting produced each. Refreshed while it is open, so the set
        #: left on a closed position is the one its exit was decided on
        #: - which is the only way the Analysis tab can say WHY a stop
        #: fired and at what number. None until priced, and None for
        #: every position closed before this was kept.
        self.exit_levels = None

    @property
    def is_open(self):
        return self.closed_at is None

    def reduce_by(self, closed_fraction, realized=None):
        """Book PART of this position closing, and stay open for the rest.

        A closing order fills partially like any other, and both
        half-measures are wrong. Marking the whole position closed
        loses the leg still on at the broker — the book reads flat
        while the money is there, which is the one failure this system
        must never have. Leaving it at full size claims an exposure
        that is no longer on, and every close sized from it then asks
        the broker for more than it has.

        The leg volumes come down with the quantity because the
        reconciler and the P&L both read them. The TICKETS do not
        change: MT5 keeps the same ticket at a reduced volume, and a
        later close still has to name it.

        Returns the quantity still open.
        """
        try:
            fraction = float(closed_fraction)
        except (TypeError, ValueError):
            return self.quantity
        fraction = max(0.0, min(1.0, fraction))
        if fraction <= 0.0:
            return self.quantity
        left = 1.0 - fraction
        # TIDIED, because this is a REPEATED multiplication.
        #
        # Every other subtraction that walks a click down a stack of
        # positions already goes through `sizing.tidy`; this one did
        # not, and it is the one that runs again on the same number
        # every time another piece comes off. Two part-closes off a ten
        # left 6.999999999999999 spreads open - on the panel, in the
        # size of the next closing order, and in the `share` the one
        # after that is computed from. Dust that is merely ugly at the
        # second close is a position nobody can flatten at the tenth.
        self.quantity = sizing.tidy(self.quantity * left)
        for fill in (self.leg_a, self.leg_b):
            if fill is not None:
                fill.volume = sizing.tidy(fill.volume * left)
        if realized is not None:
            # ACCUMULATED, not replaced: a position closed in three
            # pieces earned its P&L in three pieces.
            self.realized_pnl = (realized if self.realized_pnl is None
                                 else self.realized_pnl + realized)
        return self.quantity

    def mark(self, closing_spread):
        """Open P&L in dollars at the spread it would close at.

        Commission is NOT subtracted here — the caller adds it once
        (spec §5: the crossing is already in the two prices; charging
        the round trip again is the bid-ask twice).
        """
        if closing_spread is None or self.entry_spread is None:
            return None
        move = closing_spread - self.entry_spread
        if self.side is SpreadSide.SELL:
            move = -move
        # SIZE IS ALREADY IN `spread_units`, AND MUST NOT BE APPLIED
        # TWICE.
        #
        # Every place a position is built passes
        # `spread_units(leg_b.volume, contract_b)`, and `leg_b.volume`
        # is the volume of the WHOLE position — 1.0 lots for 100
        # spreads, not the 0.01 one spread costs. So `spread_units` is
        # already the dollars a 1.00 move in the spread is worth for
        # this position entire. Multiplying by `quantity` on top
        # charged the size a second time.
        #
        # It was invisible at Qty 1, which is the size everything was
        # tested at: there `spread_units` is 0.01 x 100 = 1.0 and
        # `quantity` is 1, so the double count is x1 and the answer
        # comes out right. At Qty 10 it read 10x, and at Qty 100 it
        # read $6,000.00 against MT5's own -$56.70.
        #
        # The per-ONE-spread k of the same name — `spread_units(
        # clip_lots_b, ...)` on the ladder and in the exit maths — is a
        # different number and IS multiplied by quantity by its own
        # callers. Only a POSITION's is whole already.
        return move * self.spread_units

    def to_dict(self):
        return {
            'position_id': self.position_id,
            'pair_key': self.pair_key,
            'side': self.side.value,
            'quantity': self.quantity,
            'entry_spread': self.entry_spread,
            'exit_spread': self.exit_spread,
            'order_type': self.order_type.value,
            'spread_units': self.spread_units,
            'opened_at': self.opened_at,
            'closed_at': self.closed_at,
            'close_reason': self.close_reason,
            'realized_pnl': self.realized_pnl,
            'entry_slippage': self.entry_slippage,
            'exit_slippage': self.exit_slippage,
            'click_to_on_ms': self.click_to_on_ms,
            'naked_ms': self.naked_ms,
            'recovered': self.recovered,
            'confirmed': self.confirmed,
            'source': self.source,
            'entry_margin': self.entry_margin,
            'entry_atr': self.entry_atr,
            'peak_pnl': self.peak_pnl, 'peak_min': self.peak_min,
            'trough_pnl': self.trough_pnl, 'trough_min': self.trough_min,
            'exit_levels': self.exit_levels,
            'leg_a': self.leg_a.to_dict() if self.leg_a else None,
            'leg_b': self.leg_b.to_dict() if self.leg_b else None,
        }

    @classmethod
    def from_dict(cls, raw):
        """Rebuild a position from the database, at restart.

        It comes back OPEN and under management — its id, its fills and
        its tickets exactly as they were. A recovered position that came
        back under a new id would be an orphan to the reconciler and a
        ghost to the book.
        """
        position = cls(raw['pair_key'], raw['side'], raw['quantity'],
                       LegFill.from_dict(raw.get('leg_a')),
                       LegFill.from_dict(raw.get('leg_b')),
                       raw.get('entry_spread'),
                       raw.get('order_type') or OrderType.MARKET.value,
                       raw.get('spread_units') or 0.0)
        position.position_id = raw['position_id']
        position.opened_at = raw.get('opened_at') or position.opened_at
        position.closed_at = raw.get('closed_at')
        position.close_reason = raw.get('close_reason')
        position.realized_pnl = raw.get('realized_pnl')
        position.exit_spread = raw.get('exit_spread')
        position.entry_slippage = raw.get('entry_slippage')
        position.exit_slippage = raw.get('exit_slippage')
        position.click_to_on_ms = raw.get('click_to_on_ms')
        position.naked_ms = raw.get('naked_ms')
        position.source = raw.get('source') or MANUAL
        position.entry_margin = raw.get('entry_margin')
        position.entry_atr = raw.get('entry_atr')
        for name in ('peak_pnl', 'peak_min', 'trough_pnl', 'trough_min',
                     'exit_levels'):
            setattr(position, name, raw.get(name))
        #: Recovered from disk rather than seen happen. The monitor says
        #: so until the reconciler has confirmed both legs at the broker.
        position.recovered = True
        return position
