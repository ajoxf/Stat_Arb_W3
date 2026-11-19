"""The bridge between the web process and the coordinator.

The browser talks to a Flask process; the orders are placed by the
coordinator process, which is the only one holding the legs. Commands
cross that gap as append-only JSON lines, and results come back the
same way.

**The watermark is PRIMED at startup, and that is the whole point of
this module.** In the system this is ported from every watermark
initialised to 0, so on restart the engine replayed the ENTIRE history
of commands in half a second — opening an unintended live position and
placing real test orders. A command is not state: replaying "place this
order" is placing another order.

So: persistent STATE (which ladders exist, what is configured) lives in
the config; a COMMAND (place this, cancel that) is executed once, by
the process that was running when it was written, and never again.
"""

import json
import logging
import os
import time
import uuid

from . import atomicfile
from .models import OrderType, OvernightMode, TimeInForce


class CommandLog:
    """Append-only commands, written by the web process."""

    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock

    def submit(self, kind, payload=None):
        """Write one command and return its id."""
        command = {'id': uuid.uuid4().hex[:12], 'kind': kind,
                   'at': self.clock(), 'payload': payload or {}}
        line = json.dumps(command)
        # Append is atomic enough for one line under O_APPEND, and a
        # torn line is skipped by the reader rather than crashing it.
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
            f.flush()
            os.fsync(f.fileno())
        return command['id']

    def read_all(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        commands = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                commands.append(json.loads(line))
            except ValueError:
                # A half-written line from the instant we read it. It
                # will be complete next pass; guessing at it would be
                # guessing at an order.
                logging.debug('skipping a partial command line')
        return commands


class CommandRunner:
    """Executes commands on the coordinator side, exactly once.

    `prime()` must be called at startup, BEFORE the first drain. It
    marks everything already in the file as history — those commands
    belong to a process that is gone.
    """

    def __init__(self, coordinator, log_path, results_path, clock=time.time):
        self.coordinator = coordinator
        self.log = CommandLog(log_path, clock)
        self.results_path = results_path
        self.clock = clock
        self.seen = set()
        self.primed = False
        self.results = {}

    def prime(self):
        """Everything already written happened in a previous life."""
        history = self.log.read_all()
        self.seen = {command['id'] for command in history}
        self.primed = True
        if history:
            logging.info('primed past %d command(s) from a previous run — '
                         'they are history, not instructions', len(history))
        return len(history)

    def drain(self):
        """Run every command written since we started. Returns results."""
        if not self.primed:
            # Refusing is the safe answer: an unprimed drain is the
            # replay this module exists to prevent.
            raise RuntimeError('CommandRunner.drain() before prime() — that '
                               'would replay the whole command history')
        done = []
        for command in self.log.read_all():
            if command['id'] in self.seen:
                continue
            self.seen.add(command['id'])
            result = self.execute(command)
            self.results[command['id']] = result
            done.append(result)
        if done:
            self.publish()
        return done

    def execute(self, command):
        kind = command.get('kind')
        payload = command.get('payload') or {}
        try:
            handler = getattr(self, f'_do_{kind}', None)
            if handler is None:
                return self._result(command, False,
                                    f'unknown command: {kind}')
            return self._result(command, True, None, handler(payload))
        except Exception as e:                      # never die on a command
            logging.exception('command %s failed: %s', kind, e)
            return self._result(command, False, str(e))

    def _result(self, command, ok, error=None, data=None):
        return {'id': command['id'], 'kind': command.get('kind'), 'ok': ok,
                'error': error, 'data': data, 'at': self.clock()}

    def publish(self):
        """Results, through a tmp file and `os.replace` like everything
        else the web process reads while we write it."""
        tmp = self.results_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            # Bounded: this is a UI convenience, not the audit trail.
            recent = dict(list(self.results.items())[-200:])
            json.dump(recent, f, default=str)
        atomicfile.replace(tmp, self.results_path)

    # -- the commands themselves --------------------------------------------

    #: The per-ladder settings the grid and the ladder both edit, each
    #: with the type it must be. An enum coercion is also the
    #: validation: 'MARKETT' raises here rather than arming a mode
    #: nothing understands.
    EDITABLE = {
        'order_type': OrderType,
        #: How this ladder gets OUT by default: MARKET crosses now,
        #: LIMIT rests at a level and waits. CLOSE ALL crosses either
        #: way — the way out must not change meaning under the button
        #: pressed in a hurry.
        'exit_type': OrderType,
        'time_in_force': TimeInForce,
        'overnight': OvernightMode,
        'increment': lambda v: float(v) if v not in (None, '') else None,
        'default_quantity': float,
        'quoting_leg': lambda v: v if v in ('a', 'b') else None,
        'rows': int,
        #: What ONE unit of Qty is on each leg, in lots. Both typed;
        #: blank reads as 1.
        'clip_lots_a': lambda v: float(v or 1.0),
        'clip_lots_b': lambda v: float(v or 1.0),
        #: What one LOT is, in the instrument's units. Blank is MT5's
        #: own, which is the right answer almost always.
        'contract_size_a': lambda v: float(v) if v not in (None, '') else None,
        'contract_size_b': lambda v: float(v) if v not in (None, '') else None,
        #: How long THIS pair may go unchanged before orders are
        #: withheld. Blank = the desk-wide MAX_QUOTE_AGE_SEC.
        'max_quote_age_sec': (
            lambda v: float(v) if v not in (None, '') else None),
        #: Which ALGO this ladder runs — one at a time, NONE by
        #: default. It measures and says what it would do; it does not
        #: trade, and a click on the ladder is unaffected either way.
        #: Routed to `set_algo`: ALGO is a switch on the running engine,
        #: not a field.
        'algo': str,
        'algo_window': bool,
        #: The Algo's own numbers for this ladder (entry z, candles,
        #: gates, optional exits). Blanks mean the default.
        'algo_params': lambda v: {k: x for k, x in dict(v or {}).items()
                                  if x not in (None, '')},
        #: Spot vs future, calendar, or two different instruments. It
        #: decides whether a fair spread applies at all, and it is
        #: DECLARED: two expiries do not make a calendar.
        'pair_type': lambda v: str(v or 'SPOT_FUTURE').upper(),
        #: The name the window toggle had before there were two algos.
        'show_fair_window': bool,
    }

    def _do_click(self, payload):
        return self.coordinator.click(payload['pair'], payload['side'],
                                      float(payload['level']),
                                      payload.get('quantity'))

    def _do_refresh_feed(self, payload):
        return self.coordinator.refresh_feed(payload['pair'])

    def _do_recentre_ladder(self, payload):
        """Put the window back around the market, on demand.

        The anchor holds the ladder still (coordinator.ladder_anchor);
        dropping it is how the trader says "I have finished reading up
        there, show me the market again".
        """
        self.coordinator.recentre_ladder(payload['pair'])
        return {'ok': True, 'pair': payload['pair']}

    def _do_lock_ladder(self, payload):
        """Hold this pair's price window still, or release it.

        The engine has to know, not just the browser: the anchor and
        the row window are built server-side, and a Lock the engine
        never hears about leaves both of them following the market
        under a ladder the trader believes is still.
        """
        locked = self.coordinator.lock_ladder(payload['pair'],
                                              payload.get('locked'))
        return {'ok': True, 'pair': payload['pair'], 'locked': locked}

    def _do_cancel_order(self, payload):
        return self.coordinator.cancel_order(payload['order_id'])

    def _do_cancel_where(self, payload):
        pulled = self.coordinator.cancel_where(payload.get('pair'),
                                               payload.get('side'),
                                               'cancelled by trader')
        return {'cancelled': len(pulled)}

    def _do_close_position(self, payload):
        position = self.coordinator.book.position(payload['position_id'])
        if position is None:
            return {'ok': False, 'reason': 'no such position'}
        pair = self.coordinator.config.pairs[position.pair_key]
        # The trader's Close rests as maker (W3); `now` - the Working
        # Orders "Market now" button - crosses at once.
        result = self.coordinator.close(
            pair, position, self.coordinator.market.get(position.pair_key),
            'closed by trader', maker=not payload.get('now'))
        self.coordinator.remember(position)
        return result

    def _do_flatten_pair(self, payload):
        """Every position on one ladder, at market, by ticket."""
        key = payload['pair']
        pair = self.coordinator.config.pairs[key]
        results = []
        for position in self.coordinator.book.positions(key):
            results.append(self.coordinator.executor.close_position(
                pair, position, self.coordinator.market.get(key),
                reason='flattened by trader'))
            self.coordinator.remember(position)
        return {'closed': len(results),
                'failed': [r for r in results if not r['ok']]}

    def _do_close_at_limit(self, payload):
        """Every position on one ladder, rested at ONE price.

        CLOSE ALL crosses now; this waits at the price the trader
        named. Both close by TICKET — an opposite market order on a
        hedging account opens a second position instead.
        """
        return self.coordinator.close_at_limit(payload['pair'],
                                               payload.get('level'),
                                               payload.get('quantity'))

    def _do_kill(self, payload):
        """The global kill: cancel everything, and on confirm flatten
        everything, across every ladder."""
        pulled = self.coordinator.cancel_where(reason='global kill')
        flattened = []
        if payload.get('flatten'):
            for key in list(self.coordinator.config.pairs):
                flattened.append(self._do_flatten_pair({'pair': key}))
        return {'cancelled': len(pulled), 'flattened': flattened}

    #: Settings the running engine can adopt without a restart. Each one
    #: is read fresh on every use, so changing it here changes the next
    #: click — the launcher is only needed for what it reads at startup.
    HOT_SETTINGS = {
        'CONFIRM_MARKET_CLICKS': lambda v: bool(v),
        'ROW_HEIGHT_PX': lambda v: max(12, min(40, int(v))),
        #: Anything unrecognised falls back to TT rather than
        #: leaving the ladder with no convention at all.
        #: Hot, because a desk that wants its old behaviour back in a
        #: hurry should not have to restart the engine to get it.
        'CLOSE_FIRST': lambda v: str(v).strip().lower() not in
            ('0', 'false', 'no', 'off', ''),
        'CLICK_CONVENTION': lambda v: (
            'TOUCH' if str(v).strip().upper() == 'TOUCH' else 'TT'),
        'MARKET_PROTECTION_TICKS': float,
        'CLICK_AWAY_RESTS': lambda v: bool(v),
        'REFUSE_SHARED_ACCOUNT': lambda v: bool(v),
        'TP_TARGET_PCT_OF_MARGIN': lambda v: max(0.0, float(v)),
        'RECENTRE_SEC': lambda v: max(0.0, min(300.0, float(v))),
        'REPEG_DEAD_BAND_TICKS': float,
        'MAX_QUOTE_AGE_SEC': float,
        'MAX_SPREAD_JUMP_SIGMA': float,
        'COMMAND_POLL_SEC': lambda v: max(0.005, min(1.0, float(v))),
        'OVERNIGHT_CLOSE_HOUR': lambda v: max(0, min(23, int(v))),
        'OVERNIGHT_CLOSE_MINUTE': lambda v: max(0, min(59, int(v))),
    }

    def _do_close_unclaimed(self, payload):
        """Close a position at the broker that our book cannot explain.

        By TICKET, at market, and only because a person asked: an
        unclaimed position is exactly the one an automatic close must
        not touch.
        """
        return self.coordinator.close_unclaimed(payload['account'],
                                                payload['ticket'])

    def _do_adopt_unclaimed(self, payload):
        """Take an unclaimed pair of tickets into the book as one
        spread position, so it is managed and marked like any other."""
        return self.coordinator.adopt_unclaimed(
            payload['pair'], payload['ticket_a'], payload['ticket_b'])

    def _do_set_setting(self, payload):
        """Change a hot setting on the RUNNING engine.

        The web process also writes it to config.json, so it survives a
        restart; this is what makes it true now. A setting the launcher
        only reads at startup is not in here — it would look applied
        while nothing had changed.
        """
        applied = {}
        for name, value in (payload.get('fields') or {}).items():
            coerce = self.HOT_SETTINGS.get(name)
            if coerce is None:
                continue
            self.coordinator.config.settings[name] = coerce(value)
            applied[name] = self.coordinator.config.settings[name]
        return {'applied': applied}

    def _do_algo_backtest(self, payload):
        """Replay history through one ladder's Algo. Sends nothing."""
        return self.coordinator.algo_backtest(payload['pair'],
                                              payload.get('days', 5))

    def _do_chart_history(self, payload):
        """One ladder's spread as 15m, 1h or 4h candles from MT5's bars,
        for the chart window. Reads only."""
        return self.coordinator.chart_history(
            payload['pair'], payload.get('timeframe_min', 15))

    def _do_backfill_signals(self, payload):
        """DERIVE the last N days of signals from MT5's own bars.

        A reading, and it says so: the rows it writes are stamped
        REPLAY. It sends nothing — `backtest.run` cannot reach an
        order, and this only writes down what it answers.
        """
        return self.coordinator.backfill_signals(
            payload.get('pair') or None, payload.get('days'))

    def _do_reset_excursions(self, payload):
        """Zero the Analysis tab's z-score excursion counters."""
        return self.coordinator.reset_excursions(payload.get('pair') or None)

    def _do_set_algo(self, payload):
        """NONE / FAIR_SPREAD / ALGO for one ladder, and ALGO's mode.

        ALGO switches the ladder's Algo ON in the running engine, off
        again after a restart. `mode` DRY_RUN signals and records; LIVE
        trades, and needs `confirmed` every time. Leaving LIVE while it
        holds a position needs `off_action`: 'close' or 'manual'.
        Refused in words — never silently ignored.
        """
        return self.coordinator.set_algo(
            payload['pair'], payload.get('algo'), mode=payload.get('mode'),
            confirmed=bool(payload.get('confirmed')),
            off_action=payload.get('off_action'))

    def _do_set_pair(self, payload):
        """Mode / TIF / overnight / increment / quantity, per ladder.

        The ladder and the Market Grid edit the SAME setting — one config
        object, one write path — so a change made in either is the change
        the other shows.
        """
        pair = self.coordinator.config.pairs[payload['pair']]
        fields = payload.get('fields') or {}
        applied = {}
        for name, value in fields.items():
            coerce = self.EDITABLE.get(name)
            if coerce is None:
                continue          # not a per-ladder setting; ignored
            if name == 'algo':
                answer = self.coordinator.set_algo(pair.key, value)
                applied[name] = answer.get('algo') or answer.get('reason')
                continue
            # The old name for the window toggle still works, so a page
            # that has not been reloaded goes on working.
            field = 'algo_window' if name == 'show_fair_window' else name
            setattr(pair, field, coerce(value))
            applied[name] = getattr(pair, field)
        return {'pair': pair.key,
                'applied': {k: getattr(v, 'value', v)
                            for k, v in applied.items()}}
