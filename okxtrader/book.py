"""What is working and what is on — the coordinator's own books.

Three clicks at 58.40 are three orders of 1, not one order of 3, and
each must be individually cancellable. That is the rule this module
exists to keep: synthetics are tracked one per click, and any
aggregation (into a single real pending, or into a Work-column number)
is a VIEW over them, computed here, never a replacement for them.
"""

from collections import defaultdict

from . import sizing
from .models import (OrderState, SpreadSide, SyntheticOrder)


def _side_value(side):
    """'BUY' from a string, an enum, or anything that names a side.

    One reader, because "which side is this" is asked of values that
    arrive from three places — the command bridge (strings), the
    quoter (enums) and the recovered book (whatever was persisted).
    """
    if side is None:
        return None
    value = getattr(side, 'value', side)
    try:
        return str(value).strip().upper() or None
    except Exception:
        return None


class Book:
    def __init__(self):
        self._orders = {}                 # order_id -> SyntheticOrder
        self._positions = {}              # position_id -> SpreadPosition
        #: Our own fills, per pair and level — the ladder's LTQ column.
        #: MT5 gives us no spread tape, so this is all there is, and the
        #: UI says so rather than implying a market print.
        self.prints = defaultdict(list)

    # -- working orders ---------------------------------------------------

    def add_order(self, pair, side, level, quantity, order_type=None,
                  time_in_force=None, position_id=None, auto_armed=False):
        order = SyntheticOrder(
            pair.key, side, level, quantity,
            order_type or pair.order_type, time_in_force or pair.time_in_force,
            position_id=position_id, auto_armed=auto_armed)
        self._orders[order.order_id] = order
        return order

    def auto_armed_for(self, position_id):
        """Only the closing orders AUTOMATION armed against a position.

        What the AutoRouting switch is allowed to pull. A close the
        trader placed by hand is not automation and does not stand
        down with it.
        """
        return [o for o in self.orders_for_position(position_id)
                if getattr(o, 'auto_armed', False)]

    def orders_for_position(self, position_id, working_only=True):
        """The closing orders armed against one position.

        A closing order left armed after its position is gone would
        fire at the next tick that reaches its level and close
        something that is not there.
        """
        return [o for o in self._orders.values()
                if o.position_id == position_id
                and (o.is_working or not working_only)]

    def orders(self, pair_key=None, working_only=True):
        return [o for o in self._orders.values()
                if (pair_key is None or o.pair_key == pair_key)
                and (o.is_working or not working_only)]

    def order(self, order_id):
        return self._orders.get(order_id)

    def cancel(self, order_id, reason='cancelled by trader'):
        """Pull ONE synthetic. Returns it, or None if it is not working."""
        order = self._orders.get(order_id)
        if order is None or not order.is_working:
            return None
        order.state = OrderState.CANCELLED
        order.reason = reason
        return order

    def cancel_where(self, pair_key=None, side=None,
                     reason='cancelled by trader'):
        """`CXL B` / `CXL S` / `CXL All`, and the global kill.

        Returns the orders actually pulled, so the button can report a
        count instead of claiming success over an empty set.
        """
        if side is not None:
            side = SpreadSide(getattr(side, 'value', side))
        pulled = []
        for order in self.orders(pair_key):
            if side is not None and order.side is not side:
                continue
            if self.cancel(order.order_id, reason):
                pulled.append(order)
        return pulled

    def working_at(self, pair_key, level, tolerance=1e-9):
        """The Work column's number for one row: (buy qty, sell qty).

        Aggregated for display only — `orders()` still holds each click
        separately, and cancelling the cell pulls exactly one of them.
        """
        buys = sells = 0.0
        for order in self.orders(pair_key):
            if abs(order.level - level) > tolerance:
                continue
            if order.side is SpreadSide.BUY:
                buys += order.remaining
            else:
                sells += order.remaining
        return buys, sells

    def working_counts(self, pair_key=None):
        """(buys, sells) — the superscripts on the CXL buttons, so the
        trader can see a button will do something before pressing it."""
        buys = sum(1 for o in self.orders(pair_key)
                   if o.side is SpreadSide.BUY)
        sells = sum(1 for o in self.orders(pair_key)
                    if o.side is SpreadSide.SELL)
        return buys, sells

    # -- positions ---------------------------------------------------------

    def add_position(self, position):
        self._positions[position.position_id] = position
        if position.entry_spread is not None:
            self.prints[position.pair_key].append(
                {'level': position.entry_spread, 'quantity': position.quantity,
                 'side': position.side.value, 'at': position.opened_at})
        return position

    def positions(self, pair_key=None, open_only=True):
        return [p for p in self._positions.values()
                if (pair_key is None or p.pair_key == pair_key)
                and (p.is_open or not open_only)]

    def position(self, position_id):
        return self._positions.get(position_id)

    def armed_by_hand(self, position_id):
        """Spreads of one position already covered by the TRADER's own
        resting closes.

        Not automation's: an AutoRouting target is subordinate to a
        click — the click either matches its level or replaces it — so
        counting it here would make the trader's own click look like it
        had nothing left to close.
        """
        return sizing.tidy(sum(
            float(order.remaining or 0.0)
            for order in self.orders_for_position(position_id)
            if not getattr(order, 'auto_armed', False)))

    def positions_to_reduce(self, pair_key, side, quantity, exclude=None,
                            reserve_armed=False):
        """What an opposite click covers: `[(position, spreads)]`.

        OLDEST FIRST, which is what FIFO means and what every desk
        expects when they say "close my position". A price ladder is
        expected to REDUCE before it opens: click the offer while short
        and you are covering, not stacking a second short. These
        accounts are HEDGING, so MT5 will never net for us — an
        opposite order always opens a second position — which means the
        netting has to be done here, by closing tickets.

        THE LAST ONE IS TAKEN IN PART. Whole tickets only was the rule
        here, with a ticket bigger than the click ending the scan:

            live 2026-09-03, short 1207 spreads, BUY 100 clicked
            -> "resting at 7.69 to CLOSE 2 position(s), then open 93"

        Two old tickets of 5 and 2 fitted; the 1,200 behind them did
        not, so 93 of the trader's 100 went the OTHER WAY — opening
        longs against their own short instead of covering it. A reduce
        that opens is the exact failure "reduce before you open" exists
        to prevent; it had simply moved from the side to the size.

        A part-close is supported end to end: `close_position` takes a
        quantity, `_closed_fraction` measures what actually came off,
        and `reduce_by` leaves the position open for the rest. So the
        oldest ticket is closed as far as the click reaches and no
        further, and nothing opens while there is still an opposite
        position to cover.
        """
        try:
            left = float(quantity)
        except (TypeError, ValueError):
            return []
        if left <= 0:
            return []
        # COMPARE ON THE VALUE, never on identity.
        #
        # `side` arrives as a plain string ('BUY') from the command
        # bridge and as a SpreadSide enum from the quoter. `is not`
        # against a string is ALWAYS true, so every open position —
        # including ones on the SAME side — looked opposite, and a
        # second buy while long would have closed the trader's own
        # position instead of adding to it. Caught by the end-to-end
        # suite, which clicks BUY twice.
        want = _side_value(side)
        if want is None:
            return []
        opposite = [p for p in self.positions(pair_key)
                    if _side_value(p.side) not in (None, want)
                    and (exclude is None or p.position_id != exclude)]
        opposite.sort(key=lambda p: (p.opened_at or 0, str(p.position_id)))
        picked = []
        for position in opposite:
            size = float(position.quantity or 0.0)
            if reserve_armed:
                # WHAT IS STILL UNCOVERED, for a click that RESTS.
                #
                # Two closing clicks at two prices are two working
                # orders — that is how a position is scaled out of, and
                # it is what the sell side does with two opening
                # clicks. But each of them may only cover what is not
                # already spoken for, or a 10-lot short ends up with 20
                # lots of resting closes against it and the second one
                # to fill opens a long.
                #
                # Only for the RESTING path. A market close is now, and
                # it disarms whatever was resting as it goes.
                size = sizing.tidy(size - self.armed_by_hand(
                    position.position_id))
            if size <= 0:
                continue
            # As much of this ticket as the click still reaches. Never
            # more: over-closing would flip the net the other way,
            # which is the mistake at the opposite end from the one
            # above and just as expensive.
            take = sizing.tidy(min(size, left))
            if take <= 1e-9:
                break
            picked.append((position, take))
            # Tidied, because this subtraction is what the NEXT take is
            # measured against: `0.15 - 0.1` is 0.04999999999999999 in
            # float, and that is then the size of the second closing
            # order, on the panel, in full.
            left = sizing.tidy(left - take)
            if left <= 1e-9:
                break
        return picked

    def net_position(self, pair_key):
        """(net spreads, average entry spread) for one ladder.

        Signed: buys positive. The average is weighted by quantity and
        anchored on executed fills; it is None when nothing is on, never
        0.0 — an average of no trades is not zero (spec §11).
        """
        net = 0.0
        weighted = 0.0
        volume = 0.0
        for position in self.positions(pair_key):
            signed = position.quantity if position.side is SpreadSide.BUY \
                else -position.quantity
            net += signed
            if position.entry_spread is not None:
                weighted += position.entry_spread * position.quantity
                volume += position.quantity
        # SWEPT, because this is a RUNNING SUM and running sums are
        # where binary dust collects. Three 0.01 buys covered by three
        # 0.01 sells is -3.469446951953614e-18, not zero - and the
        # ladder printed exactly that where it should have said FLAT,
        # because a number that tiny is still not zero to an `if`.
        # The trader who reported "0.000000001122411 when 3-4 positions
        # are open" was looking at this.
        #
        # `+ 0.0` turns the -0.0 that rounding can leave into 0.0, so
        # nothing downstream renders a negative nothing.
        net = sizing.tidy(net) + 0.0
        if not volume:
            # An average of no trades is not zero (spec §11).
            return net, None
        return net, sizing.tidy(weighted / volume)

    def last_print(self, pair_key):
        prints = self.prints.get(pair_key) or []
        return prints[-1] if prints else None


def reduce_first(book, executor, pair, side, quantity, md,
                 exclude=None, on_closed=None):
    """Close the tickets an opposite click covers; return what is left.

    ONE implementation, called from both places a position can be
    created: the MARKET click closes before it opens, and a resting
    order closes when it fills. Two implementations of "which tickets
    does this click cover" is two answers to reconcile the day they
    disagree, on a live book.

    Returns (closed position ids, quantity still to open, failure).

    `failure` is None when every close asked for went through, and the
    BROKER'S OWN WORDS when one did not. It has to be told apart from
    "there was nothing to close": both used to return an empty list, so
    a caller could not tell a refusal from a quiet no-op, and the
    MARKET path opened a new position on top of one that had just
    failed to close — which is the exact mess this feature exists to
    prevent, and worse if the close half-executed and left a leg naked.

    A close is never withheld: `close_position` consults no guard, and
    neither does this.
    """
    try:
        left = float(quantity)
    except (TypeError, ValueError):
        return [], quantity, None
    closed = []
    for position, take in book.positions_to_reduce(pair.key, side, left,
                                                   exclude=exclude):
        # BEFORE, because a part-close reduces it in place.
        was = float(position.quantity or 0.0)
        result = executor.close_position(
            pair, position, md, reason='reduced by an opposite click',
            quantity=take)
        # WHAT ACTUALLY CAME OFF, not what was asked for.
        #
        # A part-close can come back having closed less than the piece
        # requested, and `imbalanced` is the case where it closed
        # NOTHING but still reported ok — the quoting leg came down and
        # the other leg's step was too big to follow it. Both used to
        # count as the whole `take`, so the click believed it had
        # covered spreads that are still on and opened the remainder
        # the other way. A reduce that opens is the exact failure this
        # function exists to prevent.
        covered = min(take, was * float(result.get('fraction', 1.0) or 0.0))
        if not result.get('ok') or result.get('imbalanced') \
                or covered <= 1e-9:
            # Stop at the first refusal rather than stepping over it.
            # The rest of the queue is no longer the queue the trader
            # would have closed, and opening the remainder on top of a
            # position that would NOT close is how a reduce quietly
            # becomes a bigger position.
            return closed, max(left, 0.0), close_failure(result, position)
        if on_closed is not None:
            on_closed(position)
        closed.append(position.position_id)
        left = sizing.tidy(left - covered)
    return closed, max(left, 0.0), None


def close_failure(result, position):
    """Why a close did not go through, in the BROKER's own words.

    "check the log" is not an answer on a live account. Each leg
    reports its own error, and a close that went through on one leg and
    not the other has left a NAKED LEG — which is the sentence the
    trader has to see first.
    """
    result = result or {}
    # A refusal that never reached a broker carries its own sentence —
    # the piece was too small for one leg's volume step, say — and
    # there are no leg errors to quote.
    if result.get('reason') and not (result.get('legs') or {}):
        return f"could not close position {position.position_id}: " \
               f"{result['reason']}"
    legs = result.get('legs') or {}
    said = []
    done = []
    for leg, answer in sorted(legs.items()):
        if (answer or {}).get('ok'):
            done.append(leg.upper())
        else:
            reason = ((answer or {}).get('error')
                      or (answer or {}).get('reason') or 'refused')
            said.append(f'leg {leg.upper()}: {reason}')
    head = f'could not close position {position.position_id}'
    if done and said:
        head = (f'NAKED LEG — position {position.position_id} closed on '
                f'leg {done[0]} but NOT on the other')
    return f"{head} ({'; '.join(said) if said else 'no reason reported'})"
