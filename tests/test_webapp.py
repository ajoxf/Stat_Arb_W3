"""The web process: it renders, and it asks. It never trades.

Plus the UI rules the spec makes non-negotiable, asserted as tests
rather than trusted: no CDN, no native dialogs, and a refusal that
carries its own words.
"""

import json
import os
import re
from pathlib import Path

import pytest

from okxtrader import config as cfg
from okxtrader.webapp import create_app

STATIC = Path(__file__).resolve().parent.parent / 'okxtrader' / 'static'
TEMPLATES = Path(__file__).resolve().parent.parent / 'okxtrader' / 'templates'


@pytest.fixture
def paths(tmp_path):
    return {
        'status': str(tmp_path / 'status.json'),
        'commands': str(tmp_path / 'commands.jsonl'),
        'results': str(tmp_path / 'results.json'),
        'config': str(tmp_path / 'config.json'),
    }


@pytest.fixture
def client(paths):
    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'])
    app.config.update(TESTING=True)
    return app.test_client()


def write_status(paths, **overrides):
    import time
    snapshot = {
        'at': time.time(), 'loop_interval_sec': 0.3,
        'accounts': {'acct_a': {'profit': 0.0}},
        'pairs': {'A|B': {'key': 'A|B', 'name': 'A vs B', 'enabled': True,
                          'net_position': 0.0, 'working_buys': 0,
                          'working_sells': 0, 'positions': [], 'orders': [],
                          'rows': [], 'errors': []}},
    }
    snapshot.update(overrides)
    with open(paths['status'], 'w', encoding='utf-8') as f:
        json.dump(snapshot, f)
    # The app re-reads the file when (mtime, size) changes. Windows'
    # clock is coarse: two writes in a row of the same size kept one
    # mtime, and the second status was never read. A stamp of its own.
    global _LAST_STAMP
    _LAST_STAMP = max(time.time_ns(), _LAST_STAMP + 10_000_000)
    os.utime(paths['status'], ns=(_LAST_STAMP, _LAST_STAMP))
    return snapshot


_LAST_STAMP = 0


def test_a_dead_coordinator_is_never_mistaken_for_a_quiet_market(client,
                                                                  paths):
    """That confusion is what gets orders clicked into a screen with
    nothing behind it."""
    body = client.get('/api/status').get_json()
    assert body['engine'] == 'down'
    assert 'a click would go nowhere' in body['engine_note']

    write_status(paths, at=0)                    # an ancient snapshot
    body = client.get('/api/status').get_json()
    assert body['engine'] == 'stalled'
    assert 'Nothing on this screen is live' in body['engine_note']


def test_a_command_is_refused_while_the_engine_is_down(client, paths):
    """Refuse rather than queue: a command written while nothing is
    running would be executed by whatever starts next, at prices from
    another hour."""
    response = client.post('/api/command',
                           json={'kind': 'click',
                                 'payload': {'pair': 'A|B', 'side': 'BUY',
                                             'level': 1.0}})
    assert response.status_code == 409
    assert 'not running' in response.get_json()['error']
    assert not Path(paths['commands']).exists()


def test_a_command_reaches_the_log_when_the_engine_is_up(client, paths):
    write_status(paths)
    response = client.post('/api/command',
                           json={'kind': 'click',
                                 'payload': {'pair': 'A|B', 'side': 'BUY',
                                             'level': 58.4}})
    assert response.status_code == 200
    command_id = response.get_json()['id']

    written = [json.loads(line) for line in
               open(paths['commands'], encoding='utf-8').read().splitlines()]
    assert written[0]['kind'] == 'click'
    assert written[0]['payload']['level'] == 58.4

    # The result is pending until the coordinator has run it.
    assert client.get(f'/api/result/{command_id}').get_json()['pending']
    with open(paths['results'], 'w', encoding='utf-8') as f:
        json.dump({command_id: {'ok': True, 'data': {'order': {}}}}, f)
    assert client.get(f'/api/result/{command_id}').get_json()['ok'] is True








def test_a_pair_with_an_open_position_cannot_be_renamed_or_deleted(client,
                                                                   paths):
    """A leftover row is one resolving symbol away from a second live
    position — but a pair carrying money must not be removed out from
    under it either."""
    cfg.save_raw(paths['config'], {'accounts': {'a': {}},
                                   'pairs': {'A|B': {'name': 'A vs B'}}})
    write_status(paths, pairs={'A|B': {'net_position': -2.0}})

    response = client.post('/api/pairs/A|B', json={'name': 'renamed'})
    assert response.status_code == 409
    assert 'Flatten it first' in response.get_json()['error']

    response = client.delete('/api/pairs/A|B')
    assert response.status_code == 409

    # The control: flat, and both go through.
    write_status(paths, pairs={'A|B': {'net_position': 0.0}})
    assert client.post('/api/pairs/A|B', json={'name': 'renamed'}).status_code \
        == 200
    assert client.delete('/api/pairs/A|B').status_code == 200


def test_what_one_spread_MEANS_cannot_be_changed_under_a_position(client,
                                                                  paths):
    """The exit levels of an open position are computed from the pair's
    CURRENT lots-per-spread. Move it and a position entered at 0.01
    silently acquires the take-profit of a 0.10 one — the position
    would still be there, and the number it was aiming at would not."""
    cfg.save_raw(paths['config'], {
        'accounts': {'a': {}},
        'pairs': {'A|B': {'name': 'A vs B', 'clip_lots_a': 0.01}}})
    write_status(paths, pairs={'A|B': {'net_position': 2.0}})

    response = client.post('/api/pairs/A|B', json={'clip_lots_a': 0.10})
    assert response.status_code == 409
    error = response.get_json()['error']
    assert 'what one spread MEANS' in error and 'Flatten it first' in error
    assert json.load(open(paths['config'], encoding='utf-8'))[
        'pairs']['A|B']['clip_lots_a'] == 0.01

    # The control 1: the SAME value is not a change, so a save of
    # anything else on the pane still goes through with a position on.
    assert client.post('/api/pairs/A|B',
                       json={'clip_lots_a': 0.01,
                             'increment': 0.02}).status_code == 200

    # The control 2: flat, and it goes through.
    write_status(paths, pairs={'A|B': {'net_position': 0.0}})
    assert client.post('/api/pairs/A|B',
                       json={'clip_lots_a': 0.10}).status_code == 200
    assert json.load(open(paths['config'], encoding='utf-8'))[
        'pairs']['A|B']['clip_lots_a'] == 0.10


def test_a_pair_key_with_a_slash_is_routable(client, paths):
    """The pair that most needs deleting has a slash in its key."""
    cfg.save_raw(paths['config'], {'accounts': {'a': {}},
                                   'pairs': {'XAU/USD|GC': {'name': 'x'}}})
    write_status(paths, pairs={})
    assert client.delete('/api/pairs/XAU/USD|GC').status_code == 200
    assert json.load(open(paths['config'], encoding='utf-8'))['pairs'] == {}




def test_the_ui_loads_nothing_from_a_cdn():
    """A blocked CDN has taken this kind of UI down once. The dialog that
    reports 'could not save' must work when the network is what failed."""
    html = (TEMPLATES / 'index.html').read_text(encoding='utf-8')
    for source in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert '//' not in source, source
    assert '@import' not in (STATIC / 'ladder.css').read_text(encoding='utf-8')


def strip_comments(script):
    """Code only. The house rule is written in a comment at the top of
    the file, and a guard that trips over its own documentation is a
    guard nobody keeps."""
    script = re.sub(r'/\*.*?\*/', '', script, flags=re.S)
    return re.sub(r'(^|\s)//[^\n]*', ' ', script)


def test_no_native_confirm_alert_or_prompt_ever_comes_back():
    """One shared modal, and a test that fails the build if they return."""
    script = strip_comments((STATIC / 'app.js').read_text(encoding='utf-8'))
    native = re.compile(r'(?<![\w.$])(confirm|alert|prompt)\s*\(')
    found = native.search(script)
    assert found is None, found.group(0)
    assert 'window.confirm' not in script
    assert 'window.alert' not in script


def test_the_ladder_is_the_reference_screens_five_columns():
    """`Work | Bids | Price | Asks | LTQ`, in that order — the layout is
    a specification, not a preference."""
    html = (TEMPLATES / 'index.html').read_text(encoding='utf-8')
    # The headers carry tooltips now, so match the class and the text
    # without assuming nothing sits between them.
    headers = re.findall(r'<th class="c-(\w+)"[^>]*>([^<]+)</th>', html)
    assert [name for name, _ in headers] == ['work', 'bid', 'price', 'ask',
                                             'ltq']
    assert [label for _, label in headers] == ['Work', 'Bids', 'Price',
                                               'Asks', 'LTQ']


def test_bid_is_blue_and_ask_is_red_everywhere():
    """The reference screen's convention, global: a price must not change
    colour depending on which table it sits in."""
    css = (STATIC / 'ladder.css').read_text(encoding='utf-8')
    # The exact shades are a comfort decision and have been softened
    # once already; what must not move is that there is ONE blue and ONE
    # red, defined here, and that every table takes them from here.
    assert re.search(r'--bid: #[0-9a-f]{6}', css)
    assert re.search(r'--ask: #[0-9a-f]{6}', css)
    # Every place a bid or ask cell is painted uses those variables.
    for rule in re.findall(r'td\.(bid|ask)[^{]*\{[^}]*background:\s*([^;]+);',
                           css):
        assert 'var(--' + rule[0] in rule[1], rule


def test_the_market_is_marked_by_ONE_rule_and_it_is_the_mid():
    """A rule at the inside AND a rule at the mid meant neither read as
    "the market is here". The mid keeps the rule — it is also what the
    ladder centres on — and the inside is drawn by the two bands
    meeting, which carries the same information."""
    css = (STATIC / 'ladder.css').read_text(encoding='utf-8')
    assert re.search(r'tr\.mid-line\s*>\s*td\s*\{\s*\n?\s*'
                     r'border-top:\s*2px solid', css)
    assert not re.search(r'tr\.market-line\s*>\s*td\s*\{\s*border-top',
                         css)
    assert '--inside: #000000' in css


def test_market_mode_changes_the_click_columns():
    """The expensive misclick on any ladder is a market order the trader
    thought was a working order.

    The M rides the cursor over EVERY price cell while the mode is
    MARKET — the mode is what it names, and the mode does not change
    halfway down the ladder. The two touch rows keep a box of their
    own, because they are the only ones that can cross NOW; a click
    away from them rests, and the toast says so when it happens.
    """
    css = (STATIC / 'ladder.css').read_text(encoding='utf-8')
    assert '.window.mode-market td.bid' in css
    assert "font-size='15' font-weight='700'" in css      # the M cursor
    # ...and the touch rows are still marked out from the rest.
    assert '.window.mode-market tr.market-line td.bid' in css


# -- the settings endpoints: they must work with the ENGINE down --------



























def test_an_account_still_carrying_a_pair_cannot_be_deleted(client, paths):
    wired = client
    cfg.save_raw(paths['config'], {
        'accounts': {'spot': {'exchange': 'okx', 'demo': True},
                     'fut': {'exchange': 'okx', 'demo': True}},
        'pairs': {'A|B': {'leg_a': {'account': 'spot', 'symbol': 'X'},
                          'leg_b': {'account': 'fut', 'symbol': 'Y'}}}})

    response = wired.delete('/api/accounts/spot')
    assert response.status_code == 409
    assert 'A|B' in response.get_json()['error']

    # The control: with the pair gone, the account goes.
    wired.delete('/api/pairs/A|B')
    assert wired.delete('/api/accounts/spot').status_code == 200
    assert 'spot' not in cfg.load_raw(paths['config'])['accounts']


def test_the_settings_panel_ships_no_native_dialogs_either():
    script = strip_comments((STATIC / 'settings.js').read_text(encoding='utf-8'))
    native = re.compile(r'(?<![\w.$])(confirm|alert|prompt)\s*\(')
    found = native.search(script)
    assert found is None, found.group(0)


def test_a_setting_is_saved_and_pushed_to_the_running_engine(client, paths):
    """Written to config.json so it survives a restart, and sent as a
    command so the NEXT CLICK already obeys it."""
    write_status(paths)
    cfg.save_raw(paths['config'], {'accounts': {}, 'pairs': {}})

    response = client.post('/api/settings',
                           json={'fields': {'CONFIRM_MARKET_CLICKS': True,
                                            'ROW_HEIGHT_PX': 22}})
    body = response.get_json()
    assert body['ok']
    assert body['applied_now'] == ['CONFIRM_MARKET_CLICKS', 'ROW_HEIGHT_PX']
    assert body['restart_required'] == []

    saved = cfg.load_raw(paths['config'])['settings']
    assert saved['CONFIRM_MARKET_CLICKS'] is True
    written = [json.loads(line) for line in
               open(paths['commands'], encoding='utf-8').read().splitlines()]
    assert written[-1]['kind'] == 'set_setting'
    assert written[-1]['payload']['fields']['ROW_HEIGHT_PX'] == 22


def test_a_setting_the_launcher_only_reads_at_startup_says_so(client, paths):
    """Crying 'restart' on every save teaches the operator to ignore the
    line that matters — so only the ones that mean it say it."""
    write_status(paths)
    cfg.save_raw(paths['config'], {'accounts': {}, 'pairs': {}})
    body = client.post('/api/settings',
                       json={'fields': {'POLL_INTERVAL_SEC': 0.5}}).get_json()
    assert body['restart_required'] == ['POLL_INTERVAL_SEC']
    assert body['applied_now'] == []


def test_a_typo_is_not_quietly_saved_as_a_new_setting(client, paths):
    write_status(paths)
    cfg.save_raw(paths['config'], {'accounts': {}, 'pairs': {}})
    response = client.post('/api/settings',
                           json={'fields': {'CONFRIM_MARKET_CLICKS': True}})
    assert response.status_code == 400
    assert 'not a setting' in response.get_json()['error']
    assert 'settings' not in cfg.load_raw(paths['config'])


def test_the_engine_publishes_what_a_click_will_do(config, legs, tmp_path):
    """The UI arms itself from the ENGINE's answer, never from its own
    idea of what was last selected."""
    from okxtrader.coordinator import Coordinator
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()

    snapshot = coordinator.snapshot()
    assert snapshot['confirm_market_clicks'] is False    # one click, one order
    assert snapshot['row_height_px'] == 17
    assert snapshot['command_poll_sec'] == 0.02

    config.settings['CONFIRM_MARKET_CLICKS'] = True
    assert coordinator.snapshot()['confirm_market_clicks'] is True


def test_margin_is_reported_per_account_and_the_weakest_one_is_named(
        config, legs, tmp_path):
    """With two brokers there is no combined margin: each posts its own,
    and a pair can only be carried by the WEAKER of the two."""
    from okxtrader.coordinator import Coordinator
    from okxtrader.models import SpreadSide
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    pair = list(config.pairs.values())[0]
    pair.order_type = type(pair.order_type)('MARKET')
    coordinator.click(pair.key, SpreadSide.SELL, None)

    health = coordinator.snapshot()['account_health']

    assert set(health['accounts']) == {'acct_a', 'acct_b'}
    row = health['accounts']['acct_a']
    assert row['equity'] == pytest.approx(100_000.0)
    assert row['credit'] == pytest.approx(0.0)
    # Our own exposure, in the units the operator sized in.
    assert row['our_legs'] == 1
    assert row['our_lots'] == pytest.approx(0.1)
    assert row['our_units'] == pytest.approx(10.0)
    # Nothing is levered here, so no account is tight and none is weakest
    # by margin level — and that reads as unmeasured, not as healthy.
    assert row['margin_level'] is None
    assert health['weakest'] is None


def test_a_tight_account_is_flagged_against_the_level_you_set(config, legs):
    from types import SimpleNamespace
    from okxtrader.coordinator import Coordinator
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    # Leg B is nearly out of margin; leg A is fine.
    legs['acct_b'].broker.account_info = lambda: SimpleNamespace(
        login=2, server='S', name='b', currency='USD', leverage=100,
        balance=1000.0, credit=0.0, equity=1000.0, margin=800.0,
        margin_free=200.0, margin_level=125.0, margin_so_call=100.0,
        margin_so_so=50.0, profit=0.0)
    coordinator.poll_once()

    health = coordinator.snapshot()['account_health']

    assert health['weakest'] == 'acct_b'
    assert health['weakest_level'] == pytest.approx(125.0)
    assert health['accounts']['acct_b']['tight'] is True
    assert health['accounts']['acct_a']['tight'] is False


def test_an_account_that_cannot_be_read_is_unknown_not_funded(config, legs):
    from okxtrader.coordinator import Coordinator
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    legs['acct_a'].account_info = lambda: None
    coordinator._account_cache.clear()

    health = coordinator.snapshot()['account_health']

    assert health['unknown'] == ['acct_a']
    assert health['accounts']['acct_a']['equity'] is None


def test_the_page_stamps_its_css_and_js_so_a_stale_cache_cannot_mix(client):
    """A browser serving last week's app.js against today's HTML is not
    merely stale, it is MIXED: new markup with old handlers, and one
    missing element takes out every button wired after it. The stamp is
    what makes `git pull` enough, without a hard refresh nobody
    remembers."""
    body = client.get('/').get_data(as_text=True)

    stamps = re.findall(r"(app\.js|ladder\.css)\?v=(\d+)", body)
    assert {name for name, _ in stamps} == {'app.js', 'ladder.css'}
    assert all(int(value) > 0 for _, value in stamps)
    # And the page itself must never be the cached thing: it carries the
    # stamp.
    assert 'no-store' in client.get('/').headers.get('Cache-Control', '')


def test_the_snapshot_is_only_re_read_when_it_has_actually_changed(
        client, paths, monkeypatch):
    """Every open of the status file on Windows is a moment the
    coordinator's own os.replace cannot land — and that collision
    failed its whole poll. Asking "has it changed?" costs nothing;
    opening it does.

    Counted at `atomicfile.read_text`, which is where a read of this
    file actually happens on BOTH platforms. Counting `builtins.open`
    only worked on POSIX: on Windows `read_text` goes through
    CreateFileW and msvcrt.open_osfhandle to get FILE_SHARE_DELETE, so
    `open` is never called, the count stayed at nought, and the test
    failed on the one operating system it is about.
    """
    import time as clock
    from okxtrader import atomicfile
    write_status(paths)
    first = client.get('/api/status').get_json()

    reads = {'n': 0}
    real_read_text = atomicfile.read_text
    wanted = os.path.normcase(os.path.abspath(paths['status']))

    def counting_read_text(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(str(path))) == wanted:
            reads['n'] += 1
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(atomicfile, 'read_text', counting_read_text)

    for _ in range(5):
        client.get('/api/status')
    assert reads['n'] == 0, 'an unchanged snapshot was re-read'

    # ...and a NEW snapshot is picked up at once, not on a timer.
    # The sleep is for Windows: its file timestamps move in ~15ms steps,
    # so two writes inside one tick can share an mtime.
    clock.sleep(0.05)
    write_status(paths, loop_interval_sec=0.9)
    body = client.get('/api/status').get_json()
    assert body['loop_interval_sec'] == 0.9
    assert reads['n'] == 1
    assert first['pairs']


def test_the_page_is_rebuilt_when_the_template_changes(paths, tmp_path):
    """Jinja compiles a template once and caches it for the life of the
    process. Without auto-reload a `git pull` updated the CSS and the JS
    — those are fetched by URL with a stamp — while the HTML stayed on
    the version the process started with: new handlers against old
    markup, elements missing, and a screen that looks like the pull
    never landed."""
    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'])

    assert app.jinja_env.auto_reload is True
    assert app.config['TEMPLATES_AUTO_RELOAD'] is True




def test_the_desk_never_runs_flasks_debugger_whatever_the_env_says(
        monkeypatch, tmp_path):
    """An old Stat_Arb_W3 .env sets FLASK_DEBUG=true, and Flask obeys
    that by itself unless told otherwise."""
    import sys
    from okxtrader import webapp
    monkeypatch.setenv('FLASK_DEBUG', 'true')
    seen = {}
    monkeypatch.setattr('flask.Flask.run',
                        lambda self, **kw: seen.update(kw))
    monkeypatch.setattr(sys, 'argv', [
        'webapp', '--config', str(tmp_path / 'config.json'),
        '--status', str(tmp_path / 's.json'), '--port', '8999'])
    (tmp_path / 'config.json').write_text('{"accounts": {}, "pairs": {}}')
    webapp.main()
    assert seen.get('debug') is False and seen.get('use_reloader') is False
