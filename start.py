"""The launcher. One file, no arguments:

    py -3.11 start.py

It brings up everything: the web UI and the engine (the coordinator,
which talks to OKX's API directly - and a leg runner for any MetaTrader 5
account a desk still keeps). Three things make it the only file anyone
has to run.

**It starts the UI FIRST, even with nothing configured.** A fresh
install has no accounts, and the accounts are entered ON THE SCREEN —
credentials included, into `.env`, never into a file anyone edits by
hand. A launcher that exits with "no accounts configured" leaves the
operator with nowhere to enter them.

**It watches the config and restarts what changed.** Accounts, symbols
and the hedge ratio are read at startup by the runners and the
coordinator, so when the operator saves an account the engine has to
come back to see it. It does that itself, within a few seconds, and
says so — rather than making a restart the operator's job to remember.

**It never `terminate()`s a child that is running its own shutdown.**
The coordinator's shutdown sweeps our resting orders off OKX; killing
it half way there leaves orders resting that nothing is watching.
"""

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time

from okxtrader.config import TraderConfig, engine_hold_reasons
from okxtrader.ipc import parse_endpoint

#: How long a child gets to finish its own shutdown before it is killed.
#: The coordinator's sweep is two round trips per account, so this is
#: generous on purpose.
SHUTDOWN_GRACE_SEC = 30.0
BACKOFF_START_SEC = 1.0
BACKOFF_MAX_SEC = 30.0


def first_run(config_path):
    """Make the files a first run needs, and say what happened.

    A fresh clone has `config.example.json` and `.env.example` and
    nothing else. Copying them here means the operator never has to
    know that, and never has to open a text editor to start.
    """
    made = []
    if not os.path.exists(config_path):
        example = 'config.example.json'
        if os.path.exists(example):
            shutil.copyfile(example, config_path)
        else:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump({'accounts': {}, 'pairs': {}, 'settings': {}}, f,
                          indent=2)
        made.append(config_path)
    env = os.path.join(os.path.dirname(os.path.abspath(config_path)) or '.',
                       '.env')
    if not os.path.exists(env):
        if os.path.exists('.env.example'):
            shutil.copyfile('.env.example', env)
        else:
            open(env, 'a').close()
        try:
            os.chmod(env, 0o600)         # passwords live here
        except OSError:
            pass
        made.append(env)
    return made


def engine_fingerprint(config_path):
    """What the RUNNERS and the coordinator read at startup.

    Only these fields: a change to any of them means the engine is
    running on stale information and has to come back. Everything else
    the UI applies live, and restarting for it would interrupt trading
    for no reason.
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None                      # mid-write; try again next pass
    accounts = {
        name: {key: (account or {}).get(key)
               for key in ('endpoint', 'login', 'server', 'terminal_path',
                           'password_env', 'exchange', 'demo', 'api_key_env',
                           'secret_env', 'passphrase_env', 'base_url')}
        for name, account in (raw.get('accounts') or {}).items()}
    pairs = {
        key: {field: (pair or {}).get(field)
              for field in ('leg_a', 'leg_b', 'hedge_ratio', 'enabled')}
        for key, pair in (raw.get('pairs') or {}).items()}
    # A key typed again on the Exchanges page keeps its .env NAME, so the
    # names alone would never notice it: a digest of .env itself does
    # (the digest only - no value is held or logged here).
    import hashlib
    env_path = os.path.join(os.path.dirname(os.path.abspath(config_path)),
                            '.env')
    try:
        with open(env_path, 'rb') as f:
            env_digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        env_digest = None
    return json.dumps({'accounts': accounts, 'pairs': pairs,
                       'env': env_digest}, sort_keys=True)


def check_config(path):
    """Refuse to start on a clash, and say which field to correct.

    Every one of these is also refused at SAVE time, in the UI. This is
    the backstop for a config edited by hand.
    """
    problems = []
    if not path:
        return problems
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except OSError as e:
        return [f'could not read {path}: {e}']
    except ValueError as e:
        return [f'{path} is not valid JSON: {e}. Fix the file, or delete '
                f'it to start over — the engine waits here until it reads.']
    try:
        # The real load, with everything it validates. Anything it
        # raises is a problem to PRINT and wait on, never a traceback
        # out of the launcher: a launcher that dies here leaves the web
        # process serving the last status file it had, and the screen
        # shows an old market with no engine behind it.
        TraderConfig.from_raw(raw, path)
    except Exception as e:
        return [f'{path} could not be loaded: {type(e).__name__}: {e}']
    # The clashes, and a MetaTrader 5 that is not where an account
    # says: the same list the web screen reads to say why the engine
    # is waiting (`config.engine_hold_reasons`).
    problems.extend(engine_hold_reasons(raw))
    for name, account in (raw.get('accounts') or {}).items():
        account = account or {}
        endpoint = (account.get('endpoint') or '').strip()
        if endpoint:
            try:
                parse_endpoint(endpoint)
            except ValueError as e:
                problems.append(f"account '{name}': {e}")
    return problems


class Child:
    def __init__(self, name, argv):
        self.name = name
        self.argv = argv
        self.process = None
        self.backoff = BACKOFF_START_SEC
        self.started_at = None

    def start(self):
        self.process = subprocess.Popen(self.argv)
        self.started_at = time.time()
        print(f'[launcher] started {self.name} (pid {self.process.pid})')

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def restart_if_dead(self):
        if self.alive():
            # A child that has stayed up is not in trouble any more, so
            # the next crash starts from a short wait again.
            if time.time() - self.started_at > 60:
                self.backoff = BACKOFF_START_SEC
            return False
        code = self.process.returncode if self.process else None
        print(f'[launcher] {self.name} exited ({code}) — restarting in '
              f'{self.backoff:g}s')
        time.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, BACKOFF_MAX_SEC)
        self.start()
        return True

    def stop(self):
        """Ask, then wait, and only then insist."""
        if not self.alive():
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=SHUTDOWN_GRACE_SEC)
        except subprocess.TimeoutExpired:
            print(f'[launcher] {self.name} did not finish its shutdown in '
                  f'{SHUTDOWN_GRACE_SEC:g}s — killing it. Check both '
                  f'terminals for pendings of ours still resting.')
            self.process.kill()


def open_window_wanted(config_path):
    """Should this start open a window at all?

    A setting rather than a flag, because it is a property of the DESK -
    the trader who runs the page as a PWA wants it off every morning,
    not once. Read here and nowhere else; the launcher reads it at
    startup, so it is deliberately NOT one of the hot settings the
    Settings pane can change, which would look applied while nothing
    had happened.

    Missing, unreadable or unparseable config: TRUE. A trader whose
    config has a problem still needs the screen that fixes it.
    """
    try:
        with open(config_path, encoding='utf-8') as handle:
            raw = json.load(handle)
        setting = (raw.get('settings') or {}).get('OPEN_WINDOW_ON_START')
    except Exception:
        return True
    if setting is None:
        return True
    if isinstance(setting, str):
        return setting.strip().lower() not in ('false', 'no', '0', 'off')
    return bool(setting)


def page_is_served(url):
    """Did the screen actually answer? Injected in tests as `probe`."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=2.0):
            return True
    except urllib.error.HTTPError:
        # It ANSWERED. A 404 or a 500 is a served page, and the screen
        # is what shows the reason - refusing to open the window over
        # it would hide the only diagnosis there is.
        return True
    except Exception:
        return False


def wait_until_serving(host, port, timeout=45.0, step=0.25,
                       sleep=time.sleep, now=time.monotonic, probe=None):
    """Block until the web process is actually accepting connections.

    A fixed `sleep(2)` used to stand here, and on a PC that had just
    finished the safety tests two seconds was not enough: the window
    opened onto ERR_CONNECTION_REFUSED, the trader reloaded or opened
    the app themselves, and ended up with two windows on the same
    screen.

    A REAL REQUEST, not a bare TCP connect. The listening socket exists
    from the moment the server binds, which is before it can answer
    anything - a connect that succeeds there proves only that the port
    is taken. Asking for the page proves the page is servable, which is
    the thing the window is about to do.

    How long that takes is a property of the machine, not a number
    anybody can pick. Returns True when it is up, False if it never
    came.
    """
    probe = probe or page_is_served
    url = f'http://{host}:{port}/'
    deadline = now() + timeout
    while now() < deadline:
        if probe(url):
            return True
        sleep(step)
    return False


def port_in_use(host, port):
    """Is something already listening there?

    Almost always the answer is "the last MT5-Trader, still running".
    Windows keeps that process alive when its console is closed the
    wrong way, and the browser then goes on being served by the OLD
    one — old page, old engine, and a pull that appears to do nothing
    however many times it is run. That is a miserable thing to
    diagnose from a screenshot, so it is refused here with the fix.
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        return probe.connect_ex((host if host != '0.0.0.0' else '127.0.0.1',
                                 int(port))) == 0
    finally:
        probe.close()


def main():
    parser = argparse.ArgumentParser(description='MT5-Trader launcher')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--db', default='okxtrader.db')
    parser.add_argument('--web-port', type=int, default=8000)
    parser.add_argument('--host', default='127.0.0.1',
                        help='the UI has no password — leave this on '
                             'localhost and reach it through a tunnel')
    parser.add_argument('--no-web', action='store_true',
                        help='engine only, no browser UI')
    parser.add_argument('--print-shortcut', action='store_true',
                        help='print the command a desktop shortcut should '
                             'run, and exit — the installer uses this so '
                             'the shortcut and the launcher cannot drift '
                             'apart about what "open Nexus" means')
    parser.add_argument('--no-browser', action='store_true',
                        help='do not open a browser window')
    args = parser.parse_args()

    if args.print_shortcut:
        # Before anything else starts: this answers a question and
        # trades nothing.
        from okxtrader import appwindow
        line = appwindow.shortcut_target(
            f'http://127.0.0.1:{args.web_port}/')
        print(line or '')
        return 0 if line else 1

    # The launcher's own decisions - which children it started, which
    # it restarted, why it held the engine down - go on disk too. It is
    # the process that knows what the others were told to do.
    try:
        from okxtrader import logsetup
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s [launcher] %(message)s',
                            handlers=[logging.StreamHandler()])
        logsetup.setup('launcher')
    except Exception:
        pass                       # never stop a start over a log file

    made = first_run(args.config)
    for path in made:
        print(f'[launcher] created {path}')

    if not args.no_web and port_in_use(args.host, args.web_port):
        print(f'\n[launcher] PORT {args.web_port} IS ALREADY IN USE.\n'
              f'  Another MT5-Trader is almost certainly still running — '
              f'and your browser is being served by THAT one, which is why '
              f'a pull can look like it did nothing.\n'
              f'  Close the other black window (or end its python.exe in '
              f'Task Manager), then start this again.\n'
              f'  To run a second copy deliberately: '
              f'python start.py --web-port {args.web_port + 1}\n')
        return 1

    url = f'http://127.0.0.1:{args.web_port}/'
    web = None
    if not args.no_web:
        # FIRST, and before anything can refuse to start: the accounts
        # are entered on this page, so it has to be reachable even when
        # there are none.
        web = Child('web', [sys.executable, '-m', 'okxtrader.webapp',
                            '--config', args.config, '--status', args.status,
                            '--commands', args.commands,
                            '--results', args.results, '--db', args.db,
                            '--host', args.host,
                            '--port', str(args.web_port)])
        web.start()
        print(f'[launcher] the screen is at {url}')
        # TWO REASONS THIS IS NOT JUST "OPEN THE BROWSER".
        #
        # It waits for the port. Opening at a fixed two seconds put
        # ERR_CONNECTION_REFUSED in front of the trader on any machine
        # that was busy - and the natural response, opening the app by
        # hand, is what left two windows on one screen.
        #
        # And it can be turned off. A desk that has installed the page
        # as a PWA already has its window, in its own browser profile;
        # this one cannot see that and would open a second. Set
        # OPEN_WINDOW_ON_START to false in config.json and this start
        # leaves the screen to the window the trader already keeps.
        if not args.no_browser and open_window_wanted(args.config):
            if wait_until_serving(args.host, args.web_port):
                # SAID, immediately before it happens. A trader who
                # sees a refused page can now tell from this window
                # whether it was this launcher that opened it - and if
                # this line is not above it, it was something else:
                # a pinned tab, a restored session, or a PWA that
                # Windows starts at sign-in.
                print('[launcher] opening the screen now')
                # A WINDOW OF ITS OWN, not a tab. See
                # okxtrader/appwindow.py for why that matters on a
                # screen that sends live orders.
                from okxtrader import appwindow
                appwindow.open_window(url)
            else:
                print(f'[launcher] the screen has not come up yet - no '
                      f'window was opened. Browse to {url} once it does, '
                      f'and send whatever this window prints below to '
                      f'whoever maintains this.')

    engine = Engine(args)
    fingerprint = None
    said = None                    # the problems already printed
    try:
        while True:
            if web is not None:
                web.restart_if_dead()

            problems = check_config(args.config)
            current = engine_fingerprint(args.config)
            if problems:
                # Refuse to run the ENGINE on a clashing config, but
                # keep the screen up: the page that fixes it is the one
                # being served. Said ONCE per distinct problem: this
                # loop re-reads the file every three seconds, and the
                # same paragraph scrolling forever buries whatever the
                # operator does next.
                engine.stop('the configuration has a clash')
                if problems != said:
                    for problem in problems:
                        print(f'[launcher] {problem}')
                    print('[launcher] fix it on the Exchanges page; the '
                          'engine starts itself when it is right')
                    said = problems
                fingerprint = None
                time.sleep(3.0)
                continue
            if said:
                print('[launcher] the configuration reads — starting the '
                      'engine')
                said = None

            if current is not None and current != fingerprint:
                if fingerprint is not None:
                    print('[launcher] the accounts or pairs changed — '
                          'restarting the engine to pick it up')
                fingerprint = current
                engine.restart()
                if not engine.children:
                    print('[launcher] no account has a runner endpoint yet. '
                          f'Add your accounts on the Exchanges page at {url} '
                          f'— the engine starts itself as soon as one is '
                          f'saved.')

            engine.supervise()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print('\n[launcher] stopping — the coordinator sweeps its pendings '
              'first, so give it a moment')
        engine.stop('the launcher was stopped')
        if web is not None:
            web.stop()


class Engine:
    """The processes that talk to MT5: the runners and the coordinator.

    Kept together because they come and go together: a runner restarted
    without the coordinator leaves the coordinator holding a dead
    socket, and the coordinator restarted alone re-reads a config the
    runners have not.
    """

    def __init__(self, args):
        self.args = args
        self.children = []

    def restart(self):
        self.stop('picking up a configuration change')
        try:
            config = TraderConfig.from_file(self.args.config)
        except Exception as e:
            # Belt to check_config's braces. The engine stays down, the
            # screen stays up, and the loop tries again — an unreadable
            # config is never an exception out of the launcher.
            print(f'[launcher] {self.args.config} could not be loaded: '
                  f'{type(e).__name__}: {e} — the engine stays down until '
                  f'it reads')
            self.children = []
            return
        # A port still held is a runner from the last start, still
        # attached to the terminal. Two clients logging one terminal in
        # is a feed that ticks for a few seconds and then goes quiet —
        # so it is named here rather than left to look like a broken
        # market.
        for name, account in config.accounts.items():
            if not account.endpoint:
                continue
            try:
                host, port = parse_endpoint(account.endpoint)
            except ValueError:
                continue
            if port_in_use(host, port):
                print(f"[launcher] account '{name}': {account.endpoint} is "
                      f"ALREADY IN USE. A leg runner from a previous start "
                      f"is still there — close the other window or end its "
                      f"python.exe, or this account will have two clients "
                      f"on one terminal and a feed that keeps dropping.")
        self.children = [
            Child(f'leg:{name}',
                  [sys.executable, 'run_leg.py',
                   '--config', self.args.config, '--account', name])
            for name, account in config.accounts.items()
            if account.endpoint and not account.is_okx]
        # An OKX account has no runner: the coordinator reaches it over
        # its API itself. The engine runs when there is either.
        has_okx = any(a.is_okx for a in config.accounts.values())
        if not self.children and not has_okx:
            return
        for child in self.children:
            child.start()
        # The coordinator last: connecting to a runner that has not
        # bound its port yet is a retry loop nobody needs to watch.
        if self.children:
            time.sleep(2.0)
        coordinator = Child('coordinator',
                            [sys.executable, 'run_coordinator.py',
                             '--config', self.args.config,
                             '--status', self.args.status,
                             '--commands', self.args.commands,
                             '--results', self.args.results,
                             '--db', self.args.db])
        coordinator.start()
        self.children.append(coordinator)

    def supervise(self):
        for child in self.children:
            child.restart_if_dead()

    def stop(self, why):
        if not self.children:
            return
        print(f'[launcher] stopping the engine — {why}')
        # Coordinator first: it needs its runners alive to cancel
        # through them.
        for child in reversed(self.children):
            child.stop()
        self.children = []


if __name__ == '__main__':
    raise SystemExit(main() or 0)
