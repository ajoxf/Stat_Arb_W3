"""Where the Algo runs: one switch per ladder, its candles, its sink.

`algo.AlgoSignal` decides; `bands.SpreadCandles` measures; this module
feeds them on every poll and hands what they decide to a SINK.

**Two sinks, one seam.** Every intent — "enter, selling the spread",
"exit POS-12, profit target" — goes to `RecordingSink`: written to the
audit trail, kept for the screen, logged. In LIVE mode it ALSO goes to
the live sink (`algoexec.LiveSink`), which sends it: MARKET both legs
to enter, a close by TICKET to exit, through the same executor a
manual click uses. Nothing in this module can reach an order itself;
`tests/test_algo.py` reads it as code to keep it that way.

Rules this module keeps:

- **The switch is per ladder, in memory, and OFF at every start.** It
  is never written to the config: an algo nobody turned on today is an
  algo nobody is watching.
- **Algo or Manual, never both.** `manual_order_refusal` is what the
  click path consults: while a ladder's Algo is LIVE, a NEW manual
  order on it is refused in words. In DRY_RUN it refuses nothing. A
  close is never refused here — CLOSE ALL, flatten and closing a
  position by hand do not come through it.
- **The day's limits** — trades, a losing run, the money lost — stop
  ENTRIES for the rest of the broker's day and say which. Exits carry
  on whatever is hit.
- **History first, then the live tape.** Turning a ladder on loads its
  saved candles and asks MT5 for its bars, so the band is there at
  once. If the terminals will not answer — or a broker's clock is not
  measured, so a bar cannot be placed in time — the candles are built
  from the live mid instead and the screen says "collecting 7/20".
  Every candle that closes is saved, so a restart picks up where it
  left off.
"""

import logging
import re
from collections import deque

from . import algo as algo_module
from . import algofilters
from . import bands

#: How often a ladder that is still short of candles asks MT5 again.
BACKFILL_RETRY_SEC = 60.0

#: Warm-up progress carries over a restart when the Algo is switched on
#: again within this long of the last live price it watched — an update
#: or a quick restart. Longer, and the feed it watched is not this one.
WARMUP_CARRY_SEC = 900.0

#: How often the warm-up progress is written down while it is counting.
WARMUP_SAVE_SEC = 10.0

#: A held-back signal is written to the journal when its reason CHANGES,
#: and again at most this often while it stays the same — enough to
#: answer "why did it not trade?" afterwards without a row per poll.
BLOCKED_JOURNAL_SEC = 300.0

#: The longest gap between two live prices that still counts toward the
#: warm-up. Longer than this is a feed that stopped, not one watched.
WARMUP_GAP_SEC = 10.0

#: How long after an exit that was SENT and did not close it is sent
#: again. Not every poll: a close the broker refuses three times a
#: second is a flood, not a retry.
EXIT_RETRY_SEC = 5.0


class RecordingSink:
    """Today's only sink: record the intent, show it, send nothing."""

    def __init__(self, store=None):
        self.store = store

    def handle(self, pair_key, intent, mode=algo_module.DRY_RUN):
        detail = dict(intent, mode=mode)
        z = intent.get('z')
        logging.info('[ALGO %s] %s %s %s z=%s spread=%s (%s, nothing sent)',
                     pair_key, intent.get('action'), intent.get('side'),
                     intent.get('reason') or '',
                     '—' if z is None else f'{z:+.2f}',
                     intent.get('spread'), mode)
        if self.store is not None:
            try:
                self.store.event('algo_signal', pair_key, **detail)
            except Exception as e:           # never let a record stop a poll
                logging.error('could not record an Algo signal: %s', e)
        return detail

    def outcome(self, pair_key, intent, answer):
        """What became of a LIVE intent, in the audit trail beside it."""
        logging.info('[ALGO %s] %s %s -> %s %s', pair_key,
                     intent.get('action'), intent.get('side'),
                     'done' if answer.get('ok') else 'FAILED',
                     answer.get('reason') or answer.get('position_id') or '')
        if self.store is not None:
            try:
                self.store.event('algo_order', pair_key,
                                 action=intent.get('action'),
                                 side=intent.get('side'),
                                 position_id=(answer.get('position_id')
                                              or intent.get('position_id')),
                                 ok=bool(answer.get('ok')),
                                 reason=answer.get('reason'))
            except Exception as e:
                logging.error('could not record an Algo order: %s', e)


def signal_alert(intent, positions, params, now, previous=None):
    """What a trader in SIGNALS mode is told when the Algo signals.

    An ENTER is the trade it would take: 'H to L — SELL 0.5 @ 13.805
    (z +1.52)'. An EXIT is about THEIR position: 'Profit target — close
    your SELL 0.5 at 12.727 (net +3.90)'. The id is unique - a restart
    never re-uses one - so each is said once, wherever it is shown.
    """
    side = intent.get('side')
    words = {'SELL': 'H to L', 'BUY': 'L to H'}.get(side, side)

    def num(value, digits=4, signed=False):
        if value is None:
            return '—'
        return f'{float(value):+.{digits}f}' if signed \
            else f'{float(value):.{digits}f}'
    if intent['action'] == 'ENTER':
        text = (f"{words} — {side} {params['algo_qty']:g} @ "
                f"{num(intent.get('spread'))} (z {num(intent.get('z'), 2, True)})")
        kind, reason = 'ENTRY', None
    else:
        held = next((p for p in positions or ()
                     if p.get('position_id') == intent.get('position_id')),
                    {})
        reason = intent.get('reason')
        said = algo_module.EXIT_WORDS.get(reason, reason or 'exit')
        net = intent.get('net_pnl')
        text = (f"{said[:1].upper()}{said[1:]} — close your {side} "
                f"{float(held.get('quantity') or 0):g} at "
                f"{num(intent.get('spread'))}"
                + (f" (net {float(net):+.2f})" if net is not None else ''))
        kind = 'EXIT'
    seq = int((previous or {}).get('seq') or 0) + 1
    return {'id': f'{now:.3f}-{seq}', 'seq': seq, 'kind': kind,
            'side': side, 'reason': reason, 'text': text, 'at': now,
            'position_id': intent.get('position_id'),
            'spread': intent.get('spread'), 'z': intent.get('z')}


class _Run:
    """One ladder's Algo while it is on."""

    def __init__(self, signature, params, anchor, started_at,
                 mode=algo_module.DRY_RUN):
        self.mode = mode
        #: Positions this Algo owns: opened by it, or ALGO-tagged and
        #: adopted when it was turned on. Watched so a close — by the
        #: Algo, by the trader, by the overnight rule — is counted.
        self.mine = set()
        #: position id -> when an exit for it last FAILED.
        self.retry = {}
        self.day = {'date': None, 'trades': 0, 'losses_row': 0, 'pnl': 0.0}
        self.signature = signature
        self.params = params
        self.candles = bands.SpreadCandles(params['timeframe_min'] * 60.0,
                                           params['length'], anchor)
        self.signal = algo_module.AlgoSignal(params)
        self.started_at = started_at
        self.history = {'state': 'pending', 'note': None, 'at': None,
                        'candles': 0}
        self.recent = deque(maxlen=20)
        self.body = None
        #: The last stretch that confirmed and was HELD BACK: side, z,
        #: when, and why — what "Last signal blocked" shows.
        self.last_blocked = None
        #: position id -> the z the Algo entered it at.
        self.entry_z = {}
        #: Seconds of LIVE prices watched since it was turned on, and
        #: when the last one counted came in — the warm-up.
        self.live_sec = 0.0
        self.live_at = None
        #: (side, reason without its numbers, when) last journalled.
        self.blocked_journal = None
        #: When the warm-up progress was last written down.
        self.warmup_saved_at = None
        #: SIGNALS mode: the last thing a trader should act on - an entry
        #: signal, or their position reaching an exit - with an id the
        #: desk and Telegram each say once. None until there is one.
        self.signal_alert = None


class AlgoDesk:
    """Every ladder's Algo switch, and the Algo on the ones that are on."""

    def __init__(self, legs, store=None, clock=None, offset_for=None,
                 sink=None, live=None, result_of=None):
        self.legs = legs
        self.store = store
        self.clock = clock
        #: account name -> the broker's measured offset from UTC, or None.
        self.offset_for = offset_for or (lambda name: None)
        self.sink = sink or RecordingSink(store)
        #: The LIVE sink (`algoexec.LiveSink`), or None — then LIVE is
        #: refused, because there is nothing to send with.
        self.live = live
        #: position id -> its realized P&L once closed, or None.
        self.result_of = result_of or (lambda position_id: None)
        self._runs = {}

    # -- the switch ---------------------------------------------------------

    def atr(self, key, period):
        """The spread's ATR on this ladder's CLOSED candles, in spread
        points, or None while the Algo is off or the candles are too
        few."""
        run = self._runs.get(key)
        if run is None:
            return None
        closes = [run.candles.closed[b] for b in sorted(run.candles.closed)]
        return algofilters.atr(closes, period)

    def is_on(self, key):
        return key in self._runs

    def mode(self, key):
        run = self._runs.get(key)
        return run.mode if run else None

    def candles(self, key):
        """The candles a running Algo judges on, or None when it is off.

        Read by the signal journal's shadow Algo, so that with the Algo
        on the two read ONE band and cannot disagree about z. Read only:
        the shadow never observes into them."""
        run = self._runs.get(key)
        return run.candles if run else None

    def turn_on(self, pair, mode=algo_module.DRY_RUN, confirmed=False,
                adopt=()):
        """Switch one ladder's Algo on, or change its mode. Refused, in
        words, if it cannot be.

        LIVE sends real orders, so it is never a default and never
        implied: it needs `confirmed`, every time. `adopt` is the ladder's
        ALGO-tagged positions, which this Algo manages from now on.
        """
        mode = str(mode or algo_module.DRY_RUN).upper()
        if mode not in algo_module.MODES:
            return {'ok': False, 'pair': pair.key,
                    'reason': f'{mode} is not a mode — DRY_RUN or LIVE'}
        if mode == algo_module.LIVE:
            if self.live is None:
                return {'ok': False, 'pair': pair.key,
                        'reason': 'LIVE is not available on this engine'}
            if not confirmed:
                return {'ok': False, 'pair': pair.key, 'confirm': True,
                        'reason': 'LIVE sends real orders to both accounts '
                                  '— it has to be confirmed'}
        run = self._runs.get(pair.key)
        if run is None:
            run = self._runs[pair.key] = self._new_run(pair)
            self._carry_warmup(pair, run)
        run.mode = mode
        run.mine.update(adopt or ())
        logging.info('[ALGO %s] ON (%s)%s', pair.key, mode,
                     f' — adopted {len(adopt)} position(s)' if adopt else '')
        return {'ok': True, 'pair': pair.key, 'on': True, 'mode': mode,
                'adopted': list(adopt or ())}

    def turn_off(self, key):
        was = self._runs.pop(key, None)
        if was is not None:
            logging.info('[ALGO %s] OFF', key)
        # Switched off by the trader: the warm-up starts again next time.
        # (A restart does not come through here, which is why it carries.)
        if self.store is not None:
            try:
                self.store.clear_warmup(key)
            except Exception as e:
                logging.error('could not clear the warm-up: %s', e)
        return {'ok': True, 'pair': key, 'on': False}

    def manual_order_refusal(self, key):
        """Why a NEW manual order on this ladder is refused, or None.

        "Algo or Manual, never both", enforced on the ORDER PATH — not
        only on the screen, where a keyboard shortcut or a second
        browser would walk round it. Only LIVE refuses: in DRY_RUN the
        Algo sends nothing and the trader keeps the ladder.
        """
        run = self._runs.get(key)
        if run is None or run.mode != algo_module.LIVE:
            return None
        return ('the Algo is trading this ladder (LIVE) — manual orders are '
                'off. CLOSE ALL and the positions list still close; turn the '
                'Algo off to trade by hand.')

    def owned(self, key):
        """The position ids this ladder's Algo owns now."""
        run = self._runs.get(key)
        return set(run.mine) if run else set()

    # -- every poll ---------------------------------------------------------

    def observe(self, pair, md, positions=(), gates=None):
        """Feed one ladder's Algo this poll's market. Returns its body,
        or None for a ladder whose Algo is off — which computes nothing."""
        run = self._runs.get(pair.key)
        if run is None:
            return None
        if run.signature != self._signature(pair):
            # A new timeframe, length or beta is a different series:
            # start it again from what is saved and what MT5 has.
            was = run
            run = self._runs[pair.key] = self._new_run(pair)
            # A new series, the SAME Algo: it is still LIVE or dry, still
            # owns its positions and still has today's count. Starting
            # those over dropped a LIVE ladder to a dry run, and forgot
            # the positions it had to close. The live tape it has
            # watched has still been watched: the warm-up carries too.
            for name in ('mode', 'mine', 'day', 'retry', 'entry_z',
                         'blocked_journal', 'signal_alert',
                         'recent', 'last_blocked', 'live_sec', 'live_at',
                         'signal'):
                setattr(run, name, getattr(was, name))
            run.signal.params = run.params
        params = algo_module.clean_params(getattr(pair, 'algo_params', None))
        if params != run.params:
            # A new threshold is the same series read differently: the
            # band stays, the rules change on this pass.
            run.params = params
            run.signal.params = params
        now = self.clock()
        gates = dict(gates or {})
        self._settle_day(run, gates.get('day'), positions)
        halt = self._halt(run, positions)
        if halt:
            gates['halt'] = halt
        for position_id, failed_at in list(run.retry.items()):
            if now - failed_at >= EXIT_RETRY_SEC:
                del run.retry[position_id]
                run.signal.exit_failed(position_id)
        live = (md and md.get('mid_spread') is not None
                and not md.get('jump_reason') and not gates.get('health'))
        if live:
            if run.live_at is not None:
                run.live_sec += max(0.0, min(now - run.live_at,
                                             WARMUP_GAP_SEC))
            run.live_at = now
            self._save_warmup(pair, run, now)
        else:
            run.live_at = None
        need = float(params['warmup_min']) * 60.0
        gates['warmup'] = {'sec': min(run.live_sec, need) if need else 0.0,
                           'need_sec': need,
                           'done': run.live_sec >= need}
        if md and md.get('mid_spread') is not None \
                and not md.get('jump_reason'):
            # A price the jump guard is holding back is not fed to the
            # band either: one bad print in a close moves sigma for N
            # candles.
            closed = run.candles.observe(now, md['mid_spread'])
            if closed is not None and self.store is not None:
                self._save(pair, run, [closed], 'live')
        self._backfill_if_due(pair, run, now)
        stats = run.candles.stats()
        filters, check = self._filters(run, md, stats, gates.get('cost'))
        # READY is the band AND the warm-up: a band loaded from history
        # in a second is not a feed that has been watched.
        filters['ready'] = bool(filters['ready'] and gates['warmup']['done'])
        filters['warmup'] = gates['warmup']
        gates['entry_check'] = check
        body = run.signal.evaluate(now, md, stats, positions, gates)
        body['filters'] = filters
        body['atr'] = self.atr(pair.key, params['atr_period'])
        body['atr_period'] = params['atr_period']
        if body.get('blocked_side'):
            run.last_blocked = {'side': body['blocked_side'],
                                'z': body.get('blocked_z'), 'at': now,
                                'reason': body.get('blocked')}
            self._journal_blocked(pair, run, now)
        for intent in body['intents']:
            recorded = self.sink.handle(pair.key, intent, run.mode)
            if run.mode == algo_module.LIVE:
                answer = self._send(pair, run, intent, now)
                self.sink.outcome(pair.key, intent, answer)
                recorded = dict(recorded, done=bool(answer.get('ok')),
                                result=answer.get('reason'))
            else:
                if intent['action'] == 'ENTER':
                    run.day['trades'] += 1     # a signals run counts one
                # SIGNALS: the Algo signals, the trader trades. Nothing is
                # sent; this is what they are told to do.
                run.signal_alert = signal_alert(intent, positions, params,
                                                now, run.signal_alert)
            run.recent.appendleft(dict(recorded, at=now))
        body['halt'] = halt
        run.body = body
        return body

    def _carry_warmup(self, pair, run):
        """Pick up the warm-up where a restart left it — when the Algo is
        back on within WARMUP_CARRY_SEC of the last live price it watched.
        The gap itself is not counted."""
        if self.store is None:
            return
        try:
            saved = self.store.warmup(pair.key)
        except Exception as e:
            logging.error('could not read the warm-up: %s', e)
            return
        if saved is None:
            # Said, not silent: a missing line read as a missing feature.
            logging.info('[ALGO %s] warm-up starts: nothing saved from '
                         'before', pair.key)
            return
        live_sec, at = saved
        gap = self.clock() - at
        if 0 <= gap <= WARMUP_CARRY_SEC:
            run.live_sec = float(live_sec)
            logging.info('[ALGO %s] warm-up carried over: %.0f min watched, '
                         'back on %.0fs after the last live price',
                         pair.key, live_sec / 60.0, gap)
        else:
            logging.info('[ALGO %s] warm-up starts again: %.0f min since the '
                         'last live price it watched', pair.key, gap / 60.0)

    def _save_warmup(self, pair, run, now):
        if self.store is None:
            return
        if run.warmup_saved_at is not None \
                and now - run.warmup_saved_at < WARMUP_SAVE_SEC:
            return
        run.warmup_saved_at = now
        try:
            self.store.save_warmup(pair.key, run.live_sec, now)
        except Exception as e:               # never let a record stop a poll
            logging.error('could not save the warm-up: %s', e)

    def _journal_blocked(self, pair, run, now):
        """A signal that WOULD have entered, held back: into the journal,
        so "why did it not trade?" has an answer after the fact. Keyed on
        the reason without its numbers — "warming up: 12 of 90" and "13
        of 90" are one reason, not two rows."""
        last = run.last_blocked
        kind = re.sub(r'[-+]?\d[\d.,:]*', '#', last['reason'] or '')
        seen = run.blocked_journal
        if seen and seen[0] == last['side'] and seen[1] == kind \
                and now - seen[2] < BLOCKED_JOURNAL_SEC:
            return
        run.blocked_journal = (last['side'], kind, now)
        logging.info('[ALGO %s] %s signal at z %s held back: %s', pair.key,
                     last['side'], last['z'], last['reason'])
        if self.store is not None:
            try:
                self.store.event('algo_blocked', pair.key, side=last['side'],
                                 z=last['z'], reason=last['reason'],
                                 mode=run.mode)
            except Exception as e:           # never let a record stop a poll
                logging.error('could not record a held-back signal: %s', e)

    def _filters(self, run, md, stats, cost_in):
        """The filters' readings for the panel, and the entry check —
        `judge_filters`, on this ladder's own candles."""
        return judge_filters(run.params, md, stats, run.candles.closes(),
                             cost_in)

    def _send(self, pair, run, intent, now):
        """Hand a LIVE intent to the live sink, and keep score."""
        try:
            if intent['action'] == 'ENTER':
                answer = self.live.enter(pair, intent['side'],
                                         run.params['algo_qty'])
            else:
                answer = self.live.leave(pair, intent['position_id'],
                                         intent.get('reason'))
        except Exception as e:                   # never stop the poll
            logging.exception('[ALGO %s] send failed: %s', pair.key, e)
            answer = {'ok': False, 'reason': f'the engine raised: {e}'}
        if intent['action'] == 'ENTER':
            if answer.get('ok'):
                run.day['trades'] += 1
                if answer.get('position_id'):
                    run.mine.add(answer['position_id'])
                    run.entry_z[answer['position_id']] = intent.get('z')
            else:
                run.signal.entry_failed(now)
        elif not answer.get('ok'):
            run.retry[intent['position_id']] = now
        return answer

    def _settle_day(self, run, day, positions):
        """A new broker day clears the counts; a position of ours that
        has gone is scored."""
        if day is not None and day != run.day['date']:
            run.day = {'date': day, 'trades': 0, 'losses_row': 0, 'pnl': 0.0}
        open_ids = {p['position_id'] for p in positions or ()}
        for gone in run.mine - open_ids:
            run.mine.discard(gone)
            run.retry.pop(gone, None)
            pnl = self.result_of(gone)
            if pnl is None:
                continue                      # unmeasured is not a loss
            run.day['pnl'] += pnl
            run.day['losses_row'] = run.day['losses_row'] + 1 if pnl < 0 \
                else 0

    def _halt(self, run, positions):
        """Which of the day's limits stops entries now, in words, or None."""
        p, day = run.params, run.day
        if p['max_trades_day'] and day['trades'] >= p['max_trades_day']:
            return (f"{day['trades']} Algo trades today — the day's limit "
                    f"is {p['max_trades_day']}")
        if p['max_losses_row'] and day['losses_row'] >= p['max_losses_row']:
            return (f"{day['losses_row']} losing Algo trades in a row — "
                    f"paused for the day")
        if p['daily_loss_limit']:
            open_pnl = 0.0
            for position in positions or ():
                if position['position_id'] in run.mine:
                    open_pnl += position.get('net_pnl') or 0.0
            total = day['pnl'] + open_pnl
            if total <= -abs(p['daily_loss_limit']):
                return (f"Algo P&L today {total:,.2f} — past the "
                        f"-{abs(p['daily_loss_limit']):,.2f} limit")
        return None

    def block(self, key):
        """What the screen shows for one ladder's Algo, or None if off."""
        run = self._runs.get(key)
        if run is None:
            return None
        body = dict(run.body or {'algo': algo_module.ALGO, 'state': 'STARTING',
                                 'params': dict(run.params)})
        body.pop('intents', None)
        for row in body.get('positions') or ():
            row['entry_z'] = run.entry_z.get(row.get('position_id'))
        body.update(on=True, mode=run.mode, day=dict(run.day),
                    owned=sorted(run.mine), history=dict(run.history),
                    last_blocked=(dict(run.last_blocked)
                                  if run.last_blocked else None),
                    recent=list(run.recent), started_at=run.started_at,
                    signal_alert=(dict(run.signal_alert)
                                  if run.signal_alert else None),
                    timeframe_min=run.params['timeframe_min'],
                    length=run.params['length'])
        return body

    # -- candles ------------------------------------------------------------

    @staticmethod
    def _signature(pair):
        """What makes a candle series: its size, its length, its beta."""
        params = algo_module.clean_params(getattr(pair, 'algo_params', None))
        return (params['timeframe_min'], params['length'],
                float(pair.hedge_ratio or 1.0))

    def _new_run(self, pair):
        params = algo_module.clean_params(getattr(pair, 'algo_params', None))
        anchor = bands.round_offset(self.offset_for(pair.account_a)) or 0.0
        run = _Run(self._signature(pair), params, anchor, self.clock())
        if self.store is not None:
            try:
                saved = self.store.candles(
                    pair.key, params['timeframe_min'] * 60.0,
                    pair.hedge_ratio or 1.0,
                    limit=params['length'] * bands.KEEP_MULTIPLE)
                run.candles.seed(saved)
            except Exception as e:
                logging.error('[ALGO %s] saved candles unreadable: %s',
                              pair.key, e)
        return run

    def _save(self, pair, run, rows, source):
        try:
            self.store.save_candles(pair.key, run.candles.timeframe_sec,
                                    pair.hedge_ratio or 1.0, rows, source)
        except Exception as e:
            logging.error('[ALGO %s] could not save candles: %s', pair.key, e)

    def _backfill_if_due(self, pair, run, now):
        """Ask MT5 for the bars once, and again a minute later if short."""
        history = run.history
        if history['state'] == 'done':
            return
        if history['at'] is not None and \
                now - history['at'] < BACKFILL_RETRY_SEC:
            return
        history['at'] = now
        rows, note = self._history(pair, run, now)
        if rows:
            added = run.candles.seed(rows)
            history['candles'] = len(rows)
            if self.store is not None:
                self._save(pair, run, rows, 'mt5')
            logging.info('[ALGO %s] %d candle(s) from MT5, %d new',
                         pair.key, len(rows), added)
        if run.candles.stats()['ready']:
            history['state'] = 'done'
            history['note'] = note or (f'{len(rows)} candles from MT5'
                                       if rows else 'saved candles')
        else:
            history['state'] = 'collecting'
            history['note'] = note or 'collecting candles from the live price'

    def history(self, pair, days):
        """`days` of closed spread candles from MT5, for a backtest —
        built exactly as the live band's history is. (rows, why not)."""
        params = algo_module.clean_params(getattr(pair, 'algo_params', None))
        anchor = bands.round_offset(self.offset_for(pair.account_a)) or 0.0
        run = _Run(self._signature(pair), params, anchor, self.clock())
        per_day = int(round(1440.0 / params['timeframe_min']))
        return self._history(pair, run, self.clock(),
                             count=int(days * per_day) + params['length'] * 2)

    def _history(self, pair, run, now, count=None):
        """Closed spread candles from both legs' MT5 bars, and why not."""
        params = run.params
        if count is None:
            count = params['length'] * bands.KEEP_MULTIPLE + 2
        leg_a = self.legs.get(pair.account_a)
        leg_b = self.legs.get(pair.account_b)
        if leg_a is None or leg_b is None:
            return [], 'a leg is not connected — collecting from the live price'
        try:
            bars_a = leg_a.rates(pair.symbol_a, params['timeframe_min'], count)
            bars_b = leg_b.rates(pair.symbol_b, params['timeframe_min'], count)
        except Exception as e:
            return [], f'MT5 history unavailable ({e}) — collecting live'
        if not bars_a or not bars_b:
            which = 'leg A' if not bars_a else 'leg B'
            return [], (f'MT5 returned no history for {which} — '
                        f'collecting from the live price')
        offset_a = self.offset_for(pair.account_a)
        offset_b = self.offset_for(pair.account_b)
        if offset_a is None or offset_b is None:
            return [], ("a broker's clock is not measured, so its bars "
                        "cannot be placed in time — collecting live")
        rows = bands.spread_bars(
            bars_a, bars_b, pair.hedge_ratio, run.candles.timeframe_sec,
            bands.round_offset(offset_a), bands.round_offset(offset_b),
            anchor=run.candles.anchor)
        current = bands.bucket_of(now, run.candles.timeframe_sec,
                                  run.candles.anchor)
        rows = [(b, c) for b, c in rows if b < current]
        if not rows:
            return [], ('the two legs share no closed bars — collecting '
                        'from the live price')
        return rows, None


def trend_drift(closes, length, lookback, sigma):
    """How far the band's middle (EMA) has moved over `lookback` candles,
    in sigma: + rising, - falling. None until there is enough history to
    measure it — and unmeasured is not flat."""
    if not sigma or lookback < 1 or len(closes) < length + lookback:
        return None
    now = bands.ema(closes, length)
    then = bands.ema(closes[:-lookback], length)
    if now is None or then is None:
        return None
    return (now - then) / float(sigma)


def judge_filters(p, md, stats, closes, cost_in):
    """The filters' readings for the panel, and the entry check.

    Read at the z an entry would be taken at for the panel, so its
    numbers are real before z ever gets there; judged at the actual z
    when a stretch confirms. A filter that cannot be priced BLOCKS.
    One function for the live Algo and the backtest, so the backtest
    judges exactly what the live one would.
    """
    cost_in = cost_in or {}
    qty = p['algo_qty']
    k = cost_in.get('k')
    width = None
    if md and md.get('long_spread') is not None \
            and md.get('short_spread') is not None:
        width = md['long_spread'] - md['short_spread']
    cost = algofilters.round_trip_cost(width, k, qty,
                                       cost_in.get('commission'),
                                       cost_in.get('slippage'))
    sigma = stats.get('sigma') if stats.get('ready') else None
    hl_candles = algofilters.half_life(closes)
    hl_minutes = (None if hl_candles is None
                  else hl_candles * p['timeframe_min'])
    regime = algofilters.regime(closes[-2 * p['length']:],
                                p['regime_er_max'],
                                p['regime_min_crossings'])
    lookback = max(1, int(round(p['trend_lookback_min']
                                / float(p['timeframe_min']))))
    drift = trend_drift(closes, p['length'], lookback, sigma)

    def edge_at(z):
        return algofilters.edge(z, sigma, k, qty, cost['total'],
                                p['edge_capture_frac'],
                                p['edge_multiple'])

    def prob_at(z):
        return algofilters.probability(z, sigma, k, qty, cost['total'],
                                       stop_z=p['stop_z'],
                                       min_win=p['min_win_prob'],
                                       min_ev=p['min_ev'])

    def check(side, z):
        if p['edge_on']:
            verdict = edge_at(z)
            if verdict['ok'] is None:
                return 'edge filter: the round-trip cost is not priced yet'
            if not verdict['ok']:
                return (f"edge filter: capture {verdict['ratio']:.2f}x "
                        f"the cost, under the {p['edge_multiple']:g}x "
                        f"required")
        if p['regime_on'] and regime['state'] == 'TRENDING':
            return (f"regime: the spread is TRENDING (efficiency "
                    f"{regime['efficiency_ratio']:.2f}, "
                    f"{regime['crossings']} crossings)")
        if p['trend_on']:
            if drift is None:
                return 'trend filter: not enough candles to measure it yet'
            limit = p['trend_sigma']
            if side == 'SELL' and drift >= limit:
                return (f"trend: the middle ROSE {drift:.1f}σ in the last "
                        f"{p['trend_lookback_min']:g} min — no H to L "
                        f"against it")
            if side == 'BUY' and drift <= -limit:
                return (f"trend: the middle FELL {abs(drift):.1f}σ in the "
                        f"last {p['trend_lookback_min']:g} min — no L to H "
                        f"against it")
        if p['prob_on']:
            verdict = prob_at(z)
            if not verdict['ok']:
                return 'probability: ' + (verdict['reason'] or 'not met')
        low, high = p['half_life_min_min'], p['half_life_max_min']
        if low or high:
            if hl_minutes is None:
                return 'half-life: the spread is not mean-reverting now'
            if low and hl_minutes < low:
                return (f'half-life {hl_minutes:.0f} min under '
                        f'{low:g} — reverts too fast (noise)')
            if high and hl_minutes > high:
                return (f'half-life {hl_minutes:.0f} min over '
                        f'{high:g} — reverts too slowly to hold')
        return None

    # The z an entry is actually taken at: the band, or — with re-entry —
    # the way back inside it.
    entry = p['entry_z'] - (p['reentry_back'] if p['reentry_on'] else 0.0)
    preview_edge = edge_at(entry)
    preview_prob = prob_at(entry)
    if drift is None:
        direction = None
    elif drift >= p['trend_sigma']:
        direction = 'UP'
    elif drift <= -p['trend_sigma']:
        direction = 'DOWN'
    else:
        direction = 'FLAT'
    filters = {
        'ready': bool(stats.get('ready')),
        'cost': cost, 'k': k, 'qty': qty,
        'edge': dict(preview_edge, on=p['edge_on']),
        'probability': dict(preview_prob, on=p['prob_on']),
        'regime': dict(regime, on=p['regime_on']),
        'trend': {'on': p['trend_on'], 'drift_sigma': drift,
                  'state': direction, 'limit': p['trend_sigma'],
                  'lookback_min': p['trend_lookback_min']},
        'half_life_candles': hl_candles,
        'half_life_minutes': hl_minutes,
        'half_life_band': [p['half_life_min_min'],
                           p['half_life_max_min']],
    }
    return filters, check
