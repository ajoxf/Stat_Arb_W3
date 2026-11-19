"""The screen lock, and the PIN that opens it.

Two things a desk asked for and one mechanism underneath: a trader who
left for the day with a live account on screen, and a stray click on a
ladder.

Every refusal below is paired with a CONTROL that turns the same guard
off and asserts the opposite. A lock that refuses everything is not a
lock, it is an outage.
"""

import json
import os
import tempfile

import pytest

from okxtrader import screenlock
from okxtrader.webapp import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(
        {'accounts': {}, 'pairs': {}, 'settings': {}}), encoding='utf-8')
    # SETENV, not delenv, and the difference is the whole point.
    #
    # write_env_value puts the hash into os.environ as well as into
    # .env - that is how a PIN set on a running machine takes effect
    # without a restart. So a test that SETS a PIN leaves one in the
    # process, and every webapp test that ran afterwards got 423 from a
    # lock it never asked for.
    #
    # delenv on a key that is not there records nothing to restore.
    # setenv records it, so teardown removes whatever the test set.
    monkeypatch.setenv(screenlock.PIN_ENV_KEY, '')
    monkeypatch.chdir(tmp_path)
    return create_app(str(tmp_path / 'status.json'),
                      str(tmp_path / 'commands.jsonl'),
                      str(tmp_path / 'results.json'),
                      str(config), str(tmp_path / 'test.db'))


# --- the PIN itself ------------------------------------------------------

def test_the_pin_is_never_stored_only_a_salted_hash_of_it():
    stored = screenlock.hash_pin('2468')
    assert '2468' not in stored
    assert stored.startswith('pbkdf2_sha256$')
    assert screenlock.verify_pin('2468', stored)


def test_control_a_wrong_pin_does_not_verify():
    stored = screenlock.hash_pin('2468')
    assert not screenlock.verify_pin('2469', stored)
    assert not screenlock.verify_pin('', stored)
    assert not screenlock.verify_pin('2468', '')


def test_two_desks_with_the_same_pin_do_not_share_a_hash():
    """Per-PIN salt. A hash lifted from one machine's .env says nothing
    about any other machine."""
    assert screenlock.hash_pin('2468') != screenlock.hash_pin('2468')


def test_a_pin_that_would_lock_the_trader_out_is_refused():
    assert screenlock.check_new_pin('12', '12')
    assert screenlock.check_new_pin('2468', '2469')      # typo, caught


def test_control_a_good_pin_is_accepted():
    assert screenlock.check_new_pin('2468', '2468') == ''


# --- the guard, which is the part that matters ---------------------------

def test_a_machine_with_no_pin_is_not_locked(app):
    """The control that keeps this from being an outage. Every PC that
    updates has no PIN yet, and must go on being exactly the app it was
    yesterday until somebody sets one."""
    client = app.test_client()
    assert client.get('/api/lock').get_json()['locked'] is False
    # 409 is 'the engine is down', which is what a test box says. The
    # point is that it is NOT 423.
    assert client.post('/api/command', json={'kind': 'x'}).status_code != 423


def test_setting_a_pin_locks_the_screen(app):
    client = app.test_client()
    assert client.post('/api/pin',
                       json={'pin': '2468', 'again': '2468'}).status_code == 200
    assert client.get('/api/lock').get_json()['locked'] is True


def test_a_locked_screen_refuses_every_order(app):
    """423 Locked, from the process that queues the orders - not from
    the browser. A lock drawn only on screen is one a refresh walks
    through, and what is on the other side places live trades."""
    client = app.test_client()
    client.post('/api/pin', json={'pin': '2468', 'again': '2468'})
    for path in ('/api/command', '/api/settings', '/api/accounts/AC-1',
                 '/api/pairs/A|B'):
        assert client.post(path, json={}).status_code == 423, path
    assert client.delete('/api/pairs/A|B').status_code == 423


def test_control_the_same_requests_go_through_once_unlocked(app):
    """The control. The guard has to stop being a guard the moment the
    right PIN is typed, or it is just a broken screen."""
    client = app.test_client()
    client.post('/api/pin', json={'pin': '2468', 'again': '2468'})
    assert client.post('/api/unlock',
                       json={'pin': '2468'}).get_json()['ok'] is True
    assert client.get('/api/lock').get_json()['locked'] is False
    assert client.post('/api/command', json={'kind': 'x'}).status_code != 423


def test_reading_the_screen_is_never_blocked(app):
    """The ladder goes on ticking behind the lock, as it does behind
    TT's. A trader coming back wants to see where the spread went
    before they type anything - and a blank screen would also hide the
    banner telling them the engine had died."""
    client = app.test_client()
    client.post('/api/pin', json={'pin': '2468', 'again': '2468'})
    assert client.get('/api/status').status_code == 200
    assert client.get('/api/status').get_json()['lock']['locked'] is True


def test_a_wrong_pin_is_counted_and_then_stops_answering(app):
    """A four-digit PIN is 10,000 guesses. Without this a script does
    that in seconds."""
    client = app.test_client()
    client.post('/api/pin', json={'pin': '2468', 'again': '2468'})
    for _ in range(screenlock.MAX_ATTEMPTS):
        response = client.post('/api/unlock', json={'pin': '0000'})
        assert response.status_code == 403
    after = client.post('/api/unlock', json={'pin': '2468'}).get_json()
    assert after['ok'] is False
    assert 'seconds' in after['error']


def test_changing_the_pin_needs_the_current_one(app):
    client = app.test_client()
    client.post('/api/pin', json={'pin': '2468', 'again': '2468'})
    client.post('/api/unlock', json={'pin': '2468'})
    refused = client.post('/api/pin', json={'current': '0000', 'pin': '1111',
                                            'again': '1111'})
    assert refused.status_code == 403
    assert client.post('/api/unlock', json={'pin': '2468'}).get_json()['ok']


def test_control_the_first_pin_needs_no_current_one(app):
    """There is nothing to prove yet, and a machine that must be
    unlocked before it can be given a lock is one nobody can start
    using."""
    client = app.test_client()
    assert client.post('/api/pin',
                       json={'pin': '2468', 'again': '2468'}).status_code == 200


# --- locking itself when nobody is there ---------------------------------

def test_a_screen_nobody_touches_locks_itself():
    clock = {'t': 0.0}
    lock = screenlock.ScreenLock(clock=lambda: clock['t'],
                                 env={screenlock.PIN_ENV_KEY:
                                      screenlock.hash_pin('2468')})
    assert lock.unlock('2468')[0]
    assert lock.locked is False
    clock['t'] = 899.0
    assert lock.lock_if_idle(900) is False       # still watching
    clock['t'] = 901.0
    assert lock.lock_if_idle(900) is True
    assert lock.locked is True


def test_control_a_screen_being_used_stays_open():
    """The control. Every state-changing request touches it, so a
    trader working through a quiet hour is never locked out mid-trade."""
    clock = {'t': 0.0}
    lock = screenlock.ScreenLock(clock=lambda: clock['t'],
                                 env={screenlock.PIN_ENV_KEY:
                                      screenlock.hash_pin('2468')})
    lock.unlock('2468')
    for _ in range(10):
        clock['t'] += 800.0
        lock.touch()
        assert lock.lock_if_idle(900) is False
    assert lock.locked is False


def test_zero_minutes_turns_auto_lock_off():
    """A desk that wants only the padlock says so, and nothing here
    second-guesses it."""
    clock = {'t': 0.0}
    lock = screenlock.ScreenLock(clock=lambda: clock['t'],
                                 env={screenlock.PIN_ENV_KEY:
                                      screenlock.hash_pin('2468')})
    lock.unlock('2468')
    clock['t'] = 100000.0
    assert lock.lock_if_idle(0) is False
    assert lock.locked is False


def test_a_restart_comes_back_locked():
    """The question this answers is 'is the right person here NOW'. A
    process that starts unlocked has answered it for a session nobody
    was present for."""
    env = {screenlock.PIN_ENV_KEY: screenlock.hash_pin('2468')}
    assert screenlock.ScreenLock(env=env).locked is True


# --- what the browser does about it --------------------------------------
#
#     The overlay is the courtesy, not the guard - every test above is
#     the guard. These pin the parts of the page that would let a stray
#     click through anyway.

from pathlib import Path                                    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / 'okxtrader' / 'static' / 'app.js').read_text(encoding='utf-8')
INDEX = (ROOT / 'okxtrader' / 'templates' /
         'index.html').read_text(encoding='utf-8')
SETTINGS_JS = (ROOT / 'okxtrader' / 'static' /
               'settings.js').read_text(encoding='utf-8')


def test_the_overlay_swallows_events_before_anything_else_sees_them():
    """CAPTURE phase, or the ladder's own handlers run first and the
    order is already sent by the time the overlay is asked."""
    block = APP_JS[APP_JS.index("['click', 'pointerdown'"):][:900]
    assert 'addEventListener(kind, function (e)' in block
    assert '}, true);' in block, 'the listener is not in the capture phase'
    for kind in ('click', 'pointerdown', 'dblclick', 'keydown', 'wheel'):
        assert "'" + kind + "'" in block, kind
    assert 'e.preventDefault();' in block and 'e.stopPropagation();' in block


def test_control_the_lock_box_itself_still_takes_clicks():
    """The control, and without it this is a screen nobody can open:
    the PIN field and the Unlock button are inside the thing that
    swallows every event."""
    block = APP_JS[APP_JS.index("['click', 'pointerdown'"):][:900]
    assert "closest('#lock-overlay')" in block
    assert 'return;' in block


def test_the_padlock_is_hidden_until_there_is_a_way_back_in():
    """Locking a screen with no PIN set is an outage, not a safety
    feature."""
    assert 'id="lock-now"' in INDEX and 'hidden' in INDEX
    assert 'button.hidden = !state_.pin_set;' in APP_JS


def test_the_page_believes_the_server_and_never_itself():
    """The overlay follows what the server says on every poll. A page
    that decided for itself would only be lying to the person in front
    of it - the order is refused either way."""
    assert 'showLock(snapshot.lock || {});' in APP_JS


def test_the_pin_never_goes_anywhere_but_the_pin_endpoint():
    """Not into the settings POST, not into local state, not into a
    toast. `.env` holds a hash; the browser holds the PIN for as long
    as one fetch takes."""
    assert "api('/api/pin'" in SETTINGS_JS
    save = SETTINGS_JS[SETTINGS_JS.index('function savePin'):]
    save = save[:save.index('function onClick')]
    assert 'current.value = fresh.value = again.value = ' in save
    # The settings POST carries the TIMEOUT, never the PIN.
    body = SETTINGS_JS[SETTINGS_JS.index('AUTO_LOCK_MINUTES:'):][:200]
    assert 's-autolock' in body
    assert 's-pin' not in SETTINGS_JS[
        SETTINGS_JS.index('CLICK_CONVENTION:'):
        SETTINGS_JS.index('CLICK_CONVENTION:') + 400]


def test_control_the_pane_learns_only_whether_a_pin_exists():
    """Never the PIN, never its hash - one boolean, which is the entire
    question the pane is allowed to ask."""
    assert 'pin_set' in SETTINGS_JS
    assert 'NEXUS_PIN_HASH' not in SETTINGS_JS
    assert 'NEXUS_PIN_HASH' not in APP_JS
    assert 'NEXUS_PIN_HASH' not in INDEX


# --- A volume that is not zero must never print as zero ------------------
#
#     The Reconciler listed a stuck position as `0.00` and the trader
#     read it as nothing being there. It was 0.001 lots - and that is
#     the whole explanation of "it will not close, even from MT5": a
#     volume under the symbol's minimum lot cannot be the subject of a
#     legal close order, by us or by anybody.
#
#     The formatter that fixes it came from the orphan-leg work on
#     another branch. This is the PROPERTY, asserted here so the two
#     cells on the Reconciler tab cannot quietly go back to toFixed(2)
#     - which is what printed real money as nothing.


def test_the_reconciler_never_rounds_a_real_volume_away_to_zero():
    assert 'fmt(row.volume, 2)' not in APP_JS, 'two decimals hides 0.001'
    assert APP_JS.count('qty(row.volume)') == 2, (
        'both volume cells on the Reconciler tab must go through qty()')


def test_control_the_formatter_it_uses_really_does_keep_the_digits():
    """The control. Naming a function is worthless if the function
    rounds the same way - so this checks what qty() is built from."""
    block = APP_JS[APP_JS.index('function qty(value)'):]
    block = block[:block.index('\n  }')]
    assert 'toFixed(6)' in block, 'six decimals is what keeps 0.001'
    assert 'parseFloat' in block, 'and this is what drops the padding'
