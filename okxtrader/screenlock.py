"""The screen lock, and the PIN that opens it.

Two things a desk asked for, and they are the same mechanism:

- A trader leaves for the day, somebody else opens NEXUS from the
  Desktop, and the accounts are simply there - live, with positions,
  one click from a trade.
- A trader is watching a ladder and does not want a stray click to
  send an order.

BOTH ARE ENFORCED HERE, ON THE SERVER, and that is the whole point of
this module existing at all. A lock drawn only in the browser is a
lock that a page refresh, a second tab or the developer console walks
straight through - and the thing on the other side of it places live
orders. `webapp.py` refuses every state-changing request while this
says locked, so the overlay is a courtesy to the trader rather than
the guard itself.

WHAT THIS IS NOT. It is not a security boundary against somebody
sitting at that keyboard. They can open MetaTrader 5 and trade there,
or read `.env`. The control for that is a Windows account per trader
and a locked Windows session; this is the deliberate gate in front of
the trading screen, which is a different and smaller job. Saying so
here so nobody mistakes the one for the other.

THE PIN IS NEVER STORED. A salted PBKDF2-SHA256 hash goes into `.env`,
beside the account passwords and under the same rule: credentials live
in `.env` and nowhere else - not in `config.json`, not in a log line,
not in an error message.
"""

import hashlib
import hmac
import os
import secrets
import time

#: Where the hash lives in `.env`. A HASH, never the PIN.
PIN_ENV_KEY = 'NEXUS_PIN_HASH'

#: Cost. High enough that a stolen `.env` is not a four-digit
#: brute-force in an afternoon, low enough that unlocking is instant.
ITERATIONS = 200_000

#: Wrong PINs before the box stops answering, and for how long. A
#: four-digit PIN is 10,000 guesses; without this, a script does that
#: in seconds.
MAX_ATTEMPTS = 5
LOCKOUT_SEC = 60.0

#: Shortest PIN worth having. Four digits is what a trading desk will
#: actually use; refusing anything shorter stops '1' becoming the
#: office standard.
MIN_PIN_LENGTH = 4


def hash_pin(pin, salt=None, iterations=ITERATIONS):
    """`pbkdf2_sha256$iterations$salt$hash`, ready for `.env`.

    The salt is per-PIN and random, so two desks that pick the same
    four digits do not share a hash - and a hash lifted from one `.env`
    says nothing about any other machine.
    """
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', str(pin).encode('utf-8'),
                                 salt.encode('utf-8'), iterations)
    return f'pbkdf2_sha256${iterations}${salt}${digest.hex()}'


def verify_pin(pin, stored):
    """Does this PIN match that stored hash?

    `compare_digest`, not `==`: a comparison that returns early leaks
    how much of the answer was right.
    """
    if not stored or not pin:
        return False
    try:
        algorithm, iterations, salt, _ = str(stored).split('$', 3)
    except ValueError:
        return False
    if algorithm != 'pbkdf2_sha256':
        return False
    try:
        iterations = int(iterations)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(hash_pin(pin, salt, iterations), str(stored))


def stored_hash(env=None):
    """The hash on this machine, or '' when no PIN has been set."""
    env = os.environ if env is None else env
    return str(env.get(PIN_ENV_KEY) or '').strip()


class ScreenLock:
    """Locked or not, and who is allowed to change that.

    LOCKED IS THE STARTING STATE whenever a PIN exists. The engine
    restarts, the browser reloads, the whole PC reboots - every one of
    those comes back locked, because the question this answers is 'is
    the right person here NOW', and a process that starts unlocked has
    answered it for a session nobody was present for.
    """

    def __init__(self, clock=time.monotonic, env=None):
        self.clock = clock
        self._env = env
        self._locked = True
        self._failures = 0
        self._locked_out_until = 0.0
        self._last_seen = clock()

    # -- state -----------------------------------------------------------

    def pin_is_set(self, env=None):
        return bool(stored_hash(env if env is not None else self._env))

    @property
    def locked(self):
        """A machine with NO PIN is not locked.

        Otherwise the first desk to update would find its screen shut
        and no way in - a lock nobody can open is not a safety feature,
        it is an outage. The PIN is set from the Settings pane, and
        until it is, this is exactly the app it was yesterday.
        """
        if not self.pin_is_set():
            return False
        return self._locked

    def lock(self):
        self._locked = True

    def touch(self):
        """The trader did something. Resets the idle clock."""
        self._last_seen = self.clock()

    def idle_seconds(self):
        return self.clock() - self._last_seen

    def lock_if_idle(self, after_seconds):
        """Lock a screen nobody has touched. Returns True if it locked.

        `after_seconds` of 0 or less turns it off - a desk that wants
        only the manual lock says so, and nothing here second-guesses
        it.
        """
        if not after_seconds or after_seconds <= 0:
            return False
        if self.locked or not self.pin_is_set():
            return False
        if self.idle_seconds() >= after_seconds:
            self._locked = True
            return True
        return False

    # -- opening it ------------------------------------------------------

    def lockout_remaining(self):
        return max(0.0, self._locked_out_until - self.clock())

    def unlock(self, pin, env=None):
        """(ok, reason). The reason is shown to the trader as it is."""
        remaining = self.lockout_remaining()
        if remaining > 0:
            return False, (f'Too many wrong PINs. Try again in '
                           f'{int(remaining) + 1} seconds.')
        stored = stored_hash(env if env is not None else self._env)
        if not stored:
            # No PIN on this machine: there is nothing to unlock, and
            # saying so is better than refusing a correct empty guess.
            self._locked = False
            return True, ''
        if verify_pin(pin, stored):
            self._locked = False
            self._failures = 0
            self.touch()
            return True, ''
        self._failures += 1
        if self._failures >= MAX_ATTEMPTS:
            self._locked_out_until = self.clock() + LOCKOUT_SEC
            self._failures = 0
            return False, (f'That PIN is wrong, and that was the last try. '
                           f'This screen stops answering for '
                           f'{int(LOCKOUT_SEC)} seconds.')
        left = MAX_ATTEMPTS - self._failures
        return False, (f'That PIN is wrong. {left} '
                       f'{"try" if left == 1 else "tries"} left.')


def check_new_pin(pin, again):
    """The reason a proposed PIN is refused, or '' when it is fine.

    Typed TWICE, because a PIN nobody can remember locks a trader out
    of their own screen - and the second box is the only chance to
    catch a typo before that happens.
    """
    pin = str(pin or '')
    if len(pin) < MIN_PIN_LENGTH:
        return (f'A PIN needs at least {MIN_PIN_LENGTH} characters. '
                f'Anything shorter is a PIN the whole office will guess.')
    if pin != str(again or ''):
        return 'The two PINs are not the same. Type it again.'
    return ''
