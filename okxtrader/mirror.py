"""MIRROR: the Algo's trades, placed again in FOLLOWER accounts.

Built for OKX Managed Trading Sub-accounts: an investor's sub-account,
bound to this desk's trading team, traded through an API key the team
creates for it - one key per follower. Each follower is sized as a
MULTIPLE of the trade: 0.5 trades half the lots of each leg, 2 trades
twice. Infrastructure only: nothing here is on unless `config.json`
names followers, and a desk without a `mirror` section is untouched.

HOW IT FOLLOWS. Every poll the desk hands over the Algo's open
positions. A position that is NEW opens the same spread in each
follower - both legs, the same sides, the multiple of each leg's lots,
rounded down to the follower's lot step. A position that has GONE -
whatever closed it: a target, a stop, CLOSE ALL - closes each follower's
copy. It follows positions, not orders, so every way in and out of a
trade is covered by the one rule, and a restart picks up from what is
recorded.

WHAT IT NEVER DOES.
- It never touches the desk's own positions, orders, ledger or
  database. Followers have their own brokers (their own OKX ledgers,
  named after them) and this module its own file (`mirror_state.json`).
- It never holds the desk up: the orders go out on a thread of its own,
  one follower after another, and a follower that fails is recorded and
  left - the trade and the other followers carry on.
- It never enters late. A position the mirror first sees more than
  `open_window_sec` after it opened (the desk was restarted, the mirror
  switched on mid-trade) is not copied: a follower would buy a price the
  trade has already moved from. Said on the record, not silent.
- It never leaves a follower with one leg. If the second leg does not
  go on, the first is closed again at once.

A follower switched off (`enabled: false`) opens nothing new, and still
closes what it holds: a switch must never strand a position.
"""

import json
import logging
import math
import os
import queue
import threading
import time

from .models import OrderSide

#: A new trade is copied only this soon after it opened.
DEFAULT_OPEN_WINDOW_SEC = 60.0
#: A close that failed is tried again after this long.
CLOSE_RETRY_SEC = 15.0
#: A MAKER leg rests this long before it is given up (entry: the copy is
#: skipped) or crossed (exit: a copy must get out).
DEFAULT_MAKER_TIMEOUT_SEC = 30.0
#: How often a resting maker leg is looked at, and re-priced to its
#: account's own touch when the book has moved.
MAKER_POLL_SEC = 0.5
#: How often each follower's balance and its money in and out are read
#: from OKX, for the client's dashboard.
CASH_REFRESH_SEC = 600.0
#: The most pages of OKX's funding history read back the first time (100
#: rows a page): 5,000 transfers.
CASH_PAGES = 50
#: OKX's funding-ledger types that are the client's money moving: a
#: deposit, a withdrawal (and one cancelled), and - for a managed
#: sub-account - a transfer in from, or out to, its owner's main account.
CASH_BILL_TYPES = {'1': 'deposit', '2': 'withdrawal',
                   '13': 'withdrawal cancelled',
                   '22': 'transfer out to the main account',
                   '23': 'transfer in from the main account'}
#: How a follower executes: as the trade did (MIRROR: a maker trade is
#: copied as maker, a taker one as taker) or always crossing (TAKER).
EXECUTIONS = ('MIRROR', 'TAKER')

OPEN, FAILED, SKIPPED, CLOSED, CLOSE_FAILED = (
    'OPEN', 'FAILED', 'SKIPPED', 'CLOSED', 'CLOSE_FAILED')


def round_down(volume, step, minimum):
    """`volume` in whole steps, rounded DOWN; 0 when under the minimum.
    A follower is never sized above its multiple."""
    step = float(step or 0.0)
    if step > 0:
        volume = math.floor(volume / step + 1e-9) * step
        # Back to the step's own decimals: 0.30000000000000004 is not a
        # size OKX accepts.
        decimals = max(0, -int(math.floor(math.log10(step) + 1e-9)))
        volume = round(volume, decimals)
    return volume if volume >= float(minimum or 0.0) - 1e-12 and volume > 0 \
        else 0.0


class Follower:
    """One account the trades are copied into."""

    def __init__(self, name, multiple, enabled=True, demo=True, pairs=None,
                 base_url=None, broker=None, capital=0.0, max_margin=0.0,
                 daily_loss_limit=0.0, max_trades_day=0, execution='MIRROR',
                 maker_timeout_sec=None):
        self.name = str(name)
        self.multiple = float(multiple)
        #: The client's capital, for returns on it. 0 = not set.
        self.capital = max(0.0, float(capital or 0.0))
        # THE ACCOUNT'S LIMITS - each 0 = off. Every one stops NEW copies
        # only; a copy already on always closes.
        #: Margin all its open copies may tie up, in USDT.
        self.max_margin = max(0.0, float(max_margin or 0.0))
        #: Today's closed loss, in USDT, after which no new copy today.
        self.daily_loss_limit = abs(float(daily_loss_limit or 0.0))
        #: Copies a day.
        self.max_trades_day = max(0, int(max_trades_day or 0))
        execution = str(execution or 'MIRROR').upper()
        self.execution = execution if execution in EXECUTIONS else 'MIRROR'
        self.maker_timeout_sec = float(
            DEFAULT_MAKER_TIMEOUT_SEC if maker_timeout_sec is None
            else maker_timeout_sec)
        self.enabled = bool(enabled)
        self.demo = bool(demo)
        #: None: every ladder. Otherwise only these pair keys.
        self.pairs = None if not pairs else set(pairs)
        self.base_url = base_url
        self._broker = broker
        self.last_error = None
        #: Taken off the config while it still held a copy: closes only.
        self.removed = False
        self.key_print = self._key_print()

    def _key_print(self):
        """A fingerprint of this follower's API key as the environment
        holds it now - so a NEW key reconnects it, and nothing else does.
        Never the key itself."""
        import hashlib
        from .config import okx_env_keys
        values = '|'.join(os.environ.get(k, '')
                          for k in okx_env_keys(f'mirror_{self.name}'))
        return hashlib.sha256(values.encode('utf-8')).hexdigest()[:16]

    def follows(self, pair_key):
        return self.pairs is None or pair_key in self.pairs

    def broker(self, ledger_dir=None):
        """This follower's own OKX broker: its own API key (from `.env`,
        OKX_API_KEY_<NAME> and so on) and its own ledger."""
        if self._broker is None:
            from .config import AccountConfig
            from .okx_broker import OKXBroker
            account = AccountConfig(f'mirror_{self.name}', demo=self.demo,
                                    base_url=self.base_url)
            broker = OKXBroker(account, ledger_path=ledger_dir)
            if not broker.initialize():
                raise RuntimeError(broker.last_error or 'OKX did not answer')
            self._broker = broker
        return self._broker

    def to_dict(self):
        return {'name': self.name, 'multiple': self.multiple,
                'enabled': self.enabled, 'demo': self.demo,
                'pairs': sorted(self.pairs) if self.pairs else None,
                'capital': self.capital, 'max_margin': self.max_margin,
                'daily_loss_limit': self.daily_loss_limit,
                'max_trades_day': self.max_trades_day,
                'execution': self.execution,
                'maker_timeout_sec': self.maker_timeout_sec,
                'removed': self.removed,
                'last_error': self.last_error}


class Mirror:
    """Copies the Algo's positions into the followers."""

    def __init__(self, followers, state_path, open_window_sec=None,
                 clock=time.time, background=True, ledger_dir=None,
                 sleep=time.sleep):
        self.followers = {f.name: f for f in followers}
        self.state_path = state_path
        self.open_window_sec = float(
            DEFAULT_OPEN_WINDOW_SEC if open_window_sec is None
            else open_window_sec)
        self.clock = clock
        self.background = background
        self.ledger_dir = ledger_dir
        self._lock = threading.RLock()
        #: master position id -> {'pair_key', 'opened_at', 'legs',
        #: 'gone': bool, 'followers': {name: record}}
        self.records = {}
        #: EVERY CLOSED COPY, per follower - never pruned: the client's
        #: reports (and, later, their own login and the profit share) are
        #: built on it. {name: [{position_id, pair_key, opened_at,
        #: closed_at, gross, fees, pnl, margin}]}
        self.closed = {}
        #: Followers halted from the screen: {name: {'at', 'why'}}.
        self.halted = {}
        #: Each follower's money as OKX last said it: {name: {'balance':
        #: {'equity', 'at'}, 'flows': {bill id: {'at', 'amount', 'kind'}},
        #: 'error', 'read_at'}} - for the client's dashboard.
        self.cash = {}
        self._cash_due = {}
        #: Copying switched off on the Managed Accounts screen: no NEW
        #: copy; every copy on still closes with its trade.
        self.paused = False
        self.sleep = sleep
        #: ONE QUEUE AND ONE THREAD PER FOLLOWER: fifteen accounts trade
        #: side by side, not one after another (the last would trade a
        #: price the first had left), and each account's own orders stay
        #: in order - its open always goes before its close.
        self._queues = {}
        self._workers = {}
        #: Jobs handed to a follower's thread and not yet done.
        self._pending = 0
        self._load()

    # -- from the config --------------------------------------------------

    @classmethod
    def from_raw(cls, raw, state_path, **kw):
        """The `mirror` section of config.json, or None when it names no
        follower or is switched off - and then nothing here runs."""
        section = (raw or {}).get('mirror') or {}
        if not section.get('enabled', False):
            return None
        followers = [f for f in (cls.follower_of(row) for row in
                                 section.get('followers') or ()) if f]
        if not followers:
            return None
        return cls(followers, state_path,
                   open_window_sec=section.get('open_window_sec'), **kw)

    @staticmethod
    def follower_of(row):
        """One `followers` row of config.json as a Follower, or None (said
        in the log) when it has no name or no multiple above 0."""
        try:
            multiple = float(row.get('multiple'))
        except (TypeError, ValueError):
            logging.error('MIRROR: follower %r has no multiple - left out',
                          row.get('name'))
            return None
        if not row.get('name') or multiple <= 0:
            logging.error('MIRROR: follower %r needs a name and a multiple '
                          'above 0 - left out', row)
            return None
        return Follower(
            row['name'], multiple, enabled=row.get('enabled', True),
            demo=row.get('demo', True), pairs=row.get('pairs'),
            base_url=row.get('base_url'),
            capital=row.get('capital'), max_margin=row.get('max_margin'),
            daily_loss_limit=row.get('daily_loss_limit'),
            max_trades_day=row.get('max_trades_day'),
            execution=row.get('execution'),
            maker_timeout_sec=row.get('maker_timeout_sec'))

    #: What a change on the Managed Accounts screen may change in place.
    LIVE_FIELDS = ('multiple', 'enabled', 'capital', 'max_margin',
                   'daily_loss_limit', 'max_trades_day', 'execution',
                   'maker_timeout_sec', 'pairs')

    def reconfigure(self, section):
        """config.json's `mirror` section CHANGED (the Managed Accounts
        screen saved it): take it now, without a restart. Settings change
        in place; an account reconnects only when its key, demo/live or
        host changed; a new account is added; one taken off while it
        holds a copy closes only, and goes once it holds none. Switched
        off, the mirror takes no new copy - and still closes them all."""
        section = section or {}
        with self._lock:
            self.paused = not section.get('enabled', False)
            if section.get('open_window_sec'):
                self.open_window_sec = float(section['open_window_sec'])
            seen = set()
            for row in section.get('followers') or ():
                fresh = self.follower_of(row)
                if fresh is None:
                    continue
                seen.add(fresh.name)
                old = self.followers.get(fresh.name)
                if old is None:
                    self.followers[fresh.name] = fresh
                    logging.info('MIRROR: %s added, x%g', fresh.name,
                                 fresh.multiple)
                    continue
                if (old.demo, old.base_url, old.key_print) != \
                        (fresh.demo, fresh.base_url, fresh.key_print):
                    # Its account changed: the next order connects anew.
                    old._broker = None
                    logging.info('MIRROR: %s reconnects (key or account '
                                 'changed)', old.name)
                old.demo, old.base_url = fresh.demo, fresh.base_url
                old.key_print = fresh.key_print
                for name in self.LIVE_FIELDS:
                    setattr(old, name, getattr(fresh, name))
                old.removed = False
            for name, follower in list(self.followers.items()):
                if name in seen:
                    continue
                if self._holds(name):
                    follower.enabled = False
                    follower.removed = True
                else:
                    del self.followers[name]
            self._save()

    def _holds(self, name):
        live = (OPEN, 'PENDING', 'CLOSING', CLOSE_FAILED)
        return any((r['followers'].get(name) or {}).get('status') in live
                   for r in self.records.values())

    # -- the record ---------------------------------------------------------

    def _load(self):
        try:
            with open(self.state_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
            self.records = state.get('records') or {}
            self.closed = state.get('closed') or {}
            self.halted = state.get('halted') or {}
            self.cash = state.get('cash') or {}
        except FileNotFoundError:
            self.records = {}
        except (OSError, ValueError) as e:
            # Not silent, and not wiped: an unreadable record is kept for
            # a person to look at, and the mirror starts with none.
            logging.critical('MIRROR: %s could not be read (%s) - the '
                             'followers\' open copies must be checked by '
                             'hand', self.state_path, e)
            self.records = {}

    #: Records kept: the newest this many trades, plus any trade a
    #: follower still holds or is still working on.
    KEEP_TRADES = 300

    def _prune(self):
        if len(self.records) <= self.KEEP_TRADES:
            return
        live = ('OPEN', 'PENDING', 'CLOSING', CLOSE_FAILED)
        order = sorted(self.records.items(),
                       key=lambda kv: -(kv[1].get('opened_at') or 0))
        for pid, record in order[self.KEEP_TRADES:]:
            if record.get('gone') and not any(
                    fr.get('status') in live
                    for fr in record['followers'].values()):
                del self.records[pid]

    def _save(self):
        self._prune()
        try:
            tmp = f'{self.state_path}.{os.getpid()}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'records': self.records, 'closed': self.closed,
                           'halted': self.halted, 'cash': self.cash}, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logging.error('MIRROR: could not save %s: %s', self.state_path,
                          e)

    # -- every poll ---------------------------------------------------------

    def observe(self, positions, close_style=None):
        """The Algo's open positions now: [{'position_id', 'pair_key',
        'opened_at', 'style', 'legs': [{'symbol', 'side', 'volume'}, ...]}]
        - `style` MAKER or TAKER, how the trade went on. New ones are
        copied, gone ones closed - on the followers' threads.
        `close_style(position_id)`: how a trade that has gone came off
        (MAKER or TAKER); TAKER when it cannot be said."""
        now = self.clock()
        jobs = []
        with self._lock:
            seen = set()
            for pos in positions or ():
                pid = pos['position_id']
                seen.add(pid)
                if pid in self.records:
                    continue
                late = now - float(pos.get('opened_at') or now)
                record = {'pair_key': pos['pair_key'],
                          'opened_at': pos.get('opened_at'),
                          'style': pos.get('style') or 'TAKER',
                          'legs': [dict(leg) for leg in pos['legs']],
                          'gone': False, 'followers': {}}
                self.records[pid] = record
                for follower in self.followers.values():
                    if not follower.follows(pos['pair_key']):
                        continue
                    blocked = self._blocked(follower, now)
                    if self.paused:
                        record['followers'][follower.name] = {
                            'status': SKIPPED,
                            'why': 'copying is switched off'}
                    elif not follower.enabled:
                        record['followers'][follower.name] = {
                            'status': SKIPPED, 'why': 'follower switched off'}
                    elif blocked:
                        record['followers'][follower.name] = {
                            'status': SKIPPED, 'why': blocked, 'at': now}
                    elif late > self.open_window_sec:
                        record['followers'][follower.name] = {
                            'status': SKIPPED,
                            'why': f'first seen {late:.0f}s after it opened '
                                   f'- not copied late'}
                    else:
                        record['followers'][follower.name] = {
                            'status': 'PENDING'}
                        jobs.append(('open', pid, follower.name))
            for pid, record in self.records.items():
                if pid in seen:
                    continue
                if not record.get('gone'):
                    record['gone'] = True
                    style = 'TAKER'
                    if close_style is not None:
                        try:
                            style = close_style(pid) or 'TAKER'
                        except Exception:
                            style = 'TAKER'
                    record['close_style'] = style
                for name, fr in record['followers'].items():
                    if fr.get('status') == OPEN or (
                            fr.get('status') == CLOSE_FAILED and
                            now - float(fr.get('tried_at') or 0)
                            >= CLOSE_RETRY_SEC):
                        fr['status'] = 'CLOSING'
                        jobs.append(('close', pid, name))
            # THE CLIENT'S MONEY: each follower's balance and deposits /
            # withdrawals, read now and then on its own thread - after any
            # order it has to send, never before one.
            for name in self.followers:
                if now >= self._cash_due.get(name, 0.0):
                    self._cash_due[name] = now + CASH_REFRESH_SEC
                    jobs.append(('cash', None, name))
            if any(job[0] != 'cash' for job in jobs):
                self._save()
        for job in jobs:
            self._submit(job)

    # -- the account's limits ------------------------------------------------

    @staticmethod
    def _day_start(now):
        return now - (now % 86400.0)

    @staticmethod
    def _month_start(now):
        import calendar
        t = time.gmtime(now)
        return float(calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0)))

    def pnl_since(self, name, since):
        return sum(c.get('pnl') or 0.0 for c in self.closed.get(name, ())
                   if (c.get('closed_at') or 0) >= since)

    def _blocked(self, follower, now):
        """Why this follower takes no NEW copy now, in words, or None.
        Closes are never held back."""
        halt = self.halted.get(follower.name)
        if halt:
            return f'halted: {halt.get("why") or "by the trader"}'
        day = self._day_start(now)
        if follower.daily_loss_limit:
            lost = self.pnl_since(follower.name, day)
            if lost <= -follower.daily_loss_limit:
                return (f'daily loss limit: {lost:+,.2f} today, limit '
                        f'-{follower.daily_loss_limit:,.2f}')
        if follower.max_trades_day:
            today = sum(1 for r in self.records.values()
                        if (r.get('opened_at') or 0) >= day and
                        (r['followers'].get(follower.name) or {}).get(
                            'status') in (OPEN, CLOSED, 'CLOSING',
                                          CLOSE_FAILED))
            if today >= follower.max_trades_day:
                return f'{today} copies today - its limit is ' \
                       f'{follower.max_trades_day}'
        return None

    def margin_held(self, name):
        return sum((r['followers'].get(name) or {}).get('margin') or 0.0
                   for r in self.records.values()
                   if (r['followers'].get(name) or {}).get('status')
                   in (OPEN, 'CLOSING', CLOSE_FAILED))

    # -- the screen's controls ----------------------------------------------

    def control(self, name, action, why=None):
        """HALT (no new copies), RESUME, or CLOSE_COPIES (halt, and
        close every copy this follower holds now, at market)."""
        action = str(action or '').upper()
        if name not in self.followers:
            return {'ok': False, 'reason': f'no follower {name}'}
        jobs = []
        now = self.clock()
        with self._lock:
            if action == 'RESUME':
                self.halted.pop(name, None)
            elif action in ('HALT', 'CLOSE_COPIES'):
                self.halted[name] = {'at': now, 'why': why or (
                    'halted by the trader' if action == 'HALT' else
                    'copies closed by the trader')}
                if action == 'CLOSE_COPIES':
                    for pid, record in self.records.items():
                        fr = record['followers'].get(name)
                        if fr and fr.get('status') in (OPEN, CLOSE_FAILED):
                            fr['status'] = 'CLOSING'
                            fr['forced'] = True
                            jobs.append(('close', pid, name))
            else:
                return {'ok': False, 'reason': f'unknown action {action}'}
            self._save()
        for job in jobs:
            self._submit(job)
        return {'ok': True, 'follower': name, 'action': action,
                'closing': len(jobs)}

    def _submit(self, job):
        if not self.background:
            self._run(job)
            return
        name = job[2]
        with self._lock:
            self._pending += 1
            jobs = self._queues.setdefault(name, queue.Queue())
            jobs.put(job)
            worker = self._workers.get(name)
            if worker is None or not worker.is_alive():
                worker = threading.Thread(target=self._drain, args=(name,),
                                          daemon=True,
                                          name=f'mirror-{name}')
                self._workers[name] = worker
                worker.start()

    def _drain(self, name):
        jobs = self._queues[name]
        while True:
            try:
                job = jobs.get(timeout=5.0)
            except queue.Empty:
                with self._lock:
                    # Gone quiet: this thread ends, unless a job slipped in
                    # while it was deciding to.
                    if jobs.empty():
                        self._workers.pop(name, None)
                        return
                continue
            try:
                self._run(job)
            except Exception:                 # never let one kill the rest
                logging.exception('MIRROR: %s failed', job)
            finally:
                with self._lock:
                    self._pending -= 1

    def wait(self, timeout=10.0):
        """Until every follower's queue is done (tests, shutdown)."""
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if self._pending <= 0:
                    return True
            time.sleep(0.02)
        return False

    def _run(self, job):
        kind, pid, name = job
        follower = self.followers.get(name)
        if kind == 'cash':
            if follower is not None:
                self._read_cash(follower)
            return
        with self._lock:
            record = self.records.get(pid)
            fr = (record or {}).get('followers', {}).get(name)
        if follower is None or fr is None:
            return
        try:
            if kind == 'open':
                result = self._open(follower, pid, record)
            else:
                result = self._close(follower, pid, record, fr)
        except Exception as e:
            # Anything unexpected is THIS follower's failure, on the
            # record - never the next follower's problem.
            logging.exception('MIRROR %s: %s %s failed', name, kind, pid)
            follower.last_error = f'{kind} failed: {e}'
            now = self.clock()
            result = ({'status': FAILED, 'why': follower.last_error,
                       'at': now} if kind == 'open' else
                      {'status': CLOSE_FAILED, 'why': follower.last_error,
                       'tried_at': now})
        with self._lock:
            fr.update(result)
            # A trade that closed while its copy was going on: close the
            # copy now rather than leave it to the next poll.
            reclose = kind == 'open' and record.get('gone') and \
                fr.get('status') == OPEN
            if reclose:
                fr['status'] = 'CLOSING'
            self._save()
        if reclose:
            self._run(('close', pid, name))
        with self._lock:
            # A follower taken off the config goes once it holds nothing.
            if follower.removed and not self._holds(name):
                self.followers.pop(name, None)

    def _cash_summary(self, name):
        """What OKX says moved in and out of this account: the totals
        and the latest few - for the Managed Accounts screen."""
        entry = self.cash.get(name) or {}
        flows = sorted((entry.get('flows') or {}).values(),
                       key=lambda f: -(f.get('at') or 0))
        return {'deposited': round(sum(f['amount'] for f in flows
                                       if f['amount'] > 0), 2),
                'withdrawn': round(-sum(f['amount'] for f in flows
                                        if f['amount'] < 0), 2),
                'count': len(flows), 'recent': flows[:12],
                'read_at': entry.get('read_at'),
                'error': entry.get('error')}

    def _read_cash(self, follower):
        """This follower's balance (OKX's total equity) and its money in
        and out (OKX's funding ledger), merged into what is kept by OKX's
        own bill id - read again, nothing is counted twice. A read that
        fails keeps what was there and says why."""
        now = self.clock()
        entry = self.cash.setdefault(follower.name, {'flows': {}})
        try:
            broker = follower.broker(self.ledger_dir)
            info = broker.account_info()
            if info is not None:
                entry['balance'] = {
                    'equity': float(info.equity), 'at': now,
                    # What it can still put on, and what its open
                    # positions tie up - OKX's own figures.
                    'available': (float(info.margin_free)
                                  if info.margin_free is not None else None),
                    'margin_used': (float(info.margin)
                                    if info.margin is not None else None),
                    'unrealized': (float(info.profit)
                                   if getattr(info, 'profit', None)
                                   is not None else None)}
            # OKX's funding ledger, ALL of it (since Feb 2021): the newest
            # page every read, and - the first time - every page back to
            # the account's first transfer, at OKX's 1 a second.
            def page(after=None):
                params = {'ccy': 'USDT', 'limit': '100'}
                if after:
                    params['after'] = after
                return broker.client.get('/api/v5/asset/bills-history',
                                         params, private=True) or []

            def keep(bills):
                for bill in bills:
                    kind = CASH_BILL_TYPES.get(str(bill.get('type')))
                    if kind is None or not bill.get('billId'):
                        continue
                    entry['flows'][str(bill['billId'])] = {
                        'at': float(bill.get('ts') or 0) / 1000.0,
                        'amount': float(bill.get('balChg') or 0.0),
                        'kind': kind, 'source': 'OKX'}
            bills = page()
            keep(bills)
            if not entry.get('history_done'):
                pages = 0
                while bills and len(bills) >= 100 and pages < CASH_PAGES:
                    self.sleep(1.1)
                    bills = page(bills[-1].get('ts'))
                    keep(bills)
                    pages += 1
                entry['history_done'] = True
            entry['error'] = None
        except Exception as e:
            entry['error'] = f'could not read the balance from OKX: {e}'
        entry['read_at'] = now
        with self._lock:
            self._save()

    # -- the orders ---------------------------------------------------------

    def _open(self, follower, pid, record):
        now = self.clock()
        try:
            broker = follower.broker(self.ledger_dir)
        except Exception as e:
            follower.last_error = f'could not connect: {e}'
            return {'status': FAILED, 'why': follower.last_error, 'at': now}
        sizes = []
        for leg in record['legs']:
            info = broker.symbol_info(leg['symbol'])
            volume = round_down(float(leg['volume']) * follower.multiple,
                                getattr(info, 'volume_step', 0.0),
                                getattr(info, 'volume_min', 0.0))
            if volume <= 0:
                return {'status': SKIPPED, 'at': now,
                        'why': f'{leg["symbol"]}: {leg["volume"]:g} x '
                               f'{follower.multiple:g} is under the '
                               f'smallest size'}
            sizes.append(volume)
        # THE MARGIN LIMIT: this copy and what the account already holds.
        margin = self._margin_of(broker, record['legs'], sizes)
        if follower.max_margin and margin is not None:
            held = self.margin_held(follower.name)
            if held + margin > follower.max_margin + 1e-9:
                return {'status': SKIPPED, 'at': now,
                        'why': f'margin: {held:,.2f} held + {margin:,.2f} '
                               f'this copy is over its {follower.max_margin:,.2f}'}
        style = ('TAKER' if follower.execution == 'TAKER'
                 else record.get('style') or 'TAKER')
        done = []
        legs = list(zip(record['legs'], sizes))
        if style == 'MAKER':
            # MAKER, as the trade went on: the first leg RESTS at this
            # account's own touch; the second is hedged at market for
            # what filled. Nothing filled in time: not copied - a copy
            # never chases.
            first, first_volume = legs[0]
            try:
                filled, parts = self._maker(
                    broker, first['symbol'], OrderSide(first['side']),
                    first_volume, follower.maker_timeout_sec,
                    comment=f'mirror {pid}')
            except Exception as e:
                follower.last_error = (
                    f'maker: {first["symbol"]} could not be worked ({e}) - '
                    f'CHECK THIS ACCOUNT for a resting or filled order')
                return {'status': FAILED, 'at': now, 'style': style,
                        'why': follower.last_error}
            if filled <= 0:
                return {'status': SKIPPED, 'at': now, 'style': style,
                        'why': f'maker: {first["symbol"]} did not fill in '
                               f'{follower.maker_timeout_sec:g}s - not '
                               f'copied (a copy never chases)'}
            done.extend(self._part(broker, first, part, 'maker')
                        for part in parts)
            info = broker.symbol_info(legs[1][0]['symbol'])
            legs = [(legs[1][0], round_down(
                legs[1][1] * filled / first_volume,
                getattr(info, 'volume_step', 0.0),
                getattr(info, 'volume_min', 0.0)) or legs[1][1])]
        for leg, volume in legs:
            result = broker.send_market_order(
                leg['symbol'], OrderSide(leg['side']), volume,
                comment=f'mirror {pid}')
            if result.ticket is not None and result.volume:
                done.append(self._part(broker, leg, {
                    'ticket': result.ticket, 'volume': float(result.volume),
                    'price': result.executed_price}, 'taker'))
            if not result.success:
                # NEVER ONE LEG: what went on is taken off again.
                undone = self._close_legs(broker, done)
                why = (f'{leg["symbol"]} did not go on: {result.error}'
                       + ('' if not done else
                          '; the leg that did was closed again'
                          if not undone else
                          f'; AND the leg that did could not be closed: '
                          f'{"; ".join(undone)} - check this account'))
                follower.last_error = why
                return {'status': FAILED if not undone else CLOSE_FAILED,
                        'why': why, 'legs': done if undone else [],
                        'at': now, 'tried_at': now}
        follower.last_error = None
        return {'status': OPEN, 'legs': done, 'at': now, 'style': style,
                'margin': margin}

    @staticmethod
    def _margin_of(broker, legs, sizes):
        """What this copy ties up, both legs, or None when the account
        cannot price it (and then the margin limit cannot be judged)."""
        try:
            return sum(float(broker.margin_for(leg['symbol'],
                                               OrderSide(leg['side']), size))
                       for leg, size in zip(legs, sizes))
        except Exception:
            return None

    @staticmethod
    def _fee_bps(broker, symbol, kind):
        try:
            maker, taker = broker.fees_bps(symbol)
            return float(maker if kind == 'maker' else taker)
        except Exception:
            return 0.0

    def _part(self, broker, leg, fill, kind):
        """One fill of one leg, as it is kept: its ticket, size, price,
        contract size and the fee it paid."""
        info = broker.symbol_info(leg['symbol'])
        ct = float(getattr(info, 'contract_size', 1.0) or 1.0)
        volume = float(fill['volume'])
        price = fill.get('price')
        fee = (abs(price) * volume * ct * self._fee_bps(
            broker, leg['symbol'], kind) / 1e4) if price else 0.0
        return {'symbol': leg['symbol'], 'side': leg['side'],
                'ticket': fill['ticket'], 'volume': volume,
                'entry_volume': volume, 'price': price, 'ct': ct,
                'fee_in': round(fee, 6), 'exit_volume': 0.0,
                'exit_value': 0.0, 'fee_out': 0.0, 'kind': kind}

    def _maker(self, broker, symbol, side, volume, timeout, comment='',
               close_ticket=None):
        """Rest a post-only order at this account's own touch (the bid
        to buy, the offer to sell), re-priced as the book moves, until it
        fills or `timeout` runs out. -> (filled volume, [{'ticket',
        'volume', 'price'}] one per order that filled)."""
        remaining, filled_total, parts = float(volume), 0.0, []
        elapsed, ticket, placed, order_filled = 0.0, None, None, 0.0

        def touch():
            tick = broker.symbol_tick(symbol)
            if tick is None:
                return None
            return tick.bid if side is OrderSide.BUY else tick.ask

        def finish(state):
            nonlocal filled_total, remaining
            done = float(state.get('filled_volume') or 0.0)
            if done > 0:
                tickets = state.get('position_tickets') or [ticket]
                parts.append({'ticket': tickets[0] if close_ticket is None
                              else close_ticket, 'volume': done,
                              'price': state.get('price')})
                filled_total += done
                remaining -= done
        while remaining > 1e-12 and elapsed < timeout:
            price = touch()
            if ticket is None:
                if price is None:
                    self.sleep(MAKER_POLL_SEC)
                    elapsed += MAKER_POLL_SEC
                    continue
                res = broker.place_pending_limit(
                    symbol, side, remaining, price, comment=comment,
                    position_ticket=close_ticket, post_only=True)
                if not res.get('ok'):
                    self.sleep(MAKER_POLL_SEC)
                    elapsed += MAKER_POLL_SEC
                    continue
                ticket, placed, order_filled = res['ticket'], res.get(
                    'price', price), 0.0
            self.sleep(MAKER_POLL_SEC)
            elapsed += MAKER_POLL_SEC
            state = broker.order_fill_state(ticket)
            order_filled = float(state.get('filled_volume') or 0.0)
            if not state.get('still_open'):
                # Filled - or cancelled because a post-only would have
                # crossed: either way this order is done; any rest goes
                # on a new one at the new touch.
                finish(state)
                ticket = None
                continue
            if price is not None and placed is not None and \
                    abs(price - placed) > 1e-12:
                if broker.modify_pending(ticket, price).get('ok'):
                    placed = price
        if ticket is not None:
            finish(broker.cancel_pending(ticket))
        return filled_total, parts

    def _close(self, follower, pid, record, fr):
        now = self.clock()
        try:
            broker = follower.broker(self.ledger_dir)
        except Exception as e:
            follower.last_error = f'could not connect: {e}'
            return {'status': CLOSE_FAILED, 'why': follower.last_error,
                    'tried_at': now}
        legs = fr.get('legs') or []
        style = ('TAKER' if follower.execution == 'TAKER' or fr.get('forced')
                 else record.get('close_style') or 'TAKER')
        if style == 'MAKER' and legs:
            # MAKER, as the trade came off: the first leg's fills rest at
            # the touch; what has not filled in time CROSSES - a copy
            # must always get out - and the second leg follows at market.
            first = legs[0]['symbol']
            try:
                for part in legs:
                    if part['symbol'] != first or part.get('volume', 0) <= 0:
                        continue
                    close_side = OrderSide(part['side']).opposite
                    _filled, done = self._maker(
                        broker, part['symbol'], close_side, part['volume'],
                        follower.maker_timeout_sec, comment='mirror close',
                        close_ticket=part['ticket'])
                    for fill in done:
                        self._took_off(broker, part, fill['volume'],
                                       fill.get('price'), 'maker')
            except Exception as e:
                # The maker exit could not be worked: CROSS what is left,
                # now - a copy must always get out.
                logging.warning('MIRROR %s: maker close of %s failed (%s) '
                                '- crossing', follower.name, pid, e)
                style = 'TAKER'
        failed = self._close_legs(broker, legs, self)
        if failed:
            follower.last_error = '; '.join(failed)
            return {'status': CLOSE_FAILED, 'why': follower.last_error,
                    'tried_at': now, 'legs': legs}
        gross, fees = self.pnl_of(legs)
        pnl = round(gross - fees, 6)
        with self._lock:
            self.closed.setdefault(follower.name, []).append({
                'position_id': pid, 'pair_key': record.get('pair_key'),
                'opened_at': fr.get('at') or record.get('opened_at'),
                'closed_at': now, 'gross': round(gross, 6),
                'fees': round(fees, 6), 'pnl': pnl,
                'margin': fr.get('margin'), 'forced': bool(fr.get('forced')),
                'style_in': fr.get('style'), 'style_out': style})
        return {'status': CLOSED, 'closed_at': now, 'why': None,
                'legs': legs, 'gross': round(gross, 6),
                'fees': round(fees, 6), 'pnl': pnl, 'style_out': style}

    def _took_off(self, broker, part, volume, price, kind):
        """Book `volume` of this part as closed at `price`."""
        volume = float(volume or 0.0)
        if volume <= 0:
            return
        part['volume'] = max(0.0, part['volume'] - volume)
        part['exit_volume'] = part.get('exit_volume', 0.0) + volume
        if price:
            part['exit_value'] = part.get('exit_value', 0.0) + price * volume
            part['fee_out'] = round(part.get('fee_out', 0.0) + abs(price) *
                                    volume * part.get('ct', 1.0) *
                                    self._fee_bps(broker, part['symbol'],
                                                  kind) / 1e4, 6)

    @staticmethod
    def pnl_of(legs):
        """(gross, fees) of a closed copy, from its own fills: each part's
        exit against its entry, times its size and contract size, signed
        for its side; the fees both ways."""
        gross = fees = 0.0
        for part in legs or ():
            fees += (part.get('fee_in') or 0.0) + (part.get('fee_out') or 0.0)
            done = part.get('exit_volume') or 0.0
            if not done or not part.get('price') or not part.get('exit_value'):
                continue
            exit_avg = part['exit_value'] / done
            sign = 1.0 if part['side'] == 'BUY' else -1.0
            gross += sign * (exit_avg - part['price']) * done * \
                (part.get('ct') or 1.0)
        return gross, fees

    @staticmethod
    def _close_legs(broker, legs, mirror=None):
        """Close each leg by its ticket, at market. What could not be
        closed, in words; each leg's open volume is brought down by what
        closed (and, with `mirror`, its exit booked for the P&L)."""
        failed = []
        for leg in legs:
            if leg.get('volume', 0) <= 0:
                continue
            result = broker.close_position_ticket(
                leg['symbol'], leg['ticket'], leg['volume'],
                OrderSide(leg['side']), comment='mirror close')
            done = float(result.volume or 0.0)
            if mirror is not None:
                mirror._took_off(broker, leg, done, result.executed_price,
                                 'taker')
            else:
                leg['volume'] = max(0.0, leg['volume'] - done)
            if not result.success and leg['volume'] > 0:
                failed.append(f'{leg["symbol"]}: {result.error}')
        return failed

    # -- for the screen -----------------------------------------------------

    def snapshot(self, limit=60):
        """For the screen: each follower with its totals - copies held now,
        today's copies, failures, the last thing it did - the trades,
        newest first, and what needs a person's attention."""
        now = self.clock()
        day_start = now - (now % 86400.0)
        live = ('OPEN', 'PENDING', 'CLOSING', CLOSE_FAILED)
        with self._lock:
            rows = sorted(self.records.items(),
                          key=lambda kv: -(kv[1].get('opened_at') or 0))
            followers = []
            attention = []
            for f in self.followers.values():
                held = copied = failed = skipped = 0
                last = None
                for pid, record in rows:
                    fr = record['followers'].get(f.name)
                    if fr is None:
                        continue
                    status = fr.get('status')
                    stamp = max(float(fr.get('closed_at') or 0),
                                float(fr.get('at') or 0),
                                float(fr.get('tried_at') or 0)) or None
                    if stamp and (last is None or stamp > last):
                        last = stamp
                    if status in live and status != CLOSE_FAILED:
                        held += 1
                    if (record.get('opened_at') or 0) >= day_start:
                        if status in (OPEN, CLOSED, 'CLOSING'):
                            copied += 1
                        elif status in (FAILED, CLOSE_FAILED):
                            failed += 1
                        elif status == SKIPPED:
                            skipped += 1
                    # A copy still held after its trade closed - or one
                    # that would not close - is what a person must see.
                    if status == CLOSE_FAILED:
                        attention.append({'follower': f.name,
                                          'position_id': pid,
                                          'why': fr.get('why') or
                                          'its close failed',
                                          'severity': 'high'})
                    elif status == FAILED and \
                            (record.get('opened_at') or 0) >= day_start:
                        attention.append({'follower': f.name,
                                          'position_id': pid,
                                          'why': fr.get('why') or
                                          'its copy did not go on',
                                          'severity': 'low'})
                month = self._month_start(now)
                closed = self.closed.get(f.name, [])
                pnl_today = self.pnl_since(f.name, day_start)
                pnl_month = self.pnl_since(f.name, month)
                pnl_total = sum(c.get('pnl') or 0.0 for c in closed)
                followers.append(dict(
                    f.to_dict(), holding=held, copied_today=copied,
                    failed_today=failed, skipped_today=skipped,
                    last_at=last, halted=self.halted.get(f.name),
                    blocked=self._blocked(f, now),
                    margin_held=round(self.margin_held(f.name), 2),
                    trades_today=sum(
                        1 for r in self.records.values()
                        if (r.get('opened_at') or 0) >= day_start and
                        (r['followers'].get(f.name) or {}).get('status')
                        in (OPEN, CLOSED, 'CLOSING', CLOSE_FAILED)),
                    pnl_today=round(pnl_today, 2),
                    pnl_month=round(pnl_month, 2),
                    pnl_total=round(pnl_total, 2),
                    closed_count=len(closed),
                    balance=(self.cash.get(f.name) or {}).get('balance'),
                    cash=self._cash_summary(f.name),
                    return_month_pct=(round(100.0 * pnl_month / f.capital, 3)
                                      if f.capital else None),
                    return_total_pct=(round(100.0 * pnl_total / f.capital, 3)
                                      if f.capital else None)))
            trades = []
            for pid, rec in rows[:limit]:
                counts = {}
                for fr in rec['followers'].values():
                    counts[fr.get('status')] = \
                        counts.get(fr.get('status'), 0) + 1
                trades.append(dict(position_id=pid, counts=counts,
                                   **json.loads(json.dumps(rec))))
            return {'on': True, 'at': now, 'paused': self.paused,
                    'open_window_sec': self.open_window_sec,
                    'followers': followers,
                    'summary': {
                        'followers': len(followers),
                        'following': sum(1 for f in followers
                                         if f['enabled']),
                        'holding': sum(f['holding'] for f in followers),
                        'failed_today': sum(f['failed_today']
                                            for f in followers),
                        'attention': len([a for a in attention
                                          if a['severity'] == 'high']),
                        'open_trades': sum(1 for _pid, r in rows
                                           if not r.get('gone'))},
                    'attention': attention[:50],
                    'trades': trades}


# -- the follower's reports -------------------------------------------------

def load_closed(state_path):
    """Every closed copy, per follower, as `mirror_state.json` holds it -
    for the web page, which runs apart from the engine. {} when there is
    no record yet."""
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            return json.load(f).get('closed') or {}
    except (OSError, ValueError):
        return {}


def report_positions(closed, name, since=None):
    """One follower's closed copies as the desk's own report reads its
    positions: a closed trade with its net P&L, by the time it closed.
    Nothing about the copy's size or legs leaves here."""
    rows = []
    for c in closed.get(name, ()):
        if c.get('closed_at') is None or (since and c['closed_at'] < since):
            continue
        rows.append({'position_id': c.get('position_id'),
                     'pair_key': c.get('pair_key'), 'side': None,
                     'quantity': None, 'source': 'ALGO',
                     'opened_at': c.get('opened_at'),
                     'closed_at': c['closed_at'],
                     'realized_pnl': c.get('pnl'),
                     'close_reason': ('closed by the trader' if
                                      c.get('forced') else 'mirrored'),
                     'entry_margin': c.get('margin')})
    return rows
