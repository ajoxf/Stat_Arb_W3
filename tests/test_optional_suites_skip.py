"""An optional suite must SKIP on a desk, never ERROR.

`pytest tests/ -q` is the gate START-TRADING and UPDATE.BAT run before
they will let a machine trade. The browser suites are optional - a
trading desk has no browser and does not need one - so anything that
stops them importing has to end in a skip. An error there fails the
gate, and UPDATE.BAT then rolls a good version back off the machine.

That is not hypothetical. On a locked-down desk Windows Application
Control blocked `_greenlet.pyd`, which playwright imports:

    ImportError: DLL load failed while importing _greenlet:
    An Application Control policy has blocked this file.

`pytest.importorskip` did not catch it. Since pytest 9.1 it defaults to
ModuleNotFoundError only, and requirements.txt asks for
`pytest>=8.0.0` - so a desk installed that day got 9.1 and the new
behaviour without anyone choosing it. Collection errored, the gate
failed, and a machine that was perfectly able to trade was put back on
the previous version.

This runs pytest in a subprocess against a playwright that raises
exactly that ImportError, and requires a clean exit.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


#: THE WHOLE DIRECTORY, because that is what the gate runs.
#:
#: Pointing this at the two browser files alone would collect nothing
#: once they skip, and pytest exits 5 for that - which reads as a
#: failure here while the real gate, `pytest tests/ -q`, is perfectly
#: happy. Testing the command the desk actually runs is the point.
GATE = ['tests/']

BLOCKED_DLL = (
    'raise ImportError("DLL load failed while importing _greenlet: '
    'An Application Control policy has blocked this file.")\n'
)

REPO = Path(__file__).resolve().parent.parent


def collect(extra_path=None):
    """Collect the gate's own suite in a subprocess; (rc, output)."""
    # THE REAL ENVIRONMENT, with only PYTHONPATH changed.
    #
    # Built from scratch first - {'PATH': '/usr/bin:/bin', ...} - which
    # is a POSIX box's environment and nothing else's. On Windows,
    # dropping SystemRoot takes Winsock with it, so the child could not
    # even `import asyncio`:
    #
    #     OSError: [WinError 10106] The requested service provider
    #     could not be loaded or initialized
    #
    # Collection then errored for a reason that had nothing to do with
    # what is under test, and THIS FILE would have failed the gate on
    # every Windows desk - the exact failure it was written to prevent.
    # Caught by the `pytest on Windows` job, which is the whole reason
    # that job exists.
    #
    # PYTHONPATH is popped before it is set so a parent run that
    # already has one cannot leak into the control.
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    if extra_path is not None:
        env['PYTHONPATH'] = str(extra_path)
    done = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q',
         '-p', 'no:cacheprovider', *GATE],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=300)
    return done.returncode, done.stdout + done.stderr


def test_a_playwright_that_cannot_LOAD_skips_instead_of_erroring(tmp_path):
    """The desk's own failure, reproduced: the module is installed and
    importable as a name, and blows up with a plain ImportError when it
    is actually loaded."""
    stub = tmp_path / 'blocked'
    (stub / 'playwright').mkdir(parents=True)
    (stub / 'playwright' / '__init__.py').write_text('')
    (stub / 'playwright' / 'sync_api.py').write_text(BLOCKED_DLL)

    code, output = collect(extra_path=stub)

    # PYTEST'S OWN MARKERS, not the word "error" anywhere in the text:
    # a bare substring match hits the names of tests that are about
    # errors, of which this suite has plenty.
    assert 'errors during collection' not in output, output[-2000:]
    assert not re.search(r'^ERROR ', output, re.M), output[-2000:]
    assert code == 0, f'the gate would have failed (rc={code})\n{output[-2000:]}'

    # ...and the skip is the REASON, so the next person sees why.
    assert 'skipped' in output.lower(), output[-2000:]


def test_CONTROL_a_working_playwright_still_collects_the_suites(tmp_path):
    """The control. "Skip on ImportError" must not become "skip always"
    — that would retire both browser suites silently, and the CI job
    that runs them would go green having tested nothing."""
    # THE SAME TRAP, IN THE TEST THAT GUARDS AGAINST IT.
    #
    # Written first as `pytest.importorskip('playwright.sync_api')`,
    # which is precisely the call this file exists because of: on the
    # desk where the DLL is blocked it raises instead of skipping, and
    # this control - the one meant to prove the gate still works -
    # would have failed the gate itself. Caught by running the suite
    # against the blocked-DLL stub before shipping it.
    try:
        import playwright.sync_api                  # noqa: F401
    except ImportError as exc:
        pytest.skip(f'no playwright here to control against: {exc}')
    code, output = collect()

    assert code == 0, output[-2000:]
    # The browser suites are REALLY in the collection, not skipped away
    # to nothing - otherwise the CI job that runs them goes green
    # having tested nothing at all.
    assert 'test_ui_browser.py' in output, output[-2000:]
    assert 'test_end_to_end.py' in output, output[-2000:]
