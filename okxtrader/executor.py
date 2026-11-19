"""Getting a pair ON, and getting it OFF again.

This module owns the two dangerous moments in the product: the naked
window between one leg being on and the other landing, and the close
that must target a position TICKET because these accounts are hedging
mode.

MARKET mode only lives here for now (spec §14: get flat-and-back-to-flat
exactly right before anything rests). The LIMIT path reuses everything
below it — the hedge arithmetic, the deadline, the unwind, the
ticket-based closes — and differs only in how the first leg gets on.

Three rules, each of which cost money in the system this is ported
from:

1. **The crossing order goes IMMEDIATELY on fill — not after a wait.**
   `LEG_DEADLINE_SEC` is a failure-ESCALATION window, not a patience
   window. A market order round-trips in ~24ms; 2.0s is ~80x headroom
   and only fires on a real fault.
2. **Only a REJECTED crossing leg unwinds.** Slow is not rejected.
3. **The unwind CLOSES, it does not offset.** A plain opposite market
   order on a hedging account opens a SECOND position; that has already
   happened live, and the reconciler found both 60 seconds later.
"""

import logging
import time

from . import costs, sizing
from .models import (LegFill, OrderSide, OrderType, SpreadPosition,
                     SpreadSide, new_id)
from .spread import closing_prices, executable_spread


class ExecutionResult:
    """What a click did, in the words the ladder will print.

    `reason` is never "check the log" — it carries the broker's own
    words where the broker is the one who refused (spec §11).
    """

    def __init__(self, ok, position=None, reason=None, refused=False,
                 naked=None, elapsed_ms=None, legs=None):
        self.ok = ok
        self.position = position
        self.reason = reason
        #: Refused BEFORE anything moved — no money at risk. Distinct
        #: from a failure that left a leg on.
        self.refused = refused
        #: Set when a leg is on and its hedge is not. The loudest thing
        #: on the screen (spec §16).
        self.naked = naked
        self.elapsed_ms = elapsed_ms
        self.legs = legs or {}

    def to_dict(self):
        return {'ok': self.ok, 'reason': self.reason, 'refused': self.refused,
                'naked': self.naked, 'elapsed_ms': self.elapsed_ms,
                'position': self.position.to_dict() if self.position else None,
                'legs': {k: (v.to_dict() if hasattr(v, 'to_dict') else v)
                         for k, v in self.legs.items()}}


class PairExecutor:
    """Executes one click on one pair, across two accounts."""

    def __init__(self, config, legs, clock=time.time, sleep=time.sleep):
        self.config = config
        #: {account name: LocalLeg | RemoteLeg}
        self.legs = legs
        self.clock = clock
        self.sleep = sleep
        #: Every fill's click->pair-on time, so the operator can see
        #: whether 2.0s is the right deadline (spec §4, §16).
        self.timings = []
        #: Called with (position_id, reason) BEFORE a position is
        #: closed, whoever closes it. The coordinator points it at the
        #: quoter's disarm, so an AutoRouting take-profit is pulled
        #: FIRST and then the position goes — never the other way
        #: round, which leaves a resting order with a guaranteed fill
        #: and nothing to close.
        self.before_close = None

    # -- entry ----------------------------------------------------------

    def market_entry(self, pair, side, md, spreads=None, clicked_level=None):
        """Cross both legs now. The clicked price is a slippage guard.

        Returns an ExecutionResult. Nothing here loops or re-enters: one
        click is one order (spec, decision 5).
        """
        side = SpreadSide(getattr(side, 'value', side))
        spreads = float(spreads if spreads is not None
                        else pair.default_quantity)
        started = self.clock()

        refusal = self.precheck(pair, side, md, spreads, clicked_level)
        if refusal is not None:
            return ExecutionResult(False, reason=refusal, refused=True)

        plan = self.size(pair, md, spreads)
        leg_a_side, leg_b_side = side.leg_sides()
        # The decision touch — what the market offered at the moment the
        # order was decided. Slippage is scored against THIS, not the
        # mid (spec §5).
        decision_spread = executable_spread(md, side)

        first, second = self.crossing_order(pair)
        sides = {'a': leg_a_side, 'b': leg_b_side}
        volumes = {'a': plan['leg_a_lots'], 'b': plan['leg_b_lots']}
        contracts = {'a': plan['leg_a_contract'], 'b': plan['leg_b_contract']}
        tag = new_id('LADDER')

        # The harder-to-fill leg FIRST. Filling the easy leg and then
        # discovering the hard one will not fill is how you end up naked.
        first_fill = self._send_leg(pair, first, sides[first], volumes[first],
                                    contracts[first], tag)
        if not first_fill.get('ok'):
            # A REFUSAL IS ONLY A REFUSAL IF NOTHING WENT ON.
            #
            # This branch used to assert "nothing is on. This is a
            # refusal, not a naked position" and return on the strength
            # of `ok` alone. `ok` is the broker's verdict on the
            # REQUEST, and a request can be refused with part of it
            # already dealt - a 10010 partial fill, or a rejection that
            # lands after the first deal. Then something IS on: the
            # second leg was never sent, nothing was unwound, nothing
            # was booked, and the leg sat at the broker until the
            # reconciler listed it as a position carrying our own
            # comment that nothing may close. That is the orphan leg
            # two desks reported.
            #
            # So the question asked here is the broker's book, not the
            # broker's opinion.
            if self._went_on(first_fill) is None:
                return ExecutionResult(
                    False, refused=True,
                    reason=(f"leg {first.upper()} refused: "
                            f"{first_fill.get('error')}"))
            reason = (f"leg {first.upper()} was refused "
                      f"({first_fill.get('error')}) but went on anyway — "
                      f"unwinding it; leg {second.upper()} was not sent")
            logging.critical("%s: %s", pair.key, reason)
            naked = self._unwind_what_went_on(pair, sides,
                                              {first: first_fill}, (first,))
            elapsed_ms = (self.clock() - started) * 1000.0
            return ExecutionResult(False, reason=reason, naked=naked,
                                   elapsed_ms=elapsed_ms,
                                   legs={first: first_fill})

        # IMMEDIATELY — no wait, no poll interval, no patience.
        second_fill = self._cross_with_deadline(
            pair, second, sides[second], volumes[second], contracts[second],
            tag)

        elapsed_ms = (self.clock() - started) * 1000.0
        fills = {first: first_fill, second: second_fill}

        if not second_fill.get('ok'):
            # A genuine rejection. Unwind the leg that IS on, by ticket.
            reason = (f"leg {second.upper()} rejected: "
                      f"{second_fill.get('error')} — unwinding leg "
                      f"{first.upper()}")
            logging.critical("%s: %s", pair.key, reason)
            # BOTH legs, not just the first. A rejected crossing leg can
            # still have dealt part of itself on the way to being
            # rejected, and unwinding only the leg we believe is on
            # leaves the other piece exactly where the orphans came
            # from.
            naked = self._unwind_what_went_on(pair, sides, fills,
                                              (first, second))
            return ExecutionResult(False, reason=reason, naked=naked,
                                   elapsed_ms=elapsed_ms, legs=fills)

        matched = self._matched_fraction(plan, fills)
        if matched < float(self.config.get('MIN_MATCHED_FRACTION', 0.4)):
            reason = (f"only {matched:.0%} of the clip matched on both legs "
                      f"— unwinding rather than holding a part-hedged pair")
            logging.warning("%s: %s", pair.key, reason)
            # A trader clicked, one leg went on, the other did not, and
            # this branch tried to undo the one that had. If that undo
            # FAILED - a refused close, a ticket the broker would not
            # take back - nothing said so, and the first sign of it was
            # something else tidying the leg away. The unwind's answer
            # is read in one place now, for all three of these
            # branches; see _unwind_what_went_on.
            naked = self._unwind_what_went_on(pair, sides, fills, ('a', 'b'))
            return ExecutionResult(False, reason=reason, naked=naked,
                                   elapsed_ms=elapsed_ms, legs=fills)

        position = self._book_position(pair, side, spreads, plan, fills,
                                       sides, decision_spread, elapsed_ms)
        self.timings.append(elapsed_ms)
        return ExecutionResult(True, position=position,
                               elapsed_ms=elapsed_ms, legs=fills)

    # -- the checks that happen BEFORE anything moves ---------------------

    def precheck(self, pair, side, md, spreads, clicked_level=None):
        """A one-line reason to refuse this click, or None.

        Both legs are verified up front: a pair whose child order is
        under either leg's minimum must be refused before any money
        moves — and, on the quoting path, before anything rests at the
        broker.
        """
        if md is None:
            return (f"{pair.key} has no price yet — both legs need a live "
                    f"quote before an order can be sized")
        for leg in ('a', 'b'):
            account = pair.account_a if leg == 'a' else pair.account_b
            if account not in self.legs:
                return (f"account '{account}' (leg {leg.upper()}) is not "
                        f"connected — start its leg runner")

        plan = self.size(pair, md, spreads)
        if plan.get('reason'):
            return plan['reason']

        guard = md.get('guard_reason')
        if guard:
            # A click is the trader looking at the screen, so the ladder
            # shows the guard's state continuously (spec §8) — but a
            # market order into a stale or desynced print is still
            # refused, with the reason, not silently swallowed.
            return f"{guard} — the click was not sent"

        if clicked_level is not None:
            breach = self.protection_breach(pair, side, md, clicked_level)
            if breach:
                return breach
        return None

    def protection_breach(self, pair, side, md, clicked_level):
        """Market-with-protection: refuse a fill worse than the clicked
        spread by more than MARKET_PROTECTION_TICKS.

        Without it, a click anywhere on the ladder fills at whatever the
        touch happens to be, which on a desynced print is exactly the
        $20.40 fault the jump guard exists for.
        """
        increment = pair.effective_increment()
        if not increment:
            return None                      # nothing to measure in
        ticks = float(self.config.get('MARKET_PROTECTION_TICKS', 3.0) or 0.0)
        if ticks <= 0:
            return None
        touch = executable_spread(md, side)
        if touch is None:
            return None
        # Worse means: paying MORE than clicked to buy, receiving LESS
        # than clicked to sell.
        slip = (touch - clicked_level) if side is SpreadSide.BUY \
            else (clicked_level - touch)
        allowed = ticks * increment
        if slip > allowed:
            return (f"the market is {slip:.4g} through your {clicked_level:.4g} "
                    f"— more than the {ticks:g} x {increment:g} protection. "
                    f"Nothing was sent; click again at the price on screen.")
        return None

    def size(self, pair, md, spreads):
        """Leg lots and `k` for this click — one place, one answer."""
        return sizing.clip_plan(pair, pair.meta_a or {}, pair.meta_b or {},
                                (md or {}).get('leg_a_mid'),
                                (md or {}).get('leg_b_mid'), spreads)

    def crossing_order(self, pair):
        """(first, second) leg keys — the harder-to-fill leg FIRST.

        Harder means: the wider book, the larger minimum volume, or the
        slower feed — usually the future. Measured from the legs' own
        MT5 metadata rather than assumed, and the ladder shows both
        widths so the operator can see why.
        """
        meta_a, meta_b = pair.meta_a or {}, pair.meta_b or {}
        score_a = (meta_a.get('volume_min') or 0.0, meta_a.get('width') or 0.0)
        score_b = (meta_b.get('volume_min') or 0.0, meta_b.get('width') or 0.0)
        return ('b', 'a') if score_b >= score_a else ('a', 'b')

    # -- leg mechanics ---------------------------------------------------

    def _symbol(self, pair, leg):
        return pair.symbol_a if leg == 'a' else pair.symbol_b

    def _leg(self, pair, leg):
        account = pair.account_a if leg == 'a' else pair.account_b
        return self.legs.get(account)

    def _send_leg(self, pair, leg, side, volume, contract, tag):
        runner = self._leg(pair, leg)
        symbol = self._symbol(pair, leg)
        if runner is None:
            return {'ok': False, 'error': f'no leg runner for {symbol}'}
        result = runner.order(
            symbol, side.value, volume,
            slippage_points=float(self.config.get('SLIPPAGE_POINTS', 1.0)),
            # Stamped so our own orders are tellable from the trader's
            # terminal clicks in the order log (spec §6).
            comment=f'{tag}:{leg.upper()}')
        result = dict(result or {})
        result['symbol'] = symbol
        result['side'] = side.value
        result['contract_size'] = contract
        return result

    def _cross_with_deadline(self, pair, leg, side, volume, contract, tag):
        """Send the crossing leg, retrying only until the deadline.

        The deadline is not patience — there is nothing to be patient
        for, the hedge must go on. It bounds how long a genuine fault
        (a broker not answering, a filling mode nothing accepts) can
        leave us naked before it is escalated.
        """
        deadline = self.clock() + float(
            self.config.get('LEG_DEADLINE_SEC', 2.0))
        attempt = 0
        result = None
        while True:
            attempt += 1
            result = self._send_leg(pair, leg, side, volume, contract, tag)
            if result.get('ok'):
                result['attempts'] = attempt
                return result
            if self._went_on(result) is not None:
                # IT FAILED AND WENT ON ANYWAY - a partial fill, or a
                # rejection that arrived after the first deal. Sending
                # the next attempt now would put a SECOND order on top
                # of the piece that is already there, and the deadline
                # allows a hundred of them. The failure is returned
                # with what is on, for the caller to unwind.
                result['attempts'] = attempt
                return result
            if self.clock() >= deadline:
                result['attempts'] = attempt
                result['error'] = (f"{result.get('error')} (after {attempt} "
                                   f"attempts in "
                                   f"{self.config.get('LEG_DEADLINE_SEC')}s)")
                return result
            self.sleep(0.02)

    @staticmethod
    def _went_on(fill):
        """What a leg left AT THE BROKER, whatever the broker said.

        `ok` answers "did the broker accept the request". This answers
        the only question that matters when the answer to that is no:
        is any of it on? A filled volume or a position ticket both
        mean yes, and either is enough to unwind.

        None means nothing went on, which is the one case that may be
        reported as a clean refusal.
        """
        fill = fill or {}
        volume = float(fill.get('filled_volume') or 0.0)
        tickets = list(fill.get('position_tickets') or [])
        if volume <= 0 and not tickets:
            return None
        return {'volume': volume, 'tickets': tickets}

    def _unwind_what_went_on(self, pair, sides, fills, legs):
        """Take back every leg that is actually on. Returns the naked
        report, or None when nothing is left at the broker.

        ONE place, because all three ways a click can fail end the same
        way - something may be on and it has to come off - and the two
        that had their own copy of this drifted apart: one unwound a
        single leg, one read `filled_volume` alone, and neither noticed
        a leg that filled under a refusal.

        THE UNWIND'S ANSWER IS READ. A leg that will not come off is
        the loudest thing on the screen (spec §16) and CRITICAL in a
        log that reaches disk; a failure reported as a refusal with
        nothing said is how a trader ends up holding an unhedged leg
        overnight.
        """
        naked = None
        for leg in legs:
            fill = fills.get(leg) or {}
            on = self._went_on(fill)
            if on is None:
                continue                 # nothing of ours went on
            undo = self._unwind_leg(pair, leg, sides[leg], fill)
            if undo.get('ok'):
                continue
            naked = {
                'leg': leg.upper(),
                'symbol': self._symbol(pair, leg),
                'volume': on['volume'],
                'tickets': on['tickets'],
                'why': undo.get('error'),
            }
            logging.critical(
                "%s: leg %s is ON and could NOT be unwound (%s) — %s "
                "lots of %s are naked at the broker",
                pair.key, leg.upper(), undo.get('error'),
                on['volume'], self._symbol(pair, leg))
        return naked

    def _matched_fraction(self, plan, fills):
        """How much of the intended clip is hedged on BOTH legs.

        Hedge to what actually FILLED, not to what was sent: a pending
        can fill partially, and a market order under IOC can too.
        """
        wanted_a = plan['leg_a_lots'] or 0.0
        wanted_b = plan['leg_b_lots'] or 0.0
        got_a = (fills.get('a') or {}).get('filled_volume') or 0.0
        got_b = (fills.get('b') or {}).get('filled_volume') or 0.0
        if not wanted_a or not wanted_b:
            return 0.0
        return min(got_a / wanted_a, got_b / wanted_b)

    def _unwind_leg(self, pair, leg, entry_side, fill):
        """Close what is on, BY TICKET, newest first.

        Never an opposite market order: on a hedging account that opens
        a second position and leaves the first one live.
        """
        runner = self._leg(pair, leg)
        symbol = self._symbol(pair, leg)
        if runner is None:
            return {'ok': False, 'error': f'no leg runner for {symbol}'}
        tickets = list(fill.get('position_tickets') or [])
        if not tickets:
            return {'ok': False,
                    'error': f'leg {leg.upper()} filled but reported no '
                             f'position ticket — close {symbol} by hand'}
        return self.close_tickets(runner, symbol, tickets, entry_side,
                                  comment='UNWIND')

    def close_tickets(self, runner, symbol, tickets, entry_side,
                      volume=None, comment='CXL'):
        """Close specific position tickets, taking volumes from the
        BROKER's book rather than from what we sent.

        A ticket may be partly closed already, and asking to close more
        than is there fails the whole request. A ticket MT5 no longer
        lists is already gone — that is not an error.
        """
        entry_side = OrderSide(getattr(entry_side, 'value', entry_side))
        live = runner.positions(symbol)
        if live is None:
            # None means the leg could not be read — NOT that it is flat.
            return {'ok': False, 'error': f'{symbol}: the broker could not '
                                          f'be read; nothing was closed'}
        by_ticket = {str(p['ticket']): p for p in live}
        # What the broker actually holds of OUR tickets right now. A
        # ticket it no longer lists is already gone, and that leg is
        # done — which is not the same as having closed nothing.
        found = sum(float(by_ticket[str(t)]['volume'])
                    for t in tickets if str(t) in by_ticket)
        if tickets and not found:
            # NOT ONE OF OUR TICKETS IS THERE. That is normally true —
            # something closed them first — and it is also exactly what
            # ONE bad read looks like, and this answer is believed:
            # `_closed_fraction` books the position as fully closed on
            # it, and `_unwind_what_went_on` reports "nothing naked" on
            # it. Both would be a live leg struck off our own books
            # while the money sits at the broker.
            #
            # So it is READ AGAIN before it is believed. One extra call,
            # on a path that is rare, to turn a conclusion drawn from a
            # single answer into one drawn from two.
            again = runner.positions(symbol)
            if again is None:
                return {'ok': False, 'found': None,
                        'error': f'{symbol}: our tickets were not in the '
                                 f'first read and the broker could not be '
                                 f'read again; nothing was closed'}
            by_ticket = {str(p['ticket']): p for p in again}
            found = sum(float(by_ticket[str(t)]['volume'])
                        for t in tickets if str(t) in by_ticket)
            if not found:
                logging.warning(
                    '%s: none of tickets %s are at the broker on two '
                    'consecutive reads — treating them as already closed, '
                    'because nothing else can be concluded.', symbol,
                    ', '.join(str(t) for t in tickets))
        remaining = None if volume is None else float(volume)
        closed, errors = [], []
        # Newest first, so a partial unwind takes off the piece that was
        # most recently added. Sorted as NUMBERS: MT5 tickets are
        # numbers, and as strings '9999' sorts above '10000' — so the
        # day a ticket rolls a digit, "newest first" quietly becomes
        # "oldest first" and a partial unwind takes off the wrong piece.
        for ticket in sorted((str(t) for t in tickets),
                             key=_ticket_order, reverse=True):
            position = by_ticket.get(ticket)
            if position is None:
                continue                    # already gone, not an error
            want = float(position['volume'])
            if remaining is not None:
                want = min(want, remaining)
                if want <= 0:
                    break
            result = runner.close_ticket(symbol, int(ticket), want,
                                         entry_side.value, comment=comment)
            if result.get('ok'):
                # WHAT FILLED, not what was asked. A market close can
                # come back short, and booking the request as the fill
                # takes lots off our record that are still at the
                # broker.
                got = result.get('filled_volume')
                got = want if got is None else float(got)
                closed.append({'ticket': ticket, 'volume': got,
                               'price': result.get('price'),
                               'sent': result.get('requested_price')})
                if remaining is not None:
                    remaining -= got
            else:
                errors.append(f"{ticket}: {result.get('error')}")
        done = sum(float(c['volume'] or 0.0) for c in closed)
        if errors:
            return {'ok': False, 'closed': closed, 'found': found,
                    'left': max(0.0, found - done),
                    'error': '; '.join(errors)}
        return {'ok': True, 'closed': closed,
                #: What the broker HELD of our tickets when we looked.
                #: `left` alone cannot tell "we closed all of it" from
                #: "there was none of it to close" - both are zero - and
                #: those two have to be distinguishable by the caller,
                #: because one of them means a close happened and the
                #: other means nothing did.
                'found': found,
                #: Of OUR tickets, what is still open at the broker
                #: after this. Zero means the leg is done, however
                #: little was closed to get there.
                'left': max(0.0, found - done)}

    # -- exit -------------------------------------------------------------

    def close_position(self, pair, position, md=None, reason='manual',
                       disarm=True, quantity=None):
        """Close one spread position, both legs, by ticket.

        A guard NEVER prevents a close (spec §8): a trade must always be
        closable, so nothing in here consults the staleness or jump
        state.

        `quantity` in SPREADS closes PART of the position — what an
        opposite click reaches, when the click is smaller than the
        ticket it is covering. None means all of it. The volumes go to
        the broker pro rata on each leg, so a part-close stays hedged;
        what actually came off is measured from the tickets afterwards
        by `_closed_fraction`, never assumed from what was asked.

        `disarm=False` is for the one caller that OWNS the closing
        orders — the quoter, firing its own resting close. Disarming
        there would mark a close that WORKED as 'cancelled', and would
        delete the level a close that FAILED still has to be watched at.
        """
        share = 1.0
        if quantity is not None and position.quantity:
            share = max(0.0, min(1.0, float(quantity) /
                                 float(position.quantity)))
        part = None
        if share < 1.0 - 1e-9:
            part, refusal = self._part_close_volumes(pair, position, share)
            if part is None:
                # NOT a guard, and not a close being withheld: a full
                # close never comes through here, and CLOSE ALL, the
                # kill and the overnight rule all send `quantity=None`.
                # This is the broker's own volume step saying the PIECE
                # asked for cannot be traded on both legs — and closing
                # one leg of a hedge is worse than closing neither.
                logging.warning('%s: part-close refused: %s',
                                pair.key, refusal)
                return {'ok': False, 'reason': refusal, 'refused': True,
                        'legs': {}, 'position': position.to_dict()}
        if disarm and self.before_close is not None:
            # Disarm any resting close BEFORE closing. Afterwards is
            # too late: the level it was armed against no longer has a
            # position, and it would fire on the next tick that reaches
            # it.
            try:
                self.before_close(position.position_id, f'position closed '
                                                        f'({reason})')
            except Exception as e:                # never block a close
                logging.error('disarm before close failed: %s', e)
        leg_a_side, leg_b_side = position.side.leg_sides()
        results = {}
        for leg, entry_side, fill in (('a', leg_a_side, position.leg_a),
                                      ('b', leg_b_side, position.leg_b)):
            runner = self._leg(pair, leg)
            symbol = self._symbol(pair, leg)
            if runner is None:
                results[leg] = {'ok': False,
                                'error': f'no leg runner for {symbol}'}
                continue
            # Pro rata on BOTH legs, so a part-close comes off hedged.
            # None on a full close, which lets `close_tickets` take the
            # volumes from the broker's own book rather than from what
            # we think we sent.
            leg_volume = None if part is None else part.get(leg)
            results[leg] = self.close_tickets(
                runner, symbol, (fill.position_tickets if fill else []),
                entry_side, volume=leg_volume, comment='CLOSE')

        return self._settle_close(pair, position, results, md, reason)

    def can_part_close(self, pair, position, quantity):
        """(True, None) when this PIECE can be traded on both legs.

        Asked BEFORE a reducing click rests, so the refusal lands on the
        click that caused it instead of on a level hours later.

        A part-close is quantised to what both brokers can trade, and
        the piece can simply be too small: 0.01 lots of a future whose
        step is 0.10 is not an order. Once a resting close is armed at
        that size there is no good outcome left - firing it closes
        nothing while reporting success, and the trader is watching a
        price that can never get them out. The only honest moment to
        say so is the click.

        A full close is always allowed through: it never needs a share,
        and a close is never withheld.
        """
        held = float(position.quantity or 0.0)
        try:
            wanted = float(quantity)
        except (TypeError, ValueError):
            return True, None
        if held <= 0 or wanted >= held - 1e-9:
            return True, None
        share = max(0.0, min(1.0, wanted / held))
        volumes, refusal = self._part_close_volumes(pair, position, share)
        return (volumes is not None), refusal

    def _part_close_volumes(self, pair, position, share):
        """Both legs' volumes for a PART close, or (None, why not).

        A share of a position is not a volume either broker can trade.
        Leg lots come in steps — 0.01 on a spot CFD, often 0.10 on a
        future — and the pro-rata piece was going to the broker raw:
        0.0125 lots of a future is rejected outright (10014), which is
        how a part-close closes ONE leg and leaves the pair imbalanced
        at the broker with our own book still showing it whole.

        So the share is quantised to something BOTH legs can trade. Each
        leg rounds DOWN to its own step, the smaller of the two shares
        wins, and both legs are then re-derived from that one share so
        the piece that comes off is still hedged. Rounding down and not
        up because closing more than the click asked for moves the net
        the other way, which is the mistake at the far end.

        None when no piece works — the smallest tradable step on one leg
        is bigger than the whole piece asked for. The caller refuses the
        CLICK then, which is safe: nothing opens on top of a close that
        did not happen.
        """
        legs = {'a': (position.leg_a, pair.meta_a or {}),
                'b': (position.leg_b, pair.meta_b or {})}
        achievable = share
        for leg, (fill, meta) in legs.items():
            booked = float((fill.volume if fill else 0.0) or 0.0)
            if booked <= 0:
                continue
            volume = sizing.round_step(booked * share, meta.get('volume_step'),
                                       meta.get('volume_min'), down=True)
            if volume <= 0:
                return None, self._too_small(pair, leg, meta, booked, share)
            achievable = min(achievable, volume / booked)

        volumes = {}
        for leg, (fill, meta) in legs.items():
            booked = float((fill.volume if fill else 0.0) or 0.0)
            if booked <= 0:
                volumes[leg] = None
                continue
            volume = sizing.round_step(booked * achievable,
                                       meta.get('volume_step'),
                                       meta.get('volume_min'), down=True)
            if volume <= 0:
                # The other leg pulled the share down far enough that
                # this one no longer reaches its own minimum.
                return None, self._too_small(pair, leg, meta, booked,
                                             achievable)
            volumes[leg] = volume
        return volumes, None

    def _too_small(self, pair, leg, meta, booked, share):
        """Why this piece cannot be closed, with the size that can be.

        The trader's next move is to click a bigger size, and guessing
        it from "invalid volume" is not a thing to do with a position
        on.
        """
        step = float(meta.get('volume_step') or 0.0)
        minimum = float(meta.get('volume_min') or 0.0)
        floor = max(step, minimum)
        return (f'closing {share:.1%} of this position is '
                f'{booked * share:g} lots on leg {leg.upper()} '
                f'({self._symbol(pair, leg)}), under the {floor:g} lots '
                f'that broker can trade. Close more of it, or all of it.')

    def leg_open_volume(self, pair, leg, fill):
        """How much of one leg's tickets is STILL OPEN at the broker.

        None means the leg could not be READ, which is NOT zero (spec
        §7). A caller that reads "unknown" as "flat" acts on a position
        that is still on — and the money is at the broker either way.
        """
        runner = self._leg(pair, leg)
        if runner is None:
            return None
        live = runner.positions(self._symbol(pair, leg))
        if live is None:
            return None
        by_ticket = {str(p['ticket']): p for p in live}
        total = 0.0
        for ticket in ((fill.position_tickets if fill else []) or []):
            found = by_ticket.get(str(ticket))
            if found is not None:
                total += float(found.get('volume') or 0.0)
        return total

    def _closed_fraction(self, position, results):
        """How much of the position the broker ACTUALLY closed.

        Measured from what is LEFT of our tickets at the broker, not
        from what was asked for and not from what came back as filled:
        a ticket the broker no longer lists is already gone, and that
        leg is done however little was closed to get there.

        The hedged part is the SMALLER of the two legs. A close that
        took more off one leg than the other has not closed a spread —
        it has left an imbalance — and booking the larger of them would
        take lots off our record that are still at the broker.
        """
        achieved = []
        for leg, fill in (('a', position.leg_a), ('b', position.leg_b)):
            booked = float((fill.volume if fill else 0.0) or 0.0)
            if booked <= 0:
                continue
            result = results.get(leg) or {}
            if 'left' not in result:
                continue          # nothing measured: no opinion
            left = float(result.get('left') or 0.0)
            achieved.append(max(0.0, min(1.0, (booked - left) / booked)))
        return 1.0 if not achieved else min(achieved)

    def _settle_close(self, pair, position, results, md=None,
                      reason='manual'):
        """Book a close that has already been sent, however it was sent.

        One place, so a manual close, a flatten, the overnight rule and
        an AutoRouting take-profit all mark the position the same way —
        and all leave it ACTIVE when the close did not go through.

        `fraction` under 1.0 is a PART of the position closing. It stays
        OPEN for the rest: marking it closed would take the leg still at
        the broker off our own books.
        """
        ok = all(r.get('ok') for r in results.values())
        if ok:
            fraction = self._closed_fraction(position, results)
            if fraction < 1.0 - 1e-9:
                return self._settle_partial_close(pair, position, results, md,
                                                  reason, fraction)
            position.closed_at = self.clock()
            position.close_reason = reason
            # The price the exit was DECIDED at: the touch this position
            # would have closed at when the close was sent.
            decision_spread = executable_spread(md, position.side,
                                                closing=True) if md else None
            # ...and the price it actually got, from the closing fills.
            # Anchored on the EXECUTED fills for the same reason the
            # entry is: the touch is what we aimed at, not what we got.
            # Where the broker reported no close price there is nothing
            # to anchor on, and the touch is the honest second best —
            # the SLIPPAGE, which is the difference between the two,
            # then stays None rather than becoming a flattering zero.
            filled_spread = closed_spread(results, pair.hedge_ratio)
            # Each leg's close: the touch it was sent at, and the fill.
            for leg, fill in (('a', position.leg_a), ('b', position.leg_b)):
                if fill is not None:
                    fill.exit_sent_price = _weighted(results, leg, 'sent')
                    fill.exit_price = _weighted(results, leg, 'price')
            exit_spread = (decision_spread if filled_spread is None
                           else filled_spread)
            position.exit_spread = exit_spread
            position.exit_slippage = slippage(decision_spread, filled_spread,
                                              position.side, closing=True)
            gross = position.mark(exit_spread)
            fees = costs.mark_fees(
                position.leg_a.volume if position.leg_a else 0.0,
                position.leg_b.volume if position.leg_b else 0.0,
                self.config.settings)
            earned = None if gross is None else gross - fees
            # ACCUMULATED. A position closed in pieces earned its P&L in
            # pieces, and the earlier pieces are already booked — this
            # is the last of them, not the whole trade.
            position.realized_pnl = (
                earned if position.realized_pnl is None
                else (position.realized_pnl if earned is None
                      else position.realized_pnl + earned))
        else:
            # A close that did not go through leaves the position OPEN
            # and ACTIVE. Marking it ERROR/CLOSING would remove it from
            # every active lookup — the ladder, the monitor and the
            # reconciler's known-ticket set — while the money sits at
            # the broker (spec §7).
            logging.error("%s: close failed, position stays ACTIVE: %s",
                          pair.key, results)
        return {'ok': ok, 'legs': results,
                'position': position.to_dict()}

    def _settle_partial_close(self, pair, position, results, md, reason,
                              fraction):
        """Book PART of a position closing. It stays OPEN for the rest.

        The exit price and slippage are deliberately NOT set here: they
        describe the exit of a position, and this one has not exited.
        Recording the first piece's price as "the" exit would make the
        number wrong for the trade the moment the rest goes.
        """
        filled_spread = closed_spread(results, pair.hedge_ratio)
        gross = position.mark(filled_spread)
        # Fees on the volume that actually came off, which is what
        # `reduce_by` is about to take out of the leg volumes.
        fees = costs.mark_fees(
            (position.leg_a.volume if position.leg_a else 0.0) * fraction,
            (position.leg_b.volume if position.leg_b else 0.0) * fraction,
            self.config.settings)
        realized = (None if gross is None else gross * fraction - fees)
        if fraction <= 1e-9:
            # The quoting leg came down and the other leg could not
            # follow it — its step is bigger than the piece that closed.
            # NOTHING is booked: the record keeps both legs whole, which
            # errs towards believing more is on than there is, and the
            # reconciler is told the truth loudly.
            logging.critical(
                '%s: position %s closed on the quoting leg only — the other '
                'leg could not match a piece that small (step). The legs are '
                'IMBALANCED at the broker; check it by hand.',
                pair.key, position.position_id)
            return {'ok': True, 'partial': True, 'fraction': 0.0,
                    'imbalanced': True, 'remaining': position.quantity,
                    'legs': results, 'position': position.to_dict()}
        left = position.reduce_by(fraction, realized=realized)
        logging.warning(
            '%s: position %s closed %.1f%% (%s) and is STILL ON for %s '
            'spreads — both legs reduced together, so it is not naked.',
            pair.key, position.position_id, fraction * 100.0, reason, left)
        return {'ok': True, 'partial': True, 'fraction': fraction,
                'remaining': left, 'legs': results,
                'position': position.to_dict()}

    # -- bookkeeping ------------------------------------------------------

    def _book_position(self, pair, side, spreads, plan, fills, sides,
                       decision_spread, elapsed_ms):
        fill_a, fill_b = fills['a'], fills['b']
        leg_a = LegFill(pair.account_a, pair.symbol_a, sides['a'],
                        fill_a.get('filled_volume') or 0.0,
                        fill_a.get('price'),
                        order_ticket=fill_a.get('ticket'),
                        position_tickets=fill_a.get('position_tickets'),
                        contract_size=plan['leg_a_contract'], clock=self.clock)
        leg_b = LegFill(pair.account_b, pair.symbol_b, sides['b'],
                        fill_b.get('filled_volume') or 0.0,
                        fill_b.get('price'),
                        order_ticket=fill_b.get('ticket'),
                        position_tickets=fill_b.get('position_tickets'),
                        contract_size=plan['leg_b_contract'], clock=self.clock)
        # Each leg's own touch when it was sent - which leg slipped.
        leg_a.sent_price = fill_a.get('requested_price')
        leg_b.sent_price = fill_b.get('requested_price')

        # Anchored on the EXECUTED fills, never on the mid the decision
        # was taken at. Four rows agreeing to the cent proves they share
        # an anchor, not that the anchor is right (spec §11).
        entry_spread = None
        if leg_a.price is not None and leg_b.price is not None:
            entry_spread = leg_b.price - float(pair.hedge_ratio) * leg_a.price

        position = SpreadPosition(
            pair.key, side, spreads, leg_a, leg_b, entry_spread,
            OrderType.MARKET,
            sizing.spread_units(leg_b.volume, plan['leg_b_contract']),
            clock=self.clock)
        position.click_to_on_ms = elapsed_ms
        position.entry_slippage = slippage(decision_spread, entry_spread, side)
        return position


def _ticket_order(ticket):
    """Sort key that puts ticket 10000 after ticket 9999."""
    text = str(ticket)
    try:
        return (1, int(text), '')
    except (TypeError, ValueError):
        return (0, 0, text)


def _weighted(results, leg, field):
    """The volume-weighted `field` of one leg's closing fills, or None
    when any of them did not report it."""
    fills = (results.get(leg) or {}).get('closed') or ()
    volume = sum(float(f.get('volume') or 0.0) for f in fills)
    if not fills or volume <= 0 or any(f.get(field) is None for f in fills):
        return None
    return sum(float(f[field]) * float(f.get('volume') or 0.0)
               for f in fills) / volume


def leg_slip(side, sent, filled):
    """How far ONE leg filled from the touch it was sent at, in its own
    price points. Positive is a cost: a buy that paid more, a sell that
    got less. None when either price is unknown."""
    if sent is None or filled is None:
        return None
    side = getattr(side, 'value', side)
    return (float(filled) - float(sent)) if side == 'BUY' \
        else (float(sent) - float(filled))


def closed_spread(results, hedge_ratio):
    """`B - beta x A` of the prices the CLOSE actually filled at.

    Volume-weighted, because a ticket can be closed in pieces. None when
    either leg reported no price: half a spread is not a spread, and a
    number built from one leg would be reported as an exit price nobody
    traded.
    """
    prices = {}
    for leg in ('a', 'b'):
        fills = [fill for fill in (results.get(leg) or {}).get('closed') or ()
                 if fill.get('price') is not None]
        volume = sum(float(fill.get('volume') or 0.0) for fill in fills)
        if not fills or volume <= 0:
            return None
        prices[leg] = sum(float(fill['price']) * float(fill['volume'] or 0.0)
                          for fill in fills) / volume
    return prices['b'] - float(hedge_ratio) * prices['a']


def slippage(expected, filled, side, closing=False):
    """Positive is a COST, always — including on exits.

    A short buys the spread back to close, so the sign flips between
    entry and exit; getting that backwards reports every exit's cost as
    a gain. Unmeasured returns None, which the UI renders as "—" (spec
    §5, §11).
    """
    if expected is None or filled is None:
        return None
    side = SpreadSide(getattr(side, 'value', side))
    buying = (side is SpreadSide.BUY) != bool(closing)
    return (filled - expected) if buying else (expected - filled)


def mark_position(position, md, settings):
    """(gross, net, closing_spread) for an open position, marked at the
    touches it would actually CLOSE at.

    Consequence, and it is correct: a position shows a loss the instant
    it opens, equal to one round turn of both legs' bid-ask. That is
    what closing immediately would cost, and the UI says so rather than
    hiding it.

    Only COMMISSION is subtracted. The crossing is already in the two
    prices; subtracting the round trip again is the bid-ask twice.
    """
    closing_spread = executable_spread(md, position.side, closing=True)
    gross = position.mark(closing_spread)
    if gross is None:
        return None, None, closing_spread
    fees = costs.mark_fees(
        position.leg_a.volume if position.leg_a else 0.0,
        position.leg_b.volume if position.leg_b else 0.0, settings)
    return gross, gross - fees, closing_spread


def leg_marks(position, md):
    """The two prices this position would be closed at, per leg.

    Agrees with `mark_position` by construction: `B - beta x A` of these
    two IS the closing executable spread, and a test pins that.
    """
    return closing_prices(md, position.side)
