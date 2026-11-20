"""The W3 maker fill: both legs rest at the touch, and are pegged there.

This is how the Stat_Arb_W3 program filled a spread on OKX, kept as it
was tested there. The trader asked for exactly that: "keep it the way it
was in Stat_W3".

1. **Both legs rest at once** as plain limits priced passive: a BUY at
   bid + `MAKER_OFFSET_BPS`, a SELL at ask - `MAKER_OFFSET_BPS`, and
   never closer than `MAKER_SAFETY_BPS` to the other side of the book.
   Plain limits rather than post-only on purpose: W3 started on
   post-only and OKX cancelled them whenever the touch moved under the
   order in a tight BTC book (sCode 51124), leaving one leg on. Priced
   passive, a plain limit makes anyway.
2. **Re-pegged every `MAKER_REPEG_SEC` (1 s)** when a leg's target has
   moved more than `MAKER_AMEND_BPS` (1 bp). W3 tried 5 bp: on BTC
   that is about $32, and the orders sat unfilled for a minute while the
   market walked away. Re-pegged by AMEND, keeping the order and its
   ticket, with the fill read first, so an order that has just filled
   is never re-priced.
3. **One leg gone and the other still resting** (cancelled outside, or
   refused), so the resting one is cancelled at once. That is W3's
   orphan guard.
4. **`MAKER_TIMEOUT_SEC` (30 s) without both legs filled** cancels
   whatever still rests. Nothing filled means nothing happened, and the
   click ends in plain words.
5. **One leg on and the other short is maker recovery.** The missing
   piece rests at its own touch and is nudged `MAKER_RECOVERY_STEP_BPS`
   (2 bp) toward the market every tenth of `MAKER_RECOVERY_SEC` (60 s).
   That pays maker fees instead of taker fees plus slippage.
6. **Recovery out of time ends at MARKET.** The leg that is on is closed
   by ticket (Mirror's unwind, never an opposite order). Enough matched
   on both legs (`MIN_MATCHED_FRACTION`) is booked as it stands.

None of this blocks. The desk's poll loop calls `step()` once per pass
for each pair, so every other ladder keeps pricing while a fill works,
which an inline 30 s wait would stop.
"""

import logging
import time

from .models import OrderSide, OrderType, SpreadSide, new_id
from .spread import executable_spread

#: The comment stem on every order this module rests. It starts LADDER,
#: so the reconciler reads a leg left behind as OURS and never closes
#: it as a stray (see reconcile.is_ours).
TAG = 'LADDER-MK'


def fill_style(pair, config):
    """This ladder's fill: its own setting, else the desk's FILL_STYLE.
    'maker', 'quote' or 'taker'."""
    style = (getattr(pair, 'fill_style', None)
             or (config.get('FILL_STYLE') if config is not None else None)
             or 'maker')
    style = str(style).lower()
    return style if style in ('maker', 'quote', 'taker') else 'maker'


def _bps(a, b):
    return abs(a - b) / b * 1e4 if b else 0.0


def passive_price(side, bid, ask, offset_bps, safety_bps):
    """W3's maker price: at the touch, improved by the offset, and never
    within `safety_bps` of the other side. Can sit a little behind the
    touch in a one-tick book, which is the point: it makes."""
    if side == 'BUY':
        return min(bid * (1 + offset_bps / 1e4), ask * (1 - safety_bps / 1e4))
    return max(ask * (1 - offset_bps / 1e4), bid * (1 + safety_bps / 1e4))


def recovery_price(side, bid, ask, nudge_bps):
    """A recovering leg: its own touch, `nudge_bps` toward the market."""
    if side == 'BUY':
        return bid * (1 + nudge_bps / 1e4)
    return ask * (1 - nudge_bps / 1e4)


class MakerLeg:
    """One leg of a maker fill: what rests, and what has filled."""

    def __init__(self, leg, account, symbol, side, planned, contract):
        self.leg = leg                       # 'a' | 'b'
        self.account = account
        self.symbol = symbol
        self.side = side                     # 'BUY' | 'SELL'
        self.planned = float(planned)        # lots
        self.contract = contract
        self.live = None                     # resting ticket, or None
        self.price = None                    # where it rests
        self.target = None                   # where it should rest
        self.sent_price = None               # the touch when first sent
        self.orders = {}                     # ticket -> filled lots
        self.prices = {}                     # ticket -> avg fill price
        self.position_tickets = []
        self.amends = 0
        self.replaced = 0                    # post-only cancels re-placed
        self.cancel_reason = None            # OKX's own words, last cancel
        self.error = None

    @property
    def filled(self):
        return sum(self.orders.values())

    @property
    def avg_price(self):
        num = sum(v * (self.prices.get(t) or 0.0)
                  for t, v in self.orders.items() if v)
        return num / self.filled if self.filled else None

    def absorb(self, ticket, state):
        """Fold one order's fill state in. Returns True while it rests."""
        state = state or {}
        volume = float(state.get('filled_volume') or 0.0)
        if volume > 0:
            self.orders[ticket] = volume
            self.prices[ticket] = state.get('price')
            for t in state.get('position_tickets') or ():
                if t not in self.position_tickets:
                    self.position_tickets.append(t)
        return bool(state.get('still_open'))

    def as_fill(self):
        """The leg in the shape the executor books from."""
        return {'ok': self.filled > 0, 'filled_volume': self.filled,
                'price': self.avg_price, 'requested_price': self.sent_price,
                'ticket': next(iter(self.orders), None),
                'position_tickets': list(self.position_tickets),
                'symbol': self.symbol, 'side': self.side,
                'contract_size': self.contract, 'error': self.error}

    def to_dict(self):
        return {'leg': self.leg.upper(), 'symbol': self.symbol,
                'side': self.side, 'planned': self.planned,
                'filled': self.filled, 'resting_at': self.price,
                'amends': self.amends, 'replaced': self.replaced,
                'cancel_reason': self.cancel_reason}


class MakerJob:
    """One click being filled as maker on both legs."""

    def __init__(self, pair, side, spreads, plan, decision_spread, level,
                 source, now):
        self.id = new_id('MK')
        self.pair_key = pair.key
        self.side = side
        self.spreads = spreads
        self.plan = plan
        self.decision_spread = decision_spread
        self.level = level                   # the clicked/triggered level
        self.source = source                 # 'manual' | 'trigger' | 'algo'
        self.started = now
        self.phase = 'resting'               # -> 'recovering' -> 'done'
        self.deadline = None
        self.next_peg = now
        self.recovery = None                 # {leg, want, nudge, next_step}
        self.legs = {}
        self.reason = None
        #: Nothing filled before the timeout on a click at a level: the
        #: click goes back to working at that level (coordinator).
        self.rearm = False
        #: The level guard is holding a leg further out than the touch.
        self.held = False
        #: When the first leg first filled: the other then has
        #: MAKER_LEG_GAP_SEC to fill as maker before it crosses.
        self.first_fill_at = None

    def to_dict(self, now):
        return {'id': self.id, 'pair': self.pair_key,
                'side': self.side.value, 'spreads': self.spreads,
                'phase': self.phase, 'level': self.level,
                'source': self.source,
                'age_sec': round(now - self.started, 1),
                'legs': [l.to_dict() for l in self.legs.values()],
                'held_at_level': self.held,
                'reason': self.reason}


class MakerFills:
    """Every maker fill in flight, worked one step per poll."""

    def __init__(self, config, legs, executor, clock=time.time):
        self.config = config
        self.legs = legs
        self.executor = executor
        self.clock = clock
        self.jobs = {}                       # pair key -> MakerJob
        #: Called with (job, ExecutionResult) when a job ends either way.
        self.on_done = None
        self.history = []

    # -- settings ------------------------------------------------------------

    def _cfg(self, name, default):
        try:
            return float(self.config.get(name, default))
        except (TypeError, ValueError):
            return default

    def enabled(self, pair):
        """Maker unless the desk or this pair says otherwise. The W3
        fill is the OKX default; `FILL_STYLE` = 'taker' restores Mirror's
        both-legs-at-market crossing."""
        return fill_style(pair, self.config) == 'maker'

    def busy(self, key):
        return key in self.jobs

    def in_flight_tickets(self):
        """(account, ticket) for everything a working fill holds, so the
        reconciler does not read a leg that is half way on as a stray."""
        out = set()
        for job in self.jobs.values():
            for leg in job.legs.values():
                for t in list(leg.orders) + list(leg.position_tickets) + (
                        [leg.live] if leg.live is not None else []):
                    out.add((leg.account, str(t)))
        return out

    def snapshot(self, key=None):
        now = self.clock()
        return [j.to_dict(now) for k, j in self.jobs.items()
                if key is None or k == key]

    # -- starting ------------------------------------------------------------

    def start(self, pair, side, md, spreads, level=None, source='manual'):
        """Rest both legs. Returns an ExecutionResult: refused, or ok with
        `working` set, and the position arrives through `on_done`."""
        from .executor import ExecutionResult
        side = SpreadSide(getattr(side, 'value', side))
        spreads = float(spreads if spreads is not None
                        else pair.default_quantity)
        if self.busy(pair.key):
            return ExecutionResult(
                False, refused=True,
                reason='a maker fill is already working on this pair — '
                       'wait for it, or cancel it')
        refusal = self.executor.precheck(pair, side, md, spreads, level)
        if refusal is not None:
            return ExecutionResult(False, reason=refusal, refused=True)
        plan = self.executor.size(pair, md, spreads)
        now = self.clock()
        job = MakerJob(pair, side, spreads, plan,
                       executable_spread(md, side), level, source, now)
        # An ALGO entry acts on a signal that is good NOW: its legs rest
        # as maker for ALGO_MAKER_TIMEOUT_SEC (5 s), not the 30 a click
        # at a level gets. 31 s from signal to fill was live, and bad.
        job.deadline = now + (self._cfg('ALGO_MAKER_TIMEOUT_SEC', 5.0)
                              if source == 'algo'
                              else self._cfg('MAKER_TIMEOUT_SEC', 30.0))
        sides = dict(zip(('a', 'b'), side.leg_sides()))
        for leg in ('a', 'b'):
            job.legs[leg] = MakerLeg(
                leg, pair.account_a if leg == 'a' else pair.account_b,
                pair.symbol_a if leg == 'a' else pair.symbol_b,
                sides[leg].value, plan[f'leg_{leg}_lots'],
                plan[f'leg_{leg}_contract'])

        # BOTH LEGS AT ONCE, as the original desk settled on (June 2026):
        # placed one after the other, the 200-400 ms between them was
        # enough for the second leg's book to move and OKX to cancel it
        # as a post-only that would have crossed (cancelSource 31).
        targets = self._held_targets(pair, job, md, ('a', 'b'))
        placed = self._rest_both(pair, job, md, targets)
        refused = [k for k in ('a', 'b') if not placed[k]]
        if refused:
            reason = '; '.join(f'leg {k.upper()} refused: '
                               f'{job.legs[k].error}' for k in refused)
            for k in ('a', 'b'):
                if placed[k]:
                    self._cancel(pair, job.legs[k])
            if any(job.legs[k].filled > 0 for k in ('a', 'b')):
                # A leg filled before it could be pulled: that is a leg
                # on alone - its other half crosses at market now.
                self.jobs[pair.key] = job
                job.reason = reason
                job.first_fill_at = now
                self._cross_the_lag(pair, job, md, now)
                result = ExecutionResult(True, reason=reason)
                result.working = job.id
                return result
            return ExecutionResult(False, refused=True, reason=reason)
        self.jobs[pair.key] = job
        logging.info('%s: maker fill %s %s %g - both legs resting (A %s @ %s, '
                     'B %s @ %s)', pair.key, job.id, side.value, spreads,
                     job.legs['a'].side, job.legs['a'].price,
                     job.legs['b'].side, job.legs['b'].price)
        result = ExecutionResult(True, reason=(
            f'both legs resting at the touch as maker — up to '
            f'{self._cfg("MAKER_TIMEOUT_SEC", 30.0):g}s'))
        result.working = job.id
        return result

    def _legs(self, job):
        return {k: l.as_fill() for k, l in job.legs.items()}

    # -- the broker side -----------------------------------------------------

    def _runner(self, ml):
        return self.legs.get(ml.account)

    def _touch(self, md, ml):
        return (md.get(f'leg_{ml.leg}_bid'), md.get(f'leg_{ml.leg}_ask'))

    def _maker_target(self, ml, md):
        """Where this leg's maker order belongs on its own book now."""
        bid, ask = self._touch(md or {}, ml)
        if not bid or not ask:
            return None
        return passive_price(ml.side, bid, ask,
                             self._cfg('MAKER_OFFSET_BPS', 1.0),
                             self._cfg('MAKER_SAFETY_BPS', 1.0))

    def _held_targets(self, pair, job, md, keys):
        """Each leg's maker price - and, for a click at a level, never
        a pair of prices that would put the spread on worse than that
        level. A SELL at -3 whose legs would now go on at -4.28 is held
        where -3 is: the leg that is still to fill is priced further
        out on its own book (more passive, so still post-only maker),
        and if the market does not come back to it, nothing fills and
        the click goes back to working."""
        targets = {k: self._maker_target(job.legs[k], md) for k in keys}
        if job.level is None or not self._hold_level():
            return targets
        beta = float(pair.hedge_ratio or 1.0)
        if not beta:
            return targets
        a, b = job.legs['a'], job.legs['b']

        def price_of(key, leg):
            if self._done_filling(leg) or key not in targets:
                return leg.avg_price if leg.filled > 0 else leg.price
            return targets[key]
        pa, pb = price_of('a', a), price_of('b', b)
        if pa is None or pb is None:
            return targets
        spread = pb - beta * pa
        level = float(job.level)
        worse = (spread < level - 1e-12 if job.side is SpreadSide.SELL
                 else spread > level + 1e-12)
        if not worse:
            return targets
        gap = level - spread       # SELL: >0 (raise); BUY: <0 (lower)
        if 'b' in targets and not self._done_filling(b):
            # B is sold on a SELL (higher is more passive) and bought on
            # a BUY (lower is more passive): either way, + gap.
            targets['b'] = pb + gap
        elif 'a' in targets and not self._done_filling(a):
            # A is bought on a SELL (lower is more passive) and sold on
            # a BUY (higher): either way, - gap / beta.
            targets['a'] = pa - gap / beta
        job.held = True
        return targets

    def _hold_level(self):
        value = self.config.get('MAKER_HOLD_LEVEL', True)
        if isinstance(value, str):
            return value.strip().lower() not in ('0', 'false', 'off', 'no')
        return bool(value)

    def _rest_both(self, pair, job, md, targets):
        """Place both legs' maker orders at the same instant."""
        from concurrent.futures import ThreadPoolExecutor
        keys = [k for k in ('a', 'b') if targets.get(k) is not None]
        out = {'a': False, 'b': False}
        for k in ('a', 'b'):
            if targets.get(k) is None:
                job.legs[k].error = f'no price on {job.legs[k].symbol}'
        if not keys:
            return out
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {k: pool.submit(self._rest, pair, job, job.legs[k], md,
                                      True, None, 0.0, targets[k])
                       for k in keys}
            for k, future in futures.items():
                try:
                    out[k] = future.result()
                except Exception as e:
                    job.legs[k].error = f'the order raised: {e}'
        return out

    def _rest(self, pair, job, ml, md, offset=True, volume=None,
              nudge_bps=0.0, price=None):
        runner = self._runner(ml)
        if runner is None:
            ml.error = f'no connection for {ml.symbol}'
            return False
        bid, ask = self._touch(md, ml)
        if not bid or not ask:
            ml.error = f'no price on {ml.symbol}'
            return False
        if price is None:
            if offset:
                price = passive_price(ml.side, bid, ask,
                                      self._cfg('MAKER_OFFSET_BPS', 1.0),
                                      self._cfg('MAKER_SAFETY_BPS', 1.0))
            else:
                price = recovery_price(ml.side, bid, ask, nudge_bps)
        if ml.sent_price is None:
            ml.sent_price = ask if ml.side == 'BUY' else bid
        lots = ml.planned if volume is None else volume
        try:
            # POST-ONLY for every maker order (the original desk's June
            # 2026 rule: a plain limit at the touch races the book and
            # is charged TAKER - which is what the first live ETH leg
            # here was). The one exception is RECOVERY of a leg that is
            # on alone: that order has to fill, so it is a plain limit.
            placed = runner.place_limit(ml.symbol, ml.side, lots, price,
                                        comment=f'{TAG}:{ml.leg.upper()}',
                                        post_only=bool(offset))
        except Exception as e:
            ml.error = f'the order raised: {e}'
            return False
        if not placed or not placed.get('ok'):
            ml.error = (placed or {}).get('error') or 'refused'
            return False
        ml.live = placed['ticket']
        ml.price = placed.get('price', price)
        ml.target = price
        ml.orders.setdefault(ml.live, 0.0)
        return True

    def _read(self, ml):
        """Fold the resting order's fills in; False once it rests no
        more (filled, or cancelled)."""
        if ml.live is None:
            return False
        runner = self._runner(ml)
        try:
            state = runner.order_state(ml.live)
        except Exception as e:
            logging.warning('%s: could not read order %s: %s', ml.symbol,
                            ml.live, e)
            return True                       # unknown: assume it rests
        if state.get('error') and state.get('still_open') is None:
            return True
        resting = ml.absorb(ml.live, state)
        if not resting:
            if state.get('cancel_reason'):
                ml.cancel_reason = state.get('cancel_reason')
            ml.live = None
        return resting

    def _cancel(self, pair, ml):
        if ml.live is None:
            return
        runner = self._runner(ml)
        try:
            state = runner.cancel_order(ml.live)
        except Exception as e:
            logging.error('%s: cancel of %s raised: %s', ml.symbol, ml.live, e)
            return
        ml.absorb(ml.live, state)
        if state.get('cancelled') or not state.get('still_open'):
            ml.live = None

    def _amend(self, ml, price):
        runner = self._runner(ml)
        answer = runner.modify_order(ml.live, price)
        if answer and answer.get('ok'):
            ml.price = price
            ml.target = price
            ml.amends += 1
            return True
        # Refused: read it - it may have filled or gone under us.
        self._read(ml)
        return False

    # -- every poll ----------------------------------------------------------

    def step(self, pair, md):
        job = self.jobs.get(pair.key)
        if job is None:
            return None
        now = self.clock()
        try:
            if job.phase == 'resting':
                self._step_resting(pair, job, md, now)
            if job.phase == 'recovering':
                self._step_recovering(pair, job, md, now)
        except Exception as e:                  # never lose the pass
            logging.exception('%s: maker fill step raised: %s', pair.key, e)
        if job.phase == 'done':
            self.jobs.pop(pair.key, None)
        return job.to_dict(now)

    def _done_filling(self, ml):
        step = max(1e-12, ml.planned * 1e-6)
        return ml.filled >= ml.planned - step

    def _step_resting(self, pair, job, md, now):
        a, b = job.legs['a'], job.legs['b']
        if now < job.next_peg and now < job.deadline:
            return
        job.next_peg = now + self._cfg('MAKER_REPEG_SEC', 1.0)
        rest_a, rest_b = self._read(a), self._read(b)
        done_a, done_b = self._done_filling(a), self._done_filling(b)
        if done_a and done_b:
            self._finish(pair, job)
            return
        # ONE LEG FILLED, THE OTHER NOT: it gets MAKER_LEG_GAP_SEC (5 s)
        # to fill as maker, then crosses at market - a leg is not left on
        # alone for half a minute and a minute of recovery behind it.
        if job.first_fill_at is None and (a.filled > 0 or b.filled > 0):
            job.first_fill_at = now
        if job.first_fill_at is not None and \
                now - job.first_fill_at >= self._cfg('MAKER_LEG_GAP_SEC',
                                                     5.0):
            frac = {k: (l.filled / l.planned if l.planned else 0.0)
                    for k, l in job.legs.items()}
            if abs(frac['a'] - frac['b']) > 1e-6:
                self._cross_the_lag(pair, job, md, now)
                return
        # A leg's POST-ONLY order cancelled by OKX (it would have
        # crossed: cancelSource 20 / 31) is not a failure - the original
        # desk re-priced and re-sent it, and so does this, until the
        # timeout. Only an order that cannot be placed again is "gone".
        if now < job.deadline and md and not md.get('guard_reason'):
            for key, ml, resting, done in (('a', a, rest_a, done_a),
                                           ('b', b, rest_b, done_b)):
                if resting or done or ml.live is not None:
                    continue
                target = self._held_targets(pair, job, md, (key,)).get(key)
                left = ml.planned - ml.filled
                if target is None or left <= ml.planned * 1e-6:
                    continue
                if self._rest(pair, job, ml, md, True, left, 0.0, target):
                    ml.replaced += 1
                    logging.info('%s: maker fill %s - leg %s re-placed at %s'
                                 ' (%s)', pair.key, job.id, key.upper(),
                                 ml.price, ml.cancel_reason or
                                 'OKX cancelled the post-only order')
                    if key == 'a':
                        rest_a = True
                    else:
                        rest_b = True
        # One leg's order gone without filling while the other rests:
        # cancel the other at once (W3's orphan guard).
        gone_a = not rest_a and not done_a
        gone_b = not rest_b and not done_b
        if gone_a or gone_b or now >= job.deadline:
            why = ('timed out after '
                   f'{self._cfg("MAKER_TIMEOUT_SEC", 30.0):g}s'
                   if now >= job.deadline else
                   f'leg {"A" if gone_a else "B"} order was cancelled at OKX')
            self._cancel(pair, a)
            self._cancel(pair, b)
            if a.filled <= 0 and b.filled <= 0:
                job.reason = f'not filled: {why} — nothing went on'
                # A click at a level that simply was not filled goes back
                # to working at that level.
                job.rearm = (now >= job.deadline and job.level is not None
                             and job.source == 'trigger'
                             and not getattr(job, 'cancelled', False))
                self._fail(pair, job, refused=True)
                return
            if self._done_filling(a) and self._done_filling(b):
                self._finish(pair, job)
                return
            job.reason = why
            # A leg is on: cross what the other is missing now, rather
            # than a minute of maker recovery.
            self._cross_the_lag(pair, job, md, now)
            return
        if not md or md.get('guard_reason'):
            return                              # hold the peg on a bad print
        keys = tuple(k for k, ml, resting in (('a', a, rest_a),
                                              ('b', b, rest_b))
                     if resting and ml.live is not None)
        targets = self._held_targets(pair, job, md, keys)
        for key in keys:
            ml, target = job.legs[key], targets.get(key)
            if target is None:
                continue
            # Against the price actually RESTING (the original desk's
            # drift-aware amend), not last second's target.
            if _bps(target, ml.target or target) > \
                    self._cfg('MAKER_AMEND_BPS', 1.0):
                self._amend(ml, target)

    def _cross_the_lag(self, pair, job, md, now):
        """One leg is ahead of the other. Stop both resting orders, then
        send what the lagging leg is missing AT MARKET (OKX's IOC, capped
        at its price limit), so the two match. Taker on that one piece
        only. If the market order cannot go, fall back to the maker
        recovery (and its unwind) - a leg is never left on alone."""
        from . import sizing
        a, b = job.legs['a'], job.legs['b']
        self._cancel(pair, a)
        self._cancel(pair, b)
        frac = {k: (l.filled / l.planned if l.planned else 0.0)
                for k, l in job.legs.items()}
        if self._done_filling(a) and self._done_filling(b):
            self._finish(pair, job)
            return
        lead = 'a' if frac['a'] >= frac['b'] else 'b'
        lag = 'b' if lead == 'a' else 'a'
        ml = job.legs[lag]
        meta = (pair.meta_a if lag == 'a' else pair.meta_b) or {}
        missing = ml.planned * frac[lead] - ml.filled
        volume = sizing.round_step(missing, meta.get('volume_step'),
                                   meta.get('volume_min'))
        if volume <= 0:
            self._finish(pair, job)
            return
        waited = now - (job.first_fill_at or job.started)
        runner = self._runner(ml)
        result = None
        try:
            result = runner.order(ml.symbol, ml.side, volume,
                                  comment=f'{TAG}:{lag.upper()}X')
        except Exception as e:
            ml.error = f'the market order raised: {e}'
        got = float((result or {}).get('filled_volume') or 0.0)
        if got > 0 and (result or {}).get('ticket'):
            ml.absorb(result['ticket'], {
                'filled_volume': got, 'price': result.get('price'),
                'position_tickets': result.get('position_tickets') or [],
                'still_open': False})
        logging.warning('%s: maker fill %s - leg %s filled, leg %s did not '
                        'in %.1fs: crossed %g lots of leg %s at market (%s)',
                        pair.key, job.id, lead.upper(), lag.upper(), waited,
                        volume, lag.upper(),
                        'ok' if got > 0 else
                        (result or {}).get('error') or ml.error or 'refused')
        job.reason = ((job.reason + '; ') if job.reason else '') + (
            f'leg {lag.upper()} crossed at market after '
            f'{waited:.1f}s alone')
        if got >= volume - max(1e-12, volume * 1e-6):
            self._finish(pair, job)
            return
        # The market order did not (fully) go: the old path, maker
        # recovery and then the unwind, so nothing stays on alone.
        self._begin_recovery(pair, job, md, now)

    def _begin_recovery(self, pair, job, md, now):
        """One leg is ahead. Rest the missing piece of the other as maker,
        sized to MATCH the leg that is ahead."""
        a, b = job.legs['a'], job.legs['b']
        frac = {k: (l.filled / l.planned if l.planned else 0.0)
                for k, l in job.legs.items()}
        lead = 'a' if frac['a'] >= frac['b'] else 'b'
        lag = 'b' if lead == 'a' else 'a'
        lag_leg = job.legs[lag]
        want = lag_leg.planned * frac[lead] - lag_leg.filled
        job.phase = 'recovering'
        total = self._cfg('MAKER_RECOVERY_SEC', 60.0)
        job.recovery = {'leg': lag, 'want': want, 'nudge': 0.0,
                        'until': now + total,
                        'step_every': total / 10.0,
                        'next_step': now + total / 10.0}
        logging.warning('%s: maker fill %s - leg %s ahead (%.0f%% vs %.0f%%); '
                        'recovering %g lots of leg %s as maker',
                        pair.key, job.id, lead.upper(), frac[lead] * 100,
                        frac[lag] * 100, want, lag.upper())
        if want <= lag_leg.planned * 1e-6:
            self._finish(pair, job)
            return
        if not self._rest(pair, job, lag_leg, md or {}, offset=False,
                          volume=want, nudge_bps=0.0):
            logging.error('%s: recovery order refused: %s', pair.key,
                          lag_leg.error)
            self._end_recovery(pair, job)

    def _step_recovering(self, pair, job, md, now):
        rec = job.recovery
        ml = job.legs[rec['leg']]
        if ml.live is not None:
            self._read(ml)
        got = ml.filled
        lead = job.legs['b' if rec['leg'] == 'a' else 'a']
        target = ml.planned * (lead.filled / lead.planned
                               if lead.planned else 0.0)
        if got >= target - max(1e-12, ml.planned * 1e-6):
            self._cancel(pair, ml)
            self._finish(pair, job)
            return
        if now >= rec['until']:
            self._cancel(pair, ml)
            self._end_recovery(pair, job)
            return
        if now >= rec['next_step'] and md and not md.get('guard_reason'):
            rec['nudge'] = min(rec['nudge'] + self._cfg(
                'MAKER_RECOVERY_STEP_BPS', 2.0), 10 * self._cfg(
                'MAKER_RECOVERY_STEP_BPS', 2.0))
            rec['next_step'] = now + rec['step_every']
            bid, ask = self._touch(md, ml)
            if bid and ask:
                price = recovery_price(ml.side, bid, ask, rec['nudge'])
                if ml.live is not None:
                    self._amend(ml, price)
                else:
                    self._rest(pair, job, ml, md, offset=False,
                               volume=target - ml.filled,
                               nudge_bps=rec['nudge'])

    def _end_recovery(self, pair, job):
        """Recovery ran out. Book what matched if enough did; otherwise
        take the leg that is on back off at market, by ticket."""
        fills = self._legs(job)
        matched = self.executor._matched_fraction(job.plan, fills)
        if matched >= self._cfg('MIN_MATCHED_FRACTION', 0.4):
            job.reason = (f'{job.reason}; recovery out of time — booked the '
                          f'{matched:.0%} that matched on both legs')
            self._finish(pair, job)
            return
        job.reason = (f'{job.reason}; maker recovery out of time — the leg '
                      f'that went on is closed at market')
        logging.error('%s: maker fill %s - %s', pair.key, job.id, job.reason)
        sides = {k: OrderSide(l.side) for k, l in job.legs.items()}
        naked = self.executor._unwind_what_went_on(pair, sides, fills,
                                                   ('a', 'b'))
        self._fail(pair, job, naked=naked)

    # -- endings -------------------------------------------------------------

    def _finish(self, pair, job):
        from .executor import ExecutionResult
        job.phase = 'done'
        now = self.clock()
        elapsed_ms = (now - job.started) * 1000.0
        fills = self._legs(job)
        sides = {k: OrderSide(l.side) for k, l in job.legs.items()}
        position = self.executor._book_position(
            pair, job.side, job.spreads, job.plan, fills, sides,
            job.decision_spread, elapsed_ms)
        position.order_type = OrderType.LIMIT
        self.executor.timings.append(elapsed_ms)
        logging.info('%s: maker fill %s ON in %.1fs at %s', pair.key, job.id,
                     elapsed_ms / 1000.0, position.entry_spread)
        self._done(job, ExecutionResult(True, position=position,
                                        elapsed_ms=elapsed_ms, legs=fills,
                                        reason=job.reason))

    def _fail(self, pair, job, refused=False, naked=None):
        from .executor import ExecutionResult
        job.phase = 'done'
        elapsed_ms = (self.clock() - job.started) * 1000.0
        self._done(job, ExecutionResult(False, reason=job.reason,
                                        refused=refused, naked=naked,
                                        elapsed_ms=elapsed_ms,
                                        legs=self._legs(job)))

    def _done(self, job, result):
        self.history.append({'id': job.id, 'pair': job.pair_key,
                             'ok': result.ok, 'reason': result.reason,
                             'elapsed_ms': result.elapsed_ms})
        del self.history[:-50]
        if self.on_done is not None:
            try:
                self.on_done(job, result)
            except Exception as e:
                logging.exception('maker fill %s: on_done raised: %s',
                                  job.id, e)

    def cancel(self, key, reason='cancelled by the trader'):
        """Pull a working fill: cancel what rests; a leg already on goes
        through the same recovery/unwind rules as a timeout."""
        job = self.jobs.get(key)
        if job is None:
            return {'ok': False, 'reason': 'no maker fill working'}
        now = self.clock()
        job.deadline = now
        job.cancelled = True              # the trader's: never re-armed
        job.next_peg = now
        if job.recovery is not None:
            job.recovery['until'] = now
        job.reason = reason
        return {'ok': True}


# ===========================================================================
# Maker CLOSES
# ===========================================================================


class CloseJob:
    """One position being closed as maker: a closing limit per ticket on
    each leg, resting at the touch."""

    def __init__(self, pair, position, md, reason, now, level=None):
        self.id = new_id('MC')
        #: The closing spread this close is held to (an Algo take-profit):
        #: its legs are never re-priced to a close worse than it.
        self.level = level
        self.held = False
        self.first_fill_at = None
        self.pair_key = pair.key
        self.position = position
        self.md = md                       # the touch the close was decided on
        self.reason = reason
        self.started = now
        self.deadline = None
        self.next_peg = now
        self.legs = {}                     # 'a'|'b' -> dict
        self.why = None

    def to_dict(self, now):
        legs = []
        for key, leg in self.legs.items():
            filled = sum(o['filled'] for o in leg['orders'].values())
            legs.append({'leg': key.upper(), 'symbol': leg['symbol'],
                         'side': leg['close_side'], 'planned': leg['found'],
                         'filled': filled,
                         'resting_at': next((o['price'] for o in
                                             leg['orders'].values()
                                             if o['live']), None),
                         'amends': sum(o['amends'] for o in
                                       leg['orders'].values())})
        return {'id': self.id, 'pair': self.pair_key, 'kind': 'close',
                'position_id': self.position.position_id,
                'side': self.position.side.value, 'phase': 'closing',
                'spreads': self.position.quantity, 'level': self.level,
                'held_at_level': self.held,
                'age_sec': round(now - self.started, 1), 'legs': legs,
                'reason': self.why}


class MakerCloses:
    """Closes worked as maker, W3's way: both legs' closing orders rest
    at the touch, by TICKET, re-pegged every second on a >1 bp move.
    Whatever has not filled at `MAKER_TIMEOUT_SEC` is closed at market
    by ticket through Mirror's own close - a close is owed, an entry is
    not, so a close never just gives up.

    Only for closes that can wait half a minute: the trader's Close on a
    position, and an Algo exit on profit or the mean. CLOSE ALL, Flatten,
    the kill, every stop and the overnight rule cross at once.
    """

    def __init__(self, config, legs, executor, clock=time.time):
        self.config = config
        self.legs = legs
        self.executor = executor
        self.clock = clock
        self.jobs = {}                     # position_id -> CloseJob
        self.on_done = None                # (job, settle result)

    _cfg = MakerFills._cfg

    def closing_ids(self):
        return set(self.jobs)

    def snapshot(self, key=None):
        now = self.clock()
        return [j.to_dict(now) for j in self.jobs.values()
                if key is None or j.pair_key == key]

    def _runner(self, leg):
        return self.legs.get(leg['account'])

    def _close_targets(self, pair, job, md, keys):
        """Each closing leg's maker price - and, for a close held to a
        level (an Algo take-profit), never a pair of prices that closes
        the spread worse than it: closing a BUY (selling B, buying A)
        at or above the level, a SELL (buying B, selling A) at or below.
        The leg still to fill is priced further out on its own book."""
        targets = {}
        for key in keys:
            leg = job.legs.get(key) or {}
            bid, ask = md.get(f'leg_{key}_bid'), md.get(f'leg_{key}_ask')
            close_side = leg.get('close_side') or (
                dict(zip(('a', 'b'),
                         job.position.side.leg_sides()))[key].opposite.value)
            if bid and ask:
                targets[key] = passive_price(
                    close_side, bid, ask, self._cfg('MAKER_OFFSET_BPS', 1.0),
                    self._cfg('MAKER_SAFETY_BPS', 1.0))
        if job.level is None or not MakerFills._hold_level(self):
            return targets
        beta = float(pair.hedge_ratio or 1.0)
        if not beta:
            return targets

        def closed_avg(key):
            leg = job.legs.get(key) or {}
            orders = list((leg.get('orders') or {}).values())
            got = sum(o['filled'] for o in orders)
            if got <= 0 or self._left(leg) > max(1e-9, leg['found'] * 1e-6):
                return None
            return sum(o['filled'] * (o['avg'] or o['price'])
                       for o in orders if o['filled'] > 0) / got
        pa = closed_avg('a') or targets.get('a')
        pb = closed_avg('b') or targets.get('b')
        if pa is None or pb is None:
            return targets
        spread = pb - beta * pa
        level = float(job.level)
        buy = job.position.side is SpreadSide.BUY
        worse = spread < level - 1e-12 if buy else spread > level + 1e-12
        if not worse:
            return targets
        gap = level - spread
        if 'b' in targets and closed_avg('b') is None:
            targets['b'] = pb + gap
        elif 'a' in targets and closed_avg('a') is None:
            targets['a'] = pa - gap / beta
        job.held = True
        return targets

    def start(self, pair, position, md, reason, level=None):
        """Rest the closing orders. Returns {'ok', 'working'} or, when
        anything about it cannot rest, the result of closing at market
        right away. `level`: hold the close to this closing spread (an
        Algo take-profit) - see `_close_targets`."""
        pid = position.position_id
        if pid in self.jobs:
            return {'ok': True, 'working': self.jobs[pid].id,
                    'reason': 'already closing as maker'}
        if not md or md.get('guard_reason'):
            return self._market(pair, position, md, reason,
                                'no clean price to rest on')
        now = self.clock()
        job = CloseJob(pair, position, md, reason, now, level=level)
        if getattr(position, 'exit_sent_at', None) is None:
            position.exit_sent_at = now
        targets = self._close_targets(pair, job, md, ('a', 'b'))
        job.deadline = now + self._cfg('MAKER_TIMEOUT_SEC', 30.0)
        sides = dict(zip(('a', 'b'), position.side.leg_sides()))
        for key, fill in (('a', position.leg_a), ('b', position.leg_b)):
            if not fill:
                continue
            leg = {'leg': key, 'account': fill.account, 'symbol': fill.symbol,
                   'entry_side': sides[key],
                   'close_side': sides[key].opposite.value,
                   'tickets': list(fill.position_tickets or []),
                   'found': 0.0, 'orders': {}, 'sent': None}
            runner = self._runner(leg)
            live = runner.positions(leg['symbol']) if runner else None
            if live is None:
                self._pull(job)
                return self._market(pair, position, md, reason,
                                    f'{leg["symbol"]} could not be read')
            held = {str(p['ticket']): float(p['volume']) for p in live}
            bid, ask = md.get(f'leg_{key}_bid'), md.get(f'leg_{key}_ask')
            leg['sent'] = ask if leg['close_side'] == 'BUY' else bid
            price = targets.get(key) or passive_price(
                leg['close_side'], bid, ask,
                self._cfg('MAKER_OFFSET_BPS', 1.0),
                self._cfg('MAKER_SAFETY_BPS', 1.0))
            for ticket in leg['tickets']:
                volume = held.get(str(ticket))
                if not volume:
                    continue
                leg['found'] += volume
                placed = runner.place_limit(
                    leg['symbol'], leg['close_side'], volume, price,
                    comment=f'{TAG}C:{key.upper()}', post_only=True,
                    position_ticket=int(ticket))
                if not placed or not placed.get('ok'):
                    self._pull(job)
                    return self._market(
                        pair, position, md, reason,
                        f'the closing order was refused: '
                        f'{(placed or {}).get("error")}')
                leg['orders'][placed['ticket']] = {
                    'pos_ticket': int(ticket), 'volume': volume,
                    'filled': 0.0, 'avg': None, 'live': True,
                    'price': placed.get('price', price), 'target': price,
                    'amends': 0}
            job.legs[key] = leg
        self.jobs[pid] = job
        logging.info('%s: maker close %s of %s - closing orders resting at '
                     'the touch', pair.key, job.id, pid)
        return {'ok': True, 'working': job.id,
                'reason': f'closing as maker — both legs resting at the '
                          f'touch, at market after '
                          f'{self._cfg("MAKER_TIMEOUT_SEC", 30.0):g}s'}

    def _market(self, pair, position, md, reason, why):
        logging.info('%s: close of %s at market: %s', pair.key,
                     position.position_id, why)
        return self.executor.close_position(pair, position, md,
                                            reason=reason)

    def _read(self, leg, order_ticket, order):
        runner = self._runner(leg)
        try:
            state = runner.order_state(order_ticket)
        except Exception as e:
            logging.warning('%s: close order %s unreadable: %s',
                            leg['symbol'], order_ticket, e)
            return
        if state.get('error') and state.get('still_open') is None:
            return
        order['filled'] = float(state.get('filled_volume') or 0.0)
        order['avg'] = state.get('price') or order['avg']
        order['live'] = bool(state.get('still_open'))

    def _pull(self, job):
        """Cancel every closing order still resting; read what filled."""
        for leg in job.legs.values():
            runner = self._runner(leg)
            for ticket, order in leg['orders'].items():
                if not order['live']:
                    continue
                try:
                    state = runner.cancel_order(ticket)
                except Exception as e:
                    logging.error('%s: cancel of close order %s raised: %s',
                                  leg['symbol'], ticket, e)
                    continue
                order['filled'] = float(state.get('filled_volume') or
                                        order['filled'])
                order['avg'] = state.get('price') or order['avg']
                order['live'] = bool(state.get('still_open')) and \
                    not state.get('cancelled')

    def abandon(self, position_id, why='closed another way'):
        """Something else is closing this position now (CLOSE ALL, the
        kill, a stop): pull the resting closes first, so the two never
        both close the same ticket."""
        job = self.jobs.pop(position_id, None)
        if job is not None:
            job.why = why
            self._pull(job)

    def step(self, pair, md):
        for pid, job in list(self.jobs.items()):
            if job.pair_key != pair.key:
                continue
            try:
                self._step(pair, job, md)
            except Exception as e:                 # never lose the pass
                logging.exception('%s: maker close step raised: %s',
                                  pair.key, e)

    def _left(self, leg):
        return leg['found'] - sum(o['filled'] for o in leg['orders'].values())

    def _step(self, pair, job, md):
        now = self.clock()
        if now < job.next_peg and now < job.deadline:
            return
        job.next_peg = now + self._cfg('MAKER_REPEG_SEC', 1.0)
        for leg in job.legs.values():
            for ticket, order in leg['orders'].items():
                if order['live']:
                    self._read(leg, ticket, order)
        eps = 1e-9
        if all(self._left(leg) <= max(eps, leg['found'] * 1e-6)
               for leg in job.legs.values()):
            self._settle(pair, job, market={})
            return
        filled_any = any(sum(o['filled'] for o in leg['orders'].values())
                         > 0 for leg in job.legs.values())
        if filled_any and job.first_fill_at is None:
            job.first_fill_at = now
        fracs = [1.0 - self._left(leg) / leg['found'] if leg['found'] else 1.0
                 for leg in job.legs.values()]
        split = len(fracs) == 2 and abs(fracs[0] - fracs[1]) > 1e-6
        gap_over = (job.first_fill_at is not None and split and
                    now - job.first_fill_at >= self._cfg('MAKER_LEG_GAP_SEC',
                                                         5.0))
        if now >= job.deadline and job.level is not None and not filled_any:
            # A close HELD to its level that the market did not come back
            # to: nothing closed, so nothing is closed at market either.
            # The position stays on, its stop still stands, and the Algo
            # closes again the next time the spread reaches the target.
            self._pull(job)
            self.jobs.pop(job.position.position_id, None)
            note = (f'take-profit close held at {job.level:.4f}: not filled '
                    f'as maker in {self._cfg("MAKER_TIMEOUT_SEC", 30.0):g}s '
                    f'— position stays open, the target is worked again')
            logging.info('%s: maker close %s of %s - %s', pair.key, job.id,
                         job.position.position_id, note)
            if self.on_done is not None:
                try:
                    self.on_done(job, {'ok': False, 'held': True,
                                       'reason': note})
                except Exception as e:
                    logging.exception('maker close %s: on_done raised: %s',
                                      job.id, e)
            return
        if now >= job.deadline or gap_over:
            self._pull(job)
            market = {}
            for key, leg in job.legs.items():
                if self._left(leg) <= max(eps, leg['found'] * 1e-6):
                    continue
                market[key] = self.executor.close_tickets(
                    self._runner(leg), leg['symbol'], leg['tickets'],
                    leg['entry_side'], comment='CLOSE')
            self._settle(pair, job, market,
                         note=(f'one leg closed, the other not in '
                               f'{self._cfg("MAKER_LEG_GAP_SEC", 5.0):g}s '
                               f'— the rest closed at market')
                         if gap_over and now < job.deadline else
                         f'not all filled as maker in '
                         f'{self._cfg("MAKER_TIMEOUT_SEC", 30.0):g}s '
                         f'— the rest closed at market')
            return
        if not md or md.get('guard_reason'):
            return
        held_targets = self._close_targets(pair, job, md, tuple(job.legs))
        for key, leg in job.legs.items():
            target = held_targets.get(key)
            if target is None:
                continue
            # A post-only closing order OKX cancelled (it would have
            # crossed) is placed again for what it still had to close,
            # against the same position ticket.
            for ticket, order in list(leg['orders'].items()):
                left = order['volume'] - order['filled']
                if order['live'] or order.get('replaced') or \
                        left <= max(1e-12, order['volume'] * 1e-6):
                    continue
                placed = self._runner(leg).place_limit(
                    leg['symbol'], leg['close_side'], left, target,
                    comment=f'{TAG}C:{key.upper()}', post_only=True,
                    position_ticket=int(order['pos_ticket']))
                if placed and placed.get('ok'):
                    order['replaced'] = True
                    leg['orders'][placed['ticket']] = {
                        'pos_ticket': order['pos_ticket'], 'volume': left,
                        'filled': 0.0, 'avg': None, 'live': True,
                        'price': placed.get('price', target),
                        'target': target, 'amends': 0}
                    logging.info('%s: maker close %s - leg %s re-placed at '
                                 '%s (OKX cancelled the post-only order)',
                                 pair.key, job.id, key.upper(), target)
            for ticket, order in leg['orders'].items():
                if not order['live'] or _bps(target, order['target']) <= \
                        self._cfg('MAKER_AMEND_BPS', 1.0):
                    continue
                answer = self._runner(leg).modify_order(ticket, target)
                if answer and answer.get('ok'):
                    order['price'] = order['target'] = target
                    order['amends'] += 1
                else:
                    self._read(leg, ticket, order)

    def _settle(self, pair, job, market, note=None):
        """Book the close through Mirror's own settlement, maker fills
        and any market remainder together."""
        self.jobs.pop(job.position.position_id, None)
        results = {}
        for key, leg in job.legs.items():
            closed = [{'ticket': str(o['pos_ticket']), 'volume': o['filled'],
                       'price': o['avg'], 'sent': leg['sent']}
                      for o in leg['orders'].values() if o['filled'] > 0]
            extra = market.get(key)
            if extra is None:
                results[key] = {'ok': True, 'closed': closed,
                                'found': leg['found'],
                                'left': max(0.0, self._left(leg))}
                continue
            results[key] = dict(extra)
            results[key]['closed'] = closed + list(extra.get('closed') or [])
            results[key]['found'] = leg['found']
        answer = self.executor._settle_close(pair, job.position, results,
                                             job.md, job.reason)
        if note:
            answer['note'] = note
        logging.info('%s: maker close %s of %s settled (%s)%s', pair.key,
                     job.id, job.position.position_id,
                     'ok' if answer.get('ok') else 'FAILED',
                     f' - {note}' if note else '')
        if self.on_done is not None:
            try:
                self.on_done(job, answer)
            except Exception as e:
                logging.exception('maker close %s: on_done raised: %s',
                                  job.id, e)
