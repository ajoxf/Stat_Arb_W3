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
                 base_url=None, broker=None):
        self.name = str(name)
        self.multiple = float(multiple)
        self.enabled = bool(enabled)
        self.demo = bool(demo)
        #: None: every ladder. Otherwise only these pair keys.
        self.pairs = None if not pairs else set(pairs)
        self.base_url = base_url
        self._broker = broker
        self.last_error = None

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
                'last_error': self.last_error}


class Mirror:
    """Copies the Algo's positions into the followers."""

    def __init__(self, followers, state_path, open_window_sec=None,
                 clock=time.time, background=True, ledger_dir=None):
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
        followers = []
        for row in section.get('followers') or ():
            try:
                multiple = float(row.get('multiple'))
            except (TypeError, ValueError):
                logging.error('MIRROR: follower %r has no multiple - left '
                              'out', row.get('name'))
                continue
            if not row.get('name') or multiple <= 0:
                logging.error('MIRROR: follower %r needs a name and a '
                              'multiple above 0 - left out', row)
                continue
            followers.append(Follower(
                row['name'], multiple, enabled=row.get('enabled', True),
                demo=row.get('demo', True), pairs=row.get('pairs'),
                base_url=row.get('base_url')))
        if not followers:
            return None
        return cls(followers, state_path,
                   open_window_sec=section.get('open_window_sec'), **kw)

    # -- the record ---------------------------------------------------------

    def _load(self):
        try:
            with open(self.state_path, 'r', encoding='utf-8') as f:
                self.records = json.load(f).get('records') or {}
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
                json.dump({'records': self.records}, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logging.error('MIRROR: could not save %s: %s', self.state_path,
                          e)

    # -- every poll ---------------------------------------------------------

    def observe(self, positions):
        """The Algo's open positions now: [{'position_id', 'pair_key',
        'opened_at', 'legs': [{'symbol', 'side', 'volume'}, ...]}].
        New ones are copied, gone ones closed - on the mirror's thread."""
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
                          'legs': [dict(leg) for leg in pos['legs']],
                          'gone': False, 'followers': {}}
                self.records[pid] = record
                for follower in self.followers.values():
                    if not follower.follows(pos['pair_key']):
                        continue
                    if not follower.enabled:
                        record['followers'][follower.name] = {
                            'status': SKIPPED, 'why': 'follower switched off'}
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
                for name, fr in record['followers'].items():
                    if fr.get('status') == OPEN or (
                            fr.get('status') == CLOSE_FAILED and
                            now - float(fr.get('tried_at') or 0)
                            >= CLOSE_RETRY_SEC):
                        fr['status'] = 'CLOSING'
                        jobs.append(('close', pid, name))
            if jobs:
                self._save()
        for job in jobs:
            self._submit(job)

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
        done = []
        for leg, volume in zip(record['legs'], sizes):
            result = broker.send_market_order(
                leg['symbol'], OrderSide(leg['side']), volume,
                comment=f'mirror {pid}')
            if result.ticket is not None and result.volume:
                done.append({'symbol': leg['symbol'], 'side': leg['side'],
                             'ticket': result.ticket,
                             'volume': float(result.volume),
                             'price': result.executed_price})
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
        return {'status': OPEN, 'legs': done, 'at': now}

    def _close(self, follower, pid, record, fr):
        now = self.clock()
        try:
            broker = follower.broker(self.ledger_dir)
        except Exception as e:
            follower.last_error = f'could not connect: {e}'
            return {'status': CLOSE_FAILED, 'why': follower.last_error,
                    'tried_at': now}
        failed = self._close_legs(broker, fr.get('legs') or [])
        if failed:
            follower.last_error = '; '.join(failed)
            return {'status': CLOSE_FAILED, 'why': follower.last_error,
                    'tried_at': now,
                    'legs': [leg for leg in fr.get('legs') or ()
                             if leg.get('volume', 0) > 0]}
        return {'status': CLOSED, 'closed_at': now, 'why': None}

    @staticmethod
    def _close_legs(broker, legs):
        """Close each leg by its ticket. What could not be closed, in
        words; each leg's open volume is brought down by what closed."""
        failed = []
        for leg in legs:
            if leg.get('volume', 0) <= 0:
                continue
            result = broker.close_position_ticket(
                leg['symbol'], leg['ticket'], leg['volume'],
                OrderSide(leg['side']), comment='mirror close')
            leg['volume'] = max(0.0, leg['volume'] - float(
                result.volume or 0.0))
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
                followers.append(dict(f.to_dict(), holding=held,
                                      copied_today=copied,
                                      failed_today=failed,
                                      skipped_today=skipped, last_at=last))
            trades = []
            for pid, rec in rows[:limit]:
                counts = {}
                for fr in rec['followers'].values():
                    counts[fr.get('status')] = \
                        counts.get(fr.get('status'), 0) + 1
                trades.append(dict(position_id=pid, counts=counts,
                                   **json.loads(json.dumps(rec))))
            return {'on': True, 'at': now,
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
