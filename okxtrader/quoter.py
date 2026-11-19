"""Working orders: held levels that cross both legs when the SPREAD
gets there - and, behind a desk setting, the old one-leg quote.

BY DEFAULT NOTHING RESTS AT THE BROKER (`WORKING_ORDERS` = TRIGGER).
An entry is a level this module watches, exactly as a close always
was: when the EXECUTABLE spread reaches it, both legs cross at MARKET
together through the executor's own market path. See
`Quoter._work_trigger` for why the quote below stopped being the
default - a limit on one of two legs that move together fills on the
market's move, not the spread's, and every such fill is a bad one.

What follows is the QUOTE path, still here for a pair whose legs are
genuinely independent:

TWO KINDS OF RESTING ORDER LIVE HERE, and they are not symmetric.

An ENTRY is backed by a REAL pending on one leg. A CLOSE is backed by
NOTHING at the broker: it is a level this module watches, and a close by
TICKET when the market reaches it.

That asymmetry is forced, not chosen. MT5 honours `position` on
TRADE_ACTION_DEAL and IGNORES it on TRADE_ACTION_PENDING, so a "closing"
limit rests as an ordinary limit — and on a hedging account an ordinary
opposite limit OPENS a second position. Live 2026-09-02: ticket 2092 was
rested to close ticket 2090 and filled as a BUY 0.01 beside the SELL
0.01 it was meant to close. This module believed that leg was flat,
closed the other leg on the strength of it, and the reconciler swept
both futures as orphans a minute later. Attaching a broker take-profit
to the leg instead is not the answer either: one leg closing alone
converts the hedge into a naked outright.

What holding the level here costs, said plainly because it is real: the
close only fires while this is running, and it crosses at the touch
rather than earning the level.

THE ENTRY PATH, which is what the rest of this file is about:

A synthetic working order is backed by a REAL pending limit on ONE leg
(the "quoting" leg). When that limit fills, the other leg is crossed at
market IMMEDIATELY. This earns one leg's bid-ask and pays the other's
instead of paying both, and it buys that edge by taking on legging risk.

The part that is easy to get wrong: **the quoting leg's price is not
fixed.** The trader clicked a SPREAD level, but the pending lives on a
LEG, and the price that produces the clicked spread depends on where
the other leg is right now:

    quoting B, SELL the spread at S:   P_B = S + beta * ask_A
    quoting B, BUY  the spread at S:   P_B = S + beta * bid_A
    quoting A, SELL the spread at S:   P_A = (bid_B - S) / beta   (buy A)
    quoting A, BUY  the spread at S:   P_A = (ask_B - S) / beta   (sell A)

So the pending is re-priced to hold the implied spread at the clicked
level — chasing the OTHER leg, for the order's whole life. In the
stat-arb system a peg anchored on its own leg's book chased the market
away from itself, never filled, timed out at 15s and crossed: 13.96
seconds from click to pair-on against 24ms for the market path, and
+0.4700 of slippage.

Three more rules, each of which has a test:

1. **Re-peg on a dead band, not on every tick.** Every MODIFY loses
   queue position; re-pricing three times a second guarantees you are
   never at the front of a queue, which defeats the entire point.
2. **Re-peg by MODIFY, never cancel-and-replace** — one ticket for the
   order's life. (MT5 cannot change a pending's VOLUME by MODIFY, so a
   RESIZE is the one case that must cancel and replace, and it says so.)
3. **Every MODIFY can race a fill.** Check for the fill BEFORE
   modifying, or you re-price an order that has already become a naked
   position.
"""

import logging

from . import sizing
from .book import close_failure
from .executor import slippage
from .spread import compute_spread
from .models import (LegFill, OrderState, OrderType, SpreadPosition,
                     SpreadSide, new_id)


class QuoteGroup:
    """One (pair, side, level) — and what, if anything, is at the broker.

    An ENTRY group is backed by a REAL pending. Three clicks at 58.40
    are three synthetics and ONE pending at the summed size. Cancelling
    one re-sizes it; cancelling the last pulls it. The synthetics stay
    individually cancellable — an aggregation, not a replacement.

    A CLOSING group is backed by NOTHING. It is a level this system
    watches, and a close by ticket when the market reaches it. Its
    `ticket` stays None for its whole life.
    """

    def __init__(self, pair_key, side, level, leg, position_id=None):
        self.pair_key = pair_key
        #: Set when this group CLOSES a position (AutoRouting, spec
        #: 5.4). It rests NOTHING at the broker: MT5 ignores `position`
        #: on a pending order, so a "closing" limit is an ordinary one
        #: and OPENS a second position on a hedging account. The level
        #: is held here and closed by ticket instead.
        self.position_id = position_id
        self.side = SpreadSide(side)
        self.level = float(level)
        self.leg = leg                 # 'a' or 'b' — which leg quotes
        self.ticket = None
        self.price = None              # where the pending is resting
        self.volume = 0.0              # lots resting on the quoting leg
        self.repegs = 0
        self.reason = None             # in the broker's own words
        self.orders = []               # the synthetics behind it
        #: Consecutive fires of a CLOSING group that got nowhere. A
        #: broker refusing a close three times a second is a broker
        #: being hammered, so this is bounded and then escalated.
        self.close_attempts = 0
        #: Spreads to OPEN once this close has gone through, when the
        #: click that armed it was bigger than the position it covered.
        #:
        #: A reducing click is ONE instruction at ONE level. Resting the
        #: remainder as an ordinary pending at the same moment gave it
        #: a different engine: the pending sits at the BROKER and fills
        #: on the broker's own tick stream, while the close is watched
        #: HERE at poll rate. Live 2026-09-03 the pending filled and the
        #: close never saw a qualifying tick, so a BUY 12 over a SELL 10
        #: left the trader +2 AND still -10 — the one net position they
        #: certainly did not ask for.
        self.open_after = 0.0
        self.escalated = False
        #: An ENTRY held here and crossed at the level (`WORKING_ORDERS`
        #: TRIGGER): nothing at the broker, by design, like a close.
        self.held = False
        #: Times the poll saw the level and the fresh re-read just
        #: before sending did not: a flicker NOT traded.
        self.rechecks_missed = 0

    @property
    def quantity(self):
        """Spreads still working across this group's synthetics."""
        return sum(o.remaining for o in self.orders if o.is_working)

    @property
    def closing(self):
        return self.position_id is not None

    def to_dict(self):
        return {'pair_key': self.pair_key, 'side': self.side.value,
                'level': self.level,
                # A close touches BOTH legs by ticket. Naming the
                # quoting leg here would say an order rests on one
                # account, which is the thing that was never true.
                'leg': 'BOTH' if (self.closing or self.held)
                       else self.leg.upper(),
                'held': self.held,
                'rechecks_missed': self.rechecks_missed,
                'position_id': self.position_id,
                'intent': 'CLOSE' if self.closing else 'OPEN',
                'ticket': self.ticket, 'price': self.price,
                'volume': self.volume, 'repegs': self.repegs,
                'quantity': self.quantity, 'reason': self.reason,
                # Spreads this click will OPEN once the close goes
                # through. It is intent the engine is holding and the
                # screen showed NOWHERE: a trader who clicked 100 saw
                # 93 in the Work cell and had no way to learn the other
                # 7 existed, was waiting, or had been dropped.
                'open_after': self.open_after,
                'escalated': self.escalated,
                'orders': [o.order_id for o in self.orders]}


def quoting_leg(pair):
    """Which leg rests the pending.

    Default: the leg with the WIDER bid-ask — that is the spread being
    earned. The tension is real and the ladder shows both widths so the
    choice is made from measurement: the wider leg is usually the less
    liquid one, where you queue longest and fill least.
    """
    if pair.quoting_leg in ('a', 'b'):
        return pair.quoting_leg
    width_a = (pair.meta_a or {}).get('width') or 0.0
    width_b = (pair.meta_b or {}).get('width') or 0.0
    return 'b' if width_b >= width_a else 'a'


def peg_price(pair, md, side, level, leg):
    """The quoting leg's price that makes the spread equal `level`.

    Returns (price, order side on that leg). Anchored on the OTHER
    leg's touch — the whole point of the module.
    """
    beta = float(pair.hedge_ratio or 1.0)
    side = SpreadSide(getattr(side, 'value', side))
    leg_a_side, leg_b_side = side.leg_sides()
    if leg == 'b':
        # The other leg is A, and we cross it when this fills — so the
        # anchor is the touch we would PAY on A.
        anchor = md['leg_a_ask'] if side is SpreadSide.SELL else md['leg_a_bid']
        return level + beta * anchor, leg_b_side
    anchor = md['leg_b_bid'] if side is SpreadSide.SELL else md['leg_b_ask']
    return (anchor - level) / beta, leg_a_side


def implied_spread(pair, md, side, leg, price):
    """What spread a pending resting at `price` currently implies."""
    beta = float(pair.hedge_ratio or 1.0)
    side = SpreadSide(getattr(side, 'value', side))
    if leg == 'b':
        anchor = md['leg_a_ask'] if side is SpreadSide.SELL else md['leg_a_bid']
        return price - beta * anchor
    anchor = md['leg_b_bid'] if side is SpreadSide.SELL else md['leg_b_ask']
    return anchor - beta * price


def closing_trigger_reached(side, level, md):
    """Has the market come to a resting CLOSE?

    Reads the EXECUTABLE side for the order's own direction: a BUY is
    reached when the spread can be BOUGHT at or under its level, a SELL
    when it can be SOLD at or over it. That is the same side the ladder
    drew the level on, so an order fires where the trader watched it.

    A price nobody has NEVER triggers. None is unknown, not "reached".
    """
    if not md or level is None:
        return False
    side = SpreadSide(getattr(side, 'value', side))
    if side is SpreadSide.BUY:
        offer = md.get('long_spread')
        return offer is not None and offer <= level + 1e-9
    bid = md.get('short_spread')
    return bid is not None and bid >= level - 1e-9


class Quoter:
    """Holds every LIMIT-mode group and works them each poll."""

    def __init__(self, config, legs, executor, book):
        self.config = config
        #: Called with (pair key, reason, side, level) when a working
        #: order that reached its level could not go on - in the
        #: broker's words. Set by the coordinator, to the journal, so
        #: the desk and Telegram say it rather than the order vanishing.
        self.on_refusal = None
        self.legs = legs
        self.executor = executor
        self.book = book
        self.groups = {}              # (pair_key, side, level) -> QuoteGroup
        #: Fill -> hedge-on times, in ms. The number that says whether
        #: the 2.0s deadline is right (spec §4, §16).
        self.hedge_times = []
        #: The last market seen per pair, so an auto-close can be marked
        #: at the touch it was decided on rather than at nothing.
        self._markets = {}
        #: (pair key, ticket) -> when we last shouted that this order
        #: could not be read. An unreadable leg is re-read three times a
        #: second and a CRITICAL at that rate buries what matters.
        self._unreadable_said = {}
        #: Called with a position whenever THIS module changes it, so it
        #: reaches the database. Set by the coordinator, like
        #: `executor.before_close`.
        #:
        #: `work()` returns its events and the coordinator drops them, so
        #: without this hook nothing the quoter did was ever written: a
        #: LIMIT entry was recovered as NOTHING after a restart, and a
        #: close that filled on a resting order came back OPEN.
        self.on_change = None

    # -- the ladder's side of it -------------------------------------------

    def key_for(self, order):
        # The position is PART of the key. Aggregating an auto-TP close
        # into an entry order at the same level and side would silently
        # change what one of them means (spec 5.4).
        return (order.pair_key, order.side.value, round(order.level, 10),
                order.position_id)

    def group_for(self, pair, order):
        key = self.key_for(order)
        group = self.groups.get(key)
        if group is None:
            group = QuoteGroup(order.pair_key, order.side, order.level,
                               quoting_leg(pair),
                               position_id=order.position_id)
            group.held = order.position_id is None and self.triggers()
            self.groups[key] = group
        if order not in group.orders:
            group.orders.append(order)
        return group

    def cancel(self, order):
        """Pull one synthetic. The real pending is re-sized (or removed)
        on the next pass, which is also where a fill that raced the
        cancel is caught."""
        self.book.cancel(order.order_id)
        return self.groups.get(self.key_for(order))

    # -- the loop ----------------------------------------------------------

    def work(self, pair, md):
        """One pass over this pair's groups. Returns what happened.

        Order matters: FILLS first (a MODIFY can race one), then
        placement and re-pegging.
        """
        events = []
        self._markets[pair.key] = md
        for key, group in list(self.groups.items()):
            if group.pair_key != pair.key:
                continue
            if group.closing:
                # A CLOSING order rests nothing at the broker. It is a
                # level this system watches, and a close by TICKET when
                # the market reaches it.
                event = self._work_closing(pair, md, group)
                if event:
                    events.append(event)
                continue
            if group.held and group.ticket is None:
                # A held entry: nothing at the broker, both legs cross
                # when the spread reaches the level.
                event = self._work_trigger(pair, md, group)
                if event:
                    events.append(event)
                continue
            if group.ticket is not None:
                event = self._check_fill(pair, md, group)
                if event:
                    events.append(event)
                    if key not in self.groups:
                        # A rejected hedge pulls the whole pair; there is
                        # nothing left here to re-rest.
                        continue
                    # Otherwise fall through: a PARTIAL fill leaves
                    # clicks still working, and they re-rest on this same
                    # pass rather than a poll later.
            if not group.quantity:
                if group.ticket is not None:
                    self._pull(pair, group, 'the last synthetic at this level '
                                            'was cancelled')
                    events.append({'group': key, 'action': 'pulled'})
                self.groups.pop(key, None)
                continue
            event = self._rest_or_repeg(pair, md, group)
            if event:
                events.append(event)
        return events

    def watch_dark(self, pair, reason):
        """This pair has NO PRICE. Watch what is already at the broker.

        THE 2026-09-16 NAKED LEG IS THIS METHOD'S REASON TO EXIST.

        When a leg's tick went missing the coordinator skipped the pair
        outright, so `work()` never ran, so nothing ever asked whether
        the OTHER leg's pending had filled. It had: four lots sat naked
        for twelve minutes and fifty-one seconds, and were hedged in one
        second the moment a tick came back.

        No price is a reason to stop PLACING. It is the opposite of a
        reason to stop WATCHING - a pending is a real order at the
        broker and fills whether or not we can see a price for it. So:

          - FILLS are checked. If one is found the ordinary path runs:
            hedge it, and if the hedge cannot be sent (the dark leg is
            usually the one it would be sent to) unwind by ticket. The
            trader ends flat in seconds instead of naked for minutes.
          - The pending is PULLED. We cannot hedge what we cannot price,
            so an order that might fill into that is an order that
            should not be resting. `_pull` hedges a fill that races the
            cancel, and keeps the ticket if the broker cannot be reached.
          - Nothing is placed and nothing is re-pegged. There is no
            price to peg to.
          - CLOSING groups are left alone entirely. They rest nothing at
            the broker, and a close is never withheld or pulled.

        The cost is queue position: the order is re-rested when the feed
        returns. That is the right trade - the alternative is a fill we
        cannot hedge.
        """
        events = []
        for key, group in list(self.groups.items()):
            if group.pair_key != pair.key:
                continue
            if group.closing:
                group.reason = reason
                continue
            if group.ticket is not None:
                event = self._check_fill(pair, None, group)
                if event:
                    events.append(event)
                    if key not in self.groups:
                        continue
            if group.ticket is not None:
                answer = self._pull(pair, group, reason) or {}
                if answer.get('action'):
                    # It filled as we pulled it; `_pull` hedged it and
                    # this is that event, not a clean pull.
                    events.append(answer)
                else:
                    events.append({'group': key, 'action': 'pulled_dark',
                                   'reason': reason})
            group.reason = reason
        return events

    def _wanted_volume(self, pair, group, md=None):
        """Lots on the quoting leg for the spreads still working here.

        Returns `(lots, reason)`. ENTRY groups only — a closing group
        rests nothing to size.

        THE PRICES ARE THE LIVE ONES. They used to come from `meta_a`
        and `meta_b` — the symbol report MT5 handed back when the pair
        RESOLVED, which is once, at startup. That is harmless while the
        two sizing bases that ignore price are in force, and wrong the
        moment NOTIONAL is: equal money a side computed from this
        morning's prices is not equal money now, and a pair that
        resolved before its market opened has no mid at all, so it
        could never size and every click sat in the book unplaced.
        """
        meta_a, meta_b = pair.meta_a or {}, pair.meta_b or {}
        md = md or {}
        plan = sizing.clip_plan(
            pair, meta_a, meta_b,
            md.get('leg_a_mid') or meta_a.get('mid'),
            md.get('leg_b_mid') or meta_b.get('mid'), group.quantity)
        # RETURNED, not stashed on the group. Setting `group.reason`
        # here made `_held_off` see a reason that had not changed, so
        # the one log line explaining an unplaceable order never fired.
        if plan.get('reason'):
            return 0.0, plan['reason']
        return (plan['leg_a_lots'] if group.leg == 'a'
                else plan['leg_b_lots']), None

    def _held_off(self, pair, group, reason):
        """An order that cannot rest, SAID OUT LOUD — once.

        This ran silently for the whole life of the module. An entry
        that could not be placed set `group.reason` and returned, and
        the reason then existed in exactly two places: the eleventh
        column of a panel that scrolls sideways, and 9px of ellipsised
        text on the rail. Nothing reached `coordinator.log`.

        So an order sat in the book looking like one that was working,
        the footer read `W:1 (broker 0)` in small type, and the answer
        was on the screen but unreachable. That cost the desk several
        sessions and it is the reason this method exists.

        Logged on the CHANGE, not on the poll: the level is worked
        three times a second and a line per pass would bury itself.
        """
        if group.reason != reason:
            logging.warning('%s: %s %g at %s is NOT at the broker — %s',
                            pair.key, group.side.value, group.quantity,
                            group.level, reason)
        group.reason = reason
        return None

    def _rest_or_repeg(self, pair, md, group):
        leg = self.legs.get(pair.account_a if group.leg == 'a'
                            else pair.account_b)
        symbol = pair.symbol_a if group.leg == 'a' else pair.symbol_b
        if leg is None:
            return self._held_off(pair, group, f'no leg runner for {symbol}')

        # A synthetic must not rest on a stale or desynced print: the
        # level is still there when the quote refreshes, and if it is
        # not then it was never offered.
        if md.get('guard_reason'):
            return self._held_off(pair, group,
                                  f"{md['guard_reason']} — holding off")

        price, order_side = peg_price(pair, md, group.side, group.level,
                                      group.leg)
        volume, unsized = self._wanted_volume(pair, group, md)
        if volume <= 0:
            return self._held_off(
                pair, group,
                unsized or 'the click could not be sized on either leg')

        if group.ticket is None:
            result = leg.place_limit(
                symbol, order_side.value, volume, price,
                comment=new_id('LADDER'))
            if not result.get('ok'):
                group.reason = result.get('error')
                for order in group.orders:
                    if order.is_working:
                        order.state = OrderState.REJECTED
                        order.reason = result.get('error')
                return {'group': self.key_for(group.orders[0]),
                        'action': 'rejected', 'reason': result.get('error')}
            if group.reason:
                logging.warning('%s: %s %g at %s reached the broker after '
                                'all — was: %s', pair.key, group.side.value,
                                group.quantity, group.level, group.reason)
            group.ticket = result['ticket']
            group.price = result.get('price', price)
            group.volume = volume
            # The broker's stops level can make the required peg
            # unreachable. Say so in words, on the ladder — not in a log
            # file (spec §4, §11).
            group.reason = result.get('price_note')
            for order in group.orders:
                order.pending_ticket = group.ticket
            return {'group': (group.pair_key, group.side.value, group.level,
                              group.position_id),
                    'action': 'placed', 'ticket': group.ticket,
                    'price': group.price}

        if abs(volume - group.volume) > 1e-9:
            # MT5 cannot change a pending's VOLUME by MODIFY, so the one
            # case that must cancel and replace is a re-size — and only
            # a re-size. Prices always MODIFY.
            return self._resize(pair, leg, symbol, group, order_side, price,
                                volume)

        drift = self._drift(pair, md, group)
        band = self._dead_band(pair)
        if drift is None or drift <= band:
            return None
        result = leg.modify_order(group.ticket, price)
        if not result.get('ok'):
            group.reason = result.get('error')
            return {'group': (group.pair_key, group.side.value, group.level,
                              group.position_id),
                    'action': 'repeg_failed', 'reason': result.get('error')}
        group.price = price
        group.repegs += 1
        return {'group': (group.pair_key, group.side.value, group.level,
                          group.position_id),
                'action': 'repegged', 'price': price, 'drift': drift}

    def _drift(self, pair, md, group):
        """How far the resting pending's implied spread has wandered."""
        if group.price is None:
            return None
        return abs(implied_spread(pair, md, group.side, group.leg,
                                  group.price) - group.level)

    def _dead_band(self, pair):
        increment = pair.effective_increment() or 0.0
        ticks = float(self.config.get('REPEG_DEAD_BAND_TICKS', 1.0) or 0.0)
        return increment * ticks

    def _resize(self, pair, leg, symbol, group, order_side, price, volume):
        cancel = leg.cancel_order(group.ticket)
        if cancel.get('filled_volume'):
            # It filled while we were re-sizing it. That is a fill, not a
            # cancel, and it must be hedged rather than tidied away.
            return self._on_fill(pair, group, cancel)
        result = leg.place_limit(
            symbol, order_side.value, volume, price,
            comment=new_id('LADDER'))
        if not result.get('ok'):
            group.ticket = None
            group.reason = result.get('error')
            return {'group': (group.pair_key, group.side.value, group.level,
                              group.position_id),
                    'action': 'resize_failed', 'reason': result.get('error')}
        group.ticket = result['ticket']
        group.price = result.get('price', price)
        group.volume = volume
        for order in group.orders:
            order.pending_ticket = group.ticket
        return {'group': (group.pair_key, group.side.value, group.level,
                          group.position_id),
                'action': 'resized', 'volume': volume,
                'ticket': group.ticket}

    def _pull(self, pair, group, reason):
        leg = self.legs.get(pair.account_a if group.leg == 'a'
                            else pair.account_b)
        if leg is None or group.ticket is None:
            return None
        state = leg.cancel_order(group.ticket)
        group.reason = reason
        if state.get('filled_volume'):
            # A cancel that did not prevent a fill is a distinct event
            # and must stay visible, not be smoothed into a clean pull.
            #
            # `_on_fill` takes the ticket off the group itself. Clearing
            # it here FIRST handed it None, so the fill was booked
            # against no ticket at all and the exit price had nothing to
            # anchor on.
            logging.critical("%s: pending at %s FILLED as it was cancelled — "
                             "hedging it", pair.key, group.level)
            return self._on_fill(pair, group, state)
        # A CANCEL THAT WAS NEVER DELIVERED MUST NOT ERASE THE TICKET.
        #
        # This cleared it unconditionally. On a broker we could not
        # reach, that forgot a pending which is still live and still
        # fillable - and once the ticket is gone nothing checks it
        # again, which is precisely how a leg goes on with nobody
        # watching. Keeping it means the next pass reads it, pulls it
        # again, and hedges it if it filled meanwhile.
        #
        # "Could not reach" only. An order the broker says is simply no
        # longer there is gone, and holding its ticket for ever would
        # wedge the group.
        if state.get('cancelled') or state.get('readable', True) is not False:
            group.ticket = None
        else:
            logging.critical(
                '%s: could not reach the broker to pull order %s at %s (%s) '
                '- KEEPING the ticket: it may still be live and fillable.',
                pair.key, group.ticket, group.level,
                state.get('error') or 'no answer')
        return state

    def _check_fill(self, pair, md, group):
        """Read the pending BEFORE touching it. Every MODIFY can race a
        fill, and `positions_get` shows the fill (carrying the ORDER's
        ticket) before deal history does."""
        leg = self.legs.get(pair.account_a if group.leg == 'a'
                            else pair.account_b)
        if leg is None:
            return None
        state = leg.order_state(group.ticket)
        # UNREADABLE IS NOT UNFILLED.
        #
        # The whole of the 2026-09-16 naked leg is in this distinction.
        # A pending is a REAL order at the broker: it fills whether or
        # not we can see it. So "I could not ask" must never be spent as
        # "it has not filled" - that answer tells the caller to carry
        # on, and carrying on is the one thing that cannot be right.
        #
        # Nothing is closed or sent from here: with no answer there is
        # nothing to act on. It is SAID - on the ladder and, at CRITICAL,
        # in the log - and the level is left alone for the next pass,
        # which is the only honest move.
        if state.get('readable', True) is False:
            why = state.get('error') or 'the leg could not be read'
            group.reason = (f'cannot tell whether this order has filled: '
                            f'{why} — NOT assuming it has not')
            self._say_unreadable(pair, group, why)
            return None
        # It answered. Forget that it once did not, so the next outage
        # on this order is shouted about at once rather than swallowed
        # by a rate limit set half an hour ago - and so this dict does
        # not grow a row per ticket for the life of the process.
        self._unreadable_said.pop((pair.key, group.ticket), None)
        if not state.get('filled_volume'):
            return None
        return self._on_fill(pair, group, state)

    #: How often one unreadable order may shout. A leg that cannot be
    #: read is read again three times a second, and a CRITICAL line at
    #: that rate buries the one thing worth seeing.
    UNREADABLE_LOG_EVERY_SEC = 30.0

    def _say_unreadable(self, pair, group, why):
        now = self.executor.clock()
        key = (pair.key, group.ticket)
        if now - self._unreadable_said.get(key, 0.0) < \
                self.UNREADABLE_LOG_EVERY_SEC:
            return
        self._unreadable_said[key] = now
        logging.critical(
            '%s: order %s at %s CANNOT BE READ (%s). It is a real pending at '
            'the broker and may already have filled - this system cannot '
            'tell, and is NOT treating it as unfilled.',
            pair.key, group.ticket, group.level, why)

    def _on_fill(self, pair, group, state):
        """The quoting leg filled. Cross the other leg NOW.

        A pending can fill PARTIALLY: hedge to what actually filled, not
        to what rested.
        """
        filled = float(state.get('filled_volume') or 0.0)
        started = self.executor.clock()
        ticket, group.ticket = group.ticket, None
        # A PARTIAL fill leaves the rest of the pending resting, and it
        # is pulled on BOTH paths. A closing order used to skip this:
        # its residual stayed at the broker carrying a position ticket
        # that the fill had just closed, nobody tracked it any more, and
        # every further click rested another one beside it — pendings
        # piling up on the quoting account while the other account got
        # nothing. That is the state the desk reported.
        state, filled = self._pull_residual(pair, group, state, filled, ticket)

        meta_a, meta_b = pair.meta_a or {}, pair.meta_b or {}
        contract_a = float(meta_a.get('contract_size') or 0.0)
        contract_b = float(meta_b.get('contract_size') or 0.0)
        leg_a_side, leg_b_side = group.side.leg_sides()
        # THE TRADER'S OWN RATIO, read the way round this fill needs
        # it. A click and a fill must cross the same pair of sizes, or
        # the pair is hedged on one route and not on the other.
        ratio = sizing.leg_ratio(pair)      # leg B lots per lot of A

        if group.leg == 'b':
            cross_leg, cross_side = 'a', leg_a_side
            cross_volume = sizing.round_step(
                (filled / ratio) if ratio else 0.0,
                meta_a.get('volume_step'), meta_a.get('volume_min'), down=True)
            quote_side = leg_b_side
        else:
            cross_leg, cross_side = 'b', leg_b_side
            cross_volume = sizing.round_step(
                filled * ratio,
                meta_b.get('volume_step'), meta_b.get('volume_min'), down=True)
            quote_side = leg_a_side

        if cross_volume <= 0:
            # The fill was smaller than one step of the crossing leg, so
            # there is no hedge to send. Sending it anyway means a
            # zero-volume order and the broker's "invalid volume" — a
            # true statement about the wrong thing. Say what happened
            # instead, and take the quoting leg back off: half a spread
            # is a naked leg, whatever the arithmetic behind it.
            cross = {'ok': False, 'error': (
                f'{filled:g} lots filled on leg {group.leg.upper()} is under '
                f'one volume step of leg {cross_leg.upper()} — there is no '
                f'hedge that size')}
        else:
            cross = self.executor._cross_with_deadline(
                pair, cross_leg, cross_side, cross_volume,
                contract_a if cross_leg == 'a' else contract_b,
                new_id('HEDGE'))
        elapsed_ms = (self.executor.clock() - started) * 1000.0
        self.hedge_times.append(elapsed_ms)
        # HOW LONG THE LEG WAS ACTUALLY ALONE, from the broker's stamp
        # on the fill rather than from the moment we noticed it. Those
        # were 12m51s apart on 2026-09-16 and the report showed the
        # ONE SECOND, which is the number that made a catastrophe look
        # like a clean trade.
        naked_ms = self._naked_ms(state)

        tickets = list(state.get('position_tickets') or [])
        quote_fill = {'ok': True, 'filled_volume': filled,
                      'price': state.get('price'),
                      'position_tickets': tickets,
                      'ticket': tickets[0] if tickets else None}

        if not cross.get('ok'):
            # Only a REJECTED crossing leg unwinds — and it unwinds by
            # ticket, on a hedging account.
            reason = (f"the hedge was rejected: {cross.get('error')} — "
                      f"unwinding leg {group.leg.upper()}")
            if naked_ms is not None:
                reason = (f'{reason} (that leg was alone for '
                          f'{naked_ms / 1000.0:.1f}s)')
            logging.critical("%s: %s", pair.key, reason)
            # THE UNWIND'S ANSWER IS READ, and this is the worse of the
            # two places it was thrown away.
            #
            # A resting order fills when the market comes to it, which
            # may be an hour after the click and with nobody watching.
            # If the hedge is then rejected and the unwind ALSO fails,
            # the trader owns an outright position they never asked
            # for and were never told about.
            #
            # The naked BANNER cannot cover this: it scans positions in
            # the BOOK for one leg on and one off, and a fill that was
            # never booked is not there to scan. So it is said here, at
            # CRITICAL, with the tickets - which now reaches a file on
            # disk - and carried out in the event this returns.
            #
            # BOTH LEGS. The hedge is "rejected" on the broker's
            # verdict, and a rejected order can still have dealt part
            # of itself first (10010, or a rejection after the first
            # deal). Unwinding only the quoting leg leaves that piece
            # at the broker, unbooked and unhedged - the orphan leg
            # again, on the path where nobody is watching.
            naked = self.executor._unwind_what_went_on(
                pair, {group.leg: quote_side, cross_leg: cross_side},
                {group.leg: quote_fill, cross_leg: cross},
                (group.leg, cross_leg))
            if naked is not None:
                reason = (f"{reason} — AND THE UNWIND FAILED "
                          f"({naked['why']}): {naked['volume']:g} lots of "
                          f"{naked['symbol']} are ON and unhedged, "
                          f"tickets "
                          f"{', '.join(str(t) for t in naked['tickets'])}")
                logging.critical("%s: %s", pair.key, reason)
            # If the crossing account cannot trade, none of the remaining
            # synthetics on this pair can complete either.
            self._pull_pair(pair, reason)
            for order in group.orders:
                if order.is_working:
                    order.state = OrderState.REJECTED
                    order.reason = reason
            return {'group': (group.pair_key, group.side.value, group.level,
                              group.position_id),
                    'action': 'hedge_rejected', 'reason': reason,
                    'naked': naked, 'naked_ms': naked_ms}

        fills = ({'a': cross, 'b': quote_fill} if group.leg == 'b'
                 else {'a': quote_fill, 'b': cross})
        position = self._book_fill(pair, group, fills, contract_a, contract_b,
                                   elapsed_ms, naked_ms)
        self._settle_orders(group, position.quantity)
        # NOTHING IS REDUCED HERE, deliberately.
        #
        # An earlier version closed the opposite position at this
        # point, after the fill had already opened a new one. The
        # arithmetic looked right and the result was not: a click meant
        # to flatten left a BRAND NEW position on — SELL 1 became BUY 1
        # instead of flat, with two fresh tickets, which is what the
        # desk reported.
        #
        # A resting order that reduces is now a CLOSING order from the
        # moment it is placed (coordinator._rest_reducing_orders), so
        # its fill lands in `_on_closing_fill` and never reaches here
        # at all. Two mechanisms for one job would double-close.
        return {'group': (group.pair_key, group.side.value, group.level,
                          group.position_id),
                'action': 'filled', 'position': position.position_id,
                'hedge_ms': elapsed_ms, 'naked_ms': naked_ms}

    def _naked_ms(self, state, at=None):
        """From the BROKER's fill stamp to now, in ms. None if unknown.

        `filled_at` is the server's wall clock encoded as an epoch, so
        the measured offset has to come off it before it can be
        subtracted from ours - the two are in different time zones and
        the difference is hours, not milliseconds.

        None when either half is missing, and never a zero: an
        unmeasured naked window rendered as 0ms is the report telling
        the desk that nothing happened.
        """
        stamp = state.get('filled_at')
        offset = state.get('server_offset_sec')
        if not stamp or offset is None:
            return None
        now = self.executor.clock() if at is None else at
        # Clamped at zero rather than allowed negative: a clock that
        # disagrees by a second should read "instant", not "-1000ms".
        return max(0.0, (now - (float(stamp) - float(offset))) * 1000.0)

    def _pull_residual(self, pair, group, state, filled, ticket):
        """Pull what is left of a partially filled pending.

        The residual is re-rested at the next pass at the size that is
        actually still working; leaving it would put a second pending on
        the same level. Returns the (state, filled) to act on, because
        it can fill FURTHER while being cancelled.
        """
        if not state.get('still_open') or ticket is None:
            return state, filled
        leg = self.legs.get(pair.account_a if group.leg == 'a'
                            else pair.account_b)
        if leg is None:
            return state, filled
        rest = leg.cancel_order(ticket)
        extra = float(rest.get('filled_volume') or 0.0) - filled
        if extra > 0:
            # It filled further while we were cancelling it.
            filled += extra
            state = dict(state, price=rest.get('price') or state.get('price'),
                         position_tickets=rest.get('position_tickets')
                         or state.get('position_tickets'))
        return state, filled

    def triggers(self):
        """Are entries held here and crossed at the level (the default),
        rather than quoted on one leg at the broker?"""
        return str(self.config.get('WORKING_ORDERS', 'TRIGGER')
                   or 'TRIGGER').upper() != 'QUOTE'

    def _work_trigger(self, pair, md, group):
        """One pass over a held ENTRY. Nothing of this is at the broker.

        WHY NOT A LIMIT ON ONE LEG. Live on spot gold against the gold
        future: SELL clicked at 28.30, on at 26.84 - and the half-size
        one before it 0.25 worse, the same way. The limit rested on
        GCZ6 at 28.30 + XAUUSD's ask. It can only fill when GCZ6 rises
        to it, and two legs that are the same metal rise TOGETHER: by
        the time the fill was seen and XAUUSD crossed, its ask had
        risen by the same $1.46. A fill there is a move in gold, not in
        the spread, so every fill is a bad one, and a gold move the
        other way never fills at all.

        So the order is held HERE, like a close. When the EXECUTABLE
        spread is at the level or better - the Sell spread at or over a
        SELL, the Buy spread at or under a BUY, the side the ladder drew
        it on - both legs cross at MARKET together, through the same
        `market_entry` a market click takes: the same sizing, the same
        unwind of a leg that goes on alone, the same refusals in the
        broker's words. It fills when the SPREAD gets there.

        What it costs, honestly: both legs' bid-ask (the quote earned
        one), and it is only working while this is running - which the
        quote was too, since our pendings are swept at shutdown.
        """
        key = self.key_for_group(group)
        if not group.quantity:
            self.groups.pop(key, None)
            return None
        if not md or md.get('guard_reason'):
            # Not fired off a print the system itself does not trust:
            # the level is still there when the quote is good again.
            group.reason = (f"{(md or {}).get('guard_reason') or 'no price'}"
                            f" — holding the order, nothing sent")
            return None
        if not closing_trigger_reached(group.side, group.level, md):
            group.reason = None
            return None

        if self.config.get('RECHECK_BEFORE_SEND', True):
            # The poll saw the level. Is it STILL there? Read both legs
            # again now, and send on those prices or not at all.
            fresh, why = self._fresh_market(pair, md)
            if fresh is None:
                group.reason = (f'could not re-read the prices before '
                                f'sending ({why}) — nothing sent')
                return None
            if not closing_trigger_reached(group.side, group.level, fresh):
                group.rechecks_missed += 1
                group.reason = None
                logging.info(
                    '%s: %s at %s - the poll saw the level, the fresh '
                    'prices did not (%s %s); not sent, still working',
                    pair.key, group.side.value, group.level,
                    'Sell spread' if group.side is SpreadSide.SELL
                    else 'Buy spread',
                    fresh.get('short_spread' if group.side is SpreadSide.SELL
                              else 'long_spread'))
                return {'group': key, 'action': 'recheck_missed',
                        'level': group.level}
            md = fresh

        quantity = group.quantity
        try:
            result = self.executor.market_entry(
                pair, group.side, md, quantity, clicked_level=group.level)
        except Exception as e:
            # A network call inside the pricing pass. Raising out of
            # `work()` would take every pair's orders down with it.
            result = None
            reason = f'the order raised: {e}'
        else:
            reason = result.reason
        if result is not None and result.ok and result.position:
            position = result.position
            position.order_type = OrderType.LIMIT
            # Scored against the level the trader NAMED, as a working
            # order always was - not the touch it crossed.
            position.entry_slippage = slippage(group.level,
                                               position.entry_spread,
                                               group.side)
            position = self.book.add_position(position)
            self._settle_orders(group, position.quantity or quantity)
            if not group.quantity:
                self.groups.pop(key, None)
            self._remember(position)
            logging.info('%s: %s %g at %s reached - both legs crossed, on at '
                         '%s', pair.key, group.side.value, quantity,
                         group.level, position.entry_spread)
            return {'group': key, 'action': 'triggered', 'ok': True,
                    'position': position.position_id,
                    'level': group.level,
                    'entry_spread': position.entry_spread}

        # It reached its level and did not go on. The order ENDS, with
        # the broker's own words on it: a refusal re-fired three times a
        # second is a broker hammered, and an entry is never owed.
        reason = (f'reached {group.level:g} but did not go on: '
                  f'{reason or "no reason given"}')
        logging.warning('%s: %s %g - %s', pair.key, group.side.value,
                        quantity, reason)
        for order in group.orders:
            if order.is_working:
                order.state = OrderState.REJECTED
                order.reason = reason
        self.groups.pop(key, None)
        if self.on_refusal is not None:
            try:
                self.on_refusal(pair.key, reason, group.side.value,
                                group.level)
            except Exception as e:            # never lose the pass
                logging.error('could not record the refusal: %s', e)
        return {'group': key, 'action': 'rejected', 'ok': False,
                'reason': reason,
                'naked': getattr(result, 'naked', None)}

    def _fresh_market(self, pair, md):
        """Both legs' prices read NOW, as the poll's market data.

        Returns (md, None), or (None, why) when either leg could not be
        read - and then nothing is sent: the point of looking again is
        lost if the look is skipped. Everything the poll measured that
        a tick cannot (the guards, sigma) is carried over from `md`.
        """
        ticks = {}
        for name, account, symbol in (('A', pair.account_a, pair.symbol_a),
                                      ('B', pair.account_b, pair.symbol_b)):
            leg = self.legs.get(account)
            try:
                tick = leg.tick(symbol) if leg is not None else None
            except Exception as e:
                tick, error = None, str(e)
            else:
                error = 'no tick'
            if not tick:
                return None, f'leg {name} {symbol}: {error}'
            ticks[name] = tick
        try:
            fresh = compute_spread(pair, ticks['A'], ticks['B'],
                                   pair.hedge_ratio,
                                   clock=self.executor.clock)
        except Exception as e:
            return None, f'the prices did not make a spread: {e}'
        return dict(md, **fresh), None

    def _work_closing(self, pair, md, group):
        """One pass over a resting CLOSE. Nothing of this is at the broker.

        A closing order CANNOT be a broker pending. MT5 honours
        `position` on TRADE_ACTION_DEAL and ignores it on
        TRADE_ACTION_PENDING, so a "closing" limit rests as an ordinary
        limit and, on a hedging account, OPENS a second position facing
        the other way. Live 2026-09-02: ticket 2092 was rested to close
        ticket 2090 and filled as a BUY 0.01 beside the SELL 0.01 it was
        meant to close.

        Attaching a broker take-profit to the leg instead is not the
        answer either — one leg closing alone converts the hedge into a
        naked outright, which is the rule this system is built on.

        So the level is held HERE, and when the market reaches it the
        position is closed BY TICKET on both legs at once. What this
        costs, honestly: the close only happens while this is running,
        and it crosses at the touch rather than earning the level.
        """
        key = self.key_for_group(group)
        position = self.book.position(group.position_id)
        if position is None or not position.is_open:
            # Nothing left to close. Not an error — a manual close, the
            # overnight rule or the reconciler got there first.
            #
            # The remainder goes with it, deliberately. The click said
            # "cover this and open the rest"; something else covered it,
            # so the rest is no longer the trade that was asked for, and
            # putting a position on by itself minutes later is the last
            # thing anyone wants from a ladder.
            if group.open_after > 0:
                logging.info(
                    '%s: %s was closed elsewhere, so the %g spread(s) that '
                    'click would have opened are dropped', pair.key,
                    group.position_id, group.open_after)
            self.groups.pop(key, None)
            return None
        if not group.quantity:
            self.groups.pop(key, None)
            return None

        if not md or md.get('guard_reason'):
            # A guard NEVER withholds a close the trader asks for — and
            # this is not that. Firing a resting order off a print the
            # system itself calls untrustworthy closes at a level the
            # market may never have shown. It is HELD, and said on the
            # ladder, so the trader can close by hand at once. Nothing
            # blocks that.
            group.reason = (f"{(md or {}).get('guard_reason') or 'no price'}"
                            f" — the resting close is held")
            return None
        if not closing_trigger_reached(group.side, group.level, md):
            # The market has moved away. Whatever refused the close a
            # moment ago gets a clean slate when the level comes back:
            # a session break or a spread that widened out is not a
            # fault to stay escalated over.
            group.close_attempts = 0
            group.escalated = False
            group.reason = None
            return None
        if group.escalated:
            # Already said, loudly, and the reason stays on the ladder.
            # A close the trader asks for is still never blocked.
            return None
        group.reason = None

        # WHAT THIS GROUP ASKED FOR, NOT THE WHOLE TICKET.
        #
        # The bug this replaces cost a trader ten times the size they
        # clicked. A ladder click for 1 spread over an older 10-spread
        # ticket arms a closing order for 1 — `positions_to_reduce`
        # takes the oldest ticket only as far as the click reaches, and
        # `arm` records that 1 on the synthetic. Then the level was
        # reached and THIS call closed `position` with no quantity at
        # all, which means all of it. The part-close intent was built
        # correctly and thrown away one line before it was used.
        #
        # Live: a 0.01 click against a 0.10 ticket from the previous
        # day took the whole 0.10 off, at a level chosen for a 0.01
        # trade. The trader's own 0.01 stayed open.
        #
        # It reached BOTH modes, because both rest through here: LIMIT
        # at any level, and MARKET clicked away from the touch. Only a
        # MARKET click AT the touch was right, because that path goes
        # through `reduce_first`, which has always passed its `take`.
        #
        # None when the group covers the whole ticket — AutoRouting's
        # target, CLOSE ALL, a click at or over the position's size.
        # Those take the unchanged full-close path, where the volumes
        # come from the broker's own book rather than from a share we
        # computed, and no rounding enters a close that needs none.
        held = float(position.quantity or 0.0)
        wanted = group.quantity
        part = None if wanted >= held - 1e-9 else wanted

        # STILL the whole position, and deliberately so: `fraction`
        # below is measured against the WHOLE ticket by
        # `_closed_fraction`, so this is what turns it back into
        # spreads. Reading `wanted` here would under-settle a part
        # close by the square of the share.
        quantity = position.quantity
        try:
            answer = self.executor.close_position(
                pair, position, md, reason=self._close_reason(group),
                quantity=part,
                # This module owns these orders; it settles them itself
                # below. Letting the close disarm them would mark a
                # close that WORKED as 'cancelled', and would delete the
                # level a close that FAILED still has to be watched at.
                disarm=False)
        except Exception as e:
            # This is a NETWORK call inside the pricing pass. Unguarded,
            # a broker that hangs up mid-close would raise out of
            # `work()` and take the whole poll down with it — every
            # pair's quotes, not just this level.
            self._close_got_nowhere(pair, group, f'the close raised: {e}')
            return {'group': key, 'action': 'close_failed',
                    'position': position.position_id, 'ok': False,
                    'escalated': group.escalated, 'reason': group.reason}
        if not answer.get('ok'):
            # The position stays open and so does the level. The
            # broker's own words go on the ladder — never "check the
            # log" (spec §11).
            self._close_got_nowhere(pair, group,
                                    close_failure(answer, position))
            self._remember(position)
            return {'group': key, 'action': 'close_failed',
                    'position': position.position_id, 'ok': False,
                    'escalated': group.escalated, 'reason': group.reason}
        if answer.get('partial') and not answer.get('fraction'):
            # It reported a close and took nothing off. Counted as a
            # failure, or a broker that answers without acting would be
            # retried at poll rate for ever.
            self._close_got_nowhere(
                pair, group, 'the close reported success and moved nothing')
            self._remember(position)
            return {'group': key, 'action': 'close_failed',
                    'position': position.position_id, 'ok': False,
                    'escalated': group.escalated, 'reason': group.reason}
        group.close_attempts = 0
        if answer.get('partial'):
            # The broker closed only PART of it. The synthetics settle
            # for what actually went and the rest keep working, so the
            # level stays armed and takes the remainder off at the next
            # pass it is still reached on.
            self._settle_orders(group, quantity * answer.get('fraction', 0.0))
            self._remember(position)
            return {'group': key, 'action': 'closed_part_at_level',
                    'position': position.position_id, 'level': group.level,
                    'imbalanced': answer.get('imbalanced', False),
                    'remaining': answer.get('remaining'), 'ok': True}
        self.groups.pop(key, None)
        for order in group.orders:
            if order.is_working:
                order.filled_quantity = order.quantity
                order.state = OrderState.FILLED
        self._remember(position)
        event = {'group': key, 'action': 'closed_at_level',
                 'position': position.position_id, 'level': group.level,
                 'ok': True}
        # AND ONLY NOW the remainder, if the click was bigger than what
        # it covered. Not before: a remainder resting while the close is
        # still outstanding is the two-engines bug above, and it puts
        # the new position on over the old one.
        if group.open_after > 0:
            opened = self.book.add_order(pair, group.side, group.level,
                                         group.open_after)
            self.group_for(pair, opened)
            logging.info('%s: closed %s at %s, now resting the remaining '
                         '%g spread(s) the click asked for', pair.key,
                         position.position_id, group.level, group.open_after)
            event['opened_remainder'] = group.open_after
        return event

    def _close_got_nowhere(self, pair, group, reason):
        """A fire that did not reduce the position. Bounded, then said.

        The level is watched every poll, so a broker that refuses would
        be asked three times a second for as long as the market sits
        there. That is hammering, and it buries the one line the trader
        needs. After `CLOSE_ATTEMPTS` it is escalated ONCE and left
        alone — the same shape the reconciler uses, and for the same
        reason.

        Nothing here blocks a close the trader asks for by hand.
        """
        group.close_attempts += 1
        attempts = int(self.config.get('CLOSE_ATTEMPTS', 3) or 3)
        group.reason = reason
        if group.close_attempts < attempts:
            logging.error('%s: a resting close at %s did not go through: %s',
                          pair.key, group.level, reason)
            return
        group.escalated = True
        group.reason = (f'CLOSE IT BY HAND — {reason} '
                        f'(after {group.close_attempts} attempts)')
        logging.critical('%s: %s', pair.key, group.reason)

    def _close_reason(self, group):
        """Whose close this was, in the words the trader reads.

        A close the trader clicked is not automation (spec 5.4), and
        reporting their own click as a take-profit is the same mistake
        the AutoRouting switch made when it swept their order away.
        """
        if any(getattr(o, 'auto_armed', False) for o in group.orders):
            return 'auto take-profit'
        return 'closed by the trader'

    def key_for_group(self, group):
        return (group.pair_key, group.side.value, round(group.level, 10),
                group.position_id)

    def arm(self, pair, position, level, quantity=None, auto=False):
        """Rest a working order to CLOSE `position` at `level` — the
        trader's own, from a reducing click or Close @ Limit.

        `auto` marks an order as automation's. Nothing arms one any more
        (AutoRouting has been removed); the mark is kept so a target it
        armed before the upgrade can still be told apart from the
        trader's and pulled on its own.
        """
        if level is None:
            return None
        existing = self.book.orders_for_position(position.position_id)
        # ONE TARGET FOR AUTOMATION, AS MANY AS ARE CLICKED FOR THE
        # TRADER.
        #
        # This returned any existing closing order, at any level, to
        # whoever asked — which is right for AutoRouting, called on
        # every fill and every poll, and must not stack duplicates. It
        # was wrong for the trader: their second buy click at a second
        # price got the FIRST order handed back, so the blue side could
        # only ever hold one working order while the red side held as
        # many as were clicked. Scaling out at two prices is the
        # ordinary way a ladder is used.
        if auto:
            if existing:
                return existing[0]
        else:
            # ...except where the trader has named the level automation
            # already holds. Then they agree, and re-arming would trade
            # an order that is already resting for an identical one.
            agreed = [o for o in existing
                      if getattr(o, 'auto_armed', False)
                      and abs(o.level - level) < 1e-9]
            if agreed:
                return agreed[0]
        order = self.book.add_order(
            pair, position.side.opposite, level,
            quantity if quantity is not None else position.quantity,
            order_type=OrderType.LIMIT,
            position_id=position.position_id, auto_armed=auto)
        self.group_for(pair, order)
        logging.info('%s: %s armed a %s to close %s at %s', pair.key,
                     'AutoRouting' if auto else 'the trader',
                     position.side.opposite.value,
                     position.position_id, level)
        return order

    def carry_remainder(self, order, quantity):
        """Let a reducing click's leftover ride on the close it armed.

        The remainder opens when — and only when — that close has
        actually gone through. See `QuoteGroup.open_after`.
        """
        group = self.groups.get(self.key_for(order))
        if group is None:
            return False
        group.open_after = float(quantity or 0.0)
        return True

    def disarm(self, position_id, reason='its position is gone'):
        """Pull every closing order armed against one position.

        Called BEFORE the close, never after: a level left armed
        against a position that has gone fires at the next tick that
        reaches it, and closes something that is not there.
        """
        pulled = []
        for order in self.book.orders_for_position(position_id):
            self.book.cancel(order.order_id, reason)
            pulled.append(order)
        for key, group in list(self.groups.items()):
            if group.position_id != position_id:
                continue
            # THE REMAINDER GOES WITH IT, AND IS SAID SO.
            #
            # A reducing click bigger than the position it covered
            # holds the difference here until the close goes through
            # (`QuoteGroup.open_after`). Dropping it when the position
            # is gone is deliberate - the click said "cover this and
            # open the rest", something else covered it, and putting a
            # naked position on by itself minutes later is the last
            # thing anyone wants from a ladder - but `_work_closing`
            # says so on its own path and this one did not. A trader
            # who clicked 100 over a 93 saw the 7 simply never appear.
            #
            # It is NOT scaled to what was closed. The click named an
            # absolute size, not a proportion of somebody else's fill.
            if group.open_after > 0:
                logging.info(
                    '%s: %s — the %g spread(s) that click would have opened '
                    'afterwards are dropped with it', group.pair_key,
                    reason, group.open_after)
            pair = self.config.pairs.get(group.pair_key)
            if pair is not None:
                self._pull(pair, group, reason)
            self.groups.pop(key, None)
        return pulled

    def disarm_auto(self, position_id, reason):
        """Pull only what AUTOMATION armed against a position.

        `disarm` pulls everything, which is right when the position is
        GONE — any closing order left behind would fill and open a
        naked one. It is wrong for the AutoRouting switch, which stands
        automation down and has no business touching a close the trader
        placed by hand.
        """
        pulled = []
        for order in self.book.auto_armed_for(position_id):
            self.book.cancel(order.order_id, reason)
            pulled.append(order)
            for key, group in list(self.groups.items()):
                if group.position_id != position_id:
                    continue
                if not any(o.order_id == order.order_id
                           for o in group.orders):
                    continue
                pair = self.config.pairs.get(group.pair_key)
                if pair is not None:
                    self._pull(pair, group, reason)
                self.groups.pop(key, None)
        return pulled

    def _pull_pair(self, pair, reason):
        for key, group in list(self.groups.items()):
            if group.pair_key != pair.key:
                continue
            self._pull(pair, group, reason)
            for order in group.orders:
                if order.is_working:
                    order.state = OrderState.CANCELLED
                    order.reason = reason
            self.groups.pop(key, None)

    def _book_fill(self, pair, group, fills, contract_a, contract_b,
                   elapsed_ms, naked_ms=None):
        leg_a_side, leg_b_side = group.side.leg_sides()
        leg_a = LegFill(pair.account_a, pair.symbol_a, leg_a_side,
                        fills['a'].get('filled_volume') or 0.0,
                        fills['a'].get('price'),
                        order_ticket=fills['a'].get('ticket'),
                        position_tickets=fills['a'].get('position_tickets'),
                        contract_size=contract_a, clock=self.executor.clock)
        leg_b = LegFill(pair.account_b, pair.symbol_b, leg_b_side,
                        fills['b'].get('filled_volume') or 0.0,
                        fills['b'].get('price'),
                        order_ticket=fills['b'].get('ticket'),
                        position_tickets=fills['b'].get('position_tickets'),
                        contract_size=contract_b, clock=self.executor.clock)
        entry_spread = None
        if leg_a.price is not None and leg_b.price is not None:
            entry_spread = leg_b.price - float(pair.hedge_ratio) * leg_a.price
        # In SPREADS, from what leg B actually holds — the same unit the
        # trader clicked in, so a partial fill reads as 0.5 spreads
        # rather than as a lot count nobody sized in.
        quantity = (leg_b.volume / pair.clip_lots_b
                    if pair.clip_lots_b else leg_b.volume)
        position = SpreadPosition(
            pair.key, group.side, quantity, leg_a, leg_b, entry_spread,
            OrderType.LIMIT, sizing.spread_units(leg_b.volume, contract_b),
            clock=self.executor.clock)
        position.click_to_on_ms = elapsed_ms
        # The one that cannot be flattered by how late we looked.
        position.naked_ms = naked_ms
        # Scored against the level the trader NAMED: a maker fill's
        # benchmark is the clicked level, not the touch it crossed.
        position.entry_slippage = slippage(group.level, entry_spread,
                                           group.side)
        return self._remember(self.book.add_position(position))

    def _remember(self, position):
        """Write a position the quoter changed through to the database.

        On the change, not on a timer: the window between an order
        filling and the state being safe is the window a crash turns
        into a position nobody can recover.
        """
        if self.on_change is not None and position is not None:
            try:
                self.on_change(position)
            except Exception as e:            # never lose the trade
                logging.critical('could not persist %s: %s',
                                 position.position_id, e)
        return position

    def _settle_orders(self, group, filled_spreads):
        """Mark the synthetics behind a fill, OLDEST first.

        A partial fill fills the earliest clicks and leaves the rest
        working — queue order, which is the only fair reading of "three
        separate orders at one price".
        """
        remaining = filled_spreads
        for order in sorted((o for o in group.orders if o.is_working),
                            key=lambda o: o.created_at):
            if remaining <= 1e-9:
                break
            take = min(order.remaining, remaining)
            order.filled_quantity += take
            remaining -= take
            if order.remaining <= 1e-9:
                order.state = OrderState.FILLED

    # -- what the monitor renders -------------------------------------------

    def snapshot(self, pair_key=None):
        """Every group, with the leg its real pending is ON named.

        In LIMIT mode the pending rests on ONE leg and the other is
        crossed at market when it fills. Which leg that is, and at what
        price, is the difference between "my order is at the broker"
        and "half of it is" — and it was only ever a bare 'a'/'b' on
        one panel, so leg A looked like it had no order at all.
        """
        out = []
        for group in self.groups.values():
            if pair_key is not None and group.pair_key != pair_key:
                continue
            row = group.to_dict()
            pair = self.config.pairs.get(group.pair_key)
            if pair is not None and not group.held:
                on_a = group.leg == 'a'
                row['account'] = pair.account_a if on_a else pair.account_b
                row['symbol'] = pair.symbol_a if on_a else pair.symbol_b
                row['crosses'] = pair.symbol_b if on_a else pair.symbol_a
                row['crosses_leg'] = 'B' if on_a else 'A'
            out.append(row)
        return out
