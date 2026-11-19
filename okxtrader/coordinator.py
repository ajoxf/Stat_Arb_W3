"""The coordinator: two feeds in, one spread per pair out.

It is the only process that sees both accounts at once. Each poll it
reads a tick from each leg runner, fuses them into a spread per pair,
runs the guards, and publishes one status snapshot that the ladders,
the Market Grid and the positions monitor all render from — so a number
cannot disagree with itself between panels, and the grid costs no extra
MT5 round trips.

What it must not do: poll `account_info` on every pass (it is an IPC
round trip, cached ~5s), and lag the panel beside it. The achieved loop
interval is published so "is the engine or the browser slow?" is
answerable rather than guessed at.
"""

import json
import logging
import os
import threading
import time
from types import SimpleNamespace
from datetime import datetime

from . import algo as algo_module, algofilters, analysis, atomicfile, \
    backtest, bands, carry, costs, config as config_module, \
    depth as depth_book, fairvalue, hedgeratio, signals, sizing, \
    takeprofit
from . import book as book_module
from .algodesk import AlgoDesk
from .algoexec import LiveSink
from .book import Book
from .chart import SpreadTape, chart_path
from .database import Store
from .executor import PairExecutor, mark_position, slippage
from .maker import MakerFills
from .models import (ALGO_SOURCE, MAGIC_NUMBER, MANUAL, LegFill, OrderType,
                     SpreadPosition, SpreadSide)
from .quoter import Quoter, quoting_leg
from .shadow import ShadowAlgo
from .reconcile import Reconciler
from .session import (PairSession, SessionClock, day_orders,
                      overnight_action)
from .spread import (LevelSigma, QuoteAgeTracker, SpreadJumpTracker,
                     compute_spread, executable_spread, stale_quote)


#: Settings whose only consumer is a panel or a display rule, so a save
#: takes effect on the next poll rather than at the next restart.
HOT_SETTINGS = (
    'TP_TARGET_PCT_OF_MARGIN', 'SLIPPAGE_ALLOWANCE', 'BREAK_EVEN_NIGHTS',
    'CARRY_RATE_PCT', 'COMMISSION_PER_LOT_A', 'COMMISSION_PER_LOT_B',
    'SPREAD_COST_FACTOR',
)



def priced(tick):
    """Does this tick carry a price? MT5 answers 0.0 for a side it has
    no price for - right after a re-subscribe, before the terminal has
    heard from the server, it is 0.0/0.0. That is NO price, not a price
    of zero: put into the spread it read as a thousand-sigma stretch.
    Unmeasured is not zero, so the leg is dark until a real one comes."""
    try:
        return float(tick.get('bid') or 0) != 0.0 and \
            float(tick.get('ask') or 0) != 0.0
    except (TypeError, ValueError, AttributeError):
        return False

class Coordinator:
    def __init__(self, config, legs, status_path='status.json',
                 clock=time.time, sleep=time.sleep,
                 monotonic=time.monotonic, store=None, leg_factory=None):
        self.config = config
        self.legs = legs                    # {account name: leg}
        #: Called with an account name to try, ONCE, to bring up a leg
        #: that was not there at startup. None = never retry (the
        #: tests, and anything driving the legs itself).
        self.leg_factory = leg_factory
        self._last_leg_try = None
        self.status_path = status_path
        #: Each ladder's spread and the Algo's z, for the chart window.
        self.tape = SpreadTape()
        self.chart_path = chart_path(status_path)
        self.clock = clock
        self.sleep = sleep
        self.book = Book()
        self.executor = PairExecutor(config, legs, clock=clock, sleep=sleep)
        self.quoter = Quoter(config, legs, self.executor, self.book)
        # Whoever closes a position — the trader, the overnight rule,
        # a flatten, the kill — disarms its resting close first.
        self.executor.before_close = self.quoter.disarm
        # ...and whatever the quoter books itself reaches the database.
        # `quoter.work` returns its events and this module drops them, so
        # without this a LIMIT entry was recovered as NOTHING and a close
        # that filled on a resting order came back OPEN.
        self.quoter.on_change = self.remember
        # A working order that reached its level and did not go on is
        # journalled as a refusal, in the broker's words - so the desk
        # and Telegram both say it.
        self.quoter.on_refusal = self._working_order_refused
        # The W3 maker fill: both legs rest at the touch, re-pegged,
        # recovered as maker - the OKX default for every entry.
        self.maker = MakerFills(config, legs, self.executor, clock=clock)
        self.maker.on_done = self._maker_done
        self.quoter.maker = self.maker
        self.reconciler = Reconciler(config, legs, self.book, self.executor,
                                     clock=clock, store=store)
        self.reconciler.in_flight = self.maker.in_flight_tickets
        self._last_reconcile = None
        self.session_clock = SessionClock(config, offset=self.broker_offset)
        #: The measured broker clock, per account: (measured at, offset).
        #: Re-measured slowly — a broker's clock does not drift on the
        #: scale of a poll, and it is a round trip per account.
        self._offsets = {}
        #: The local database: crash-safe position state, and the fill
        #: journal. None means run without one — the tests that do not
        #: care, and nothing else.
        self.store = store
        #: The Analysis tab's live trackers (see `_track`): when each open
        #: position's extremes were last saved, the open ids per ladder
        #: last pass (a missing one has closed), the running what-if
        #: shadows, and the z-score excursion counters.
        self._extremes_saved = {}
        self._open_ids = {}
        self._shadows = None
        self._shadow_saved = {}
        self._excursions = {}
        self._excursions_saved = {}
        #: The ANALYSIS band, one per ladder: the same candles and the
        #: same arithmetic the Algo's band uses, kept whether its Algo
        #: is on, off, dry or live.
        #:
        #: It exists because the z track used to be fed from the Algo's
        #: own body, and `_work_algo` returns None for a ladder whose
        #: Algo is off — which is EVERY ladder after every restart,
        #: since LIVE is off at every start. So the tab that is meant
        #: to tell a trader whether the Algo is worth switching on
        #: recorded nothing until they had switched it on.
        #:
        #: It measures and it is read by the Analysis tab. It cannot
        #: reach an order: `_track` is the only caller, and nothing in
        #: it returns a signal (`tests/test_algo.py` fails the build if
        #: anything but `algoexec` sends).
        self._bands = {}
        #: The zone last RECORDED per (ladder, timeframe), read back
        #: from the database at first use so a restart does not record
        #: the zone the spread is already sitting in.
        self._zone_last = None
        #: Live signals per ladder, and where each ladder's arming had
        #: got to. Both reopened from the database at first use: a
        #: forward watch that is lost on a restart never settles, and
        #: arming that is lost re-arms on a spread that has not moved.
        self._signals = None
        self._arming = {}
        #: Each ladder's SHADOW ALGO (`shadow.ShadowAlgo`) - the Algo's
        #: own code, run without an executor, whose decisions are the
        #: signal journal - and the stretch each side is in.
        self._shadow_algos = {}
        self._episodes = {}
        self._shadow_algo_saved = {}
        #: What recovery found at startup, and what it could not claim.
        self.recovery = {'recovered': 0, 'unclaimed': [], 'complete': False}
        self._last_journal = None
        #: Set by the launcher: the bridge from the web process. Primed
        #: at startup, so a restart never replays a command history.
        self.commands = None
        #: What the cutoff did, per day, for the monitor to show.
        self.session_events = []
        # The guards measure on a MONOTONIC clock and the quote's own
        # identity, never on the broker's timestamp — a clock offset
        # would poison a guard that gates real orders.
        self.quote_ages = QuoteAgeTracker(monotonic)
        self.jumps = SpreadJumpTracker(monotonic)
        self.sigmas = {}                    # pair key -> LevelSigma
        self.market = {}                    # pair key -> snapshot
        self.session = {}                   # pair key -> O/H/L/V of OUR series
        self._account_cache = {}            # account -> (at, info)
        #: The last comparison of our open P&L against MT5's own, taken
        #: on a pass where both halves were read together. Held between
        #: such passes rather than recomputed against a stale half —
        #: see pnl_check().
        self._pnl_check = None
        #: (account, symbol) -> (at, the leg's own session figures)
        self._session_cache = {}
        #: (account, symbol) -> (at, that leg's DOM)
        self._depth_cache = {}
        #: pair key -> (at, margin for ONE spread across both accounts)
        self._margin_cache = {}
        #: (mtime, size) of the config last read, so the hot-apply
        #: watcher opens the file only when it has actually changed.
        self._config_marker = None
        #: pair key -> the price its ladder window is anchored on. Held
        #: still between polls so a row keeps its price; see
        #: `ladder_anchor`.
        self._ladder_anchor = {}
        #: Pairs whose ladder the trader has LOCKED. A locked ladder
        #: does not re-anchor and does not widen its window to follow
        #: the touches: the price at every row stays where it is until
        #: they press Centre. See `_anchor_for` and `ladder_rows`.
        self._ladder_locked = {}
        #: pair key -> when a stale leg was last written to the log
        self._stale_logged = {}
        #: pair key -> when a stale pair last re-subscribed itself
        self._auto_refreshed = {}
        #: pair key -> why this pair has NO price at all, while it has
        #: none. Missing is not stale: a stale pair still has a book to
        #: read, a dark one has nothing, and the ladder has to say which
        #: it is looking at.
        self._dark = {}
        self._dark_logged = {}
        self._loop_interval = None
        self._last_poll = None
        self._stop = threading.Event()
        #: One lock over the book and the executor. Commands arrive on
        #: their own thread — a click must not wait for the next poll —
        #: and a poll reading the book while a click mutates it would
        #: publish a half-placed order.
        self.lock = threading.RLock()
        #: Symbol resolution failures, in the operator's words, kept on
        #: the snapshot so a broken pair is VISIBLE rather than absent
        #: (spec §17: never hide a broken row).
        self.errors = {}
        #: Each ladder's Algo switch, OFF for every ladder at start.
        #: DRY_RUN signals and records; LIVE also trades, through
        #: `algo_enter` / `algo_exit` below (see `algodesk`, `algoexec`).
        self.algos = AlgoDesk(legs, store=store, clock=clock,
                              offset_for=self._offset_of,
                              live=LiveSink(self),
                              result_of=self._realized_of)

    # -- startup ------------------------------------------------------------

    def start(self):
        """Sweep, resolve symbols, recover the book, then poll.

        The sweep is before ANYTHING else: a pending of ours still
        resting is from a previous life, and one that filled while we
        were down is an unhedged outright position nobody was watching.

        Recovery comes before the first reconcile for a sharper reason:
        the book starts EMPTY, and an empty book makes every real
        position look like an orphan. Sixty seconds later the reconciler
        would close them — the machinery working correctly on a book
        that lied to it.
        """
        swept = self.sweep_pendings('startup')
        self.resolve_symbols()
        self.recover()
        return swept

    def recover(self):
        """Bring back the positions that were open when we stopped.

        Each one comes back ACTIVE, with its own id, fills and tickets —
        a position recovered under a NEW id is an orphan to the
        reconciler and a ghost to the book.

        Then: anything at the broker carrying OUR magic that recovery
        did not account for is UNCLAIMED. It is not closed and it is not
        struck out; it is put on the screen for a person to adopt or
        close, because a position we cannot explain is exactly the one
        an automatic close should not touch.
        """
        report = {'recovered': 0, 'unclaimed': [], 'complete': False,
                  'error': None}
        if self.store is None:
            # No database: we cannot know what was open. Say so, and let
            # the reconciler hold its fire rather than guess.
            report['error'] = ('no database — open positions cannot be '
                               'recovered, so nothing at the broker will be '
                               'auto-closed')
            self.recovery = report
            self.reconciler.book_complete = False
            return report
        try:
            for row in self.store.open_positions():
                position = SpreadPosition.from_dict(row)
                self.book.add_position(position)
                report['recovered'] += 1
        except Exception as e:                       # a broken database
            logging.critical('could not recover positions: %s', e)
            report['error'] = str(e)
            self.recovery = report
            self.reconciler.book_complete = False
            return report

        known = self.reconciler.known_tickets()
        for name, leg in self.legs.items():
            positions = leg.positions()
            if positions is None:
                # UNKNOWN, not flat: we cannot complete recovery against
                # an account we could not read.
                report['error'] = (f"account '{name}' could not be read at "
                                   f"startup — recovery is incomplete")
                continue
            for position in positions:
                if (name, str(position['ticket'])) in known:
                    continue
                report['unclaimed'].append(dict(position, account=name))

        for name in self.dark_accounts():
            # An account with no runner is UNKNOWN, not flat. Saying
            # recovery is complete here would licence the reconciler to
            # auto-close positions on the accounts it CAN read, on the
            # strength of a book missing everything on the one it
            # cannot.
            report['error'] = (f"account '{name}' has no leg runner — "
                               f"recovery is incomplete")
        report['complete'] = report['error'] is None
        # The reconciler auto-closes orphans only when the book is known
        # to be complete. It never auto-closes anything on this list.
        self.reconciler.book_complete = report['complete']
        self.reconciler.unclaimed = {
            (row['account'], str(row['ticket'])): row
            for row in report['unclaimed']}
        if report['recovered']:
            logging.warning('recovered %d open position(s) from the database',
                            report['recovered'])
        if report['unclaimed']:
            logging.critical(
                '%d position(s) at the broker carry our magic but are not in '
                'our book. They will NOT be closed automatically — adopt or '
                'close them by hand: %s', len(report['unclaimed']),
                ', '.join(f"{row['account']}:{row['ticket']} {row['symbol']} "
                          f"{row['side']} {row['volume']}"
                          for row in report['unclaimed']))
        if self.store is not None:
            self.store.event('recovery', **{k: v for k, v in report.items()
                                            if k != 'unclaimed'})
        self.recovery = report
        return report

    def dark_accounts(self):
        """Configured accounts whose leg runner is not answering.

        Published, because this is the difference between "the market
        is quiet" and "half the engine is not there" — and the screen
        must never have to guess which.
        """
        return [name for name in self.config.accounts
                if name not in self.legs]

    def retry_dark_legs(self):
        """Try, on a slow clock, to attach the accounts that are dark.

        A leg runner started late — or restarted at lunchtime — used to
        need the whole engine restarted with it. Here it just joins:
        its pendings are swept (magic-scoped: they are from a previous
        life and one still resting can fill unhedged), its symbols are
        resolved, and the book is recovered against it before the
        reconciler is allowed to call itself complete.
        """
        if self.leg_factory is None:
            return []
        dark = self.dark_accounts()
        if not dark:
            return []
        every = float(self.config.get('LEG_RETRY_SEC', 5.0))
        now = self.clock()
        if self._last_leg_try is not None and now - self._last_leg_try < every:
            return []
        self._last_leg_try = now
        joined = []
        for name in dark:
            try:
                leg = self.leg_factory(name)
            except Exception as e:                 # a runner mid-start
                logging.warning("account '%s': leg runner did not answer "
                                "(%s)", name, e)
                continue
            if leg is None:
                continue
            with self.lock:
                self.legs[name] = leg
                self.sweep_pendings(f'join:{name}', legs={name: leg})
                self.resolve_symbols()
                self.recover()
            joined.append(name)
            logging.warning(
                "account '%s' joined: its leg runner answered and its "
                "symbols were resolved", name)
        return joined

    def sweep_pendings(self, when, legs=None):
        """Cancel every pending of OURS on both accounts, and verify.

        Magic-scoped, so the trader's own terminal orders are never
        touched. A pending we failed to cancel is a CRITICAL line, not a
        warning — it can fill unhedged with nobody watching.
        """
        report = {'when': when, 'cancelled': [], 'failed': [], 'unknown': []}
        for name, leg in (self.legs if legs is None else legs).items():
            pendings = leg.pending_orders()
            if pendings is None:
                # None means the leg could not be read — NOT "no orders".
                report['unknown'].append(name)
                logging.critical(
                    "%s sweep: account '%s' could not be read, so it is "
                    "UNKNOWN whether pendings of ours are resting there",
                    when, name)
                continue
            for pending in pendings:
                result = leg.cancel_order(pending['ticket'])
                entry = dict(pending, account=name)
                if result.get('cancelled'):
                    if result.get('filled_volume'):
                        # It filled before the cancel landed. That is an
                        # orphan position, not a tidy cancel.
                        entry['leaked_fill'] = result.get('filled_volume')
                        entry['position_tickets'] = result.get(
                            'position_tickets')
                        logging.critical(
                            "%s sweep: pending %s on '%s' FILLED before it "
                            "could be cancelled — %s lots are on with no "
                            "hedge", when, pending['ticket'], name,
                            result.get('filled_volume'))
                    report['cancelled'].append(entry)
                else:
                    entry['error'] = result.get('error')
                    report['failed'].append(entry)
                    logging.critical(
                        "%s sweep: could NOT cancel pending %s on '%s': %s",
                        when, pending['ticket'], name, result.get('error'))
        return report

    def resolve_symbols(self):
        """Read both legs' contract specs from MT5 — never typed in.

        A failure names what the account actually offers, because
        brokers spell gold XAUUSD, GOLD, XAUUSD.r, and an account that
        offers none of them is probably the wrong leg.
        """
        for key, pair in self.config.pairs.items():
            problems = []
            for leg_key, meta_attr in (('a', 'meta_a'), ('b', 'meta_b')):
                account = pair.account_a if leg_key == 'a' else pair.account_b
                symbol = pair.symbol_a if leg_key == 'a' else pair.symbol_b
                leg = self.legs.get(account)
                if leg is None:
                    problems.append(
                        f"leg {leg_key.upper()}: account '{account}' has no "
                        f"leg runner — start it, or fix the account on this "
                        f"pair")
                    continue
                report = leg.symbol_report(symbol)
                if not report.get('found'):
                    offered = _offered(leg, symbol)
                    names = ', '.join(s['symbol'] for s in offered[:8])
                    problems.append(
                        f"leg {leg_key.upper()}: '{symbol}' is not on account "
                        f"'{account}'. " + (
                            f"It offers {names}." if names else
                            f"Nothing there resembles it — this is probably "
                            f"the wrong account for this leg."))
                    continue
                meta = _meta_from_report(report)
                # The trader's OWN contract size, where they set one.
                # MT5's is kept beside it so the ladder and Diagnose
                # can name the disagreement rather than quietly
                # trading on a number nobody checked.
                override = (pair.contract_size_a if leg_key == 'a'
                            else pair.contract_size_b)
                meta['contract_size_mt5'] = meta.get('contract_size')
                if override:
                    meta['contract_size'] = float(override)
                setattr(pair, meta_attr, meta)
            self.errors[key] = problems
            if not problems:
                self._settle_beta(pair)
        return self.errors

    def _settle_beta(self, pair):
        """Re-derive beta when it was stamped for a DIFFERENT pair.

        An operator who tuned beta on their own pair keeps their number;
        a beta left behind by an instrument change does not get to
        silently define the spread.
        """
        price_a = (pair.meta_a or {}).get('mid')
        price_b = (pair.meta_b or {}).get('mid')
        beta, why = hedgeratio.resolve(
            pair.hedge_ratio, pair.hedge_ratio_for, pair.pair_type,
            pair.symbol_a, pair.symbol_b, price_a, price_b)
        if beta is not None:
            logging.warning("%s: hedge ratio %g -> %g (%s)",
                            pair.key, pair.hedge_ratio, beta, why)
            pair.hedge_ratio = beta
        pair.hedge_ratio_for = hedgeratio.pair_signature(pair.symbol_a,
                                                         pair.symbol_b)
        pair.beta_reason = why

    # -- the loop ------------------------------------------------------------

    def poll_once(self):
        """One pass over every enabled pair. Returns the snapshot dict."""
        with self.lock:
            snapshot = self._poll_once()
            self._stamp_our_tickets()
            return snapshot

    def _stamp_our_tickets(self):
        """Write every ticket the book is holding into OUR ledger.

        Done on every poll, and cheap: it is INSERT OR IGNORE on a
        handful of rows. The point is durability, not speed - a ticket
        recorded here stays recorded after the book stops holding it,
        which is exactly the case that cost a trader two legs of a live
        spread. See database.py's our_tickets table.

        Never fatal. A ledger that cannot be written is a guard we do
        not have; it is not a reason to stop trading, and the two other
        signals - the book itself and the order comment - are
        untouched.
        """
        if self.store is None:
            return
        rows = []
        for position in self.book.positions():
            for fill in (position.leg_a, position.leg_b):
                if not fill:
                    continue
                for ticket in (fill.position_tickets or []):
                    rows.append((fill.account, ticket, position.pair_key))
        if not rows:
            return
        try:
            self.store.remember_tickets(rows)
        except Exception as e:
            logging.critical('could not record our own tickets (%s) - the '
                             'reconciler has the book and the order comment '
                             'to go on, and nothing else', e)

    def reload_reference_fields(self):
        """Re-read the config and take the REFERENCE-ONLY pair fields.

        Symbols, contract sizes and beta are structural and need a
        restart; four fields whose only consumer is a panel are not. In
        the system this is ported from they were blocked behind the
        structural keys, and the log said "an assets change requires a
        restart" ten lines above a live trade while the values sat
        saved and correct.

        Reads only when the file has actually CHANGED, and logs only
        what actually moved — a watcher that prints every poll is a
        watcher nobody reads.
        """
        path = getattr(self.config, 'path', None)
        if not path:
            return []
        try:
            stamp = os.stat(path)
            marker = (stamp.st_mtime, stamp.st_size)
        except OSError:
            return []
        if marker == self._config_marker:
            return []
        self._config_marker = marker
        try:
            raw = config_module.load_raw(path)
        except Exception as e:                # a half-written config
            logging.debug('config re-read: %s', e)
            return []
        moved = []
        for key, pair in self.config.pairs.items():
            changed = pair.apply_hot((raw.get('pairs') or {}).get(key) or {})
            if changed:
                moved.append(f"{key}: {', '.join(changed)}")
        settings = raw.get('settings') or {}
        for key in HOT_SETTINGS:
            if key in settings and self.config.settings.get(key) != \
                    settings[key]:
                self.config.settings[key] = settings[key]
                moved.append(key)
        if moved:
            logging.info('applied without a restart — %s', '; '.join(moved))
        return moved

    def _poll_once(self):
        started = self.clock()
        self.reload_reference_fields()
        if self._last_poll is not None:
            self._loop_interval = started - self._last_poll
        self._last_poll = started

        ticks = self._read_ticks()
        for key, pair in self.config.enabled_pairs().items():
            tick_a = ticks.get((pair.account_a, pair.symbol_a))
            tick_b = ticks.get((pair.account_b, pair.symbol_b))
            if not tick_a or not tick_b:
                # NO PRICE IS NOT NOTHING TO DO.
                #
                # This was a bare `continue`, and it was the whole of
                # the 2026-09-16 naked leg. Skipping here skipped
                # EVERYTHING below it: the staleness log, the automatic
                # re-subscribe, and `quoter.work` - the only place that
                # asks whether a resting order has filled. A pending is
                # a real order at the broker; it filled at 18:47, was
                # never noticed, and was hedged at 19:00 the instant a
                # tick returned. Twelve minutes fifty-one seconds naked,
                # in silence.
                #
                # The dark path now does MORE than the stale one, not
                # less: it says so, it tries to fix itself, and it
                # watches what is already at the broker.
                self.market[key] = None
                self._go_dark(key, pair, tick_a, tick_b)
                self._work_algo(pair, None)
                continue
            self._dark.pop(key, None)
            md = compute_spread(pair, tick_a, tick_b, pair.hedge_ratio,
                                clock=self.clock)
            self.quote_ages.observe(key, md)
            sigma = self.sigmas.setdefault(
                key, LevelSigma(self.config.get('SIGMA_WINDOW_QUOTES', 600)))
            sigma.observe(md)
            md['spread_sigma'] = sigma.sigma

            # THIS PAIR'S OWN patience, falling back to the desk's.
            # A dated oil future that trades a few times a minute and a
            # gold spot that ticks several times a second cannot share
            # one threshold — and sharing it is what withheld every
            # order on the oil ladder while the ladder looked fine.
            stale = stale_quote(md, pair.max_quote_age_sec
                                if pair.max_quote_age_sec is not None
                                else self.config.get('MAX_QUOTE_AGE_SEC'))
            jumped = self.jumps.observe(
                key, md, sigma.sigma,
                self.config.get('MAX_SPREAD_JUMP_SIGMA'),
                self.config.get('JUMP_SETTLE_SEC'))
            md['stale_reason'] = stale
            # A feed that is silent BECAUSE this ladder's market is shut
            # - its break, or outside the hours set for it - is not a
            # fault: no warning and no re-subscribe loop. Orders are
            # still withheld on a stale price.
            if stale and not PairSession(pair, self.config).quiet(
                    self.session_clock.broker_now()):
                self._log_stale(key, pair, md)
                self._auto_refresh(key, md)
            md['jump_reason'] = jumped
            md['guard_reason'] = stale or jumped
            # The badge the ladder shows continuously, so a trader can
            # see what they are clicking into (spec §8).
            md['feed_badge'] = _badge(md, stale, jumped)
            self._observe_session(key, md, pair)
            self.market[key] = md
            body = self._work_algo(pair, md)
            self._track(pair, md, body)
            # LIMIT-mode orders are worked on the SAME pass that priced
            # them: a peg re-priced off a snapshot older than the one on
            # screen is a peg holding a level nobody is showing.
            self.quoter.work(pair, md)
            self.maker.step(pair, md)
            self.tidy_closing_orders(pair)
        return self.market

    def _read_ticks(self):
        """One tick per (account, symbol), fetched once per poll.

        Two pairs on the same symbol cost one round trip, not two: the
        poll budget is two IPC calls per pair per pass and it is the
        thing that decides whether the ladder keeps up.
        """
        wanted = set()
        for pair in self.config.enabled_pairs().values():
            wanted.add((pair.account_a, pair.symbol_a))
            wanted.add((pair.account_b, pair.symbol_b))
        ticks = {}
        for account, symbol in wanted:
            leg = self.legs.get(account)
            if leg is None or not symbol:
                continue
            tick = leg.tick(symbol)
            if tick and priced(tick):
                ticks[(account, symbol)] = tick
        return ticks

    def refresh_feed(self, pair_key):
        """Take both legs out of Market Watch and put them back.

        The answer to "the price is moving in MT5 and this says stale":
        a re-subscribe is what restarts a feed the terminal has gone
        quiet on, and it is a thing the trader can DO rather than a
        thing to wait out. The staleness clock is reset with it — not
        to hide the age, but because the old age was measured against a
        subscription that no longer exists.

        It reports the prices that came back, so the screen can say
        whether it worked instead of claiming it did.
        """
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'no pair {pair_key}'}
        got = {}
        for leg_name, account, symbol in (('A', pair.account_a, pair.symbol_a),
                                          ('B', pair.account_b,
                                           pair.symbol_b)):
            leg = self.legs.get(account)
            if leg is None or not symbol:
                got[leg_name] = None
                continue
            try:
                got[leg_name] = leg.resubscribe(symbol)
            except Exception as e:
                got[leg_name] = None
                logging.error('refresh %s: %s', symbol, e)
        with self.lock:
            self.quote_ages.forget(pair_key)
            if hasattr(self.jumps, 'forget'):
                self.jumps.forget(pair_key)
        alive = [name for name, tick in got.items() if tick]
        return {'ok': bool(alive),
                'reason': ('re-subscribed ' +
                           ', '.join(f"leg {name} "
                                     f"{(got[name] or {}).get('bid')}"
                                     f"/{(got[name] or {}).get('ask')}"
                                     for name in ('A', 'B') if got.get(name))
                           if alive else
                           'neither leg answered — check the terminals'),
                'ticks': got}

    def margin_per_spread(self, pair):
        """What ONE spread ties up, in money, across BOTH accounts.

        Priced by the terminals themselves — margin depends on the
        broker's leverage, the symbol's margin mode and the account
        group, so a number computed here would be a guess wearing a
        figure's clothes. Cached: it moves with the price, but slowly.

        None when either leg cannot price it. The take-profit target
        then shows an em dash and says why, rather than being built on
        half a number.
        """
        return (self.margin_detail(pair) or {}).get('money')

    def margin_detail(self, pair):
        """What one spread ties up, WITH where the figure came from.

        The terminals price it themselves (`order_calc_margin`), which
        is the honest number: margin depends on the broker's leverage,
        the symbol's margin mode and the account group. Where a leg
        cannot price it, each leg's notional over ITS OWN account's
        leverage is the fallback — not one leverage for both — and it
        is LABELLED, because a target computed off the wrong base looks
        like a considered number.

        No leverage either, and there is NO suggestion: a target on a
        base nobody set is worse than no target.
        """
        ttl = float(self.config.get('MARGIN_TTL_SEC', 60.0))
        now = self.clock()
        cached = self._margin_cache.get(pair.key)
        if cached and now - cached[0] < ttl:
            return cached[1]
        detail = self._price_margin(pair)
        self._margin_cache[pair.key] = (now, detail)
        return detail

    def _price_margin(self, pair):
        total = 0.0
        source = 'terminal'
        for meta, account, symbol, lots, side in (
                (pair.meta_a, pair.account_a, pair.symbol_a,
                 pair.clip_lots_a, 'SELL'),
                (pair.meta_b, pair.account_b, pair.symbol_b,
                 pair.clip_lots_b, 'BUY')):
            leg = self.legs.get(account)
            if leg is None or not symbol or not lots:
                total = None
                break
            try:
                margin = leg.margin_for(symbol, side, lots)
            except Exception:                 # a leg mid-restart
                margin = None
            if margin is None:
                margin = self._margin_from_leverage(account, meta, lots)
                if margin is not None:
                    source = 'leverage'
            if margin is None:
                total = None
                break
            total += float(margin)
        if total is None:
            return {'money': None, 'source': None,
                    'note': 'the terminals have not priced the margin for '
                            'one spread, and no account leverage is known — '
                            'set leverage to size a % target'}
        return {'money': total, 'source': source,
                'note': ('priced by the terminals themselves'
                         if source == 'terminal' else
                         "each leg's notional over its own account's "
                         "leverage — the terminals could not price it")}

    def _margin_from_leverage(self, account, meta, lots):
        """Notional over THAT account's leverage. Not one for both."""
        info = self.account_info(account) or {}
        leverage = info.get('leverage')
        price = (meta or {}).get('ask') or (meta or {}).get('bid')
        contract = (meta or {}).get('contract_size')
        if not leverage or not price or not contract:
            return None
        return float(lots) * float(contract) * float(price) / float(leverage)

    def fair_spread(self, pair, md):
        """What the CARRY says this basis should be, both directions.

        Priced from the broker's OWN swap and the nights to expiry —
        `-carry / k`, which is size-free — and compared against the
        price each direction would actually trade at, never a midpoint.

        ONE path to the carry: the broker's swap per leg per side,
        with the four typed fields as overrides. There used to be a
        second — a single number in spread points per day that
        short-circuited all of it — and two ways to price the same
        carry is two answers to reconcile when the reading looks
        wrong.
        """
        now = self.session_clock.broker_now() or datetime.now()
        expiry = pair.effective_expiry('b')
        # A CALENDAR is decided when its near leg expires, so that is
        # where the carry runs to. Running it to the far leg prices a
        # trade that is already over.
        kind = algo_module.pair_kind(pair, pair.effective_expiry('a'), expiry)
        nights, kind_note = algo_module.carry_nights(
            kind,
            fairvalue.days_to_expiry(pair.effective_expiry('a'), now),
            fairvalue.days_to_expiry(expiry, now))
        body = carry.describe(
            pair.meta_a, pair.meta_b, pair.hedge_ratio,
            pair.clip_lots_a, pair.clip_lots_b,
            sizing.spread_units(pair.clip_lots_b,
                                (pair.meta_b or {}).get('contract_size')),
            nights, market=md, overrides=pair.swap_overrides(),
            expects_expiry=(kind != algo_module.RELATED),
            rate_pct=pair.exit_settings(
                self.config.settings).get('CARRY_RATE_PCT'))
        body['kind'] = kind
        body['kind_note'] = kind_note
        return body

    def tidy_closing_orders(self, pair):
        """Pull the closing orders that must not stay resting.

        - **Whoever rested it**, a closing order whose POSITION has gone
          is pulled: it would fill, and with nothing to close it would
          OPEN a naked position.
        - **AutoRouting's leftovers.** AutoRouting — rest a take-profit
          on every fill — has been removed: the Algo is the one thing
          that exits by itself now, and two would confuse the desk. A
          target it armed before the upgrade, recovered at startup, is
          pulled here and said so. ONLY those: a close the TRADER rested
          by hand is theirs and is never touched.
        """
        events = []
        live = {p.position_id for p in self.book.positions(pair.key)}
        for order in self.book.orders(pair.key):
            if order.position_id and order.position_id not in live:
                self.quoter.disarm(order.position_id,
                                   'its position is no longer open')
                events.append({'pair': pair.key, 'action': 'close_pulled',
                               'position': order.position_id})
        for position in self.book.positions(pair.key):
            if self.book.auto_armed_for(position.position_id):
                self.quoter.disarm_auto(position.position_id,
                                        'AutoRouting has been removed')
                events.append({'pair': pair.key,
                               'action': 'auto_route_pulled',
                               'position': position.position_id})
        if events:
            self.session_events.extend(events)
        return events

    def algo_block(self, pair, md, exit_levels=None):
        """What the selected algo says. NONE says nothing at all.

        Nothing in here places an order, and nothing in here is
        consulted by the click path: an algo is a reading beside the
        market, exactly like the fair spread was before it had a name.
        """
        running = self.algos.block(pair.key)
        if running is not None:
            body = dict(running, window=pair.algo_window)
            # Where a NEW entry's stop would be, beside the take-profit
            # the Exit panel already shows for it: the same break-even,
            # the other way.
            stop = self._algo_stop_points(pair, pair.default_quantity,
                                          sizing.spread_units(
                                              pair.clip_lots_b,
                                              (pair.meta_b or {}).get(
                                                  'contract_size')))
            levels = exit_levels or {}
            body['sl_buy'] = (None if stop is None or
                              levels.get('break_even_buy') is None
                              else levels['break_even_buy'] - stop)
            body['sl_sell'] = (None if stop is None or
                               levels.get('break_even_sell') is None
                               else levels['break_even_sell'] + stop)
            return body
        selected = pair.algo or algo_module.NONE
        body = {'algo': selected, 'window': pair.algo_window}
        if selected == algo_module.FAIR_SPREAD:
            body['fair'] = self.fair_spread(pair, md)
            body['kind'] = body['fair'].get('kind')
            body['kind_note'] = body['fair'].get('kind_note')
        return body

    # -- the Algo -------------------------------------------------------------

    def set_algo(self, pair_key, choice, mode=None, confirmed=False,
                 off_action=None):
        """Pick what a ladder runs: NONE, FAIR_SPREAD or ALGO.

        ALGO is a switch on the RUNNING engine and is never saved —
        every restart comes up with it off. `mode` is DRY_RUN (signals
        only, the default) or LIVE (it trades), and LIVE needs
        `confirmed` every time. The other two choices are the Fair
        Spread window, which IS the pair's saved setting.

        Algo or Manual, never both: LIVE is refused on a ladder with a
        MANUAL position or a working order. Leaving LIVE while the Algo
        holds a position needs `off_action` — 'close' it now, or hand it
        to 'manual' — because a position nobody manages is the worst of
        the three.
        """
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'no pair {pair_key}'}
        chosen = str(choice or algo_module.NONE).upper()
        if chosen not in algo_module.ALGOS:
            return {'ok': False, 'pair': pair_key,
                    'reason': f'{choice!r} is not an algo — choose one of '
                              + ', '.join(algo_module.ALGOS)}
        mode = str(mode or algo_module.DRY_RUN).upper()
        with self.lock:
            leaving_live = (self.algos.mode(pair_key) == algo_module.LIVE
                            and not (chosen == algo_module.ALGO
                                     and mode == algo_module.LIVE))
            if leaving_live:
                handed = self._hand_back(pair, off_action)
                if not handed.get('ok'):
                    return dict(handed, pair=pair_key, algo=None)
            if chosen == algo_module.ALGO:
                if mode == algo_module.LIVE:
                    refusal = self._live_refusal(pair)
                    if refusal:
                        answer = {'ok': False, 'pair': pair_key,
                                  'reason': refusal}
                        self._record_switch(pair_key, chosen, mode, answer)
                        return dict(answer, algo=None)
                adopt = [p.position_id for p in self.book.positions(pair_key)
                         if getattr(p, 'source', MANUAL) == ALGO_SOURCE]
                answer = self.algos.turn_on(pair, mode, confirmed,
                                            adopt=adopt
                                            if mode == algo_module.LIVE
                                            else ())
            else:
                self.algos.turn_off(pair_key)
                pair.algo = chosen
                answer = {'ok': True, 'pair': pair_key, 'on': False}
        self._record_switch(pair_key, chosen, mode, answer)
        return dict(answer, algo=chosen if answer.get('ok') else None)

    def _record_switch(self, pair_key, chosen, mode, answer):
        if self.store is None:
            return
        try:
            self.store.event('algo_switch', pair_key, algo=chosen, mode=mode,
                             ok=answer.get('ok'), reason=answer.get('reason'))
        except Exception as e:
            logging.error('could not record the Algo switch: %s', e)

    def _live_refusal(self, pair):
        """Why this ladder cannot go LIVE, in words, or None."""
        manual = [p for p in self.book.positions(pair.key)
                  if getattr(p, 'source', MANUAL) != ALGO_SOURCE]
        if manual:
            net = sum(p.quantity if p.side is SpreadSide.BUY else -p.quantity
                      for p in manual)
            return (f'{pair.key} has {len(manual)} manual position(s) open '
                    f'({net:+g} spreads) — Algo or Manual, never both. Close '
                    f'them first.')
        working = self.book.orders(pair.key)
        if working:
            return (f'{pair.key} has {len(working)} working order(s) — '
                    f'cancel them before the Algo trades this ladder.')
        return None

    def _hand_back(self, pair, off_action):
        """The Algo is leaving LIVE: what happens to what it holds."""
        owned = [self.book.position(pid)
                 for pid in self.algos.owned(pair.key)]
        owned = [p for p in owned if p is not None and p.is_open]
        if not owned:
            return {'ok': True}
        action = str(off_action or '').lower()
        if action == 'manual':
            for position in owned:
                position.source = MANUAL
                self.remember(position)
            return {'ok': True, 'handed': [p.position_id for p in owned]}
        if action == 'close':
            failed = []
            for position in owned:
                answer = self.algo_exit(pair, position.position_id,
                                        'the Algo was turned off')
                if not answer.get('ok'):
                    failed.append(answer.get('reason') or 'refused')
            if failed:
                # Still ON, still managing: a position the close could
                # not take off must not also lose its manager.
                return {'ok': False,
                        'reason': 'could not close the Algo\'s position — '
                                  + '; '.join(failed) + '. The Algo stays on.'}
            return {'ok': True}
        return {'ok': False, 'choose': ['close', 'manual'],
                'positions': [p.position_id for p in owned],
                'reason': f'the Algo holds {len(owned)} position(s) on '
                          f'{pair.key} — close it now, or hand it to manual'}

    def algo_enter(self, pair, side, quantity):
        """A LIVE Algo entry: MARKET both legs, through the executor a
        manual MARKET click uses, with the price the decision was taken
        at as the slippage guard. Tagged ALGO and remembered."""
        with self.lock:
            md = self.market.get(pair.key)
            side = SpreadSide(getattr(side, 'value', side))
            level = executable_spread(md, side) if md else None
            result = self.enter(pair, side, md, quantity, level, 'algo')
            if result.ok and result.working:
                return {'ok': True, 'working': result.working,
                        'reason': result.reason}
            if result.ok and result.position:
                result.position.source = ALGO_SOURCE
                self.remember(self.book.add_position(result.position))
                return {'ok': True,
                        'position_id': result.position.position_id}
            if self.store is not None and result.reason:
                self.store.event('refused', pair.key, reason=result.reason,
                                 side=side.value, level=level, algo=True,
                                 naked=result.naked)
            return {'ok': False, 'reason': result.reason or 'not filled'}

    def enter(self, pair, side, md, quantity, level, source):
        """Put a spread on. The W3 maker fill where it is on (the OKX
        default: both legs rest, the position arrives in `_maker_done`),
        otherwise Mirror's both-legs-at-market crossing."""
        if self.maker.enabled(pair):
            return self.maker.start(pair, side, md, quantity, level, source)
        return self.executor.market_entry(pair, side, md, quantity, level)

    def _maker_done(self, job, result):
        """A maker fill ended: book it, or say why not."""
        with self.lock:
            if result.ok and result.position:
                position = result.position
                if job.source == 'algo':
                    position.source = ALGO_SOURCE
                if job.level is not None:
                    position.entry_slippage = slippage(
                        job.level, position.entry_spread, job.side)
                position = self.book.add_position(position)
                self.remember(position)
                if job.source == 'algo':
                    self.algos.adopt(job.pair_key, position.position_id)
                return
            if self.store is not None and result.reason:
                self.store.event('refused', job.pair_key,
                                 reason=f'maker fill: {result.reason}',
                                 side=job.side.value, level=job.level,
                                 algo=job.source == 'algo',
                                 naked=result.naked)
            if result.naked:
                logging.critical('%s: maker fill left a leg ON: %s',
                                 job.pair_key, result.naked)

    def algo_exit(self, pair, position_id, reason):
        """A LIVE Algo exit: close one position, both legs, by TICKET."""
        with self.lock:
            position = self.book.position(position_id)
            if position is None or not position.is_open:
                return {'ok': True, 'reason': 'already closed'}
            md = self.market.get(pair.key)
            if reason == 'PROFIT_TARGET' and \
                    self.config.get('RECHECK_TAKE_PROFIT', True):
                # A TAKE-PROFIT, and only a take-profit, is confirmed on
                # prices read NOW. Live, one poll showed oil's closing
                # spread 0.16 past the target; the close filled 0.235
                # back from it, and a +$3.90 target banked +$0.10. A
                # stop, a cutoff, a kill and every manual close go out
                # unconfirmed and at once - waiting there adds risk.
                # Waiting here costs at most the opportunity: the stop
                # still stands while the target is unconfirmed.
                fresh, unconfirmed = self._confirm_take_profit(
                    pair, position, md)
                if unconfirmed:
                    logging.info('[ALGO %s] take-profit on %s not sent: %s',
                                 pair.key, position_id, unconfirmed)
                    if self.store is not None:
                        self.store.event('algo_tp_unconfirmed', pair.key,
                                         position_id=position_id,
                                         reason=unconfirmed)
                    return {'ok': False, 'held': True,
                            'reason': unconfirmed}
                md = fresh
            result = self.executor.close_position(
                pair, position, md,
                reason=f'Algo: {algo_module.EXIT_WORDS.get(reason, reason)}')
            self.remember(position)
            return {'ok': bool(result.get('ok')),
                    'reason': result.get('reason') or result.get('error')}

    def _confirm_take_profit(self, pair, position, md):
        """(fresh market, None) when both legs, read again now, still
        put the closing price at or past this position's take-profit;
        (None, why) when they do not, or could not be read."""
        rows = {r['position_id']: r for r in self._algo_positions(pair, md)
                if md} if md else {}
        tp = (rows.get(position.position_id) or {}).get('tp')
        if tp is None:
            return None, 'its take-profit could not be priced'
        fresh, why = self.quoter._fresh_market(pair, md)
        if fresh is None:
            return None, f'the prices could not be read again ({why})'
        closing = executable_spread(fresh, position.side, closing=True)
        reached = closing is not None and (
            closing >= tp - 1e-9 if position.side is SpreadSide.BUY
            else closing <= tp + 1e-9)
        if not reached:
            return None, (f'the fresh closing price {closing:.4f} is short '
                          f'of the take-profit {tp:.4f} - the poll that saw '
                          f'it had a price that is not there now'
                          if closing is not None else
                          'the fresh prices make no closing price')
        return fresh, None

    def _realized_of(self, position_id):
        position = self.book.position(position_id)
        return None if position is None else position.realized_pnl

    def _offset_of(self, account):
        """One broker's measured offset from UTC, or None."""
        self.broker_offset()           # measures each account when due
        cached = self._offsets.get(account)
        return cached[1] if cached else None

    # -- the Analysis tab's trackers ------------------------------------------

    #: How often a moving extreme, shadow or counter is written. Each is
    #: also written at once on anything that matters: a close, a
    #: milestone, a touch.
    ANALYSIS_SAVE_SEC = 10.0

    def _track(self, pair, md, body):
        """Feed the Analysis tab. Observes only — it never decides or
        sends anything, and a failure here never stops the poll."""
        try:
            now = self.clock()
            settings = pair.exit_settings(self.config.settings)
            self._track_positions(pair, md, settings, now)
            self._track_shadows(pair, md, now)
            # ONE band reading per pass, shared by the counters and the
            # zone track, so the two can never disagree about what z
            # was. The Algo's own numbers when its Algo is on; the
            # analysis band's when it is off.
            reading = self._band_reading(pair, md, body, now)
            self._track_excursions(pair, md, reading, now)
            self._track_signals(pair, md, body, reading, settings, now)
        except Exception as e:
            logging.exception('[ANALYSIS %s] skipped this pass: %s',
                              pair.key, e)

    def _track_positions(self, pair, md, settings, now):
        open_ids = set()
        # The levels every open position is being JUDGED against on
        # this pass, kept ON the position. Computed here rather than
        # taken from the Algo's body because the body is None for a
        # ladder whose Algo is off - and a trade closed by hand still
        # has to be able to say what its stop was.
        levels = self._level_marks(pair, md)
        for position in self.book.positions(pair.key):
            open_ids.add(position.position_id)
            _gross, net, _closing = mark_position(position, md, settings)
            mark = levels.get(position.position_id)
            if mark:
                position.exit_levels = mark
            if analysis.observe_extremes(position, net, now):
                last = self._extremes_saved.get(position.position_id)
                if last is None or now - last >= self.ANALYSIS_SAVE_SEC:
                    self._extremes_saved[position.position_id] = now
                    self.remember(position)
        before = self._open_ids.get(pair.key)
        self._open_ids[pair.key] = open_ids
        for position_id in (before or set()) - open_ids:
            self._extremes_saved.pop(position_id, None)
            position = self.book.position(position_id)
            if position is None or position.is_open:
                continue
            self.remember(position)          # its final extremes
            self._arm_shadow(pair, position, settings, now)

    #: The fields of an Algo position row that are kept ON the position
    #: as its exit levels. Only the numbers the Analysis tab words an
    #: exit from - not the whole row, which carries this poll's mark and
    #: would be rewritten on disk every ten seconds for no reason.
    LEVEL_MARKS = ('break_even', 'tp', 'sl', 'tp_money', 'sl_money',
                   'stop_mode', 'target_mode', 'atr_stop_mult',
                   'atr_target_mult', 'tp_pct', 'stop_loss_pct',
                   'entry_margin', 'entry_atr', 'stop_points',
                   'spread_units')

    def _level_marks(self, pair, md):
        """{position_id: the levels it is judged against}, or {}.

        Never raises and never decides anything: a ladder that cannot
        be priced simply keeps the levels it last had, which are the
        ones its exit was decided on.
        """
        if md is None:
            return {}
        try:
            rows = self._algo_positions(pair, md)
        except Exception as e:
            logging.debug('[ANALYSIS %s] no exit levels this pass: %s',
                          pair.key, e)
            return {}
        marks = {}
        for row in rows:
            mark = {name: row.get(name) for name in self.LEVEL_MARKS}
            if any(value is not None for value in mark.values()):
                marks[row['position_id']] = mark
        return marks

    def _arm_shadow(self, pair, position, settings, now):
        fees = costs.mark_fees(
            position.leg_a.volume if position.leg_a else 0.0,
            position.leg_b.volume if position.leg_b else 0.0, settings)
        pct = settings.get('TP_TARGET_PCT_OF_MARGIN')
        target = None
        if pct and position.entry_margin:
            target = (float(pct) / 100.0 * float(position.entry_margin)
                      * float(position.quantity or 1.0))
        watch = analysis.arm_shadow(position, fees, target, now)
        if watch is None:
            return
        self._shadow_list().append(watch)
        self._save_shadow(watch, now)

    def _shadow_list(self):
        if self._shadows is None:
            self._shadows = []
            if self.store is not None:
                try:
                    self._shadows = self.store.shadows(active_only=True)
                except Exception as e:
                    logging.error('could not read the shadows: %s', e)
        return self._shadows

    def _save_shadow(self, watch, now):
        self._shadow_saved[watch['position_id']] = now
        if self.store is not None:
            try:
                self.store.save_shadow(watch)
            except Exception as e:
                logging.error('could not save a shadow: %s', e)

    def _track_shadows(self, pair, md, now):
        watches = self._shadow_list()
        for watch in list(watches):
            if watch.get('pair_key') != pair.key:
                continue
            milestones = (watch['reverted_be'], watch['reverted_target'])
            closing = executable_spread(md, SpreadSide(watch['side']),
                                        closing=True) if md else None
            if not analysis.update_shadow(watch, closing, now):
                continue
            last = self._shadow_saved.get(watch['position_id'])
            if watch['done'] or milestones != (watch['reverted_be'],
                                               watch['reverted_target']) \
                    or last is None or now - last >= self.ANALYSIS_SAVE_SEC:
                self._save_shadow(watch, now)
            if watch['done']:
                watches.remove(watch)
                self._shadow_saved.pop(watch['position_id'], None)

    def _excursion(self, key, now):
        tracker = self._excursions.get(key)
        if tracker is None:
            saved = None
            if self.store is not None:
                try:
                    saved = self.store.excursions().get(key)
                except Exception as e:
                    logging.error('could not read the excursions: %s', e)
            tracker = self._excursions[key] = analysis.ZExcursions(saved, now)
        return tracker

    def _band(self, pair):
        """This ladder's ANALYSIS band: the Algo's candles and the Algo's
        arithmetic, kept whether or not its Algo is on.

        Seeded from the saved candles — the same table the Algo fills,
        keyed the same way — so after a restart it measures at once
        instead of waiting `length` candles for a band it already had.
        A new timeframe, length or beta is a different series and starts
        again, exactly as the Algo's does.
        """
        signature = AlgoDesk._signature(pair)
        held = self._bands.get(pair.key)
        if held is not None and held[0] == signature:
            return held[1]
        params = algo_module.clean_params(getattr(pair, 'algo_params', None))
        anchor = bands.round_offset(self._offset_of(pair.account_a)) or 0.0
        candles = bands.SpreadCandles(params['timeframe_min'] * 60.0,
                                      params['length'], anchor)
        if self.store is not None:
            try:
                candles.seed(self.store.analysis_candles(
                    pair.key, params['timeframe_min'] * 60.0,
                    pair.hedge_ratio or 1.0,
                    limit=params['length'] * bands.KEEP_MULTIPLE))
            except Exception as e:
                logging.error('[ANALYSIS %s] saved candles unreadable: %s',
                              pair.key, e)
        self._bands[pair.key] = (signature, candles)
        return candles

    def _band_reading(self, pair, md, body, now):
        """One z for this pass, and where it came from.

        The Algo's own mean and sigma when its Algo is on, so the tab
        and the desk can never quote different numbers for the same
        moment; the analysis band's when it is off — which is every
        ladder after every restart, LIVE being off at every start.

        None when there is nothing to measure: no price, a print the
        jump guard is holding back (one bad close moves sigma for N
        candles), or a band that is not full yet.
        """
        if not md or md.get('mid_spread') is None or md.get('jump_reason'):
            return None
        candles = self._band(pair)
        closed = candles.observe(now, md['mid_spread'])
        if closed is not None and self.store is not None:
            try:
                # ITS OWN TABLE, never `algo_candles` — see
                # `Store.save_analysis_candles`. A reading must not be
                # able to change what the Algo decides.
                self.store.save_analysis_candles(
                    pair.key, candles.timeframe_sec,
                    pair.hedge_ratio or 1.0, [closed])
            except Exception as e:
                logging.error('[ANALYSIS %s] could not save a candle: %s',
                              pair.key, e)
        stats = candles.stats()
        mean, sigma, source = None, None, 'analysis'
        if body and body.get('mean') is not None and body.get('sigma'):
            mean, sigma, source = body['mean'], body['sigma'], 'algo'
        elif stats.get('ready'):
            mean, sigma = stats['mean'], stats['sigma']
        if mean is None or not sigma:
            return {'z': None, 'mean': mean, 'sigma': sigma, 'stats': stats,
                    'source': source, 'candles': candles}
        return {'z': (md['mid_spread'] - mean) / sigma, 'mean': mean,
                'sigma': sigma, 'stats': stats, 'source': source,
                'candles': candles}

    def _track_excursions(self, pair, md, reading, now):
        """The ±2 / ±3 sigma counters. Fed from the shared reading, so
        they count with the Algo off as well as on."""
        if not reading or reading.get('z') is None:
            return
        tracker = self._excursion(pair.key, now)
        what = tracker.tally(reading['z'], now)
        last = self._excursions_saved.get(pair.key)
        if what == 'event' or (what and (
                last is None or now - last >= self.ANALYSIS_SAVE_SEC)):
            self._save_excursions(pair.key, tracker, now)

    def _save_excursions(self, key, tracker, now):
        self._excursions_saved[key] = now
        if self.store is not None:
            try:
                self.store.save_excursions(key, tracker.to_dict())
            except Exception as e:
                logging.error('could not save the excursions: %s', e)

    #: How long a hypothetical trade is watched forward before it is
    #: called EXPIRED. A mean reversion that has not happened in a day
    #: is not the trade that was signalled.
    SIGNAL_WATCH_HOURS = 24.0

    def broker_day(self):
        """Today on the BROKER's clock, as YYYY-MM-DD.

        The same day the Algo counts its trades over (`_algo_gates`),
        so "5 Nov" on the Analysis tab and "5 Nov" in the day limits
        are one day and not two. None where the offset is unmeasured —
        and unmeasured is not "use this machine's date", which would
        file a row under a day it did not happen on.
        """
        now = self.session_clock.broker_now()
        return None if now is None else now.date().isoformat()

    def _signal_store(self):
        """This process's live signals, per ladder, reopened from the
        database at first use so a restart keeps watching what it was
        watching."""
        if self._signals is None:
            self._signals = {}
            self._arming = {}
            if self.store is not None:
                try:
                    for row in self.store.open_signals():
                        self._signals.setdefault(row['pair_key'],
                                                 []).append(row)
                    self._arming = self.store.signal_arming()
                except Exception as e:
                    logging.error('could not reopen the signals: %s', e)
        return self._signals

    def _shadow_for(self, pair, now):
        """This ladder's shadow Algo, restored from what was saved, with
        the stretches that were armed when the process stopped."""
        self._signal_store()
        shadow = self._shadow_algos.get(pair.key)
        if shadow is None:
            saved = (self._arming.get(pair.key) or {}).get('shadow')
            shadow = ShadowAlgo(getattr(pair, 'algo_params', None), saved,
                                now=now)
            self._shadow_algos[pair.key] = shadow
            self._episodes[pair.key] = {
                row['side']: {'row': row}
                for row in self._signals.get(pair.key, [])
                if row['state'] == signals.ARMED}
        return shadow

    def _save_shadow_algo(self, pair_key, now, force=False):
        """The shadow's arming, cooldown, day and warm-up, so a restart
        neither re-arms a spread that has not moved nor forgets the day's
        count. Every ten seconds for the warm-up; at once on a decision."""
        if self.store is None:
            return
        shadow = self._shadow_algos.get(pair_key)
        if shadow is None:
            return
        last = self._shadow_algo_saved.get(pair_key)
        if not force and last is not None and \
                now - last < self.ANALYSIS_SAVE_SEC:
            return
        self._shadow_algo_saved[pair_key] = now
        body = {'shadow': shadow.to_dict(now)}
        self._arming[pair_key] = body
        try:
            self.store.save_signal_arming(pair_key, body)
        except Exception as e:
            logging.error('could not save the shadow Algo: %s', e)

    def _write_signal(self, row):
        if self.store is None:
            return row
        try:
            row['id'] = self.store.save_signal(row)
        except Exception as e:
            logging.error('could not record the signal: %s', e)
        return row

    @staticmethod
    def _shadow_ref(row):
        """The id the shadow knows an open row's trade by. Rebuilt from
        the row itself, so it survives a restart without a column."""
        return f"sig:{row['side']}:{float(row['entered_at']):.3f}"

    def _shadow_position(self, pair, md, row, params, settings):
        """An open journal row as the Algo reads a position - priced on
        THIS pass by `_position_levels`, exactly as a live position is,
        with the margin and ATR it froze at entry. The levels are kept on
        the row, so the journal shows what the Algo is judging it on."""
        trade = SimpleNamespace(side=SpreadSide(row['side']),
                                entry_spread=row.get('entry'),
                                quantity=row.get('quantity') or 1.0,
                                spread_units=row.get('units'))
        lv = self._position_levels(pair, md, trade, params, settings,
                                   row.get('entry_margin'),
                                   row.get('entry_atr'))
        row['break_even'], row['tp'], row['sl'] = \
            lv['break_even'], lv['tp'], lv['sl']
        closing = self._closing_spread(md, row['side'])
        return {'position_id': self._shadow_ref(row), 'side': row['side'],
                'entry_spread': row.get('entry'),
                'opened_at': row.get('entered_at'),
                'break_even': row['break_even'],
                'tp': row['tp'], 'sl': row['sl'],
                'net_pnl': signals.net_at(row, closing)}

    def _track_signals(self, pair, md, body, reading, settings, now):
        """Journal what the Algo decides - with the Algo on or off.

        THE ALGO'S OWN CODE DECIDES. Every row here is a decision of
        `shadow.ShadowAlgo`: the live Algo's `AlgoSignal`, fed this
        poll's market, the live desk's gates and the same filters, with
        the journal's open rows as its positions. Nothing is sent.

        This replaced a COPY of the Algo's rules that did not match the
        Algo, and the differences were trades: one z off the mid for
        both sides where the Algo reads each side's executable price; no
        edge filter while the Algo was on; a new trade on every signal
        where the Algo holds one; trading through a stale feed. On a
        live desk it showed dozens of trades in an afternoon in which
        the real Algo took none.

        A STRETCH is one row: from the moment the Algo is engaged on a
        side (armed, or confirming) until it lets go. It ends ENTERED,
        BLOCKED - held back, in the Algo's words - or NO_ENTRY, never
        confirmed. An entered row is closed by the Algo's own exit.
        """
        if not md:
            return []
        live = self._signal_store().setdefault(pair.key, [])
        shadow = self._shadow_for(pair, now)
        params = algo_module.clean_params(getattr(pair, 'algo_params', None))
        shadow.retune(params)
        # ONE BAND. The running Algo's own candles when it is on, so the
        # journal and the desk read the same z; the analysis band when it
        # is off - fed from the same MT5 bars, so they converge.
        candles = self.algos.candles(pair.key) if body is not None else None
        if candles is None:
            candles = (reading or {}).get('candles') or self._band(pair)
        closed = [candles.closed[b] for b in sorted(candles.closed)]
        stats = candles.stats()
        gates = self._algo_gates(pair, md)
        if candles is not self.algos.candles(pair.key):
            gates['levels'] = self._levels_gate(
                pair, md, atr_for=lambda n: algofilters.atr(closed, n))
        if body is not None and body.get('warmup'):
            # The running Algo's warm-up: the two cannot disagree about
            # whether the feed has been watched.
            gates['warmup'] = body['warmup']
        opened = [r for r in live if r['state'] == signals.OPEN
                  and r.get('entered_at') is not None]
        positions = [self._shadow_position(pair, md, r, params, settings)
                     for r in opened]
        decided = shadow.step(now, md, stats, candles.closes(), gates,
                              gates.get('cost'), positions)

        # The chart: this poll's spread, and the z the Algo's own code
        # read off it.
        self.tape.observe(pair.key, now, md, decided, params)

        day = self.broker_day()
        mode = self.algos.mode(pair.key) or 'OFF'
        episodes = self._episodes.setdefault(pair.key, {})
        z_of = {'SELL': decided.get('z_sell'), 'BUY': decided.get('z_buy')}
        armed = decided.get('armed') or {}
        streak = decided.get('streak') or {}
        confirm = params['confirm_ticks']
        changed, new_rows = False, []

        def stretch(side):
            """The row for the stretch this side is in, opening one."""
            nonlocal changed
            episode = episodes.get(side)
            if episode is None:
                row = signals.new_signal(
                    pair.key, signals.ALGO, side, now,
                    z_of[side] if z_of[side] is not None else 0.0, params,
                    broker_day=day, threshold=params['entry_z'])
                row['algo_mode'] = mode
                row.update(self._leg_identity(pair))
                # The edge filter's reading as the signal is recorded.
                # Overwritten by the one it is ENTERED or held back at,
                # which is the reading that decided it.
                edge = (decided.get('edge_by_side') or {}).get(side) or {}
                row['edge_ratio'] = edge.get('ratio')
                row['edge_required'] = edge.get('required')
                live.append(self._write_signal(row))
                new_rows.append(row)
                episode = episodes[side] = {'row': row}
                changed = True
            return episode

        # 1. The Algo's own exits, on the trades it is in.
        by_ref = {self._shadow_ref(r): r for r in opened}
        for intent in decided['intents']:
            if intent['action'] == 'ENTER':
                continue
            row = by_ref.get(intent.get('position_id'))
            if row is None:
                continue
            signals.close(row, now, intent.get('reason'),
                          intent.get('spread'), intent.get('z'))
            shadow.scored(row.get('net_pnl'))
            self._write_signal(row)
            changed = True

        # 2. Each side the Algo is engaged on is one stretch.
        for side in ('SELL', 'BUY'):
            engaged = bool(armed.get(side)) or (streak.get(side) or 0) > 0
            if engaged:
                episode = stretch(side)
                row = episode.get('row')
                if row is None or row['state'] != signals.ARMED:
                    continue
                if decided.get('blocked_side') == side:
                    if row.get('block_reason') != decided.get('blocked'):
                        signals.note_block(row, decided.get('blocked'),
                                           decided.get('blocked_edge'))
                        self._write_signal(row)
                        changed = True
                elif decided.get('state') in ('IN_POSITION', 'EXIT') and \
                        (streak.get(side) or 0) >= confirm and \
                        not row.get('block_reason'):
                    # Confirmed while the Algo was already in a trade. It
                    # holds one at a time, so this one is not taken.
                    signals.note_block(row, 'one trade at a time - the '
                                            'Algo was already in a trade')
                    self._write_signal(row)
                    changed = True

        # 3. The Algo's entries.
        for intent in decided['intents']:
            if intent['action'] != 'ENTER':
                continue
            side = intent['side']
            episode = stretch(side)
            row = episode.get('row')
            if row is None or row['state'] != signals.ARMED:
                continue
            row['block_reason'] = None        # it cleared: the entry counts
            # Entered at the price the Algo's intent names - its own
            # executable side - and sized as its order would be. Margin
            # and ATR are FROZEN here, as a live position freezes them,
            # and the levels come from the same arithmetic a live
            # position's do (`_position_levels`).
            qty = params['algo_qty']
            one = sizing.spread_units(pair.clip_lots_b,
                                      (pair.meta_b or {}).get('contract_size'))
            side_params = algo_module.for_side(params, side)
            row['entry_margin'] = (self.margin_detail(pair) or {}).get('money')
            row['entry_atr'] = (
                algofilters.atr(closed, side_params['atr_period'])
                if 'ATR' in (side_params['stop_mode'],
                             side_params['target_mode']) else None)
            trade = SimpleNamespace(side=SpreadSide(side),
                                    entry_spread=intent.get('spread'),
                                    quantity=qty,
                                    spread_units=(one * qty) if one else None)
            lv = self._position_levels(pair, md, trade, params, settings,
                                       row['entry_margin'], row['entry_atr'])
            signals.enter(row, now, intent.get('z'),
                          {'entry': intent.get('spread'),
                           'break_even': lv['break_even'],
                           'tp': lv['tp'], 'sl': lv['sl']},
                          trade.spread_units, self._entry_legs(md, side),
                          qty)
            edge = intent.get('edge') or {}
            row['edge_ratio'] = edge.get('ratio')
            row['edge_required'] = edge.get('required')
            self._write_signal(row)
            # The arming is spent by the entry it gave; the stretch is
            # over for the journal even while the spread sits in it.
            episode['row'] = None
            changed = True

        # 4. A stretch the Algo has let go of without entering.
        for side in ('SELL', 'BUY'):
            engaged = bool(armed.get(side)) or (streak.get(side) or 0) > 0
            episode = episodes.get(side)
            if engaged or episode is None:
                continue
            row = episode.get('row')
            del episodes[side]
            if row is None or row['state'] != signals.ARMED:
                continue
            signals.settle_unentered(row, now, self._why_not_entered(params))
            self._write_signal(row)
            changed = True

        # 5. Mark what is open: its best, its worst, whether it came back.
        for row in opened:
            if row['state'] != signals.OPEN:
                continue
            closing = self._closing_spread(md, row['side'])
            # The z of the price it would close at - the Algo's own
            # reading for that side.
            z_close = z_of['BUY'] if row['side'] == 'SELL' else z_of['SELL']
            if signals.mark(row, closing, z_close, now):
                self._write_signal(row)
                changed = True

        self._signals[pair.key] = [r for r in live
                                   if r['state'] != signals.CLOSED]
        self._save_shadow_algo(pair.key, now, force=changed)
        return new_rows

    def _leg_identity(self, pair):
        """Each leg's account, by the name it has on the Exchanges page,
        and the broker that account is at - the terminal's own word for
        it (its company, else its server), read live. None, not a guess,
        when the terminal could not be read."""
        out = {}
        for leg, account in (('a', pair.account_a), ('b', pair.account_b)):
            info = self.account_info(account) or {}
            if not isinstance(info, dict):
                info = getattr(info, '_asdict', lambda: {})()
            broker = info.get('company') or info.get('server')
            if not broker:
                configured = self.config.accounts.get(account)
                broker = getattr(configured, 'server', None)
            out[f'leg_{leg}_account'] = account
            out[f'leg_{leg}_broker'] = broker or None
        return out

    @staticmethod
    def _why_not_entered(params):
        """Why a stretch nothing refused was never entered, in the
        Algo's terms."""
        confirm = params['confirm_ticks']
        needs = (f'{confirm} fresh prices in a row' if confirm > 1
                 else 'a fresh price')
        if params['reentry_on']:
            back_at = params['entry_z'] - params['reentry_back']
            floor = back_at * (1.0 - params['reentry_window_pct'] / 100.0)
            return (f'came back through the window ({back_at:.2f}\u03c3 to '
                    f'{floor:.2f}\u03c3) before {needs} confirmed it')
        return (f'fell back inside {params["entry_z"]:.2f}\u03c3 before '
                f'{needs} confirmed it')

    @staticmethod
    def _closing_spread(md, side):
        """The spread a position on `side` would CLOSE at — the
        opposite executable side, the same convention the ladder, the
        mark and the slippage report use."""
        if not md or side not in ('BUY', 'SELL'):
            return None
        return executable_spread(md, SpreadSide(side), closing=True)

    @staticmethod
    def _entry_legs(md, side):
        """Each leg's executable price for an entry on `side`.

        The spread is `B - beta x A`: selling it sells B and buys A, so
        B crosses the bid and A the offer. Buying it is the mirror.
        Unreadable stays None - a leg priced at 0 reads as a fill.
        """
        if not md or side not in ('BUY', 'SELL'):
            return {'leg_a_side': None, 'leg_a_entry': None,
                    'leg_b_side': None, 'leg_b_entry': None}
        a_side = 'SELL' if side == 'BUY' else 'BUY'
        b_side = side
        return {
            'leg_a_side': a_side,
            'leg_a_entry': md.get('leg_a_bid' if a_side == 'SELL'
                                  else 'leg_a_ask'),
            'leg_b_side': b_side,
            'leg_b_entry': md.get('leg_b_bid' if b_side == 'SELL'
                                  else 'leg_b_ask')}

    def reset_excursions(self, pair_key=None):
        """Zero the z-score excursion counters (one ladder, or all)."""
        now = self.clock()
        keys = [pair_key] if pair_key else list(self.config.pairs)
        for key in keys:
            tracker = self._excursions[key] = analysis.ZExcursions(None, now)
            self._save_excursions(key, tracker, now)
        return {'ok': True, 'reset': keys}

    def _work_algo(self, pair, md):
        """Feed this pass's market to the ladder's Algo, if it is on.

        A ladder whose Algo is off is not touched at all — not a candle,
        not a lookup. A failure in here is logged and the poll goes on:
        a signal is never allowed to stop the loop that watches money.
        """
        if not self.algos.is_on(pair.key):
            return None
        try:
            return self.algos.observe(pair, md, self._algo_positions(pair, md),
                                      self._algo_gates(pair, md))
        except Exception as e:
            logging.exception('[ALGO %s] skipped this pass: %s', pair.key, e)
            return None

    def _position_levels(self, pair, md, position, ladder_params,
                         ladder_settings, margin, atr):
        """The break-even, take-profit and stop the Algo judges ONE
        position against, and the setting behind each.

        One function for the live Algo's real positions and for the
        signal journal's shadow trades, so the two are priced by the
        same arithmetic. They were priced by two once, and with "TP %
        of margin" at 0 the live position's target was break-even while
        the journal's was none at all - its trade never closed, and the
        shadow, holding it, refused the next signal.

        `margin` and `atr` are the ones frozen at entry; a position-like
        object needs `side`, `entry_spread`, `quantity`, `spread_units`.
        """
        nights = float(ladder_settings.get('BREAK_EVEN_NIGHTS', 0.0) or 0.0)
        # THIS position's side's exits: the ladder's, with the side's
        # own values over them where it has any.
        params = algo_module.for_side(ladder_params, position.side)
        settings = ladder_settings
        if params['tp_pct'] is not None:
            settings = dict(ladder_settings,
                            TP_TARGET_PCT_OF_MARGIN=params['tp_pct'])
        pct = settings.get('TP_TARGET_PCT_OF_MARGIN')
        levels = takeprofit.for_position(
            position, md, pair, settings, margin, nights=nights,
            carry_for=self.holding_carry(pair, position.side.value,
                                         nights)) or {}
        tp = levels.get('tp')
        if pct and not levels.get('target_points'):
            # A target was asked for and could not be priced (no
            # margin from the terminals yet). Break-even is not the
            # target, so no profit exit is signalled on it.
            tp = None
        be = levels.get('break_even')
        stop = self._algo_stop_points(
            pair, position.quantity or 1.0,
            takeprofit.one_spread_units(position), margin, params=params)
        if params['stop_mode'] == 'ATR':
            # A multiple of the ATR at entry; unmeasured stays
            # unmeasured - never a stop placed on nothing.
            stop = (params['atr_stop_mult'] * atr
                    if params['stop_loss_on'] and atr else None)
        if params['target_mode'] == 'ATR':
            tp = None if not atr or be is None else (
                be + params['atr_target_mult'] * atr
                if position.side is SpreadSide.BUY
                else be - params['atr_target_mult'] * atr)
        sl = None
        if stop is not None and be is not None:
            sl = be - stop if position.side is SpreadSide.BUY \
                else be + stop
        return {'break_even': be, 'tp': tp, 'sl': sl, 'stop': stop,
                'pct': pct, 'settings': settings, 'params': params}

    def _algo_positions(self, pair, md):
        """This ladder's open positions as the Algo reads them: each with
        the break-even and take-profit the Exit panel shows for it."""
        ladder_settings = pair.exit_settings(self.config.settings)
        nights = float(ladder_settings.get('BREAK_EVEN_NIGHTS', 0.0) or 0.0)
        ladder_params = algo_module.clean_params(pair.algo_params)
        rows = []
        for position in self.book.positions(pair.key):
            params = algo_module.for_side(ladder_params, position.side)
            margin = self.entry_margin(pair, position)
            atr = None
            if 'ATR' in (params['stop_mode'], params['target_mode']):
                atr = self.entry_atr(pair, position)
            lv = self._position_levels(pair, md, position, ladder_params,
                                       ladder_settings, margin, atr)
            settings, pct, stop = lv['settings'], lv['pct'], lv['stop']
            be, tp, sl = lv['break_even'], lv['tp'], lv['sl']
            _gross, net_pnl, _closing = mark_position(position, md, settings)
            # What each level is worth to the WHOLE position, net: break
            # even is after every cost, so the distance from it times k
            # is the net P&L closing there. Unpriced stays None.
            units = position.spread_units
            sign = 1.0 if position.side is SpreadSide.BUY else -1.0

            def worth(level):
                if level is None or be is None or not units:
                    return None
                return round(sign * (level - be) * units, 2)
            rows.append({'position_id': position.position_id,
                         'side': position.side.value,
                         'source': getattr(position, 'source', MANUAL),
                         'quantity': position.quantity,
                         'entry_spread': position.entry_spread,
                         'opened_at': position.opened_at,
                         'age_sec': self.clock() - (position.opened_at
                                                    or self.clock()),
                         'break_even': be, 'tp': tp, 'sl': sl,
                         'tp_money': worth(tp), 'sl_money': worth(sl),
                         'net_pnl': net_pnl, 'entry_atr': atr,
                         'stop_mode': params['stop_mode'],
                         'target_mode': params['target_mode'],
                         # THIS side's multiples, for the panel's note.
                         'atr_stop_mult': params['atr_stop_mult'],
                         'atr_target_mult': params['atr_target_mult'],
                         # ...and the settings each level came OUT of,
                         # so the Analysis tab can say which number in
                         # the settings put the stop where it was.
                         'tp_pct': pct,
                         'stop_loss_pct': (params['stop_loss_pct']
                                           if params['stop_loss_on']
                                           else None),
                         'entry_margin': margin,
                         'stop_points': stop,
                         'spread_units': units,
                         **self._leg_marks(position, md)})
        return rows

    def entry_margin(self, pair, position):
        """The margin per spread a position's TP and SL are a % of:
        read from the terminals the first time it is priced, then
        FROZEN on the position and saved, so the levels stay where they
        were set. Until the terminals can price it, None — never a
        guess."""
        if position.entry_margin is None:
            margin = (self.margin_detail(pair) or {}).get('money')
            if margin:
                position.entry_margin = float(margin)
                self.remember(position)
        return position.entry_margin

    def entry_atr(self, pair, position):
        """The ATR an open position's ATR stop and target are a multiple
        of: read from the Algo's candles the first time it is priced,
        then FROZEN on the position and saved. None until it can be
        read - never a guess."""
        if position.entry_atr is None:
            params = algo_module.clean_params(pair.algo_params)
            value = self.algos.atr(pair.key, params['atr_period'])
            if value:
                position.entry_atr = float(value)
                self.remember(position)
        return position.entry_atr

    @staticmethod
    def _leg_marks(position, md):
        """Each leg's fill and the price it would CLOSE at now.

        A leg bought closes on its bid, a leg sold on its offer — the
        same opposite-touch rule the spread's own mark follows.
        """
        out = {}
        for leg in ('a', 'b'):
            fill = getattr(position, 'leg_' + leg, None)
            entry = getattr(fill, 'price', None)
            side = getattr(getattr(fill, 'side', None), 'value', None)
            now = None
            if md:
                now = md.get(f'leg_{leg}_bid' if side == 'BUY'
                             else f'leg_{leg}_ask')
            out[f'leg_{leg}_side'] = side
            out[f'leg_{leg}_entry'] = entry
            out[f'leg_{leg}_now'] = now
        return out

    #: How far back a derived history reaches by default.
    BACKFILL_DAYS = 30

    def backfill_signals(self, pair_key=None, days=None):
        """DERIVE the signals of the last `days` from MT5's own bars.

        The live track only measures forward: it starts empty and fills
        from the moment it is deployed, so it can say nothing about a
        day that has already gone. This replays the ladder's own
        settings back over MT5's history through `backtest.run` — the
        same `algo.AlgoSignal` the live Algo runs, fed candles instead
        of ticks — and writes what it would have traded as signal rows
        stamped REPLAY.

        It is a reading, not a measurement, and the difference is on
        the screen: one look per candle cannot see a stop touched and
        recovered inside one, and the bid-ask is today's width held
        constant. Re-running replaces the derived rows rather than
        adding a second copy of every trade; live rows are never
        touched.

        Sends nothing. It is `algo_backtest` with its answer written
        down instead of drawn once.
        """
        days = max(1, min(int(days or self.BACKFILL_DAYS), 90))
        keys = [pair_key] if pair_key else list(self.config.pairs)
        out = {'ok': True, 'days': days, 'pairs': {}, 'written': 0,
               'caveats': [], 'held': {}}
        if self.store is None:
            return {'ok': False, 'reason': 'no database to write to'}
        for key in keys:
            answer = self.algo_backtest(key, days=days, compare=False)
            if not answer.get('ok'):
                out['pairs'][key] = {'ok': False,
                                     'reason': answer.get('reason')}
                continue
            pair = self.config.pairs[key]
            params = algo_module.clean_params(pair.algo_params)
            units = (self._algo_cost(pair).get('k') or 0.0) \
                * params['algo_qty']
            offset = self._offset_of(pair.account_a)
            # The accounts and brokers the replay was read from: MT5's
            # bars off these two terminals, as they are now.
            legs = self._leg_identity(pair)
            rows = []
            for trade in answer.get('trades') or ():
                opened = trade.get('opened_at')
                row = signals.from_replay(
                    trade, key, params,
                    broker_day=backtest._broker_day(opened, offset),
                    units=units or None)
                row.update(legs)
                rows.append(row)
            try:
                dropped = self.store.drop_replayed(key)
                for row in rows:
                    self.store.save_signal(row)
            except Exception as e:
                logging.error('[BACKFILL %s] could not write: %s', key, e)
                out['pairs'][key] = {'ok': False, 'reason': str(e)}
                continue
            # WHY nothing was derived is the whole answer when
            # nothing was: the replay ran, the Algo armed, and every
            # entry was refused for a reason `backtest.run` counted.
            # Dropping that dict left the screen saying '0 derived'
            # with no way to tell a filter from a broken replay.
            held = {str(reason): int(count) for reason, count
                    in (answer.get('held') or {}).items()}
            out['pairs'][key] = {'ok': True, 'derived': len(rows),
                                 'replaced': dropped,
                                 'candles': answer.get('summary', {}).get(
                                     'candles'),
                                 'held': held,
                                 'note': answer.get('note')}
            for reason, count in held.items():
                out['held'][reason] = out['held'].get(reason, 0) + count
            out['written'] += len(rows)
            out['caveats'] = answer.get('caveats') or out['caveats']
        return out

    #: The pair chart's candle sizes, each built from finer MT5 bars so a
    #: candle's high and low are the spread's own: (sub-bar minutes,
    #: sub-bars per candle).
    CHART_FRAMES = {15: (1, 15), 60: (5, 12), 240: (15, 16)}
    #: Candles drawn, and kept for the bands to warm up on before them.
    CHART_CANDLES = 120
    #: How long one ladder's candle history is reused. The newest candle
    #: moves on the screen meanwhile, from the live 10-second candles;
    #: this keeps thousands of bars off the trading connection.
    CHART_HISTORY_SEC = 300.0

    def chart_history(self, pair_key, timeframe_min=15):
        """One ladder's spread as CANDLES of `timeframe_min` (15, 60 or
        240), from MT5's own bars for both legs - full the moment the
        engine starts, and days long. Reads only; sends nothing.

        The bands are not here: the screen works them out over these
        closes with the Algo's own arithmetic (an EMA middle, the
        population sigma of the last `length` closes), so the newest
        candle's band moves with the live price."""
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'no pair {pair_key}'}
        try:
            timeframe_min = int(timeframe_min)
        except (TypeError, ValueError):
            timeframe_min = 15
        frame = self.CHART_FRAMES.get(timeframe_min)
        if frame is None:
            return {'ok': False, 'reason': f'no {timeframe_min}-minute chart '
                                           f'- 15, 60 or 240'}
        now = self.clock()
        cache = getattr(self, '_chart_history', None)
        if cache is None:
            cache = self._chart_history = {}
        held = cache.get((pair_key, timeframe_min))
        if held and now - held[0] < self.CHART_HISTORY_SEC:
            return self._history_summary(held[1])
        params = algo_module.clean_params(pair.algo_params)
        sub_min, per = frame
        count = (self.CHART_CANDLES + params['length'] * 5) * per
        leg_a = self.legs.get(pair.account_a)
        leg_b = self.legs.get(pair.account_b)
        if leg_a is None or leg_b is None:
            return {'ok': False, 'reason': 'a leg is not connected'}
        try:
            bars_a = leg_a.rates(pair.symbol_a, sub_min, count)
            bars_b = leg_b.rates(pair.symbol_b, sub_min, count)
        except Exception as e:
            return {'ok': False, 'reason': f'MT5 history unavailable ({e})'}
        if not bars_a or not bars_b:
            which = 'leg A' if not bars_a else 'leg B'
            return {'ok': False,
                    'reason': f'MT5 returned no {sub_min}-minute bars for '
                              f'{which} ({pair.symbol_a if not bars_a else pair.symbol_b})'}
        offset_a = self._offset_of(pair.account_a)
        offset_b = self._offset_of(pair.account_b)
        if offset_a is None or offset_b is None:
            return {'ok': False,
                    'reason': "a broker's clock is not measured yet, so its "
                              "bars cannot be placed in time"}
        anchor = bands.round_offset(offset_a) or 0.0
        candles = bands.spread_ohlc(
            bars_a, bars_b, pair.hedge_ratio, sub_min * 60.0,
            timeframe_min * 60.0, bands.round_offset(offset_a),
            bands.round_offset(offset_b), anchor=anchor)
        if not candles:
            return {'ok': False, 'reason': 'the two legs share no bars'}
        answer = {
            'ok': True, 'pair': pair_key, 'timeframe_min': timeframe_min,
            'length': params['length'], 'entry_z': params['entry_z'],
            'stop_z': params['stop_z'] if params['stop_z_on'] else None,
            'algo_timeframe_min': params['timeframe_min'],
            'anchor': anchor, 'at': now,
            'candles': [{k: (round(v, 6) if k != 't' else v)
                         for k, v in c.items()} for c in candles]}
        cache[(pair_key, timeframe_min)] = (now, answer)
        # Written to a file of its own, not returned through the command
        # results: those are rewritten on every click's answer, and a
        # history in them would make every click's answer heavier.
        from .chart import history_path
        tmp = history_path(self.status_path) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({f'{key}#{tf}': body
                       for (key, tf), (_, body) in cache.items()}, f)
        atomicfile.replace(tmp, history_path(self.status_path))
        return self._history_summary(answer)

    @staticmethod
    def _history_summary(answer):
        """What the command answers: that the history is written, not
        the history itself."""
        out = {key: answer.get(key) for key in ('ok', 'pair',
                                                'timeframe_min', 'at')}
        out['candles'] = len(answer.get('candles') or ())
        return out

    def algo_backtest(self, pair_key, days=5, compare=True):
        """Replay `days` of MT5's own 15-minute bars through this
        ladder's Algo, with its settings — and, beside it, the same run
        with re-entry and the trend filter off, so what they change is
        on the screen. Prices the trades exactly as the live Algo would:
        today's bid-ask, the same costs, break-even, target and stop.
        Sends nothing."""
        from . import backtest
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'no pair {pair_key}'}
        days = max(1, min(int(days or 5), 30))
        params = algo_module.clean_params(pair.algo_params)
        rows, note = self.algos.history(pair, days)
        if not rows:
            return {'ok': False, 'reason': note or 'no history from MT5'}
        md = self.market.get(pair_key)
        if not md or md.get('long_spread') is None \
                or md.get('short_spread') is None:
            return {'ok': False, 'reason': 'no live price to measure the '
                                           'bid-ask a trade would cross'}
        width = md['long_spread'] - md['short_spread']
        cost_in = self._algo_cost(pair)
        k, qty = cost_in.get('k'), params['algo_qty']
        settings = pair.exit_settings(self.config.settings)
        margin = (self.margin_detail(pair) or {}).get('money')
        terms = takeprofit.break_even_terms(pair, md, settings, qty, k, 0)
        def target_for(pct):
            if not pct:
                return 0.0
            if margin:
                return takeprofit.points(
                    float(pct) / 100.0 * float(margin) * float(qty), k, qty)
            return None            # asked for, and cannot be priced
        ladder_pct = settings.get('TP_TARGET_PCT_OF_MARGIN')
        levels = {'fee_points': terms.get('added_points') or 0.0,
                  'target_points': target_for(ladder_pct),
                  'stop_points': self._algo_stop_points(pair, qty, k,
                                                        margin)}
        # Each side's own, where the side has its own exits.
        levels['by_side'] = {}
        for side in ('SELL', 'BUY'):
            own = algo_module.for_side(params, side)
            levels['by_side'][side] = {
                'target_points': target_for(
                    ladder_pct if own['tp_pct'] is None else own['tp_pct']),
                'stop_points': self._algo_stop_points(pair, qty, k, margin,
                                                      params=own)}
        cutoff = PairSession(pair, self.config).cutoff
        offset = self._offset_of(pair.account_a)
        result = backtest.run(rows, params, width, cost_in, levels, offset,
                              cutoff)
        answer = {'ok': True, 'pair': pair_key, 'days': days,
                  'width': width, 'levels': levels, **result}
        if compare:
            plain = dict(params, reentry_on=False, trend_on=False)
            answer['without_protections'] = backtest.run(
                rows, plain, width, cost_in, levels, offset,
                cutoff)['summary']
        return answer

    def _algo_stop_points(self, pair, quantity, units, margin=None,
                          params=None):
        """The stop loss's distance from break-even, in spread points:
        `stop_loss_pct` of the margin `quantity` spreads tie up, through
        the one `k`. None when the stop is off or cannot be priced —
        never 0, which would put the stop AT break-even. `params` are a
        side's own (`algo.for_side`); None is the ladder's."""
        if params is None:
            params = algo_module.clean_params(pair.algo_params)
        if not params['stop_loss_on'] or not params['stop_loss_pct']:
            return None
        if margin is None:
            margin = (self.margin_detail(pair) or {}).get('money')
        if not margin:
            return None
        return takeprofit.points(
            params['stop_loss_pct'] / 100.0 * float(margin) * float(quantity),
            units, quantity)

    def _algo_gates(self, pair, md):
        """What can hold an Algo ENTRY back on this pass."""
        health = (md or {}).get('guard_reason')
        now = self.session_clock.broker_now()
        # THIS ladder's hours: the minutes to its own next close, and -
        # outside its session or in its break - why it may not enter.
        hours = PairSession(pair, self.config)
        cutoff_min = hours.minutes_to_close(now)
        session = hours.entry_block(now)
        # The day the Algo's limits are counted over: the broker's, as
        # the session is; this machine's while that is unmeasured.
        day = (now or datetime.now()).date().isoformat()
        # The Algo's OWN size, sized exactly as its order would be. A
        # size under either leg's minimum used to be found only when the
        # order was sent — refused inside the program, every time, with
        # nothing on the screen but an Algo that never traded.
        size = None
        if md is not None:
            qty = algo_module.clean_params(pair.algo_params)['algo_qty']
            size = (self.executor.size(pair, md, qty) or {}).get('reason')
        return {'health': health, 'cutoff_min': cutoff_min, 'day': day,
                'cost': self._algo_cost(pair), 'size': size,
                'session': session, 'levels': self._levels_gate(pair, md)}

    def _levels_gate(self, pair, md, atr_for=None):
        """The levels check for an entry, per SIDE when the sides have
        their own exits: {'SELL': reason, 'BUY': reason}. One reason (or
        None) when both sides would get the same answer. `atr_for`
        (period -> ATR) reads the ATR off other candles than the running
        Algo's - the signal journal's, with the Algo off."""
        sell = self._levels_check(pair, md, 'SELL', atr_for)
        buy = self._levels_check(pair, md, 'BUY', atr_for)
        return sell if sell == buy else {'SELL': sell, 'BUY': buy}

    def _levels_check(self, pair, md, side=None, atr_for=None):
        """Why the Algo's stop or target cannot be set for an entry NOW,
        in words, or None.

        An ATR level with no ATR yet is a level placed on nothing: the
        entry waits. And a stop closer than the spread's own bid-ask is
        one the trade opens already past - it is taken off at a loss the
        moment it is on (2 October: in and stopped out in the same
        second). Exits are never held by this. `side` reads that side's
        own exits; None is the ladder's.
        """
        params = algo_module.clean_params(pair.algo_params)
        if side is not None:
            params = algo_module.for_side(params, side)
        atr = None
        if 'ATR' in (params['stop_mode'], params['target_mode']):
            atr = (atr_for(params['atr_period']) if atr_for is not None
                   else self.algos.atr(pair.key, params['atr_period']))
            if not atr:
                return (f'levels: ATR({params["atr_period"]}) not measured '
                        f'yet - it needs {params["atr_period"] + 1} closed '
                        f'candles')
        if not params['stop_loss_on'] or md is None:
            return None
        if params['stop_mode'] == 'ATR':
            stop = params['atr_stop_mult'] * atr
        else:
            stop = self._algo_stop_points(
                pair, params['algo_qty'],
                sizing.spread_units(pair.clip_lots_b,
                                    (pair.meta_b or {}).get('contract_size')),
                params=params)
        width = None
        if md.get('long_spread') is not None and \
                md.get('short_spread') is not None:
            width = md['long_spread'] - md['short_spread']
        if stop is not None and width is not None and stop <= width:
            return (f'levels: the stop ({stop:.4f}) is inside the bid-ask '
                    f'({width:.4f}) - the trade would open already stopped')
        return None

    def _algo_cost(self, pair):
        """What one Algo trade costs besides the crossing, and `k`.

        The same terms break-even uses: commission on both legs, both
        ways, for the Algo's own qty, and the slippage budget. `k` is
        money per 1.00 of spread for ONE spread; the crossing is priced
        from the live touches by the filters themselves.
        """
        settings = pair.exit_settings(self.config.settings)
        qty = algo_module.clean_params(pair.algo_params)['algo_qty']
        allowance = settings.get('SLIPPAGE_ALLOWANCE')
        return {'k': sizing.spread_units(
                    pair.clip_lots_b, (pair.meta_b or {}).get('contract_size')),
                'commission': takeprofit.commission(pair, settings, qty),
                'slippage': (0.0 if allowance in (None, '')
                             else float(allowance) * float(qty))}

    def holding_carry(self, pair, direction, nights):
        """What holding one spread `nights` nights costs, in money.

        The same conversion the fair spread uses — and the same
        refusal: a swap MT5 reports in units this cannot read comes
        back as None with the reason, and break-even then says so
        rather than silently dropping the term.
        """
        if not nights:
            return {'money': 0.0, 'reason': None}
        return carry.carry_money(pair.meta_a, pair.meta_b, direction,
                                 pair.clip_lots_a, pair.clip_lots_b, nights,
                                 pair.swap_overrides())

    def leg_depth(self, account, symbol):
        """One leg's market depth, cached for a fraction of a second.

        A DOM is a round trip per leg per poll. Most CFD accounts
        publish none at all, and the broker remembers that refusal, so
        this costs nothing where there is nothing.
        """
        if not self.config.get('SHOW_DEPTH', True):
            return None
        ttl = float(self.config.get('DEPTH_TTL_SEC', 0.25))
        now = self.clock()
        cached = self._depth_cache.get((account, symbol))
        if cached and now - cached[0] < ttl:
            return cached[1]
        leg = self.legs.get(account)
        book = None
        if leg is not None and symbol:
            try:
                book = leg.depth(symbol)
            except Exception:                 # a leg mid-restart
                book = None
        self._depth_cache[(account, symbol)] = (now, book)
        return book

    def implied_depth(self, pair):
        """How many SPREADS each side of the two books can actually do.

        Empty when either broker publishes no depth: a size invented
        from one leg would show a hundred lots available on a spread
        that can do four, and it is a lie a trader would click on.
        """
        book_a = self.leg_depth(pair.account_a, pair.symbol_a)
        book_b = self.leg_depth(pair.account_b, pair.symbol_b)
        if not book_a or not book_b:
            return {'buy': {}, 'sell': {}, 'published': False}
        sizes = depth_book.implied(
            book_a, book_b, pair.hedge_ratio,
            clip_a=pair.clip_lots_a, clip_b=pair.clip_lots_b,
            increment=pair.effective_increment())
        sizes['published'] = True
        return sizes

    def leg_session(self, account, symbol):
        """One leg's own session O/H/L/V, cached: it is an IPC round
        trip for numbers that move slowly, and the ladder polls three
        times a second."""
        ttl = float(self.config.get('SESSION_STATS_TTL_SEC', 2.0))
        now = self.clock()
        cached = self._session_cache.get((account, symbol))
        if cached and now - cached[0] < ttl:
            return cached[1]
        leg = self.legs.get(account)
        stats = None
        if leg is not None and symbol:
            try:
                stats = leg.session_stats(symbol)
            except Exception:                 # a leg mid-restart
                stats = None
        self._session_cache[(account, symbol)] = (now, stats)
        return stats

    def _go_dark(self, key, pair, tick_a, tick_b):
        """A leg has no price at all. Say it, try to fix it, WATCH it.

        Missing is worse than stale and used to be treated as less: a
        stale pair is logged, auto-refreshed and worked; a dark pair was
        skipped in silence, orders and all. Whatever the cause - the
        terminal dropping a subscription, the broker, the LP, the line,
        the PC - the consequences here are identical and so is the
        answer.

        A close is never touched from here. `watch_dark` leaves closing
        groups alone: they rest nothing at the broker, and a guard must
        never prevent a close.
        """
        dark = [name for name, tick in (('A', tick_a), ('B', tick_b))
                if not tick]
        symbols = {'A': pair.symbol_a, 'B': pair.symbol_b}
        reason = ('no price for leg ' + ' and leg '.join(
            f'{leg} ({symbols[leg]})' for leg in dark) +
            ' — this pair is DARK: nothing is placed, and anything '
            'already resting is pulled')
        self._dark[key] = reason

        every = float(self.config.get('STALE_LOG_EVERY_SEC', 60.0))
        now = self.clock()
        if now - self._dark_logged.get(key, 0.0) >= every:
            self._dark_logged[key] = now
            logging.critical('%s: %s', key, reason)

        # Same self-repair the stale path gets. A dropped subscription
        # is the commonest cause and re-subscribing is what fixes it;
        # withholding that from the WORSE case made no sense.
        self._auto_refresh(key, None)

        # ...and the part that matters for money: a pending already at
        # the broker is still live and still fillable.
        self.quoter.watch_dark(pair, reason)

    def _auto_refresh(self, key, md):
        """Re-subscribe a stale pair by itself, now and then.

        Some terminals drop a symbol's subscription silently: the
        ladder ticks for a few seconds after a refresh and then goes
        quiet again, and pressing Feed brings it back every time. A
        machine that needs the same button pressed every twenty seconds
        should press it itself.

        Logged on every attempt. A feed being nursed along is a fact
        the operator needs — nursing it quietly would hide exactly the
        problem worth fixing at the broker or the terminal.
        """
        every = float(self.config.get('AUTO_REFRESH_STALE_SEC', 20.0) or 0)
        if not every:
            return
        now = self.clock()
        if now - self._auto_refreshed.get(key, 0.0) < every:
            return
        self._auto_refreshed[key] = now
        answer = self.refresh_feed(key)
        logging.warning('%s: stale — re-subscribed both legs automatically '
                        '(%s). If this repeats, the terminal is dropping the '
                        'subscription: add both symbols to Market Watch and '
                        'leave them there.', key, answer.get('reason'))

    def _log_stale(self, key, pair, md):
        """Say what a frozen leg actually looks like, once a minute.

        The two faults are indistinguishable on the screen and have
        different answers: a symbol that is NOT VISIBLE is not
        subscribed, and re-subscribing fixes it; a visible symbol whose
        broker stamp is not advancing is a terminal receiving nothing,
        and the answer is in MT5. Printing both, with the stamps, turns
        "it goes stale again" into a fact somebody can act on.
        """
        every = float(self.config.get('STALE_LOG_EVERY_SEC', 60.0))
        now = self.clock()
        if now - self._stale_logged.get(key, 0.0) < every:
            return
        self._stale_logged[key] = now
        for leg, symbol in (('a', pair.symbol_a), ('b', pair.symbol_b)):
            logging.warning(
                "%s leg %s (%s): visible=%s broker stamp=%s last change "
                "%.1fs ago", key, leg.upper(), symbol,
                md.get(f'leg_{leg}_visible'),
                md.get(f'leg_{leg}_tick_time'),
                md.get(f'leg_{leg}_quote_age_sec') or 0.0)

    def _observe_session(self, key, md, pair=None):
        """The session line above the ladder.

        Taken from the two LEGS' own session figures where the terminals
        publish them — H is `high B - beta x high A`, the same
        convention as every other price on this screen — and from our
        own mid series where they do not. Which of the two it is, is
        said on the strip: a borrowed number read as the exchange's is
        how a spread gets judged against a range nobody traded.

        Volume stays PER LEG, never added: one lot of gold and one lot
        of the future are not two lots of anything, and a combined
        figure would be a number with no unit.
        """
        spread = md.get('spread')
        session = self.session.get(key)
        if session is None:
            session = {'open': spread, 'high': spread, 'low': spread,
                       'volume': 0.0, 'ours': True}
            self.session[key] = session
        session['high'] = max(session['high'], spread)
        session['low'] = min(session['low'], spread)
        session['volume'] = sum(p['quantity']
                                for p in self.book.prints.get(key, ()))
        published = dict(session)

        if pair is not None:
            beta = float(pair.hedge_ratio or 1.0)
            stats_a = self.leg_session(pair.account_a, pair.symbol_a) or {}
            stats_b = self.leg_session(pair.account_b, pair.symbol_b) or {}

            def combined(field):
                first, second = stats_a.get(field), stats_b.get(field)
                if first is None or second is None:
                    return None
                return second - beta * first

            legs_high = combined('high')
            legs_low = combined('low')
            legs_open = combined('open')
            if legs_high is not None or legs_low is not None:
                published.update({
                    'high': legs_high, 'low': legs_low,
                    'open': legs_open if legs_open is not None
                    else session['open'],
                    'ours': False,
                })
            published['volume_a'] = stats_a.get('volume')
            published['volume_b'] = stats_b.get('volume')
            published['legs'] = {'a': stats_a or None, 'b': stats_b or None}

        md['session'] = published
        # Net change is always against OUR open — the one the trader has
        # been watching this process quote.
        md['net_change'] = (spread - session['open']
                            if session['open'] is not None else None)

    # -- what a click does ---------------------------------------------------

    def click(self, pair_key, side, level, quantity=None):
        """One click on one row. One click is ONE order.

        MARKET crosses both legs now with the clicked price as the
        slippage guard; LIMIT creates a synthetic working order at that
        level, backed by a real pending on the quoting leg. Three clicks
        at one price are three orders, individually cancellable, however
        they are aggregated at the broker.

        The Market Grid and the ladder both come through here, so a grid
        click and a ladder click at the same price are the same order.
        """
        with self.lock:
            answer = self._click(pair_key, side, level, quantity)
            self._log_click(pair_key, side, level, quantity, answer)
            return answer

    def _log_click(self, pair_key, side, level, quantity, answer):
        """One line per click: what was asked for, and what came of it.

        A placement used to write NOTHING. The ladder showed it, so
        nobody added a line - and then "which cell did he click, and was
        it LIMIT or MARKET?" could only be answered by reading the
        database and doing arithmetic on the slippage. The trader who
        was told his order filled where he never clicked waited a day
        for that answer.

        The INTENT is logged even when the click was refused, because a
        refusal is exactly the case where nothing else records what was
        asked for. Never raises: a click is not going to fail over its
        own log line.
        """
        try:
            pair = self.config.pairs.get(pair_key)
            mode = getattr(getattr(pair, 'order_type', None), 'value', '?')
            if quantity is None and pair is not None:
                quantity = pair.default_quantity
            outcome = 'refused' if answer.get('refused') else (
                'taken' if answer.get('ok') else 'failed')
            logging.info(
                'CLICK %s %s %s qty %s at %s -> %s%s', pair_key,
                getattr(side, 'value', side), mode,
                '?' if quantity is None else f'{float(quantity):g}',
                '?' if level is None else f'{float(level):g}',
                outcome,
                f" ({answer['reason']})" if answer.get('reason') else '')
        except Exception as e:                # never lose a click to a log
            logging.debug('could not log the click: %s', e)

    def _click(self, pair_key, side, level, quantity=None):
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'no pair {pair_key}'}
        # Algo or Manual, never both: while this ladder's Algo is LIVE a
        # NEW manual order is refused here, in words. Dry run and off
        # refuse nothing. Closes do not come through this path.
        refusal = self.algos.manual_order_refusal(pair_key)
        if refusal:
            return self._refuse(pair_key, side, level, refusal)
        shared = self.legs_share_an_account(pair)
        if shared and self.config.get('REFUSE_SHARED_ACCOUNT', False):
            # OFF by default. Two accounts on one terminal is usually a
            # misconfiguration, but "both legs on one account" is also
            # an ordinary spread, and which of the two it is, is the
            # trader's to say — so the screen says it loudly and this
            # refuses only when the desk has asked it to.
            return {'ok': False, 'refused': True,
                    'reason': (
                        f"'{pair.account_a}' and '{pair.account_b}' are "
                        f"both attached to MT5 login #{shared}, and "
                        f"REFUSE_SHARED_ACCOUNT is on.")}

        md = self.market.get(pair_key)
        quantity = pair.default_quantity if quantity is None else quantity

        if pair.order_type is OrderType.MARKET and \
                self._away_from_the_market(pair, side, md, level):
            # A ladder click away from the touch is an order to WORK,
            # not an order to refuse. In MARKET mode the trader is
            # saying "cross now" — but a buy under the offer cannot
            # cross now at any price, and the only honest readings are
            # "rest it here" or "do nothing". TT rests it, and so does
            # this; the toast says which it did, because a market click
            # that quietly became a working order is a surprise.
            armed, quantity, closed, failure = self._rest_reducing_orders(
                pair, side, level, quantity)
            if failure is not None:
                return self._refuse(pair_key, side, level, failure, closed)
            if closed and quantity <= 1e-9:
                return self._closed_at_market(closed, armed)
            if armed:
                # ONE CLICK, ONE INSTRUCTION, IN ORDER. Any remainder
                # rides on the LAST close armed and opens only once
                # that close has gone through. Resting it now gives it
                # a DIFFERENT ENGINE from the close — a pending at the
                # BROKER, filling on the broker's own tick stream,
                # against a level watched here at poll rate. Live
                # 2026-09-03 the pending filled and the close never saw
                # a qualifying tick, so a BUY 12 over a SELL 10 left the
                # trader +2 AND still -10.
                if quantity > 1e-9:
                    self.quoter.carry_remainder(armed[-1], quantity)
                return {'ok': True, 'order': armed[0].to_dict(),
                        'rested': True, 'reducing': True, 'closed': closed,
                        'remainder': quantity if quantity > 1e-9 else None,
                        'reason': f'{level:g} is away from the market — '
                                  f'resting there to CLOSE '
                                  f'{len(armed)} position(s)'
                                  + (f', then open {quantity:g}'
                                     if quantity > 1e-9 else '')}
            order = self.book.add_order(pair, side, level, quantity)
            self.quoter.group_for(pair, order)
            return {'ok': True, 'order': order.to_dict(), 'rested': True,
                    'reason': f'{level:g} is away from the market — '
                              f'resting here as a working order'}

        if pair.order_type is OrderType.MARKET:
            # REDUCE BEFORE OPENING. A market click the opposite way is
            # a cover, not a second position. Closing first (rather
            # than opening and closing after) is deliberate: it never
            # holds both sides at once, so it cannot be caught halfway
            # by a disconnect with double the exposure on.
            closed, quantity, failure = self._reduce_first(
                pair, side, quantity, md)
            if failure is not None:
                # DO NOT OPEN ON TOP OF A CLOSE THAT FAILED. The trader
                # clicked to cover; the cover did not happen. Opening
                # anyway leaves them with the position they wanted gone
                # AND a new one — and if the close half-executed, a
                # naked leg under both. Refuse, in the broker's own
                # words, and let them decide.
                if self.store is not None:
                    self.store.event('refused', pair_key, reason=failure,
                                     side=getattr(side, 'value', side),
                                     level=level)
                return {'ok': False, 'refused': True, 'closed': closed,
                        'reason': failure}
            if closed and quantity <= 1e-9:
                return {'ok': True, 'closed': closed, 'reduced': True,
                        'reason': f'closed {len(closed)} position(s) '
                                  f'oldest first'}
            result = self.enter(pair, side, md, quantity, level, 'manual')
            if result.ok and result.position:
                self.remember(self.book.add_position(result.position))
            elif self.store is not None and result.reason:
                self.store.event('refused', pair_key, reason=result.reason,
                                 side=getattr(side, 'value', side),
                                 level=level, naked=result.naked)
            return result.to_dict()

        refusal = self.executor.precheck(pair, side, md, quantity)
        if refusal is not None and md is None:
            # No price at all is a refusal either way. A guard reason is
            # NOT: a resting order simply waits for a quote it can rest
            # on (spec §8), which is what the quoter does.
            return {'ok': False, 'refused': True, 'reason': refusal}
        armed, quantity, closed, failure = self._rest_reducing_orders(
            pair, side, level, quantity)
        if failure is not None:
            return self._refuse(pair_key, side, level, failure, closed)
        if closed and quantity <= 1e-9:
            return self._closed_at_market(closed, armed)
        if armed:
            # Same rule as above: the remainder waits for the close.
            if quantity > 1e-9:
                self.quoter.carry_remainder(armed[-1], quantity)
            return {'ok': True, 'order': armed[0].to_dict(),
                    'reducing': True, 'closed': closed,
                    'remainder': quantity if quantity > 1e-9 else None,
                    'reason': f'resting at {level:g} to CLOSE '
                              f'{len(armed)} position(s)'
                              + (f', then open {quantity:g}'
                                 if quantity > 1e-9 else '')}
        order = self.book.add_order(pair, side, level, quantity)
        self.quoter.group_for(pair, order)
        return {'ok': True, 'order': order.to_dict(), 'closed': closed}

    def _working_order_refused(self, pair_key, reason, side, level):
        if self.store is not None:
            self.store.event('refused', pair_key, reason=reason, side=side,
                             level=level)

    def _refuse(self, pair_key, side, level, reason, closed=()):
        if self.store is not None:
            self.store.event('refused', pair_key, reason=reason,
                             side=getattr(side, 'value', side), level=level)
        return {'ok': False, 'refused': True, 'closed': list(closed),
                'reason': reason}

    def _closed_at_market(self, closed, armed):
        """A click that RESTED nothing because there was nothing to rest
        against — said out loud.

        A resting click that quietly went to market is exactly the
        surprise the other direction already guards against, and the
        trader has to be told which of the two happened.
        """
        reason = (f'closed {len(closed)} NAKED position(s) at market — the '
                  f'other leg was already gone, so there was nothing left '
                  f'to rest against')
        if armed:
            # Both happened: say so, or the toast reports half the click.
            reason += f', and rested a close for {len(armed)} more'
        answer = {'ok': True, 'closed': closed, 'reduced': True,
                  'at_market': True, 'reason': reason}
        if armed:
            answer['order'] = armed[0].to_dict()
        return answer

    def _reduce_first(self, pair, side, quantity, md, exclude=None):
        """Close what this click covers, and say what is left to open.

        Returns (closed position ids, quantity still to open). One
        implementation for both paths — a MARKET click closes before it
        opens, a resting order closes when it fills — because two
        implementations of "which tickets does this cover" is two
        answers to reconcile the day they disagree.

        A close is never withheld: `close_position` consults no guard,
        and neither does this.
        """
        if not self.config.get('CLOSE_FIRST', True):
            return [], quantity, None
        return book_module.reduce_first(
            self.book, self.executor, pair, side, quantity, md,
            exclude=exclude, on_closed=self.remember)

    def _rest_reducing_orders(self, pair, side, level, quantity):
        """Rest CLOSING orders for what this click covers.

        THE FIX FOR THE BUG THE DESK FOUND. The first version opened a
        position on the fill and closed the old one afterwards, so a
        click meant to flatten left a BRAND NEW position on: SELL 1
        became BUY 1 instead of flat, with two fresh tickets.

        A resting order that reduces must be a CLOSING order from the
        moment it is placed, not an opening one with a close bolted on
        after. `quoter.arm` already builds exactly that — it is what
        AutoRouting rests. It puts NOTHING at the broker: MT5 ignores
        `position` on a pending order, so a "closing" limit is an
        ordinary one and opens a second position on a hedging account.
        The level is held by the quoter, and when the market reaches it
        BOTH legs are closed by ticket. The result is FLAT.

        Returns (orders armed, quantity still to open, positions closed at
        market, failure).
        """
        if not self.config.get('CLOSE_FIRST', True):
            return [], quantity, [], None
        try:
            left = float(quantity)
        except (TypeError, ValueError):
            return [], quantity, [], None
        armed = []
        closed = []
        for position, take in self.book.positions_to_reduce(
                pair.key, side, left, reserve_armed=True):
            # THE LEG THIS WOULD REST ON MAY ALREADY BE FLAT.
            #
            # A resting close fires by TICKET. Once the broker no
            # longer has that ticket there is nothing for the level to
            # close, and arming one would leave the trader watching a
            # price that can never get them out.
            #
            # What is left is not a spread any more, it is one naked
            # leg, and a naked leg cannot wait at a price. It is closed
            # BY TICKET, at market, now — and a close is never withheld.
            if self._quoting_leg_is_gone(pair, position):
                answer = self.executor.close_position(
                    pair, position, self.market.get(pair.key),
                    reason='the other leg was already gone')
                if not answer.get('ok'):
                    return (armed, max(left, 0.0), closed,
                            book_module.close_failure(answer, position))
                self.remember(position)
                closed.append(position.position_id)
                # The WHOLE position went: a naked leg cannot be
                # part-closed at a level, so what came off is the
                # ticket, not the slice the click reached.
                left = sizing.tidy(left - float(position.quantity or 0.0))
                continue
            # A target may already be armed here by AUTOMATION, at ITS
            # level. The trader has just named a different one, and a
            # click that silently rested at somebody else's price is a
            # click that did not do what it said. Theirs wins, and the
            # old one is pulled rather than left to race it.
            #
            # AUTOMATION'S ONLY. This read every closing order on the
            # position, including ones the TRADER had placed — so a
            # second buy click at a second price pulled the first, and
            # the blue side could never hold more than one working
            # order while the red side held as many as were clicked.
            # Scaling out of a position at two prices is the ordinary
            # way a ladder is used, and it was impossible.
            existing = self.book.auto_armed_for(position.position_id)
            # ALREADY RESTING AT THIS LEVEL? LEAVE IT ALONE.
            #
            # Pulling a live pending and putting an identical one back
            # loses its place in the broker's QUEUE — the trader gives
            # up their position in the line for no change at all. This
            # is the ordinary case where AutoRouting's target and the
            # click agree, and it must be a no-op.
            at_this_level = [o for o in existing
                             if abs(o.level - level) < 1e-9]
            if at_this_level:
                # The one AT THIS LEVEL, not merely the first of them:
                # `existing[0]` reported an order resting somewhere else
                # as the one the click had just placed.
                armed.append(at_this_level[0])
                # What that order already covers, which is its own
                # size — not the whole position, when it is a part.
                left = sizing.tidy(left - float(at_this_level[0].quantity
                                                or take))
                continue
            if existing:
                # A DIFFERENT level: the trader has just named their
                # own, and a click that silently rested at somebody
                # else's price is a click that did not do what it said.
                # Theirs wins, and the old one is pulled rather than
                # left to race it.
                self.quoter.disarm_auto(
                    position.position_id,
                    f'the trader clicked {level:g} to close this instead')
            # CAN THE BROKER TRADE THIS PIECE AT ALL?
            #
            # Asked HERE, on the click, because this is the last moment
            # a refusal is useful. `take` is a share of an older ticket,
            # and a share does not have to be a tradable volume: 0.01
            # lots of a future whose step is 0.10 is not an order. An
            # exit armed at that size cannot ever fire, and the trader
            # would be watching a price that can never get them out -
            # which is the failure at the far end from closing too
            # much, and quieter.
            #
            # The refusal carries the broker's own volume step and the
            # size that WOULD work, because the trader's next move is to
            # click a bigger one.
            #
            # Stopping here rather than skipping to the next ticket is
            # the same rule `reduce_first` follows: the rest of the
            # queue is no longer the queue the trader would have
            # closed.
            usable, why = self.executor.can_part_close(pair, position, take)
            if not usable:
                return armed, max(left, 0.0), closed, why
            # AS FAR AS THE CLICK REACHES, not the whole ticket. A
            # BUY 100 over a 1,200 short rests a close for 100 of it
            # and opens NOTHING; before this it armed the small old
            # tickets, gave up at the big one, and opened the rest the
            # other way.
            order = self.quoter.arm(pair, position, level, take, auto=False)
            if order is None:
                break
            armed.append(order)
            left = sizing.tidy(left - take)
        # Tidied all the way down: what is left here is the REMAINDER
        # the click still owes, and it goes on the screen.
        return armed, max(left, 0.0), closed, None

    def _quoting_leg_is_gone(self, pair, position):
        """Is the leg a closing order would REST on already flat?

        False when it is still there, and false when the leg could not
        be READ: None is "unknown", never "flat" (spec §7). Acting on an
        unreadable leg would market-close a position on no evidence,
        which is the opposite mistake and a worse one.
        """
        leg = quoting_leg(pair)
        fill = position.leg_a if leg == 'a' else position.leg_b
        if fill is None or not fill.position_tickets:
            return False
        volume = self.executor.leg_open_volume(pair, leg, fill)
        if volume is None:
            return False
        return volume <= 1e-9

    def _away_from_the_market(self, pair, side, md, level):
        """Is this click at a price the market cannot fill right now?

        A BUY under the offer and a SELL over the bid are the two, and
        they are the ordinary way a ladder is used: click where you want
        to be, and wait there. Neither is an error, and refusing them
        was making the ladder's whole left-hand side dead.

        Unknown prices are NOT "away": with no market the executor's own
        refusal is the right answer, and it names what is missing.
        """
        if not self.config.get('CLICK_AWAY_RESTS', True):
            return False
        if not md or level is None:
            return False
        side = SpreadSide(getattr(side, 'value', side))
        increment = pair.effective_increment() or 0.0
        edge = increment / 2.0
        if side is SpreadSide.BUY:
            offer = md.get('long_spread')
            return offer is not None and level < offer - edge
        bid = md.get('short_spread')
        return bid is not None and level > bid + edge

    def close_at_limit(self, pair_key, level, quantity=None):
        """Rest a CLOSING order for every open position on this pair.

        The counterpart to CLOSE ALL, which crosses at market. Here the
        trader names the price and waits — the same machinery
        AutoRouting uses, driven by hand: one working order per
        position, on the opposite side, carrying that position's ticket
        so it CLOSES rather than opening a second one on a hedging
        account.

        It rests a TARGET and no stop. Nothing re-enters, nothing
        re-prices it but the peg that holds the clicked SPREAD level as
        the other leg moves.
        """
        with self.lock:
            return self._close_at_limit(pair_key, level, quantity)

    def _close_at_limit(self, pair_key, level, quantity=None):
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'no pair {pair_key}'}
        try:
            level = float(level)
        except (TypeError, ValueError):
            return {'ok': False, 'reason': 'a limit close needs a price'}
        positions = self.book.positions(pair_key)
        if not positions:
            return {'ok': False, 'reason': f'{pair_key} is already flat — '
                                           f'nothing to close'}
        armed, already = [], []
        for position in positions:
            existing = self.book.orders_for_position(position.position_id)
            if existing:
                already.append(position.position_id)
                continue
            order = self.quoter.arm(pair, position, level,
                                    quantity=quantity, auto=False)
            if order is None:
                continue
            armed.append(order.to_dict())
            self.session_events.append(
                {'pair': pair_key, 'action': 'close_at_limit',
                 'position': position.position_id, 'level': level,
                 'order': order.order_id})
        return {'ok': bool(armed), 'armed': armed, 'level': level,
                'already_working': already,
                'reason': None if armed else (
                    f'every position on {pair_key} already has a closing '
                    f'order working — pull it first to move the price'
                    if already else 'nothing could be armed')}

    def cancel_order(self, order_id):
        with self.lock:
            return self._cancel_order(order_id)

    def _cancel_order(self, order_id):
        order = self.book.order(order_id)
        if order is None or not order.is_working:
            return {'ok': False, 'reason': 'that order is not working'}
        self.quoter.cancel(order)
        return {'ok': True, 'order': order.to_dict()}

    def cancel_where(self, pair_key=None, side=None, reason='cancelled'):
        """`CXL B` / `CXL S` / `CXL All`, and the global kill.

        The real pendings behind them are re-sized or pulled on the next
        pass, which is also where a fill that raced the cancel is
        caught.
        """
        with self.lock:
            return self._cancel_where(pair_key, side, reason)

    def _cancel_where(self, pair_key=None, side=None, reason='cancelled'):
        pulled = self.book.cancel_where(pair_key, side, reason)
        for key, pair in self.config.pairs.items():
            if pair_key in (None, key):
                self.quoter.work(pair, self.market.get(key))
                # A maker fill resting both legs is a working order too:
                # CXL pulls it (a leg already on is recovered/unwound by
                # the same rules as a timeout, never left alone).
                job = self.maker.jobs.get(key)
                wanted = getattr(side, 'value', side)
                if job is not None and wanted in (None, job.side.value):
                    self.maker.cancel(key, reason)
                    self.maker.step(pair, self.market.get(key))
                    pulled = list(pulled) + [job.id]
        return pulled

    def close_unclaimed(self, account, ticket):
        """Close one unexplained position at the broker, by ticket.

        Only ever because a person asked. The volume comes from the
        broker's own book, and a ticket MT5 no longer lists is already
        gone rather than an error.
        """
        with self.lock:
            row = self.reconciler.unclaimed.get((account, str(ticket)))
            if row is None:
                return {'ok': False, 'reason': f'{account}:{ticket} is not '
                                               f'on the unclaimed list'}
            # A TICKET THE BOOK HOLDS IS NOT UNEXPLAINED, whatever a
            # stale notice says. This is the hedged leg of a live
            # spread: closing it leaves the OTHER leg naked, which
            # turns a cosmetic fault into an outright position the
            # trader never asked for. The one button the screen offered
            # for this position was this one.
            #
            # `close_position` on the pair is how a trader gets out of
            # a spread; this is only ever for something nothing
            # accounts for.
            if (account, str(ticket)) in self.reconciler.known_tickets():
                self.reconciler.unclaimed.pop((account, str(ticket)), None)
                return {'ok': False, 'refused': True, 'reason': (
                    f'{account}:{ticket} IS in the book — it is a leg of a '
                    f'live position, not an unexplained one. Closing it '
                    f'alone would leave the other leg naked. The notice '
                    f'was stale and has been cleared; use Flatten on the '
                    f'position itself.')}
            leg = self.legs.get(account)
            if leg is None:
                return {'ok': False, 'reason': f"no leg runner for {account}"}
            result = self.executor.close_tickets(
                leg, row['symbol'], [ticket], row['side'],
                comment='UNCLAIMED')
            if result.get('ok'):
                self.reconciler.unclaimed.pop((account, str(ticket)), None)
                if self.store is not None:
                    self.store.event('unclaimed_closed', None, account=account,
                                     ticket=str(ticket), symbol=row['symbol'],
                                     volume=row['volume'])
            return result

    def adopt_unclaimed(self, pair_key, ticket_a, ticket_b):
        """Take two unexplained tickets into the book as one spread.

        For the case the operator can see and we cannot: the two legs of
        a pair that went on before the database existed, or under a
        build that did not persist. Once adopted it is managed, marked
        and closable like any other position.
        """
        with self.lock:
            pair = self.config.pairs.get(pair_key)
            if pair is None:
                return {'ok': False, 'reason': f'no pair {pair_key}'}
            rows = {}
            for leg_key, ticket, account in (
                    ('a', ticket_a, pair.account_a),
                    ('b', ticket_b, pair.account_b)):
                row = self.reconciler.unclaimed.get((account, str(ticket)))
                if row is None:
                    return {'ok': False,
                            'reason': f'{account}:{ticket} is not on the '
                                      f'unclaimed list'}
                rows[leg_key] = row

            # The SIDE of the spread comes from what leg B actually is:
            # buying the spread is long B, and a position that says
            # otherwise is not this pair's.
            side = (SpreadSide.BUY if rows['b']['side'] == 'BUY'
                    else SpreadSide.SELL)
            leg_a_side, leg_b_side = side.leg_sides()
            if rows['a']['side'] != leg_a_side.value:
                return {'ok': False,
                        'reason': (f"those two are not a hedge: leg B is "
                                   f"{rows['b']['side']} so leg A must be "
                                   f"{leg_a_side.value}, and it is "
                                   f"{rows['a']['side']}")}

            contract_a = (pair.meta_a or {}).get('contract_size')
            contract_b = (pair.meta_b or {}).get('contract_size')
            leg_a = LegFill(pair.account_a, rows['a']['symbol'], leg_a_side,
                            rows['a']['volume'], rows['a']['price_open'],
                            position_tickets=[rows['a']['ticket']],
                            contract_size=contract_a, clock=self.clock)
            leg_b = LegFill(pair.account_b, rows['b']['symbol'], leg_b_side,
                            rows['b']['volume'], rows['b']['price_open'],
                            position_tickets=[rows['b']['ticket']],
                            contract_size=contract_b, clock=self.clock)
            entry = (leg_b.price - float(pair.hedge_ratio) * leg_a.price
                     if (leg_a.price and leg_b.price) else None)
            quantity = (leg_b.volume / pair.clip_lots_b
                        if pair.clip_lots_b else leg_b.volume)
            position = SpreadPosition(
                pair_key, side, quantity, leg_a, leg_b, entry,
                OrderType.MARKET,
                sizing.spread_units(leg_b.volume, contract_b),
                clock=self.clock)
            position.recovered = True
            self.remember(self.book.add_position(position))
            for leg_key, ticket, account in (
                    ('a', ticket_a, pair.account_a),
                    ('b', ticket_b, pair.account_b)):
                self.reconciler.unclaimed.pop((account, str(ticket)), None)
            if self.store is not None:
                self.store.event('adopted', pair_key,
                                 position_id=position.position_id,
                                 tickets=[str(ticket_a), str(ticket_b)])
            return {'ok': True, 'position': position.to_dict()}

    def remember(self, position):
        """Write a position through to the database.

        On every change, not on a timer: the window between an order
        filling and the state being safe is the window a crash turns
        into an unrecoverable position.
        """
        if self.store is not None and position is not None:
            try:
                self.store.save_position(position)
            except Exception as e:                   # never lose the trade
                logging.critical('could not persist %s: %s — a restart will '
                                 'not recover it', position.position_id, e)
        return position

    def remember_all(self):
        for position in self.book.positions(open_only=False):
            self.remember(position)

    def account_health(self):
        """The margin picture, per account, and which one is weakest.

        With two brokers there is no such thing as combined margin: each
        posts its own, and a pair can only be carried by the WEAKER of
        the two. A total would read comfortable while one side is at its
        stop-out level.

        Our own exposure is stated beside the broker's numbers in the
        units the operator sized in — leg lots and units — because a
        margin figure with nothing to compare it against is a number
        nobody can act on.
        """
        warn_at = float(self.config.get('MARGIN_WARN_LEVEL', 200.0) or 0.0)
        rows = {}
        for name in self.legs:
            info = self.account_info(name) or {}
            equity = info.get('equity')
            margin = info.get('margin') or 0.0
            level = info.get('margin_level')
            if level is None and margin and equity is not None:
                level = 100.0 * equity / margin
            lots = units = 0.0
            positions = 0
            for position in self.book.positions():
                for fill in (position.leg_a, position.leg_b):
                    if not fill or fill.account != name:
                        continue
                    lots += fill.volume
                    units += fill.volume * (fill.contract_size or 0.0)
                    positions += 1
            rows[name] = {
                'account': name,
                'known': bool(info),
                'login': info.get('login'), 'server': info.get('server'),
                'currency': info.get('currency'),
                'leverage': info.get('leverage'),
                'balance': info.get('balance'),
                'credit': info.get('credit'),
                'equity': equity,
                'profit': info.get('profit'),
                'margin': margin,
                'margin_free': info.get('margin_free'),
                'margin_level': level,
                'so_call': info.get('margin_so_call'),
                'so_so': info.get('margin_so_so'),
                'our_lots': lots, 'our_units': units,
                'our_legs': positions,
                # A level under the warn threshold, or under the
                # broker's own margin-call level, whichever is higher.
                'tight': bool(level is not None
                              and level < max(warn_at,
                                              info.get('margin_so_call') or 0)),
            }
        measured = [row for row in rows.values()
                    if row['margin_level'] is not None]
        weakest = min(measured, key=lambda row: row['margin_level']) \
            if measured else None
        return {
            'accounts': rows,
            'warn_level': warn_at,
            # TWO LEGS, ONE ACCOUNT. Two runners can both attach to the
            # same running terminal — a blank terminal path on both, or
            # one terminal open — and then every "hedge" is two orders
            # on one account: no spread, twice the exposure, and a
            # screen that looks perfectly normal. The logins are read
            # from the terminals themselves, so this catches it however
            # the config was written.
            'same_login': _same_login(rows),
            # The weakest account governs. Named, not averaged.
            'weakest': weakest['account'] if weakest else None,
            'weakest_level': weakest['margin_level'] if weakest else None,
            'unknown': [name for name, row in rows.items()
                        if not row['known']],
        }

    def legs_share_an_account(self, pair):
        """Two SEPARATE accounts that turned out to be one terminal.

        One account carrying both legs is perfectly ordinary — spot
        silver and the silver future at the same broker is a spread, and
        a hedging account holds both sides at once. That case is
        configured deliberately, by pointing both legs at the SAME
        account, and it trades here like any other.

        What this catches is the other thing, which looks identical on
        the screen and is not the same at all: two accounts configured
        as two, both attaching to one running terminal — a blank
        terminal path on both is enough. Then the config says "hedged
        across two brokers" while every order lands on one login, the
        margin is one pool nobody is watching, and two runners are
        fighting over one terminal. Entries are refused on that; closes
        and cancels are never touched.
        """
        if pair.account_a == pair.account_b:
            return None                      # deliberate, and supported
        first = (self.account_info(pair.account_a) or {}).get('login')
        second = (self.account_info(pair.account_b) or {}).get('login')
        if first and second and first == second:
            return first
        return None

    def _anchor_for(self, key, pair, md):
        """This pair's ladder anchor, moved only when it must be."""
        increment = pair.effective_increment()
        spread = (md or {}).get('spread')
        if not increment or spread is None:
            return self._ladder_anchor.get(key)
        held = self._ladder_anchor.get(key)
        # LOCKED means locked. The drift rule below is what a ladder
        # does when the trader has not said otherwise; once they have,
        # the window stays where they put it however far the market
        # walks, and Centre is how they get it back.
        if held is not None and self._ladder_locked.get(key):
            return held
        anchor = ladder_anchor(held, spread, increment, int(pair.rows))
        self._ladder_anchor[key] = anchor
        return anchor

    def lock_ladder(self, key, locked):
        """Hold this pair's price window still, or let it follow again.

        The Lock tick on the ladder. It used to stop only the BROWSER
        scrolling, which left the two movers that actually shift a
        price off its row — the anchor re-centring, and the window
        widening to cover the touches — running underneath it. A
        control named Lock that locks a third of the movement is worse
        than no control, because the trader believes the ladder is
        still.
        """
        self._ladder_locked[key] = bool(locked)
        return bool(locked)

    def recentre_ladder(self, key):
        """Drop the anchor so the next poll rebuilds around the market.

        The manual Recentre button, and what a pair falls back to when
        its market has walked out of the window entirely.
        """
        # Deliberately does NOT clear the lock: Centre means "show me
        # the market again", not "and start following it". A trader who
        # locked the ladder gets a window rebuilt around the market and
        # then held there.
        self._ladder_anchor.pop(key, None)

    def broker_offset(self):
        """Seconds the BROKER's clock runs ahead of this machine's.

        Measured from the terminals, never configured: a typed-in time
        zone goes stale at every daylight-saving change. Both accounts
        should agree; when they do not, the WORSE case is not meaningful
        here, so leg A's is used and the disagreement is published for
        the screen — two brokers on different clocks is a fact the
        operator needs, not something to average away.

        None when it has not been measured. The session cutoff then does
        not fire, and says so.
        """
        ttl = float(self.config.get('BROKER_CLOCK_TTL_SEC', 300.0))
        now = self.clock()
        offsets = []
        for name, leg in self.legs.items():
            cached = self._offsets.get(name)
            if cached is None or now - cached[0] > ttl:
                try:
                    measured = leg.server_offset()
                except Exception:                    # a leg mid-restart
                    measured = None
                if measured is not None or cached is None:
                    self._offsets[name] = (now, measured)
                    cached = self._offsets[name]
            if cached and cached[1] is not None:
                offsets.append(cached[1])
        if not offsets:
            return None
        return offsets[0]

    def broker_clock(self):
        """What the screen shows about which clock we are on."""
        block = self.session_clock.describe()
        block['per_account'] = {
            name: (cached[1] if cached else None)
            for name, cached in self._offsets.items()}
        measured = [value for value in block['per_account'].values()
                    if value is not None]
        # Two accounts whose clocks disagree by more than a minute is
        # worth saying out loud: it usually means two different brokers,
        # and the cutoff can only be on one of them.
        block['accounts_disagree'] = bool(
            measured and max(measured) - min(measured) > 60)
        return block

    def pnl_check(self, ours, accounts, started, ours_net=None):
        """Our open P&L against MT5's own — BOTH AS OF ONE MOMENT, AND
        BOTH THE SAME KIND OF NUMBER.

        GROSS against GROSS. MT5's `profit` is the floating P&L on the
        two prices and nothing else: it carries no commission and no
        swap. Ours was the NET figure - commission for both ends of both
        legs already taken off by `mark_fees` - so the difference
        carried the whole round trip's commission as a permanent,
        structural gap that no market could ever close. On a desk paying
        $3.50 a lot a side that is a row sitting red all session over a
        disagreement that does not exist.

        It was invisible here only because these accounts are billed at
        $0.00 a lot. A number that is right only while a setting is zero
        is not right.

        The NET total is still what the trader is shown everywhere else,
        and it is carried here too (`ours_net`) so the panel can show
        both and say which one was compared. Nothing about the P&L a
        trader reads has changed - only what this row compares it to.

        A disagreement here means one of us is wrong about real money,
        so it has to be shown. But the two halves are read on different
        clocks: our marks come off the tick this poll just read, while
        `account_info` is cached for ~5s. Comparing them was comparing
        a photo taken now against one taken five seconds ago, so on a
        moving market they ALWAYS differed and the row sat red all
        session. A light that is always on is a light nobody reads, and
        the day it means something nobody looks.

        So the comparison is only made on the passes where every
        account's profit was actually read from the terminal — a cache
        stamp at or after this pass began — and the answer is then HELD
        until the next such pass, carrying the time it was taken. Both
        halves therefore always describe the same instant, the row goes
        red only on a real disagreement, and nobody has to pick a
        tolerance for market noise that would be wrong the moment a
        pair with a different `k` is added.

        Costs nothing: no extra round trip, and the cache is untouched.
        """
        fresh = []
        for name in self.legs:
            stamped = self._account_cache.get(name)
            if stamped is None or stamped[0] < started:
                return self._pnl_check      # a cache hit: not this pass
            fresh.append(accounts.get(name))
        if not fresh:
            return self._pnl_check
        # UNMEASURED IS NOT ZERO, at either end. An account that could
        # not be read makes MT5's side unknown, and an unmarkable
        # position makes ours unknown; neither is a difference of zero.
        theirs = 0.0
        for info in fresh:
            profit = (info or {}).get('profit')
            if info is None or not isinstance(profit, (int, float)):
                theirs = None
                break
            theirs += float(profit)
        self._pnl_check = {
            'at': started,
            #: GROSS, because `theirs` is gross. The comparison is the
            #: whole point of the row and it has to be like for like.
            'ours': ours,
            #: ...and the net figure beside it, which is the number on
            #: every other panel. Shown, never compared.
            'ours_net': ours_net,
            'basis': 'gross',
            'theirs': theirs,
            'difference': (None if ours is None or theirs is None
                           else ours - theirs),
        }
        return self._pnl_check

    def account_info(self, name):
        """Cached ~5s. Fetching this three times a second is three IPC
        round trips a second for a number that moves slowly."""
        ttl = float(self.config.get('ACCOUNT_INFO_CACHE_SEC', 5.0))
        cached = self._account_cache.get(name)
        now = self.clock()
        if cached and now - cached[0] < ttl:
            return cached[1]
        leg = self.legs.get(name)
        info = leg.account_info() if leg else None
        self._account_cache[name] = (now, info)
        return info

    # -- what the UI renders --------------------------------------------------

    def snapshot(self):
        """Everything every panel needs, from one poll's data."""
        # ONE reading, held for the whole pass. `account_info` stamps
        # its cache with the clock at the moment it actually reads the
        # terminal, so a stamp at or after this one is a read that
        # happened INSIDE this pass — which is how the P&L comparison
        # below knows its two halves describe the same instant.
        started = self.clock()
        pairs = {}
        #: Our own open P&L across every pair, with the same
        #: unmeasured-is-not-zero rule each pair uses: one position
        #: that cannot be marked makes the TOTAL unknown, not smaller.
        ours = 0.0
        #: The same total WITHOUT commission, which is the only thing
        #: MT5's own `profit` can honestly be compared against. Same
        #: unmeasured-is-not-zero rule: one position that cannot be
        #: marked makes it unknown, not smaller.
        ours_gross = 0.0
        for key, pair in self.config.pairs.items():
            md = self.market.get(key)
            sizes = self.implied_depth(pair)
            # Break-even is only DEFINED given a holding period: the
            # swap is charged per night. 0 is intraday, where the term
            # vanishes, and it is the default.
            settings = pair.exit_settings(self.config.settings)
            nights = float(settings.get('BREAK_EVEN_NIGHTS', 0.0) or 0.0)
            margin = self.margin_detail(pair) or {}
            # The pair's own range this session, so a target can be read
            # against what the spread actually travels.
            seen = self.session.get(key) or {}
            session_range = (None if seen.get('high') is None
                             or seen.get('low') is None
                             else seen['high'] - seen['low'])
            net, avg_entry = self.book.net_position(key)
            buys, sells = self.book.working_counts(key)
            positions = []
            open_pnl = 0.0
            open_gross = 0.0
            for position in self.book.positions(key):
                gross, net_pnl, closing = mark_position(
                    position, md, settings)
                row = position.to_dict()
                row.update({'gross_pnl': gross, 'net_pnl': net_pnl,
                            'closing_spread': closing,
                            # Anchored on the price this position was
                            # ENTERED at: a take-profit that moves with
                            # the market is not a take-profit.
                            'exit': takeprofit.for_position(
                                position, md, pair, settings,
                                self.entry_margin(pair, position),
                                nights=nights,
                                carry_for=self.holding_carry(
                                    pair, position.side.value, nights))})
                positions.append(row)
                # UNMEASURED IS NOT ZERO. A position that could not be
                # marked used to be skipped, so the total read as though
                # it contributed nothing — an authoritative-looking
                # number that was silently short one position. One
                # unmarkable position makes the TOTAL unknown.
                if net_pnl is None:
                    open_pnl = None
                elif open_pnl is not None:
                    open_pnl += net_pnl
                if gross is None:
                    open_gross = None
                elif open_gross is not None:
                    open_gross += gross
            row_exit = takeprofit.describe(
                pair, md, settings,
                margin_per_spread=margin.get('money'),
                margin_source=margin.get('source'),
                session_range=session_range,
                quantity=pair.default_quantity,
                spread_units=sizing.spread_units(
                    pair.clip_lots_b,
                    (pair.meta_b or {}).get('contract_size')),
                nights=nights,
                carry_buy=self.holding_carry(pair, 'BUY', nights),
                carry_sell=self.holding_carry(pair, 'SELL', nights))
            if positions:
                if open_pnl is None:
                    ours = None
                elif ours is not None:
                    ours += open_pnl
                if open_gross is None:
                    ours_gross = None
                elif ours_gross is not None:
                    ours_gross += open_gross
            pairs[key] = {
                'key': key, 'name': pair.name, 'enabled': pair.enabled,
                'account_a': pair.account_a, 'account_b': pair.account_b,
                'symbol_a': pair.symbol_a, 'symbol_b': pair.symbol_b,
                # What one LOT is on each leg - MT5's, or the trader's
                # override - so a fill can be priced as notional.
                'contract_size_a': (pair.meta_a or {}).get('contract_size'),
                'contract_size_b': (pair.meta_b or {}).get('contract_size'),
                # What KIND of pair this is — the operator's own
                # declaration, and the only thing that says whether a
                # fair spread applies at all.
                'pair_type': pair.pair_type,
                'hedge_ratio': pair.hedge_ratio,
                'hedge_ratio_for': pair.hedge_ratio_for,
                'increment': pair.effective_increment(),
                'increment_derived': pair.derived_increment(),
                'order_type': pair.order_type.value,
                'exit_type': pair.exit_type.value,
                'time_in_force': pair.time_in_force.value,
                'overnight': pair.overnight.value,
                # This ladder's own hours, and where in them it is now.
                'session': PairSession(pair, self.config).describe(
                    self.session_clock.broker_now()),
                # The selected algo, and what it says. NONE publishes
                # only its own name — a ladder running nothing costs
                # nothing on the wire either.
                'algo': (algo_module.ALGO if self.algos.is_on(key)
                         else pair.algo),
                'algo_on': self.algos.is_on(key),
                'algo_mode': self.algos.mode(key),
                # ALGO positions nobody is managing: recovered after a
                # restart, or left when the Algo went to dry run. Said
                # on the screen until the Algo is LIVE again to adopt
                # them, or the trader closes them.
                'algo_unmanaged': [
                    p.position_id for p in self.book.positions(key)
                    if getattr(p, 'source', MANUAL) == ALGO_SOURCE
                    and p.position_id not in self.algos.owned(key)],
                'algo_params': algo_module.clean_params(pair.algo_params),
                'algo_window': pair.algo_window,
                'algo_block': self.algo_block(pair, md, row_exit),
                'show_fair_window': pair.algo_window,   # the old name
                'default_quantity': pair.default_quantity,
                # The largest Qty either broker will take, so the
                # keypad can stop offering a size that is a guaranteed
                # refusal. None where neither broker caps volume.
                'max_qty': sizing.max_qty(pair, pair.meta_a, pair.meta_b),
                # How many price levels the ladder draws. Named apart
                # from `rows`, which is the rows THEMSELVES — one of
                # them is a count and the other is a list, and a
                # settings pane reading the wrong one shows a form full
                # of prices.
                'row_count': pair.rows,
                'clip_lots_a': pair.clip_lots_a,
                'clip_lots_b': pair.clip_lots_b,
                # What ONE LOT is, in the instrument's own units —
                # 100 oz of gold, 5,000 of silver, 1,000 barrels of
                # oil. READ FROM MT5 (`trade_contract_size`) and never
                # configured: a contract size somebody typed is a
                # contract size that can be wrong.
                'contract_a': (pair.meta_a or {}).get('contract_size'),
                'contract_b': (pair.meta_b or {}).get('contract_size'),
                # What MT5 itself said, so an override is visible as an
                # override and not merely as a number.
                'contract_a_mt5': (pair.meta_a or {}).get('contract_size_mt5'),
                'contract_b_mt5': (pair.meta_b or {}).get('contract_size_mt5'),
                'max_quote_age_sec': pair.max_quote_age_sec,
                'max_quote_age_effective': (
                    pair.max_quote_age_sec
                    if pair.max_quote_age_sec is not None
                    else self.config.get('MAX_QUOTE_AGE_SEC')),
                'spread_units': sizing.spread_units(
                    pair.clip_lots_b, (pair.meta_b or {}).get('contract_size')),
                'market': md,
                # WHY there is no market, when there is none. Without
                # this the ladder simply shows dashes, which reads as a
                # quiet market rather than a blind one.
                'dark_reason': self._dark.get(key),
                'short_spread': (md or {}).get('short_spread'),
                'long_spread': (md or {}).get('long_spread'),
                # What the CARRY says this spread should be, from the
                # two things only the trader knows: the futures leg's
                # expiry and the swap per day at their broker.
                # Where to get out: break-even (the entry price plus
                # commission, both legs, both ends) and break-even plus
                # a target on the margin the spread ties up. A price on
                # the screen — nothing is sent to the broker.
                'exit': row_exit,
                'fair': self.fair_spread(pair, md),
                'errors': self.errors.get(key) or [],
                # The rows the ladder draws come from HERE, so the Work
                # column, the click that places an order and the price
                # that names it cannot disagree about where a level is.
                'rows': ladder_rows(pair, md, self.book, sizes=sizes,
                                    anchor=self._anchor_for(key, pair, md),
                                    frozen=bool(self._ladder_locked
                                                .get(key))),
                # What the window is built around, so the screen can say
                # how far the market has drifted from it.
                'ladder_anchor': self._ladder_anchor.get(key),
                # Published so a browser that reloaded, or a second one
                # on another screen, shows the tick that matches what
                # the engine is actually doing.
                'ladder_locked': bool(self._ladder_locked.get(key)),
                'depth_published': sizes['published'],
                'orders': [o.to_dict() for o in self.book.orders(key)],
                # Orders that STOPPED working recently, with the reason.
                # A rejected pending used to vanish from the ladder in
                # the same instant the click was accepted: the trader
                # saw a green toast and then an empty Work column, with
                # the broker's refusal nowhere on the screen.
                'dead_orders': [
                    o.to_dict() for o in self.book.orders(key,
                                                          working_only=False)
                    if not o.is_working and o.reason
                    and self.clock() - o.created_at < DEAD_ORDER_MEMORY_SEC],
                'quotes': self.quoter.snapshot(key),
                # W3 maker fills working on this ladder: both legs
                # resting at OKX, how far each has filled.
                'maker_fills': self.maker.snapshot(key),
                'quoting_leg': pair.quoting_leg,
                # The leg that will ACTUALLY rest the pending — the
                # setting when there is one, otherwise the wider book.
                # A ladder must be able to say which broker will show
                # an order, before the first click.
                'quoting_leg_effective': quoting_leg(pair),
                'leg_a_width': (pair.meta_a or {}).get('width'),
                'leg_b_width': (pair.meta_b or {}).get('width'),
                # WHICH ACCOUNT each leg trades. Published so the
                # Reconciler tab can offer the right tickets when a
                # position has to be adopted back into this pair: a
                # ticket belongs to one account, and a screen that let
                # the operator pick leg A's ticket from leg B's account
                # would be a screen that invites the refusal.
                'leg_a_account': pair.account_a,
                'leg_b_account': pair.account_b,
                'working_buys': buys, 'working_sells': sells,
                # What is actually RESTING at the broker for this pair,
                # beside what our book thinks is working. They should
                # agree; when they do not, the ladder says so rather
                # than showing our own number twice.
                'broker_pendings': sum(
                    1 for quote in self.quoter.snapshot(key)
                    if quote.get('ticket')),
                # How many of the working orders are resting CLOSES. A
                # closing order puts nothing at the broker by design,
                # so counting it against `broker_pendings` made every
                # reducing click look like orders that failed to place.
                'resting_closes': sum(
                    1 for order in self.book.orders(key)
                    if order.position_id),
                # ...and the same for an ENTRY held here and crossed at
                # its level (WORKING_ORDERS = TRIGGER, the default).
                'held_entries': sum(
                    1 for quote in self.quoter.snapshot(key)
                    if quote.get('held')
                    for _ in quote.get('orders') or ()),
                'working_orders': 'TRIGGER' if self.quoter.triggers()
                                  else 'QUOTE',
                'positions': positions,
                'net_position': net, 'avg_entry': avg_entry,
                'open_pnl': open_pnl if positions else None,
                'last_print': self.book.last_print(key),
            }
        accounts = {name: self.account_info(name) for name in self.legs}
        return {
            'at': started,
            #: Our open P&L against MT5's own, BOTH AS OF ONE MOMENT.
            # GROSS goes in, because MT5's own profit is gross. The
            # net total travels beside it for the panel to show.
            'pnl_check': self.pnl_check(ours_gross, accounts, started,
                                        ours_net=ours),
            # What a click does, and how fast it is drained — the UI
            # arms itself from the ENGINE's answer, never from its own
            # idea of what the trader last selected.
            'confirm_market_clicks': bool(
                self.config.get('CONFIRM_MARKET_CLICKS', False)),
            'row_height_px': self.config.get('ROW_HEIGHT_PX', 17),
            # Which column the screen should treat as a buy. UI
            # only: the side reaches the engine already decided.
            'click_convention': self.config.get('CLICK_CONVENTION', 'TT'),
            #: How often the ladder re-centres on the mid, and whether a
            #: click away from the touch rests. Both are read from the
            #: ENGINE, never from the screen's own idea of them.
            'recentre_sec': self.config.get('RECENTRE_SEC', 5.0),
            'click_away_rests': bool(self.config.get('CLICK_AWAY_RESTS',
                                                     True)),
            'command_poll_sec': self.config.get('COMMAND_POLL_SEC', 0.02),
            # Published so a slow ladder can be blamed on the right
            # process instead of argued about (spec §9).
            'loop_interval_sec': self._loop_interval,
            'poll_target_sec': self.config.get('POLL_INTERVAL_SEC'),
            'accounts': accounts,
            # Named, not omitted: an absent account looked exactly like
            # a quiet one on the screen.
            'dark_accounts': self.dark_accounts(),
            'reconciler': self.reconciler.snapshot(),
            'broker_clock': self.broker_clock(),
            'account_health': self.account_health(),
            'recovery': dict(self.recovery),
            'session_events': self.session_events[-50:],
            'hedge_times_ms': list(self.quoter.hedge_times[-50:]),
            'click_to_on_ms': list(self.executor.timings[-50:]),
            'pairs': pairs,
            'magic': MAGIC_NUMBER,
        }

    def publish(self):
        """Write the snapshot through a tmp file and `os.replace`.

        The web process reads this file while we write it; a plain
        `open(path, 'w')` means it eventually reads half a JSON
        document.

        The replace is RETRIED, because on Windows it fails outright
        while any other process has the destination open — and the web
        process opens it several times a second. Unretried, that
        collision failed the whole poll and froze the screen with the
        market moving (WinError 5).
        """
        with self.lock:
            payload = self.snapshot()
        tmp = self.status_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, default=str)
        atomicfile.replace(tmp, self.status_path)
        try:
            self.tape.write(self.chart_path, self.clock(),
                            keys=list(self.config.pairs))
        except Exception as e:
            # The chart is a reading. A failure leaves the last file
            # standing and never stops the screen being published.
            logging.warning('could not write the chart file: %s', e)

    def serve_commands(self):
        """Drain clicks on their OWN thread, far faster than the poll.

        A click that waits for the next poll waits up to a whole poll
        interval before the order is even sent — 300ms of nothing, on a
        product whose entire promise is that one click is one order. The
        prices it acts on are the ones already published; nothing here
        needs a fresh poll to run.
        """
        every = float(self.config.get('COMMAND_POLL_SEC', 0.02))
        while not self._stop.is_set():
            try:
                if self.commands is not None:
                    self.commands.drain()
            except Exception as e:                  # never die on a command
                logging.exception("command drain failed: %s", e)
            self.sleep(every)

    def run(self):
        interval = float(self.config.get('POLL_INTERVAL_SEC', 0.3))
        self.start()
        while not self._stop.is_set():
            began = self.clock()
            try:
                self.retry_dark_legs()
            except Exception as e:               # never die on a retry
                logging.exception("leg retry failed: %s", e)
            try:
                if self.commands is not None:
                    # Also here, so a single-threaded run (the tests, and
                    # anything that does not start the command thread)
                    # still executes clicks.
                    self.commands.drain()
                self.poll_once()
                self.publish()
            except Exception as e:                     # never die on one poll
                logging.exception("poll failed: %s", e)
            self.run_session_cutoff()
            self.reconcile_if_due()
            self.journal_if_due()
            elapsed = self.clock() - began
            self.sleep(max(0.0, interval - elapsed))

    def run_session_cutoff(self):
        """At the cutoff: cancel DAY orders, then apply each ladder's
        overnight rule. Once a day, per pair.

        Working orders and positions are governed by DIFFERENT settings
        (spec §3.1 and §3.2) and this is the one place both are read, so
        a trader configures a single time.
        """
        events = []
        for key, pair in self.config.pairs.items():
            hours = PairSession(pair, self.config)
            if not self.session_clock.due(key, hours.cutoff):
                continue
            self.session_clock.mark(key)
            # COUNT WHAT WAS ACTUALLY CANCELLED, not what was asked.
            #
            # The answer was discarded and the event reported
            # len(expired) either way, so a cancel that FAILED was
            # published as a cancel that worked. A DAY order still
            # resting at the session cutoff can fill overnight and put
            # a leg on with nobody watching, and the one record of the
            # cutoff said it had been pulled.
            expired = day_orders(self.book.orders(key))
            cancelled, stuck = 0, []
            for order in expired:
                # cancel_order, NOT _cancel_order: run_session_cutoff is
                # called from the poll loop WITHOUT the lock, so the
                # locking wrapper is the one that belongs here. The
                # lock is an RLock, so this is safe from a holder too.
                if self.cancel_order(order.order_id).get('ok'):
                    cancelled += 1
                else:
                    stuck.append(order.order_id)
            if stuck:
                logging.critical(
                    '%s: %d DAY order(s) could NOT be cancelled at the '
                    'session cutoff and may still be resting at the '
                    'broker: %s', key, len(stuck), ', '.join(stuck))
            if expired:
                events.append({'pair': key, 'action': 'day_orders_cancelled',
                               'count': cancelled, 'failed': stuck})
            md = self.market.get(key)
            for position in self.book.positions(key):
                # "In profit" is NET P&L, less THIS ladder's commission —
                # read on any other basis it flattens trades that are not
                # actually in profit.
                _gross, net_pnl, _closing = mark_position(
                    position, md, pair.exit_settings(self.config.settings))
                # On the BROKER's clock - the one the cutoff fired on.
                # This read this machine's own, so on a box hours behind
                # the broker the cutoff fired and the overnight rule then
                # found it "not yet", every day: EXIT_ALWAYS closed
                # nothing.
                verdict = overnight_action(
                    pair.overnight, net_pnl,
                    self.session_clock.broker_now() or
                    self.session_clock.now(), *hours.cutoff)
                if verdict is None:
                    continue
                # Urgent: market, by ticket, never resting. And no guard
                # withholds it.
                result = self.executor.close_position(pair, position, md,
                                                      reason=verdict)
                self.remember(position)
                events.append({'pair': key, 'action': 'overnight_close',
                               'position': position.position_id,
                               'mode': pair.overnight.value,
                               'net_pnl': net_pnl, 'ok': result['ok']})
        self.session_events.extend(events)
        return events

    def journal_if_due(self):
        """Write what the BROKER says happened into the journal.

        Read from MT5's own deal history rather than from our intentions,
        so it also carries the trader's manual terminal clicks — `is_ours`
        says which is which. Idempotent by deal id, and on its own slow
        clock: a history query is not something to do three times a
        second.
        """
        if self.store is None:
            return None
        every = float(self.config.get('JOURNAL_INTERVAL_SEC', 10.0) or 0.0)
        if every <= 0:
            return None
        now = self.clock()
        if self._last_journal is not None and now - self._last_journal < every:
            return None
        self._last_journal = now
        written = 0
        for name, leg in self.legs.items():
            try:
                rows = leg.order_log(
                    int(self.config.get('JOURNAL_HOURS', 24)))
            except Exception as e:
                logging.error('journal: %s would not answer: %s', name, e)
                continue
            if rows is None:
                # None is UNKNOWN, not "nothing traded".
                logging.debug('journal: %s could not be read this pass', name)
                continue
            written += self.store.record_fills(name, rows, self.resolve_leg)
        return written

    def resolve_leg(self, account, symbol):
        """(pair key, 'A'/'B') for a fill, where it belongs to a pair.

        A fill we cannot map is still journalled: it happened on the
        account, and a journal that drops what it does not recognise is
        not an audit trail.
        """
        for key, pair in self.config.pairs.items():
            if pair.account_a == account and pair.symbol_a == symbol:
                return key, 'A'
            if pair.account_b == account and pair.symbol_b == symbol:
                return key, 'B'
        return None, None

    def reconcile_if_due(self):
        """Every ~20s in LIVE. Assume you will lose track."""
        every = float(self.config.get('RECONCILE_INTERVAL_SEC', 20.0) or 0.0)
        if every <= 0:
            return None
        now = self.clock()
        if self._last_reconcile is not None and now - self._last_reconcile < every:
            return None
        self._last_reconcile = now
        try:
            report = self.reconciler.run()
            # A force-cleared ghost is a state change, and the database
            # must not hand it back at the next restart.
            if report and (report['ghosts'] or report['closed']):
                self.remember_all()
            if self.store is not None and report and (
                    report['orphans'] or report['ghosts'] or report['closed']):
                self.store.event('reconcile', **{
                    'orphans': len(report['orphans']),
                    'ghosts': len(report['ghosts']),
                    'closed': report['closed'],
                    'unknown_accounts': report['unknown_accounts']})
            return report
        except Exception as e:                      # never die on a pass
            logging.exception("reconcile failed: %s", e)
            return None

    def stop(self):
        """Sweep before anything else, then stop.

        Both DAY and GTC synthetics are cancelled here and do not resume
        on restart: while this process is down nobody is watching the
        spread, so an order that "survived" would be a promise nothing
        could keep (spec §3.1).
        """
        self._stop.set()
        self.book.cancel_where(reason='the system stopped — synthetic orders '
                                      'do not survive a restart')
        return self.sweep_pendings('shutdown')


def _offered(leg, symbol):
    """What this account actually has that resembles the symbol asked for.

    Brokers spell gold XAUUSD, GOLD, XAUUSD.r and a future GC1226 or
    GCZ4, so the search walks the stem back — `GCZ4`, `GCZ`, `GC` —
    rather than failing on an exact miss. Listing something is the whole
    point: a bare "not found" leaves the operator guessing at spellings.
    """
    stem = ''.join(c for c in (symbol or '') if c.isalnum())
    for length in range(len(stem), 1, -1):
        found = leg.find_symbols(stem[:length]) or []
        if found:
            return found
    return leg.find_symbols('') or []


def _meta_from_report(report):
    """The subset of MT5's symbol facts the sizing and ladder math read."""
    bid, ask = report.get('bid'), report.get('ask')
    return {
        'symbol': report.get('symbol'),
        'description': report.get('description'),
        'contract_size': report.get('contract_size'),
        'tick_size': report.get('tick_size'),
        'tick_value': report.get('tick_value'),
        'digits': report.get('digits'),
        'point': report.get('point'),
        'volume_min': report.get('volume_min'),
        'volume_max': report.get('volume_max'),
        'volume_step': report.get('volume_step'),
        'filling_mode': report.get('filling_mode'),
        'trade_allowed': report.get('trade_allowed'),
        'bid': bid, 'ask': ask,
        'mid': ((bid + ask) / 2.0) if (bid and ask) else None,
        # Measured, and shown on the ladder: which leg quotes should be
        # decided from the widths, not assumed (spec §4).
        'width': ((ask - bid) if (bid and ask) else None),
        # What the contract itself says it expires on, so a blank
        # expiry field can fall back to MT5 rather than to a prompt.
        'expiry': report.get('expiry'),
        # The carry inputs. `swap_mode` travels with them ALWAYS: the
        # same -4.5 is points on one symbol, account currency on
        # another and percent a year on a third, and a swap without its
        # mode is not a smaller number, it is an unusable one.
        'swap_long': report.get('swap_long'),
        'swap_short': report.get('swap_short'),
        'swap_mode': report.get('swap_mode'),
        'swap_rollover3days': report.get('swap_rollover3days'),
    }


def _badge(md, stale, jumped):
    """The feed badge: OK / oldest leg 2.3s / desynced."""
    ages = [md.get('leg_a_quote_age_sec'), md.get('leg_b_quote_age_sec')]
    ages = [a for a in ages if a is not None]
    if jumped:
        return 'desynced'
    if stale:
        # WITH the number. "stale" alone says nothing about whether the
        # feed died or the market is simply quiet, and those want
        # different answers: one is a terminal to re-log in, the other
        # is a limit to raise.
        return f'stale {max(ages):.0f}s' if ages else 'stale'
    if not ages:
        return 'warming up'
    return f'OK (oldest leg {max(ages):.1f}s)'


#: A ladder that had to grow to reach a level stops somewhere: past
#: this many rows the increment is simply wrong for the pair, and
#: drawing 10,000 of them helps nobody.
MAX_LADDER_ROWS = 400
#: How long a DEAD order keeps its place on the screen. A rejection has
#: to outlive the poll that discovered it, or the trader sees a green
#: toast, an empty Work column, and no reason anywhere.
DEAD_ORDER_MEMORY_SEC = 90.0


def _same_login(rows):
    """Which MT5 logins are being reported by more than one account."""
    seen = {}
    for name, row in rows.items():
        login = row.get('login')
        if login:
            seen.setdefault(str(login), []).append(name)
    return {login: names for login, names in seen.items() if len(names) > 1}


def ladder_anchor(previous, spread, increment, rows, drift=0.34):
    """The price the ladder's window is built around, held STILL.

    The window used to be rebuilt around the live mid on every poll —
    three times a second. One increment of movement re-centred it, so
    the row under the trader's cursor became a DIFFERENT PRICE without
    the cursor moving: the ladder crawled, and a click landed on
    whatever had slid under it.

    The anchor therefore only moves when the mid has drifted past a
    band inside the window (a third of a half-window by default), or
    when there is no anchor yet. Between those the price at every row
    is exactly where it was, which is what makes the ladder clickable.
    """
    centre = round(spread / increment) * increment
    if previous is None:
        return centre
    if abs(spread - previous) > max(1.0, rows * drift) * increment:
        return centre
    return previous


def ladder_rows(pair, md, book, rows=None, sizes=None, anchor=None,
                frozen=False):
    """The rows the ladder draws: the anchor +/- N increments.

    Generated here rather than in the browser so the ladder cannot
    disagree with the engine about where a level is — the Work column,
    the click that places an order and the price that names it all come
    off this one list.

    The window is widened to cover both executable touches and every
    working order on the pair. A level the ladder cannot show is one the
    trader can neither see nor pull, and on a wide book the touches can
    sit well outside a window centred on the mid.

    `anchor` is the price the window is built around (see
    `ladder_anchor`). It is passed in rather than recomputed from the
    mid, because a window that follows every tick moves the prices out
    from under the cursor.
    """
    increment = pair.effective_increment()
    if not md or not increment:
        return []
    rows = int(rows or pair.rows)
    centre = (round(md['spread'] / increment) * increment if anchor is None
              else anchor)
    low = centre - rows * increment
    high = centre + rows * increment

    # The two touches move with every tick, so widening the window to
    # reach them ADDS AND REMOVES ROWS AT THE ENDS as the market walks
    # — and a row appearing above shifts every price below it down by
    # one. That is the second way a ladder crawls, and holding the
    # anchor still does nothing about it. A frozen ladder therefore
    # stops following the touches.
    #
    # Working orders keep their claim either way: their levels are
    # FIXED once placed, so they widen the window once and never
    # again, and a resting order the trader cannot see is one they
    # cannot pull.
    marks = [] if frozen else [md.get('short_spread'), md.get('long_spread')]
    marks += [order.level for order in book.orders(pair.key)]
    marks = [m for m in marks if m is not None]
    if marks:
        low = min([low] + marks)
        high = max([high] + marks)

    steps = int(round((high - low) / increment))
    if steps + 1 > MAX_LADDER_ROWS:
        # Keep the market itself; the far-away level is unreachable at
        # this increment and the ladder says so by not pretending.
        low = centre - (MAX_LADDER_ROWS // 2) * increment
        steps = MAX_LADDER_ROWS - 1

    sizes = sizes or {}
    out = []
    for step in range(steps, -1, -1):
        level = round(low + step * increment, 10)
        buys, sells = book.working_at(pair.key, level)
        out.append({
            'level': level,
            'work_buy': buys or None,
            'work_sell': sells or None,
            # The inside market: the two levels the black rule sits
            # between (spec §3.3).
            'is_best_bid': _at(level, md['short_spread'], increment),
            'is_best_ask': _at(level, md['long_spread'], increment),
            # The MID of the book — the row the ladder centres on, and
            # the one carrying the heavy rule. On a wide spread the two
            # touches can be many rows apart, and "where the market is"
            # is then a question the inside rule alone cannot answer.
            'is_mid': _at(level, md.get('spread'), increment),
            # The row the WINDOW is built around, which on a locked
            # ladder does not move at all. The heavy rule sits here
            # rather than on the live mid when the trader has asked for
            # a still ladder: a rule that hops a row every few ticks is
            # the last thing left moving once the prices and the bands
            # have stopped.
            'is_anchor': _at(level, centre, increment),
            # What the two ORDER BOOKS can actually do here, in
            # spreads. None where a broker publishes no depth — which
            # is not the same as none available, and must not look it.
            'ask_size': depth_book.at(sizes.get('buy'), level, increment),
            'bid_size': depth_book.at(sizes.get('sell'), level, increment),
        })
    return out


def _at(level, price, increment):
    """Is this row the one that price falls in?"""
    if price is None:
        return False
    return abs(level - round(price / increment) * increment) < increment / 2


