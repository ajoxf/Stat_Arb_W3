"""The web process: it renders, and it asks. It never trades.

The browser talks to this Flask app; the orders are placed by the
coordinator, which is the only process holding the legs. Everything the
UI shows comes from the coordinator's status snapshot, and everything
it does goes out as a command through `commands.py` — which is executed
once and never replayed.

Two rules from the spec shape the endpoints here:

- **Symbol setup must work with the coordinator DOWN.** Otherwise the
  system deadlocks: the coordinator will not start until the symbols
  are right, and these are the tools for finding out. So the account
  and symbol endpoints open a short-lived connection straight to OKX.
- **Never send the operator to a log for a decision already made.** A
  refusal carries the broker's — or the config's — own words, in the
  response body, for the panel to print.
"""

import datetime
import logging
import json
import os
import re
import secrets
import threading
import time
from datetime import date

from flask import Flask, jsonify, render_template, request

from . import analysis, atomicfile, config as cfg, diagnostics, fairvalue, \
    hedgeratio, signals, sizing, slippage
from .commands import CommandLog
from .legs import LocalLeg

#: How old the status file may be before the UI says the engine is not
#: running. Six polls at the default 0.3s: long enough not to flicker,
#: short enough that a dead coordinator is not mistaken for a quiet
#: market — which is the one confusion that gets orders clicked into a
#: screen nothing is behind.
STATUS_STALE_SEC = 2.0


def create_app(status_path='status.json', command_path='commands.jsonl',
               results_path='results.json', config_path='config.json',
               db_path='okxtrader.db'):
    app = Flask(__name__)
    # The TEMPLATE is compiled once and cached for the life of the
    # process unless this is on. A `git pull` therefore updated the CSS
    # and the JS — they are fetched by URL with a stamp — while the
    # HTML stayed on the version the process started with: new
    # handlers, old markup, elements missing, and a screen that looks
    # like the pull did not land. It is one file read per request, and
    # it makes a pull enough on its own.
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True
    commands = CommandLog(command_path)

    # --- the screen lock ---------------------------------------------
    #
    #     Enforced HERE, in the process that queues the orders. A lock
    #     drawn only in the browser is one that a refresh, a second tab
    #     or the developer console walks straight through - and what is
    #     on the other side of it places live trades.
    #
    #     The overlay in app.js is the courtesy; this is the guard.
    from . import screenlock as lockmod
    lock = lockmod.ScreenLock()
    env_path = os.path.join(os.path.dirname(os.path.abspath(config_path)),
                            '.env')

    def auto_lock_seconds():
        raw = cfg.load_raw(config_path)
        settings = dict(cfg.DEFAULT_SETTINGS)
        settings.update(raw.get('settings') or {})
        try:
            return float(settings.get('AUTO_LOCK_MINUTES') or 0) * 60.0
        except (TypeError, ValueError):
            return 0.0

    @app.before_request
    def refuse_while_locked():
        """THE GUARD ITSELF. Every state-changing request, in one place.

        Not a decorator on each route: a route added later would
        silently not be covered, and the one it would not cover is
        whichever one somebody adds in a hurry. A list of what is
        ALLOWED while locked is a list somebody has to defend.

        The idle check rides here too, on the polling the screen does
        anyway - so a browser that was closed without unlocking still
        finds the machine locked when it comes back.
        """
        bot_secret = app.config.get('BOT_SECRET')
        if bot_secret and secrets.compare_digest(
                request.headers.get('X-MT5Trader-Bot', ''), bot_secret):
            # The Telegram bot, from INSIDE this process (telegram.py).
            # The lock guards the desk PC; the bot has its own guard,
            # the allow-list of Telegram users, and this secret is made
            # fresh at start-up and never leaves the process.
            return None
        if request.method == 'GET':
            # Reading is not trading. The ladder goes on ticking behind
            # the overlay, exactly as it does behind TT's.
            lock.lock_if_idle(auto_lock_seconds())
            return None
        if request.path in ('/api/unlock', '/api/lock', '/api/pin'):
            return None
        if lock.locked:
            return jsonify({
                'ok': False, 'locked': True,
                'error': 'The screen is locked. Enter the PIN to trade.'
            }), 423
        lock.touch()
        return None

    @app.get('/api/lock')
    def api_lock_state():
        return jsonify({'locked': lock.locked,
                        'pin_set': lock.pin_is_set(),
                        'auto_lock_sec': auto_lock_seconds(),
                        'idle_sec': lock.idle_seconds()})

    @app.post('/api/lock')
    def api_lock():
        lock.lock()
        return jsonify({'ok': True, 'locked': True})

    @app.post('/api/unlock')
    def api_unlock():
        payload = request.get_json(silent=True) or {}
        ok, reason = lock.unlock(payload.get('pin'))
        return (jsonify({'ok': True, 'locked': False}) if ok
                else (jsonify({'ok': False, 'error': reason}), 403))

    @app.post('/api/pin')
    def api_set_pin():
        """Set or change the PIN.

        Changing it needs the CURRENT one. Setting the first one does
        not - there is nothing to prove yet, and a machine that has to
        be unlocked before it can be given a lock is a machine nobody
        can start using.
        """
        payload = request.get_json(silent=True) or {}
        if lock.pin_is_set():
            if lock.locked:
                return jsonify({'ok': False, 'error': 'Unlock the screen '
                                'before changing its PIN.'}), 423
            if not lockmod.verify_pin(payload.get('current'),
                                      lockmod.stored_hash()):
                return jsonify({'ok': False,
                                'error': 'The current PIN is wrong.'}), 403
        problem = lockmod.check_new_pin(payload.get('pin'),
                                        payload.get('again'))
        if problem:
            return jsonify({'ok': False, 'error': problem}), 400
        # The HASH, into .env, beside the account passwords. The PIN
        # itself is not written anywhere and is not in this response.
        cfg.write_env_value(env_path, lockmod.PIN_ENV_KEY,
                            lockmod.hash_pin(payload.get('pin')))
        lock.touch()
        return jsonify({'ok': True, 'pin_set': True})

    def store():
        """A read-only view of the coordinator's database.

        Opened per request rather than held: SQLite in WAL mode lets a
        reader in while the writer works, and a connection kept across
        requests in a threaded server is a connection used from the
        wrong thread.
        """
        from .database import Store
        try:
            return Store(db_path)
        except Exception as e:
            logging.error('the journal could not be opened: %s', e)
            return None

    #: The snapshot is re-read only when the FILE has changed. The
    #: browser polls three times a second and every tab polls
    #: separately, so the file is opened constantly — and on Windows an
    #: open file is one the coordinator's own `os.replace` cannot land
    #: on, which failed its whole poll (WinError 5). Two things keep
    #: this side of it out of the way: `os.stat` does not hold the file
    #: at all, so asking "has it changed?" is free; and the read itself
    #: goes through `atomicfile.read_text`, which opens share-delete so
    #: the publish lands underneath it.
    cache = {'stamp': None, 'body': None, 'path': None}

    def read_json(path, default=None):
        return atomicfile.read_json(path, default)

    def read_status_file():
        try:
            stat = os.stat(status_path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if cache['path'] == status_path and cache['stamp'] == stamp \
                and stamp is not None:
            return cache['body']
        body = read_json(status_path)
        cache.update({'stamp': stamp, 'body': body, 'path': status_path})
        return body

    def held_reasons():
        """What the launcher is holding the engine for, read from the
        same config by the same rules (`config.engine_hold_reasons`).
        Only asked while the engine is not up, so it costs nothing in
        the normal case."""
        try:
            return cfg.engine_hold_reasons(cfg.load_raw(config_path))
        except Exception:
            return []

    def waiting_note(held):
        # What to DO comes first: the banner is one line, and the
        # path in the reason can be long enough to push it off the end.
        return ('the engine is waiting — open Exchanges to fix its setup; '
                'it starts by itself once that is right. ' + held[0])

    def status():
        snapshot = read_status_file()
        if snapshot is None:
            held = held_reasons()
            if held:
                # Not "down": WAITING, and for something a person can
                # fix on this screen. Said, with where to fix it.
                return {'engine': 'down', 'engine_held': held,
                        'engine_note': waiting_note(held),
                        'pairs': {}, 'accounts': {}}
            return {'engine': 'down',
                    'engine_note': ('the coordinator is not running — no '
                                    'prices, and a click would go nowhere'),
                    'pairs': {}, 'accounts': {}}
        age = time.time() - (snapshot.get('at') or 0)
        snapshot['status_age_sec'] = age
        if age > STATUS_STALE_SEC:
            snapshot['engine'] = 'stalled'
            held = held_reasons()
            if held:
                # The launcher stopped the engine over a config change
                # it will not run on. The last snapshot is still on disk,
                # so the screen must say plainly that none of it is live.
                snapshot['engine_held'] = held
                snapshot['engine_note'] = (
                    waiting_note(held) + ' Nothing on this screen is live.')
            else:
                snapshot['engine_note'] = (
                    f'the last snapshot is {age:.1f}s old — the coordinator '
                    f'has stopped or is stuck. Nothing on this screen is '
                    f'live.')
        else:
            snapshot['engine'] = 'up'
        return snapshot

    # -- what the panels render from ---------------------------------------

    def asset_version():
        """A stamp that changes when the screen's own files change.

        Without it a browser serves the CSS and JS it cached last week
        against today's HTML — which is not merely stale, it is MIXED:
        new markup with old handlers, and one missing element takes out
        every button wired after it. `git pull` then has to be followed
        by a hard refresh that nobody remembers, and the screen looks
        broken in ways that are nowhere in the code.
        """
        newest = 0.0
        for name in ('static/app.js', 'static/settings.js',
                     'static/ladder.css', 'static/okx.css',
                     'templates/index.html'):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                name)
            try:
                newest = max(newest, os.path.getmtime(path))
            except OSError:
                pass
        return str(int(newest))

    @app.get('/')
    def index():
        response = app.make_response(
            render_template('index.html', asset_version=asset_version()))
        # The PAGE itself is never cached: it carries the stamp that
        # tells the browser whether its cached CSS and JS are current.
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/status')
    def api_status():
        # The lock rides on the poll the screen already makes, rather
        # than a second one of its own. The overlay has to appear the
        # instant the auto-lock fires, and a screen that learns it is
        # locked one poll late is a screen somebody clicked on.
        snapshot = status()
        snapshot['lock'] = {'locked': lock.locked,
                            'pin_set': lock.pin_is_set(),
                            'auto_lock_sec': auto_lock_seconds()}
        return jsonify(snapshot)

    @app.get('/api/chart/<path:key>')
    def api_chart(key):
        """One ladder's chart: its spread and the Algo's z over the
        last `minutes`, as the engine last wrote them. Read-only."""
        from .chart import chart_path
        try:
            minutes = max(1.0, min(float(request.args.get('minutes', 60)),
                                   120.0))
        except ValueError:
            minutes = 60.0
        body = read_json(chart_path(status_path)) or {}
        series = (body.get('pairs') or {}).get(key)
        if series is None:
            return jsonify({'ok': True, 'candles': [], 'minutes': minutes,
                            'note': 'no prices for this ladder yet \u2014 '
                                    'the chart fills while the engine '
                                    'runs'})
        at = body.get('at')
        candles = series.get('candles') or []
        if at is not None:
            since = float(at) - minutes * 60.0
            candles = [c for c in candles if c.get('t', 0) >= since]
        lows = [c['l'] for c in candles if c.get('l') is not None]
        highs = [c['h'] for c in candles if c.get('h') is not None]
        return jsonify(dict(series, ok=True, at=at, minutes=minutes,
                            candles=candles,
                            spread_min=min(lows) if lows else None,
                            spread_max=max(highs) if highs else None))

    @app.get('/api/chart_history/<path:key>')
    def api_chart_history(key):
        """One ladder's 15m / 1h / 4h candles, as the engine last fetched
        them from MT5 - or that there are none yet, so the screen asks
        the engine for them. Read-only."""
        from .chart import history_path
        tf = request.args.get('tf', '15')
        body = (read_json(history_path(status_path)) or {}).get(
            f'{key}#{tf}')
        if not body:
            return jsonify({'ok': False, 'missing': True})
        return jsonify(body)

    # -- the screen's layout, kept on the desk -------------------------------
    #
    # Where each window sits, its size, and which tools (charts, the
    # calculator, the monitor) are open. The browser keeps its own copy;
    # this one survives a cleared cache and follows the desk to another
    # browser. A preference, never state the engine reads.

    def layout_path():
        return os.path.join(os.path.dirname(os.path.abspath(config_path)),
                            'screen_layout.json')

    @app.get('/api/layout')
    def api_layout():
        return jsonify({'ok': True,
                        'layout': read_json(layout_path(), None)})

    @app.post('/api/layout')
    def api_save_layout():
        payload = request.get_json(silent=True) or {}
        layout = payload.get('layout')
        if not isinstance(layout, dict):
            return jsonify({'ok': False, 'error': 'a layout is an object'}), 400
        body = json.dumps(layout)
        if len(body) > 200000:
            return jsonify({'ok': False, 'error': 'layout too large'}), 400
        path = layout_path()
        tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(body)
        if not atomicfile.replace(tmp, path):
            try:
                os.remove(tmp)
            except OSError:
                pass
            return jsonify({'ok': False, 'error': 'could not be written'}), 503
        return jsonify({'ok': True})

    @app.get('/api/config')
    def api_config():
        raw = cfg.load_raw(config_path)
        # The password is a NAME here, never a value (spec §10).
        return jsonify(raw)

    # -- what a click does --------------------------------------------------

    @app.post('/api/command')
    def api_command():
        payload = request.get_json(silent=True) or {}
        kind = payload.get('kind')
        if not kind:
            return jsonify({'ok': False, 'error': 'a command needs a kind'}), 400
        snapshot = status()
        if snapshot.get('engine') != 'up' and kind not in ('set_pair',):
            # Refuse rather than queue: a command written while nothing
            # is running would be executed by whatever starts next, at
            # prices from another hour.
            return jsonify({'ok': False, 'error': snapshot['engine_note']}), 409
        command_id = commands.submit(kind, payload.get('payload'))
        return jsonify({'ok': True, 'id': command_id})

    @app.get('/api/result/<command_id>')
    def api_result(command_id):
        results = read_json(results_path, {}) or {}
        result = results.get(command_id)
        if result is None:
            return jsonify({'ok': None, 'pending': True})
        return jsonify(result)

    @app.get('/api/settings')
    def api_settings():
        """Every tunable, with its default beside it.

        A guessed number gets corrected from measurement, and that needs
        it on screen first.
        """
        raw = cfg.load_raw(config_path)
        settings = dict(cfg.DEFAULT_SETTINGS)
        settings.update(raw.get('settings') or {})
        from .commands import CommandRunner
        return jsonify({'settings': settings,
                        'defaults': cfg.DEFAULT_SETTINGS,
                        'hot': sorted(CommandRunner.HOT_SETTINGS),
                        'structural': list(cfg.STRUCTURAL_SETTINGS)})

    @app.post('/api/settings')
    def api_save_settings():
        """Save settings, and make the hot ones true NOW.

        Written to config.json so they survive a restart, and pushed to
        the running coordinator as a command so the next click already
        obeys them. A setting that only takes effect on restart says so
        rather than looking applied.
        """
        payload = request.get_json(silent=True) or {}
        fields = payload.get('fields') or {}
        unknown = [name for name in fields if name not in cfg.DEFAULT_SETTINGS]
        if unknown:
            return jsonify({'ok': False,
                            'error': 'not a setting: ' + ', '.join(unknown)}), 400
        raw = cfg.load_raw(config_path)
        raw.setdefault('settings', {}).update(fields)
        cfg.save_raw(config_path, raw)

        from .commands import CommandRunner
        hot = {name: value for name, value in fields.items()
               if name in CommandRunner.HOT_SETTINGS}
        if hot and status().get('engine') == 'up':
            commands.submit('set_setting', {'fields': hot})
        cold = [name for name in fields if name not in hot]
        return jsonify({'ok': True, 'applied_now': sorted(hot),
                        'restart_required': sorted(cold)})

    # -- the journal: Fills, as the BROKER reported them ------------------

    @app.get('/api/fills')
    def api_fills():
        """The trade journal.

        Read from MT5's own deal history, so it carries the trader's
        manual terminal clicks as well as ours — `is_ours` says which is
        which. This is the record that survives the process.
        """
        db = store()
        if db is None:
            return jsonify({'ok': False,
                            'error': 'the journal database could not be '
                                     'opened — fills are still at the '
                                     'broker, but this table cannot show '
                                     'them'}), 503
        pair_key = request.args.get('pair') or None
        account = request.args.get('account') or None
        ours = request.args.get('ours') == '1'
        limit = min(int(request.args.get('limit', 200)), 2000)
        fills = db.fills(pair_key=pair_key, account=account, ours_only=ours,
                         limit=limit)
        return jsonify({'ok': True, 'fills': fills,
                        'totals': db.fill_totals(pair_key,
                                                 ours_only=ours)})

    @app.get('/api/fills.csv')
    def api_fills_csv():
        """The same rows, for a spreadsheet. Broker time first, because
        that is the column that has to line up with MT5's History."""
        import csv
        import io
        db = store()
        if db is None:
            return 'the journal database could not be opened', 503
        fills = db.fills(pair_key=request.args.get('pair') or None,
                         limit=int(request.args.get('limit', 5000)))
        buffer = io.StringIO()
        columns = ['broker_time_ms', 'server_offset_s', 'account', 'symbol',
                   'pair_key', 'leg', 'side', 'entry', 'order_type', 'volume',
                   'price', 'commission', 'swap', 'profit', 'deal_id',
                   'order_id', 'position_ticket', 'is_ours', 'comment']
        writer = csv.DictWriter(buffer, fieldnames=columns,
                                extrasaction='ignore')
        writer.writeheader()
        for fill in fills:
            writer.writerow(fill)
        return (buffer.getvalue(), 200,
                {'Content-Type': 'text/csv',
                 'Content-Disposition': 'attachment; filename=fills.csv'})

    # -- the slippage report, over a real session --------------------------

    def slippage_report():
        """Build the report, or say why it cannot be built.

        Everything it needs is already recorded: the positions table
        holds the measured slippage at both ends, the status snapshot
        holds the measured broker offset that cuts the session, and the
        journal holds what the broker filled over the same stretch. This
        assembles them; it computes no price of its own.
        """
        db = store()
        if db is None:
            return None, ('the database could not be opened — the session '
                          'report is built from recorded positions, and '
                          'there are none to read')
        raw = cfg.load_raw(config_path)
        settings = dict(cfg.DEFAULT_SETTINGS)
        settings.update(raw.get('settings') or {})
        snapshot = status()
        offset = ((snapshot.get('broker_clock') or {}).get('offset_sec'))
        if str(settings.get('MARKET_HOURS', '24/7')).upper() == '24/7':
            # Round the clock: the last 24 hours, not a session cut at
            # an MT5 desk's 16:55.
            window = slippage.rolling_window(time.time())
        else:
            window = slippage.session_window(
                time.time(), offset,
                settings.get('OVERNIGHT_CLOSE_HOUR', 16),
                settings.get('OVERNIGHT_CLOSE_MINUTE', 55))
        if request.args.get('session') == 'all':
            window = dict(window, **{
                'from': None,
                'label': 'every session recorded',
                'note': ('every position in the database, not one session '
                         '— sessions are not comparable across a change of '
                         'symbol, ratio or clip size')})
        pair_key = request.args.get('pair') or None
        positions = db.positions_between(window['from'], window['to'],
                                         pair_key=pair_key)
        names = {key: (pair or {}).get('name') or key
                 for key, pair in (raw.get('pairs') or {}).items()}
        body = slippage.report(positions, window=window, names=names)
        # Which MT5 account, at which broker: the terminal's own word.
        body['account_info'] = {
            name: {'login': (info or {}).get('login'),
                   'broker': ((info or {}).get('company')
                              or (info or {}).get('server'))}
            for name, info in (snapshot.get('accounts') or {}).items()}
        # The journal over the SAME window, on the broker's stamps, as
        # the check on coverage: positions we know about against deals
        # the account actually saw.
        shift = (offset or 0) * 1000.0
        from_ms = None if window['from'] is None else \
            (window['from'] * 1000.0 + shift)
        try:
            body['journal'] = db.fills_between(
                from_ms, window['to'] * 1000.0 + shift, ours_only=True)
        except Exception as e:                    # a journal mid-write
            logging.error('the journal could not be counted: %s', e)
            body['journal'] = None
        # Maker or taker, and the gap between the legs: from OKX's own
        # fills over the same window, with this account's VIP rates.
        try:
            to_ms = window['to'] * 1000.0 + shift
            fills = [f for f in db.fills(ours_only=True, since_ms=from_ms,
                                         limit=5000)
                     if (f.get('broker_time_ms') or 0) <= to_ms]
            fee_bps = {key: (pair or {}).get('fee_bps')
                       for key, pair in (snapshot.get('pairs') or {}).items()}
            body['fill_quality'] = slippage.fill_quality(fills, positions,
                                                         fee_bps)
        except Exception as e:
            logging.error('fill quality could not be read: %s', e)
            body['fill_quality'] = None
        return body, None

    # -- the Analysis tab ------------------------------------------------------

    @app.get('/api/analysis')
    def api_analysis():
        """The Analysis tab, built from what is recorded: the positions
        table (with each trade's extremes), the Algo's audit trail for
        its z at both ends, the what-if shadows and the excursion
        counters. Computes no price of its own.

        `days`: trades OPENED in the last N days (0 = every one kept).
        `zone_days`: the SIGMA ZONE track's own range, which is a
            different question and has its own dropdown — the zone rows
            are a tick-by-tick record and a day of them is the usual
            read, where a day of trades is usually none. Filtered in
            the query (see `Store.zone_events`), never by handing the
            browser everything and throwing most of it away.
        `pair`: one ladder, or all.
        """
        db = store()
        if db is None:
            return jsonify({'ok': False,
                            'error': 'the database could not be opened'}), 503
        try:
            days = float(request.args.get('days', 7))
        except ValueError:
            days = 7.0
        pair_key = request.args.get('pair') or None
        now = time.time()
        start = None if days <= 0 else now - days * 86400.0
        positions = db.positions_between(start, None, pair_key=pair_key)
        events = db.events_between(('algo_signal', 'algo_order'), start)
        if pair_key:
            events = [e for e in events if e.get('pair_key') == pair_key]
        shadows = db.shadows(since=start, pair_key=pair_key)
        excursions = db.excursions()
        if pair_key:
            excursions = {k: v for k, v in excursions.items()
                          if k == pair_key}
        raw = cfg.load_raw(config_path)
        names = {key: (pair or {}).get('name') or key
                 for key, pair in (raw.get('pairs') or {}).items()}
        try:
            capital = max(0.0, float((raw.get('settings') or {}).get(
                'CAPITAL_MANAGED') or 0.0))
        except (TypeError, ValueError):
            capital = 0.0
        body = analysis.report(positions, events, shadows, excursions, now,
                               capital=capital)
        try:
            zone_days = float(request.args.get('zone_days', 1))
        except ValueError:
            zone_days = 1.0
        zone_since = None if zone_days <= 0 else now - zone_days * 86400.0
        # ONE DAY, on the BROKER's clock, beats the rolling window when
        # it is given: "5 Nov" is a question about a day, not about the
        # last 24 hours, and the two are different near a boundary.
        zone_date = (request.args.get('date') or '').strip() or None
        if zone_date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', zone_date):
            return jsonify({'ok': False,
                            'error': f'{zone_date} is not a date — '
                                     f'YYYY-MM-DD'}), 400
        try:
            signal_rows = db.signals(pair_key, since=zone_since,
                                     broker_day=zone_date)
            signal_days = db.signal_days(pair_key)
        except Exception as e:
            # A tab that cannot read one section still shows the rest:
            # the zone track is a reading, not the money.
            logging.error('the signal track could not be read: %s', e)
            signal_rows, signal_days = [], []
        # NOTIONAL TRADED: the broker's own deals over the last 30 days,
        # priced lots x contract size x price. A day of margin on the
        # query, because it is on the BROKER's clock.
        # A picked RANGE as well - from one broker day to another, both
        # inclusive - from the two date pickers. One of them alone is
        # that one day.
        def day(name):
            value = (request.args.get(name) or '').strip()
            return value if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) \
                else None
        volume_from, volume_to = day('volume_from'), day('volume_to')
        volume_from = volume_from or volume_to
        volume_to = volume_to or volume_from
        if volume_from and volume_to and volume_to < volume_from:
            volume_from, volume_to = volume_to, volume_from
        try:
            since = now - 31 * 86400.0
            picked = None
            if volume_from:
                # Midnights on the BROKER's clock: its stamps are its
                # wall clock written as an epoch.
                def midnight(value):
                    return datetime.datetime.strptime(
                        value, '%Y-%m-%d').replace(
                        tzinfo=datetime.timezone.utc).timestamp()
                picked = (midnight(volume_from),
                          midnight(volume_to) + 86400.0)
                since = min(since, picked[0] - 86400.0)
            deals = db.fills(pair_key=pair_key, ours_only=True,
                             since_ms=int(since * 1000), limit=200000)
            snapshot = status() or {}
            sizes = analysis.contract_sizes(
                snapshot.get('pairs'),
                db.positions_between(now - 400 * 86400.0, None))
            label = None
            if volume_from:
                label = volume_from if volume_from == volume_to \
                    else f'{volume_from} \u2192 {volume_to}'
            notional = analysis.notional_traded(
                deals, sizes, now, date=label,
                date_ms=None if picked is None else
                (int(picked[0] * 1000), int(picked[1] * 1000)))
            notional['from'], notional['to'] = volume_from, volume_to
            # Which MT5 account, at which broker, each account is: the
            # terminal's own word, read live.
            notional['account_info'] = {
                name: {'login': (info or {}).get('login'),
                       'broker': ((info or {}).get('company')
                                  or (info or {}).get('server'))}
                for name, info in (snapshot.get('accounts') or {}).items()}
        except Exception as e:
            logging.error('notional traded could not be read: %s', e)
            notional = None
        return jsonify(dict(body, ok=True, days=days, pair=pair_key,
                            names=names,
                            notional=notional,
                            zone_days=zone_days,
                            date=zone_date,
                            # The JOURNAL shape: newest first, each row
                            # carrying why it ended and a running total.
                            signals=signals.journal(signal_rows),
                            signal_days=signal_days,
                            signal_tally=signals.tally(signal_rows)))

    @app.get('/api/position/<position_id>')
    def api_position(position_id):
        """One position as recorded, with what its message needs that the
        record alone does not hold: each leg's CLOSING price from the
        broker's own closing deals, and the Algo's z at both ends."""
        db = store()
        if db is None:
            return jsonify({'ok': False,
                            'error': 'the database could not be opened'}), 503
        row = db.position(position_id)
        if row is None:
            return jsonify({'ok': False, 'error': 'no such position'}), 404
        exits = {}
        for leg in ('leg_a', 'leg_b'):
            fill = row.get(leg) or {}
            try:
                exits[leg] = db.closing_price(fill.get('account'),
                                              fill.get('position_tickets'))
            except Exception as e:                # a journal mid-write
                logging.error('closing price of %s: %s', position_id, e)
                exits[leg] = None
        opened = row.get('opened_at') or 0
        events = [e for e in db.events_between(
            ('algo_signal', 'algo_order'), opened - 120.0,
            (row.get('closed_at') or time.time()) + 60.0)
            if e.get('pair_key') == row.get('pair_key')]
        entry_z, exit_z = analysis.algo_z(events)
        return jsonify({'ok': True, 'position': row, 'exit_prices': exits,
                        'entry_z': entry_z.get(position_id),
                        'exit_z': exit_z.get(position_id)})

    @app.get('/api/slippage')
    def api_slippage():
        body, error = slippage_report()
        if body is None:
            return jsonify({'ok': False, 'error': error}), 503
        return jsonify(dict(body, ok=True))

    @app.get('/api/slippage.csv')
    def api_slippage_csv():
        """One row per position, both ends, for a spreadsheet.

        Empty cells, not zeros, where nothing was measured: a zero here
        would be read as a perfect fill by every tool that opens it.
        """
        import csv
        import io
        body, error = slippage_report()
        if body is None:
            return error, 503
        buffer = io.StringIO()
        columns = ['opened_at', 'closed_at', 'pair_key', 'side', 'quantity',
                   'order_type', 'entry_points', 'entry_money', 'exit_points',
                   'exit_money', 'round_trip_points', 'round_trip_money',
                   'click_to_on_ms', 'realized_pnl', 'position_id',
                   # Which LEG slipped: its own price points against the
                   # touch it was sent at, positive a cost.
                   'symbol_a', 'entry_leg_a', 'exit_leg_a',
                   'entry_leg_a_bps', 'exit_leg_a_bps',
                   'symbol_b', 'entry_leg_b', 'exit_leg_b',
                   'entry_leg_b_bps', 'exit_leg_b_bps']
        writer = csv.DictWriter(buffer, fieldnames=columns,
                                extrasaction='ignore')
        writer.writeheader()
        for row in body['rows']:
            writer.writerow({column: ('' if row.get(column) is None
                                      else row.get(column))
                             for column in columns})
        return (buffer.getvalue(), 200,
                {'Content-Type': 'text/csv',
                 'Content-Disposition':
                     'attachment; filename=slippage.csv'})

    @app.get('/api/algo_signals.csv')
    def api_algo_signals_csv():
        """Every Algo signal, for a spreadsheet: what it said, when, at
        what z and spread — and, for an exit, what the position would
        have made after costs. Signals only: nothing here was sent.

        Empty cells, not zeros, where nothing was measured.
        """
        import csv
        import io
        db = store()
        if db is None:
            return 'the database could not be opened', 503
        rows = db.events('algo_signal',
                         min(int(request.args.get('limit', 5000)), 50000))
        buffer = io.StringIO()
        columns = ['at', 'pair_key', 'mode', 'action', 'side', 'reason', 'z',
                   'spread', 'mid_spread', 'mean', 'sigma', 'upper', 'lower',
                   'entry_z', 'position_id', 'entry_spread', 'break_even',
                   'tp', 'net_pnl']
        writer = csv.DictWriter(buffer, fieldnames=columns,
                                extrasaction='ignore')
        writer.writeheader()
        for row in reversed(rows):                  # oldest first
            line = dict(row.get('detail') or {})
            line['at'] = time.strftime('%Y-%m-%d %H:%M:%S',
                                       time.localtime(row['at']))
            line['pair_key'] = row.get('pair_key')
            writer.writerow({c: ('' if line.get(c) is None else line.get(c))
                             for c in columns})
        return (buffer.getvalue(), 200,
                {'Content-Type': 'text/csv',
                 'Content-Disposition':
                     'attachment; filename=algo_signals.csv'})

    @app.get('/api/events')
    def api_events():
        db = store()
        if db is None:
            return jsonify({'ok': False, 'error': 'no database'}), 503
        return jsonify({'ok': True,
                        'events': db.events(request.args.get('kind') or None,
                                            int(request.args.get('limit',
                                                                 200)))})

    # -- account and symbol setup, which must work with the engine down ----

    @app.get('/api/accounts')
    def api_accounts():
        """The OKX accounts: only WHETHER each part of a key is saved,
        and under which .env name - never a value."""
        raw = cfg.load_raw(config_path)
        accounts = []
        for name, account in (raw.get('accounts') or {}).items():
            account = account or {}
            okx = cfg.AccountConfig.from_dict(name, account)
            accounts.append({
                'name': name, 'exchange': 'okx', 'demo': okx.demo,
                'base_url': account.get('base_url') or '',
                'key_env': okx.api_key_env,
                'secret_env': okx.secret_env,
                'passphrase_env': okx.passphrase_env,
                'key_set': bool(okx.api_key),
                'secret_set': bool(okx.api_secret),
                'passphrase_set': bool(okx.api_passphrase)})
        return jsonify({'accounts': accounts})

    @app.post('/api/accounts/<path:name>')
    def api_save_account(name):
        """An OKX account: demo or live, an optional base URL, and the
        three parts of its API key - which go to .env (and this
        process's environment, so Test works at once), never to the
        config and never to a log line."""
        payload = request.get_json(silent=True) or {}
        raw = cfg.load_raw(config_path)
        raw.setdefault('accounts', {})
        account = raw['accounts'].setdefault(name, {})
        account['exchange'] = 'okx'
        if 'demo' in payload:
            account['demo'] = bool(payload.get('demo'))
        account.setdefault('demo', True)
        url = (payload.get('base_url') or '').strip()
        if url and not re.match(r'^https?://', url):
            return jsonify({'ok': False,
                            'error': 'the base URL must start with https:// '
                                     '- leave it blank for www.okx.com'}), 400
        account['base_url'] = url or None
        keys = cfg.okx_env_keys(name)
        fields = (('api_key', 'api_key_env'), ('secret', 'secret_env'),
                  ('passphrase', 'passphrase_env'))
        for env, (_field, key_field) in zip(keys, fields):
            account[key_field] = account.get(key_field) or env
        env_path = os.path.join(os.path.dirname(
            os.path.abspath(config_path)) or '.', '.env')
        for field, key_field in fields:
            value = (payload.get(field) or '').strip()
            if value:
                cfg.write_env_value(env_path, account[key_field], value)
                os.environ[account[key_field]] = value
        cfg.save_raw(config_path, raw)
        return jsonify({'ok': True, 'account': name, 'exchange': 'okx',
                        'restart_required': True,
                        'note': 'saved — the engine restarts by itself to '
                                'pick it up'})

    def okx_config(name):
        """This account as an AccountConfig, or None."""
        raw = cfg.load_raw(config_path)
        row = (raw.get('accounts') or {}).get(name)
        return None if row is None else cfg.AccountConfig.from_dict(name, row)

    def open_okx_leg(account):
        """A short-lived direct connection to OKX for this account.

        Its ticket ledger is a scratch copy, so nothing the web process
        does can overwrite the engine's own record of its tickets."""
        import tempfile
        from .okx_broker import OKXBroker
        broker = OKXBroker(account, ledger_path=tempfile.mkdtemp(
            prefix='okxweb-'))
        broker.initialize()
        return LocalLeg(broker)

    _okx_orders_cache = {}

    def desk_ledger(name):
        """The ENGINE's own ticket ledger for this account, read only:
        which OKX order is which desk ticket. The web process never
        writes it."""
        base = os.environ.get('OKX_LEDGER_DIR', 'data')
        path = os.path.join(base, f'okx_ledger_{name}.json')
        try:
            with open(path, encoding='utf-8') as handle:
                return (json.load(handle) or {}).get('ords') or {}
        except (OSError, ValueError):
            return {}

    @app.get('/api/okx_orders')
    def api_okx_orders():
        """Every OKX account's orders as OKX holds them: working ones,
        and the last week's finished ones. Read live, cached ten
        seconds - for reconciling what the desk sent with what OKX did."""
        now = time.time()
        cached = _okx_orders_cache.get('body')
        if cached and now - cached[0] < 10 and not request.args.get('fresh'):
            return jsonify(cached[1])
        raw = cfg.load_raw(config_path)
        accounts, rows, errors = (raw.get('accounts') or {}), [], {}
        for name in sorted(accounts):
            account = okx_config(name)
            if account is None:
                continue
            leg = None
            try:
                leg = open_okx_leg(account)
                ledger = desk_ledger(name)
                for row in leg.broker.okx_orders():
                    meta = ledger.get(row['ord_id']) or {}
                    row.update({
                        'account': name,
                        'desk_ticket': meta.get('ticket'),
                        'position_ticket': meta.get('position_ticket'),
                        'entry': meta.get('entry'),
                        'comment': meta.get('comment') or ''})
                    rows.append(row)
            except Exception as e:
                errors[name] = str(e)
            finally:
                if leg is not None:
                    try:
                        leg.close()
                    except Exception:
                        pass
        rows.sort(key=lambda o: -o['updated_ms'])
        body = {'ok': True, 'at': now, 'orders': rows, 'errors': errors}
        _okx_orders_cache['body'] = (now, body)
        return jsonify(body)

    def okx_checklist(name, account):
        leg = open_okx_leg(account)
        checklist = diagnostics.Checklist()
        try:
            diagnostics.check_okx_account(
                checklist, name, leg.broker,
                has_keys=account.has_api_key or bool(account.base_url))
        finally:
            leg.close()
        return checklist

    def no_account(name):
        return jsonify({'ok': False,
                        'error': f"no account '{name}' - add it on the "
                                 f"Exchanges page"}), 404

    @app.get('/api/accounts/<path:name>/symbols')
    def api_find_symbols(name):
        """Every SPOT, SWAP and FUTURES instrument OKX lists, searched by
        name: BTC, ETH-USDT, -SWAP, 2612... Works with the engine down,
        and needs no key."""
        okx = okx_config(name)
        if okx is None:
            return no_account(name)
        leg = open_okx_leg(okx)
        try:
            found = leg.find_symbols(request.args.get('q', ''), 60)
        finally:
            leg.close()
        return jsonify({'ok': True, 'symbols': found or [],
                        'terminal': {'server': 'OKX demo' if okx.demo
                                     else 'OKX live'}})

    @app.get('/api/accounts/<path:name>/test')
    @app.get('/api/accounts/<path:name>/connect')
    def api_test_account(name):
        """Can this account TRADE? OKX reachable, the key accepted and
        allowed to trade, the account set up the way the desk trades -
        in OKX's own words and codes."""
        okx = okx_config(name)
        if okx is None:
            return no_account(name)
        result = okx_checklist(name, okx).result()
        return jsonify(dict(result, account=name, connected=result['ok'],
                            problems=[c['message'] for c in result['checks']
                                      if c['status'] == 'FAIL']))

    @app.get('/api/accounts/<path:name>/diagnose')
    def api_diagnose_account(name):
        """Everything Test checks, plus every instrument on this account
        and every pair that routes through it. The one to run before the
        first trade of the day."""
        raw = cfg.load_raw(config_path)
        okx = okx_config(name)
        if okx is None:
            return no_account(name)
        leg = open_okx_leg(okx)
        checklist = diagnostics.Checklist()
        pairs = {key: cfg.PairConfig.from_dict(key, row)
                 for key, row in (raw.get('pairs') or {}).items()}
        reports = {}
        try:
            diagnostics.check_okx_account(
                checklist, name, leg.broker,
                has_keys=okx.has_api_key or bool(okx.base_url))
            account_info = leg.account_info()
            for key, pair in pairs.items():
                for symbol, role in ((pair.symbol_a, 'leg A'),
                                     (pair.symbol_b, 'leg B')):
                    account = (pair.account_a if role == 'leg A'
                               else pair.account_b)
                    if account != name or not symbol:
                        continue
                    if symbol not in reports:
                        reports[symbol] = leg.symbol_report(symbol)
                    diagnostics.check_symbol(checklist, name,
                                             reports[symbol], role)
            for key, pair in pairs.items():
                if name not in (pair.account_a, pair.account_b):
                    continue
                report_a = reports.get(pair.symbol_a) or \
                    leg.symbol_report(pair.symbol_a)
                report_b = reports.get(pair.symbol_b) or \
                    leg.symbol_report(pair.symbol_b)
                diagnostics.check_pair(checklist, pair, report_a or {},
                                       report_b or {},
                                       account_currency=(account_info or {})
                                       .get('currency'))
        finally:
            leg.close()
        result = checklist.result()
        return jsonify(dict(result, account=name, connected=result['ok']))

    @app.get('/api/connection')
    def api_connection():
        """Is the SYSTEM connected — plainly, in one answer.

        The operator's real question is never "did a request go out";
        it is "can I trade right now". This answers that, and when the
        answer is no it names the one thing to fix.
        """
        raw = cfg.load_raw(config_path)
        snapshot = status()
        accounts = []
        blockers = []
        for name in (raw.get('accounts') or {}):
            leg, error, _code = open_leg(name)
            row = {'account': name, 'connected': False, 'trading': False,
                   'error': error}
            if leg is not None:
                try:
                    terminal = leg.terminal_report()
                finally:
                    leg.close()
                row.update({
                    'connected': bool(terminal.get('logged_in')),
                    'trading': bool(terminal.get('algo_trading')),
                    'login': terminal.get('login'),
                    'server': terminal.get('server'),
                    'error': None})
                if not row['connected']:
                    blockers.append(f'{name}: OKX refused the key - '
                                    f'{terminal.get("error") or "press Test"}')
                elif not row['trading']:
                    blockers.append(f'{name}: {terminal.get("error") or "the key cannot trade"}')
            else:
                blockers.append(f'{name}: {error}')
            accounts.append(row)

        if not accounts:
            blockers.append('no accounts are configured yet')
        if snapshot.get('engine') != 'up':
            blockers.append(snapshot.get('engine_note') or
                            'the coordinator is not running')

        # "Everything passed" and "there was nothing to check" are not
        # the same answer. With no pair configured the feed loop below
        # never runs, so nothing objects — and the banner then reports a
        # system ready to trade instruments it does not have.
        configured = raw.get('pairs') or {}
        if not configured:
            blockers.append('no pairs are configured yet')
        elif not [key for key, pair in configured.items()
                  if (pair or {}).get('enabled', True)]:
            blockers.append('every pair is disabled')

        feeds = []
        for key, pair in (snapshot.get('pairs') or {}).items():
            if not pair.get('enabled'):
                continue
            market = pair.get('market') or {}
            feeds.append({'pair': key, 'name': pair.get('name'),
                          'priced': market.get('spread') is not None,
                          'badge': market.get('feed_badge'),
                          'errors': pair.get('errors') or []})
            if market.get('spread') is None:
                blockers.append(f'{key}: no price yet')

        return jsonify({
            'ok': not blockers,
            'connected': not blockers,
            'accounts': accounts,
            'feeds': feeds,
            'engine': snapshot.get('engine'),
            'broker_clock': snapshot.get('broker_clock'),
            'blockers': blockers,
            # Count them. "both" was hard coded, so ONE account with no
            # pairs at all still read "both accounts logged in ... You
            # can trade" — the screen asserting a second leg that was
            # never configured.
            'summary': (f'Connected — {len(accounts)} account'
                        f'{"" if len(accounts) == 1 else "s"} connected, '
                        f'trading allowed, and prices arriving. '
                        f'You can trade.'
                        if not blockers else
                        'Not ready to trade: ' + blockers[0]),
        })

    @app.get('/api/accounts/<path:name>/symbol/<path:symbol>')
    def api_symbol(name, symbol):
        """One instrument's contract specs, read from OKX — never typed in.

        Contract size, volume minimum and step and tick size are what the
        hedge arithmetic is built on; a typed-in contract size is a hedge
        error waiting for a different instrument.
        """
        leg, error, code = open_leg(name)
        if leg is None:
            return jsonify({'ok': False, 'error': error}), code
        try:
            report = leg.symbol_report(symbol)
        finally:
            leg.close()
        if not report.get('found'):
            return jsonify({'ok': False, 'error': report.get('error'),
                            'report': report}), 404
        return jsonify({'ok': True, 'report': report})

    def open_leg(name):
        """A short-lived direct connection to one OKX account, or why
        there is none: (leg, error, status) - the caller closes it."""
        okx = okx_config(name)
        if okx is None:
            return None, f"no account '{name}'", 404
        leg = open_okx_leg(okx)
        if not leg.broker.connected:
            error = leg.broker.last_error
            leg.close()
            return None, f'OKX did not answer for {name}: {error}', 503
        return leg, None, 200

    @app.delete('/api/accounts/<path:name>')
    def api_delete_account(name):
        """Remove an account — but never one a pair is still routing to.

        A pair pointing at an account that no longer exists is a ladder
        that cannot trade and cannot say why.
        """
        raw = cfg.load_raw(config_path)
        if name not in (raw.get('accounts') or {}):
            return jsonify({'ok': False, 'error': f'no account {name}'}), 404
        used_by = [key for key, pair in (raw.get('pairs') or {}).items()
                   if name in ((pair.get('leg_a') or {}).get('account'),
                               (pair.get('leg_b') or {}).get('account'))]
        if used_by:
            return jsonify({'ok': False,
                            'error': f"'{name}' is still leg A or leg B of "
                                     f"{', '.join(used_by)}. Point those "
                                     f"pairs somewhere else (or delete them) "
                                     f"first."}), 409
        del raw['accounts'][name]
        cfg.save_raw(config_path, raw, allow_shrink=True)
        return jsonify({'ok': True, 'deleted': name,
                        'note': 'the password stays in .env until you clear '
                                'it there'})

    # -- pairs --------------------------------------------------------------

    @app.get('/api/pairs')
    def api_pairs():
        raw = cfg.load_raw(config_path)
        return jsonify({'pairs': raw.get('pairs') or {}})

    @app.post('/api/pairs/<path:key>/derive')
    def api_derive_pair(key):
        """Read both legs out of MT5 and derive what follows from them.

        Beta, the increment, the matched-minimum clip and the minimum
        tradable notional are all consequences of the two symbols'
        contract specs and live prices. Deriving them here means the
        operator sees the numbers — and the DERIVATION — before saving,
        instead of typing a beta that silently redefines the spread.
        """
        raw = cfg.load_raw(config_path)
        payload = request.get_json(silent=True) or {}
        pair = dict((raw.get('pairs') or {}).get(key) or {})
        pair.update(payload)
        legs = {}
        specs = {}
        for side in ('a', 'b'):
            leg_cfg = pair.get(f'leg_{side}') or {}
            name, symbol = leg_cfg.get('account'), leg_cfg.get('symbol')
            if not name or not symbol:
                return jsonify({'ok': False,
                                'error': f'leg {side.upper()} needs an '
                                         f'account and a symbol'}), 400
            leg, error, code = open_leg(name)
            if leg is None:
                return jsonify({'ok': False, 'error': error}), code
            legs[side] = leg
            try:
                specs[side] = leg.symbol_report(symbol)
            finally:
                leg.close()
            if not specs[side].get('found'):
                return jsonify({'ok': False,
                                'error': specs[side].get('error')}), 404

        price_a, price_b = _mid(specs['a']), _mid(specs['b'])
        okx_type, okx_type_why = okx_pair_type(specs['a'], specs['b'])
        suggested, why = hedgeratio.suggest(
            pair.get('pair_type') or okx_type or 'SPOT_FUTURE',
            price_a, price_b)
        beta = float(pair.get('hedge_ratio') or suggested or 1.0)
        # What one Qty means on each leg. BOTH are the trader's, and a
        # blank reads as 1 — nothing is derived here any more.
        clip_a = float(pair.get('clip_lots_a') or 1.0)
        clip_b = float(pair.get('clip_lots_b') or 1.0)
        tick_a = specs['a'].get('tick_size') or 0.0
        tick_b = specs['b'].get('tick_size') or 0.0
        increment = max(tick_b, beta * tick_a) if (tick_a and tick_b) else None
        floor = sizing.minimum_notional(
            specs['a'].get('contract_size'), specs['b'].get('contract_size'),
            price_a, price_b, beta, specs['a'].get('volume_min'),
            specs['b'].get('volume_min'))
        width_a = _width(specs['a'])
        width_b = _width(specs['b'])
        return jsonify({
            'ok': True,
            'specs': specs,
            'suggested_beta': suggested,
            'beta_reason': why,
            # OKX: what the two instruments ARE decides the pair type -
            # spot or perp against a perp or future of the same coin is a
            # basis; two coins are two different instruments.
            'pair_type_suggestion': okx_type,
            'pair_type_reason': okx_type_why,
            'fees_bps': {side: {'maker': specs[side].get('maker_bps'),
                                'taker': specs[side].get('taker_bps')}
                         for side in ('a', 'b')},
            'funding': {side: specs[side].get('funding_rate')
                        for side in ('a', 'b')},
            'stamped_for': hedgeratio.pair_signature(
                (pair.get('leg_a') or {}).get('symbol'),
                (pair.get('leg_b') or {}).get('symbol')),
            'increment': increment,
            'increment_derivation': (
                f'max(tick B {tick_b:g}, beta {beta:g} x tick A {tick_a:g})'
                if increment else 'both legs need a tick size from OKX'),
            'clip_lots_a': clip_a, 'clip_lots_b': clip_b,
            'clip_derivation': (
                f'1 Qty = {clip_a:g} lots A / {clip_b:g} lots B. This '
                f"broker's minimums are {specs['a'].get('volume_min')} and "
                f"{specs['b'].get('volume_min')} lots"),
            'spread_units': sizing.spread_units(
                clip_b, specs['b'].get('contract_size')),
            'min_notional_usd': floor,
            'spread_now': (price_b - beta * price_a
                           if (price_a and price_b) else None),
            'widths': {'a': width_a, 'b': width_b},
            # Which leg SHOULD quote, from measurement rather than
            # assumption — and the tension stated, not hidden.
            'quoting_leg_suggestion': (
                'b' if (width_b or 0) >= (width_a or 0) else 'a'),
            'quoting_note': (
                'Default is the wider bid-ask — that is the spread you earn '
                'by quoting. It is usually also the less liquid leg, where '
                'you queue longest and fill least.'),
        })

    @app.post('/api/pairs/<path:key>')
    def api_save_pair(key):
        """Create or edit a pair. Routed on `<path:key>` because the pair
        that most needs deleting has a slash in its key.

        The key is TIDIED before it is used: it is an identifier, matched
        exactly by the snapshot and by every panel, and one typed by hand
        arrives with the spaces a person puts round a separator.
        `XAUUSD.f | GCZ6.f` and `XAUUSD.f|GCZ6.f` are the same pair to
        the operator and two different ones to everything else.
        """
        key = _tidy_key(key)
        payload = request.get_json(silent=True) or {}
        raw = cfg.load_raw(config_path)
        raw.setdefault('pairs', {})
        pair = raw['pairs'].setdefault(key, {})
        open_position = _open_position(status(), key)
        renaming = payload.get('name') and payload['name'] != pair.get('name')
        disabling = payload.get('enabled') is False and pair.get('enabled')
        if (renaming or disabling) and open_position:
            return jsonify({'ok': False, 'error': open_position}), 409
        # Resizing what ONE SPREAD MEANS while spreads are open changes
        # the unit every open figure is quoted in. The position's own
        # P&L is anchored to the size it filled at and would survive
        # it, but the exit maths is not: break-even and the take-profit
        # are computed from the pair's CURRENT clip, so a position
        # entered at 0.01 would silently acquire the target of a 0.10
        # one. It is refused while anything is on.
        # The Algo's numbers are checked BEFORE anything is written: a
        # timeframe MT5 has no bars for, or an entry z of 0, saved
        # quietly would signal on every tick or never.
        if 'algo_params' in payload:
            from . import algo as algo_module
            problems = algo_module.check_params(payload['algo_params'] or {})
            if problems:
                return jsonify({'ok': False,
                                'error': 'Algo: ' + '; '.join(problems)}), 400
        # This ladder's hours: a time that will not parse is REFUSED with
        # what was wrong, and a break needs both its ends.
        from . import session as session_module
        hours = {'session_open': 'Session open',
                 'session_close': 'Session close',
                 'break_start': 'Break start', 'break_end': 'Break end'}
        for field, label in hours.items():
            if field in payload:
                try:
                    session_module.parse_hhmm(payload[field])
                except ValueError as e:
                    return jsonify({'ok': False,
                                    'error': f'{label}: {e}'}), 400
                payload[field] = session_module.clean_hhmm(payload[field])
        ends = [payload.get(f, pair.get(f)) for f in ('break_start',
                                                       'break_end')]
        if sum(1 for v in ends if v not in (None, '')) == 1:
            return jsonify({'ok': False, 'error': (
                'Break: give both a start and an end, or leave both '
                'blank')}), 400
        resizing = ('clip_lots_a' in payload
                    and _changed(payload['clip_lots_a'],
                                 pair.get('clip_lots_a')))
        if resizing and open_position:
            net = ((status().get('pairs') or {}).get(key) or {}).get(
                'net_position')
            return jsonify({'ok': False, 'error': (
                f'{key} has {net:+g} spreads open, so what one spread '
                f'MEANS cannot be changed now — the exit levels of the '
                f'open position are computed from it. Flatten it first.'
            )}), 409
        for field in ('name', 'leg_a', 'leg_b', 'pair_type', 'hedge_ratio',
                      'increment', 'default_quantity', 'order_type',
                      'exit_type',
                      'time_in_force', 'overnight', 'quoting_leg',
                      'fill_style', 'usd_per_qty', 'enabled',
                      'rows', 'clip_lots_a', 'clip_lots_b',
                      'contract_size_a', 'contract_size_b',
                      'max_quote_age_sec', 'max_positions',
                      'expiry', 'expiry_a',
                      'swap_a_long_per_lot', 'swap_a_short_per_lot',
                      'swap_b_long_per_lot', 'swap_b_short_per_lot',
                      # What a trade on THIS ladder costs, and therefore
                      # where it gets out. Per pair, because a gold
                      # basis and an oil differential are not the same
                      # trade priced twice.
                      'commission_per_lot_a', 'commission_per_lot_b',
                      'slippage_allowance', 'break_even_nights',
                      'tp_target_pct_of_margin',
                      'carry_rate_pct',
                      'show_fair_window', 'algo_window', 'algo_params',
                      'session_open', 'session_close',
                      'break_start', 'break_end'):
            if field in payload:
                pair[field] = payload[field]
        # A date that will not parse is REPORTED and the old value kept:
        # silently dropping it leaves the operator looking at a blank
        # field with no way to tell whether it was rejected or ignored.
        # A date already past is accepted, and flagged.
        notes = []
        for field, leg in (('expiry', 'Leg B'), ('expiry_a', 'Leg A')):
            if field not in payload or payload[field] in (None, ''):
                continue
            parsed = fairvalue.parse_expiry(payload[field])
            if parsed is None:
                pair[field] = (raw.get('pairs', {}).get(key) or {}).get(field)
                notes.append(f"{leg}: {payload[field]!r} is not a date this "
                             f"reads — try 2026-09-26 or 26/09/2026. The "
                             f"previous value was kept.")
            elif parsed < date.today():
                notes.append(f'{leg} expiry {parsed} has already passed.')
        if 'hedge_ratio' in payload:
            # Beta belongs to the PAIR: stamp it, so a stale one from the
            # previous instrument cannot silently define the spread.
            from .hedgeratio import pair_signature
            pair['hedge_ratio_for'] = pair_signature(
                (pair.get('leg_a') or {}).get('symbol'),
                (pair.get('leg_b') or {}).get('symbol'))
        cfg.save_raw(config_path, raw)
        return jsonify({'ok': True, 'pair': key, 'notes': notes,
                        # The carry inputs feed a PANEL, not the engine:
                        # they are read again on the next poll, so they
                        # are deliberately NOT in this set.
                        'restart_required': bool(
                            {'leg_a', 'leg_b', 'hedge_ratio', 'enabled'}
                            & set(payload))})

    @app.delete('/api/pairs/<path:key>')
    def api_delete_pair(key):
        raw = cfg.load_raw(config_path)
        if key not in (raw.get('pairs') or {}):
            return jsonify({'ok': False, 'error': f'no pair {key}'}), 404
        open_position = _open_position(status(), key)
        if open_position:
            return jsonify({'ok': False, 'error': open_position}), 409
        del raw['pairs'][key]
        # allow_shrink: this endpoint legitimately removes something.
        cfg.save_raw(config_path, raw, allow_shrink=True)
        return jsonify({'ok': True, 'deleted': key})

    return app


def _tidy_key(key):
    """A pair key as everything else writes it: no stray whitespace, and
    one spelling of the separator."""
    return re.sub(r'\s*\|\s*', '|', str(key or '').strip())


def okx_pair_type(spec_a, spec_b):
    """(pair type, why) for two OKX instruments, or (None, None) when
    either is not one. BTC-USDT / BTC-USDT-SWAP / BTC-USD_UM-261225: the
    coin is the first part of the instrument id."""
    kind_a, kind_b = spec_a.get('inst_type'), spec_b.get('inst_type')
    if not kind_a or not kind_b:
        return None, None
    coin_a = str(spec_a.get('symbol') or '').split('-')[0]
    coin_b = str(spec_b.get('symbol') or '').split('-')[0]
    if coin_a != coin_b:
        return 'RELATED', (f'{coin_a} against {coin_b}: two different '
                           f'coins, priced by a hedge ratio, no carry')
    if kind_a == 'FUTURES' and kind_b == 'FUTURES':
        return 'FUTURE_FUTURE', (f'two {coin_a} futures: a calendar, decided '
                                 f'when the near one expires')
    return 'SPOT_FUTURE', (f'{coin_a} {kind_a.lower()} against '
                           f'{kind_b.lower()}: a basis, beta 1')


def _mid(report):
    bid, ask = report.get('bid'), report.get('ask')
    return ((bid + ask) / 2.0) if (bid and ask) else None


def _width(report):
    bid, ask = report.get('bid'), report.get('ask')
    return (ask - bid) if (bid and ask) else None


def _changed(new, old):
    """Is this field actually being MOVED?

    A save sends the whole form, so every field arrives on every save.
    Comparing numerically keeps 0.10 and 0.1 the same value, and keeps
    a guard from refusing a save that changes something else entirely.
    """
    if new in (None, '') or old in (None, ''):
        return bool(new not in (None, '')) != bool(old not in (None, ''))
    try:
        return abs(float(new) - float(old)) > 1e-9
    except (TypeError, ValueError):
        return new != old


def _open_position(snapshot, key):
    """The refusal message for a pair that is not free to be changed.

    A leftover row is one resolving symbol away from a second live
    position on the same underlying — but a pair carrying money must not
    be renamed or deleted out from under it either.
    """
    row = (snapshot.get('pairs') or {}).get(key) or {}
    net = row.get('net_position') or 0.0
    if not net:
        return None
    return (f'{key} has {net:+g} spreads open. Flatten it first — renaming '
            f'or removing it now would leave the position with nothing '
            f'watching it.')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='OKX spread desk web UI')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--db', default='okxtrader.db')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    # The screen's own process logs too: a refused command, a lock, a
    # settings change. Without it the only record of what the operator
    # did was the console window they closed.
    from . import logsetup
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - [web] '
                               '%(message)s',
                        handlers=[logging.StreamHandler()])
    logsetup.setup('web')
    # The screen polls three times a second; a line for each poll buried
    # every error in web.log. Button presses and failures still log.
    logsetup.quiet_polling()
    app = create_app(args.status, args.commands, args.results, args.config,
                     args.db)
    # Telegram, if .env carries a bot token. The token is read from the
    # environment only — never from config.json, never logged.
    if cfg.load_dotenv is not None:
        cfg.load_dotenv(os.path.join(
            os.path.dirname(os.path.abspath(args.config)) or '.', '.env'))
    from . import telegram
    telegram.start(app)
    # Never Flask's debug mode or reloader, whatever the environment says:
    # an old Stat_Arb_W3 .env carries FLASK_DEBUG=true, and Flask obeys it
    # on its own - an interactive debugger on the trading desk, and a
    # reloader that runs a second copy of this process (and its Telegram
    # bot) beside the first.
    app.run(host=args.host, port=args.port, threaded=True, debug=False,
            use_reloader=False)


if __name__ == '__main__':
    main()
