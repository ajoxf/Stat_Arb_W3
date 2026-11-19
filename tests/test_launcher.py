"""The launcher: one file, no arguments, and nothing to edit by hand.

The rules here exist because the operator is not the person who built
this: a fresh clone must start, the screen must come up even with
nothing configured, and saving an account on that screen must be enough
to get the engine running.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import start                                                    # noqa: E402


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_fresh_clone_makes_the_files_it_needs(workdir):
    """Nobody should have to know that config.example.json exists."""
    (workdir / 'config.example.json').write_text(
        json.dumps({'accounts': {}, 'pairs': {'X|Y': {}}}), encoding='utf-8')
    (workdir / '.env.example').write_text('MT5_PASSWORD_A=""\n',
                                          encoding='utf-8')

    made = start.first_run('config.json')

    assert (workdir / 'config.json').exists()
    assert (workdir / '.env').exists()
    assert len(made) == 2
    assert json.loads((workdir / 'config.json').read_text())['pairs'] == {
        'X|Y': {}}
    # Second time: nothing to do, and nothing overwritten.
    (workdir / 'config.json').write_text('{"accounts": {"a": {}}}',
                                         encoding='utf-8')
    assert start.first_run('config.json') == []
    assert 'a' in json.loads((workdir / 'config.json').read_text())['accounts']


def test_it_starts_even_with_no_example_to_copy(workdir):
    made = start.first_run('config.json')
    assert made == ['config.json', str(workdir / '.env')]
    assert json.loads((workdir / 'config.json').read_text()) == {
        'accounts': {}, 'pairs': {}, 'settings': {}}


def test_the_env_file_is_not_world_readable(workdir):
    """It holds the passwords, and nothing else does."""
    start.first_run('config.json')
    mode = os.stat(workdir / '.env').st_mode & 0o777
    assert mode == 0o600 or os.name == 'nt'


def test_only_what_the_engine_reads_at_startup_triggers_a_restart(workdir):
    """Restarting for a display setting would interrupt trading for no
    reason; not restarting for a symbol change would leave the engine
    trading the wrong instrument."""
    config = {
        'accounts': {'a': {'endpoint': '127.0.0.1:9101', 'login': 1}},
        'pairs': {'X|Y': {'leg_a': {'account': 'a', 'symbol': 'X'},
                          'hedge_ratio': 1.0, 'enabled': True,
                          'increment': 0.01}},
        'settings': {'MARKET_PROTECTION_TICKS': 3.0},
    }
    (workdir / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    before = start.engine_fingerprint('config.json')

    # Comfort settings and per-ladder knobs: no restart.
    config['settings']['MARKET_PROTECTION_TICKS'] = 5.0
    config['pairs']['X|Y']['increment'] = 0.05
    (workdir / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    assert start.engine_fingerprint('config.json') == before

    # A symbol is structural: the runners read it at startup.
    config['pairs']['X|Y']['leg_a']['symbol'] = 'Z'
    (workdir / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    assert start.engine_fingerprint('config.json') != before


def test_a_new_account_is_a_restart_because_the_engine_cannot_see_it(workdir):
    (workdir / 'config.json').write_text(
        json.dumps({'accounts': {}, 'pairs': {}}), encoding='utf-8')
    empty = start.engine_fingerprint('config.json')

    (workdir / 'config.json').write_text(
        json.dumps({'accounts': {'a': {'endpoint': '127.0.0.1:9101'}},
                    'pairs': {}}), encoding='utf-8')

    assert start.engine_fingerprint('config.json') != empty


def test_a_half_written_config_is_not_read_as_a_change(workdir):
    """The UI writes through a tmp file, but a reader can still catch a
    filesystem mid-flight. A restart on a partial read would be a
    restart for nothing."""
    (workdir / 'config.json').write_text('{"accounts": ', encoding='utf-8')
    assert start.engine_fingerprint('config.json') is None


def test_the_launcher_refuses_to_run_an_engine_on_a_clashing_config(workdir):
    """Two accounts on one port is the same MT5 account twice. The
    screen stays up so the clash can be fixed on it."""
    (workdir / 'config.json').write_text(json.dumps({
        'accounts': {'a': {'endpoint': '127.0.0.1:9101'},
                     'b': {'endpoint': '127.0.0.1:9101'}},
        'pairs': {}}), encoding='utf-8')

    problems = start.check_config('config.json')

    assert problems and 'already belongs to account' in problems[0]


def test_a_config_with_no_accounts_is_not_a_problem_to_refuse(workdir):
    """A fresh install has none — that is where every install starts,
    and the answer is to open the page, not to exit."""
    (workdir / 'config.json').write_text(
        json.dumps({'accounts': {}, 'pairs': {}}), encoding='utf-8')
    assert start.check_config('config.json') == []


class FakeChild:
    """Records what the launcher would have spawned."""

    started = []
    stopped = []

    def __init__(self, name, argv):
        self.name = name
        self.argv = argv
        self.alive_ = False

    def start(self):
        self.alive_ = True
        FakeChild.started.append(self.name)

    def alive(self):
        return self.alive_

    def restart_if_dead(self):
        return False

    def stop(self):
        self.alive_ = False
        FakeChild.stopped.append(self.name)


def test_the_engine_spawns_a_runner_per_account_and_one_coordinator(
        workdir, monkeypatch):
    (workdir / 'config.json').write_text(json.dumps({
        'accounts': {'spot': {'endpoint': '127.0.0.1:9101'},
                     'fut': {'endpoint': '127.0.0.1:9102'},
                     'no-runner': {}},
        'pairs': {}}), encoding='utf-8')
    FakeChild.started, FakeChild.stopped = [], []
    monkeypatch.setattr(start, 'Child', FakeChild)
    monkeypatch.setattr(start.time, 'sleep', lambda seconds: None)
    engine = start.Engine(_args(workdir))

    engine.restart()

    assert FakeChild.started == ['leg:spot', 'leg:fut', 'coordinator']
    # An account with no endpoint has no runner — and is not an error:
    # it is an account that has not been given a port yet.
    assert 'leg:no-runner' not in FakeChild.started

    engine.stop('test')
    # Coordinator FIRST: it needs its runners alive to cancel through
    # them.
    assert FakeChild.stopped == ['coordinator', 'leg:fut', 'leg:spot']


def test_no_endpoint_anywhere_starts_nothing_and_is_not_a_crash(workdir,
                                                                monkeypatch):
    (workdir / 'config.json').write_text(
        json.dumps({'accounts': {'a': {'exchange': 'mt5'}}, 'pairs': {}}),
        encoding='utf-8')
    FakeChild.started = []
    monkeypatch.setattr(start, 'Child', FakeChild)
    engine = start.Engine(_args(workdir))

    engine.restart()

    assert engine.children == []
    assert FakeChild.started == []


def test_an_okx_account_needs_no_runner_only_the_coordinator(workdir,
                                                             monkeypatch):
    """OKX is a REST API the coordinator talks to directly: there is no
    terminal to host, so no leg runner — just the coordinator."""
    (workdir / 'config.json').write_text(
        json.dumps({'accounts': {'okx': {'exchange': 'okx'}}, 'pairs': {}}),
        encoding='utf-8')
    FakeChild.started = []
    monkeypatch.setattr(start, 'Child', FakeChild)
    monkeypatch.setattr(start.time, 'sleep', lambda seconds: None)
    engine = start.Engine(_args(workdir))

    engine.restart()

    assert FakeChild.started == ['coordinator']
    engine.stop('test')


def _args(workdir):
    import argparse
    return argparse.Namespace(
        config='config.json', status='status.json',
        commands='commands.jsonl', results='results.json', db='trader.db')


def test_a_second_launcher_refuses_the_port_instead_of_being_invisible(
        workdir, monkeypatch, capsys):
    """The failure this exists for: the last instance is still running,
    the browser goes on being served by THAT one, and a pull looks like
    it did nothing however many times it is run — old page, old engine,
    and nothing on screen to say so."""
    import socket
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    # Backlog of 5, not 1, and the number is load-bearing on Windows.
    #
    # This stand-in never calls accept(), so every probe stays queued.
    # `port_in_use` is called TWICE here - once by the assert below and
    # once inside main() - and Linux quietly allows backlog+1 waiting,
    # so a backlog of 1 held both and the test passed there.
    #
    # Windows holds exactly `backlog`. The second probe could not
    # complete, port_in_use answered False, and main() went on to start
    # a REAL engine in the tmp directory: a web child that cannot import
    # okxtrader, restarted forever on a backoff that caps at 30s. That
    # is the fourteen minutes the suite appeared to hang for.
    #
    # A real second instance is a running server that accepts, so its
    # backlog never fills. Five keeps the stand-in honest to that.
    listener.listen(5)
    port = listener.getsockname()[1]
    try:
        assert start.port_in_use('127.0.0.1', port) is True

        monkeypatch.setattr(sys, 'argv',
                            ['start.py', '--web-port', str(port)])
        code = start.main()

        assert code == 1
        printed = capsys.readouterr().out
        assert 'ALREADY IN USE' in printed
        assert 'Close the other black window' in printed
        # ...and the way to run a second one on purpose.
        assert f'--web-port {port + 1}' in printed
    finally:
        listener.close()

    # The control: a free port is not refused.
    assert start.port_in_use('127.0.0.1', port) is False


def test_a_runner_port_still_held_is_named_before_the_engine_starts(
        workdir, monkeypatch, capsys):
    """A port still held is a leg runner from the last start, still
    attached to the terminal. Two clients logging one terminal in is a
    feed that ticks for a few seconds and then goes quiet — it must not
    be left to look like a broken market."""
    import socket
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    (workdir / 'config.json').write_text(json.dumps({
        'accounts': {'leg_a': {'endpoint': f'127.0.0.1:{port}'}},
        'pairs': {}}), encoding='utf-8')
    FakeChild.started = []
    monkeypatch.setattr(start, 'Child', FakeChild)
    monkeypatch.setattr(start.time, 'sleep', lambda seconds: None)
    engine = start.Engine(_args(workdir))
    try:
        engine.restart()
    finally:
        listener.close()

    printed = capsys.readouterr().out
    assert 'ALREADY IN USE' in printed
    assert "account 'leg_a'" in printed
    assert 'two clients on one terminal' in printed


# --- Opening the screen -------------------------------------------------
#
#     A desk reported ERR_CONNECTION_REFUSED at every start, then TWO
#     windows on one screen. Both came from the same line: a fixed
#     sleep(2) before opening the browser.

def test_the_window_waits_for_the_server_rather_than_a_fixed_two_seconds():
    """The port is the only honest signal. How long the web child takes
    to import Flask, read the config and bind is a property of the
    machine, not a number anybody can pick - and on a PC that had just
    run the safety tests, two seconds was not enough."""
    import start
    calls = {'slept': 0.0}
    clock = {'t': 0.0}

    def sleep(seconds):
        calls['slept'] += seconds
        clock['t'] += seconds

    # Answering on the fourth look, not the first.
    looks = {'n': 0, 'url': None}

    def busy(url):
        looks['n'] += 1
        looks['url'] = url
        return looks['n'] >= 4

    assert start.wait_until_serving('127.0.0.1', 8000, sleep=sleep,
                                    now=lambda: clock['t'], probe=busy)
    assert looks['n'] == 4
    assert calls['slept'] > 0        # it waited, rather than spinning
    # It asks for the PAGE. A bare TCP connect succeeds from the moment
    # the server binds, which is before it can answer anything.
    assert looks['url'] == 'http://127.0.0.1:8000/'


def test_control_a_server_that_never_comes_up_opens_no_window():
    """The control: it must give up and SAY so. A window opened onto a
    port with nothing behind it is the refused-connection page the
    trader was shown, and the natural fix - opening the app by hand -
    is what left two windows on the screen."""
    import start
    clock = {'t': 0.0}

    def sleep(seconds):
        clock['t'] += seconds

    assert start.wait_until_serving('127.0.0.1', 8000, timeout=5.0,
                                    sleep=sleep, now=lambda: clock['t'],
                                    probe=lambda url: False) is False


def test_a_pwa_desk_can_stop_a_second_window_opening(tmp_path):
    """A trader who has installed the page as a PWA already has a
    window, in their own browser profile. This start cannot see it and
    would open a second one beside it."""
    import json as _json
    import start
    path = tmp_path / 'config.json'
    path.write_text(_json.dumps(
        {'settings': {'OPEN_WINDOW_ON_START': False}}), encoding='utf-8')
    assert start.open_window_wanted(str(path)) is False
    path.write_text(_json.dumps(
        {'settings': {'OPEN_WINDOW_ON_START': 'false'}}), encoding='utf-8')
    assert start.open_window_wanted(str(path)) is False


def test_control_the_window_opens_when_nothing_says_otherwise(tmp_path):
    """The control, and it covers the cases that matter more than the
    setting: a config with no such key, a config that will not parse,
    and no config at all. A trader whose config has a problem still
    needs the screen that fixes it."""
    import json as _json
    import start
    path = tmp_path / 'config.json'
    path.write_text(_json.dumps({'settings': {}}), encoding='utf-8')
    assert start.open_window_wanted(str(path)) is True
    path.write_text('{ not json', encoding='utf-8')
    assert start.open_window_wanted(str(path)) is True
    assert start.open_window_wanted(str(tmp_path / 'nope.json')) is True


def test_a_page_that_answers_with_an_error_still_counts_as_up():
    """A 404 or a 500 is a SERVED page. The screen is what shows the
    reason, so refusing to open the window over it would hide the only
    diagnosis there is - and leave the trader with nothing at all
    rather than a page with the fault written on it."""
    import urllib.error
    import start

    def raises_http(url, timeout=None):
        raise urllib.error.HTTPError(url, 500, 'boom', {}, None)

    import urllib.request
    original = urllib.request.urlopen
    urllib.request.urlopen = raises_http
    try:
        assert start.page_is_served('http://127.0.0.1:8000/') is True
    finally:
        urllib.request.urlopen = original


def test_control_a_refused_connection_is_not_up():
    """The control. The point of the probe is to tell those apart."""
    import start
    import urllib.request
    original = urllib.request.urlopen

    def refused(url, timeout=None):
        raise ConnectionRefusedError('nothing listening')

    urllib.request.urlopen = refused
    try:
        assert start.page_is_served('http://127.0.0.1:8000/') is False
    finally:
        urllib.request.urlopen = original


def test_the_launcher_says_when_it_is_the_one_opening_a_window():
    """So a refused page can be attributed. If this line is not above
    it in the black window, the window came from somewhere else - a
    pinned tab, a restored session, or a PWA Windows starts at
    sign-in."""
    source = (Path(__file__).resolve().parent.parent /
              'start.py').read_text(encoding='utf-8')
    assert "print('[launcher] opening the screen now')" in source
    assert source.index('opening the screen now') < \
        source.index('appwindow.open_window(url)')
