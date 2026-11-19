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
                'amends': self.amends}


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

    def to_dict(self, now):
        return {'id': self.id, 'pair': self.pair_key,
                'side': self.side.value, 'spreads': self.spreads,
                'phase': self.phase, 'level': self.level,
                'source': self.source,
                'age_sec': round(now - self.started, 1),
                'legs': [l.to_dict() for l in self.legs.values()],
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
        style = (getattr(pair, 'fill_style', None)
                 or self.config.get('FILL_STYLE') or 'maker')
        return str(style).lower() == 'maker'

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
        job.deadline = now + self._cfg('MAKER_TIMEOUT_SEC', 30.0)
        sides = dict(zip(('a', 'b'), side.leg_sides()))
        for leg in ('a', 'b'):
            job.legs[leg] = MakerLeg(
                leg, pair.account_a if leg == 'a' else pair.account_b,
                pair.symbol_a if leg == 'a' else pair.symbol_b,
                sides[leg].value, plan[f'leg_{leg}_lots'],
                plan[f'leg_{leg}_contract'])

        # Leg A first, then B; B refused cancels A (W3's order).
        for leg in ('a', 'b'):
            ml = job.legs[leg]
            ok = self._rest(pair, job, ml, md, offset=True)
            if not ok:
                reason = f'leg {leg.upper()} refused: {ml.error}'
                if leg == 'b':
                    self._cancel(pair, job.legs['a'])
                    if job.legs['a'].filled > 0:
                        # A filled before it could be pulled: that is a
                        # leg on alone, and it goes through recovery.
                        self.jobs[pair.key] = job
                        job.reason = reason
                        self._begin_recovery(pair, job, md, now)
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

    def _rest(self, pair, job, ml, md, offset=True, volume=None,
              nudge_bps=0.0):
        runner = self._runner(ml)
        if runner is None:
            ml.error = f'no connection for {ml.symbol}'
            return False
        bid, ask = self._touch(md, ml)
        if not bid or not ask:
            ml.error = f'no price on {ml.symbol}'
            return False
        if offset:
            price = passive_price(ml.side, bid, ask,
                                  self._cfg('MAKER_OFFSET_BPS', 1.0),
                                  self._cfg('MAKER_SAFETY_BPS', 0.5))
        else:
            price = recovery_price(ml.side, bid, ask, nudge_bps)
        if ml.sent_price is None:
            ml.sent_price = ask if ml.side == 'BUY' else bid
        lots = ml.planned if volume is None else volume
        try:
            placed = runner.place_limit(ml.symbol, ml.side, lots, price,
                                        comment=f'{TAG}:{ml.leg.upper()}',
                                        post_only=False)
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
                self._fail(pair, job, refused=True)
                return
            if self._done_filling(a) and self._done_filling(b):
                self._finish(pair, job)
                return
            job.reason = why
            self._begin_recovery(pair, job, md, now)
            return
        if not md or md.get('guard_reason'):
            return                              # hold the peg on a bad print
        for ml, resting in ((a, rest_a), (b, rest_b)):
            if not resting or ml.live is None:
                continue
            bid, ask = self._touch(md, ml)
            if not bid or not ask:
                continue
            target = passive_price(ml.side, bid, ask,
                                   self._cfg('MAKER_OFFSET_BPS', 1.0),
                                   self._cfg('MAKER_SAFETY_BPS', 0.5))
            if _bps(target, ml.target or target) > \
                    self._cfg('MAKER_AMEND_BPS', 1.0):
                self._amend(ml, target)

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
        job.next_peg = now
        if job.recovery is not None:
            job.recovery['until'] = now
        job.reason = reason
        return {'ok': True}
