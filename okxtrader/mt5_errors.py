"""What MT5's own error codes mean, and what to actually do about them.

The Python package reports failures as `(code, description)` from
`mt5.last_error()`. The description is MT5's and is authoritative; what
it never supplies is the fix, and the codes are opaque enough that an
operator cannot get from "(-10001, 'IPC send failed')" to "your
terminal is running elevated and Python is not" without help.

Two callers share this table: `check_mt5.py` (the standalone checker)
and `okxtrader/diagnostics.py` (the Accounts page checklist). They used
to disagree — the checklist had ONE generic fix list for every terminal
failure, so a live -10001 with the terminal plainly open and logged in
told the operator to "Open the MT5 terminal" and "Log in". Advice that
describes something already done reads as the tool being broken.

Codes follow the RES_E_* constants the package exposes. The IPC family
(-10000..-10005) all mean the same class of thing — Python and the
terminal cannot talk — and on a machine where the terminal is visibly
running they almost always have the same handful of causes, so they
share a fix list ordered by how often each one is really it.
"""

#: Terminal is running but Python cannot reach it. Ordered by real
#: frequency, not by severity.
_IPC_FIXES = [
    'Run BOTH the same way: a terminal started as Administrator will '
    'not accept IPC from a normally-started Python (and vice versa). '
    'This is the usual cause when the terminal is plainly open.',
    'Check terminal_path on the Accounts page. If it points at a '
    'DIFFERENT installation from the one that is open, Python tries to '
    'attach to that other copy. Clearing the path makes it attach to '
    'the running terminal, which is the more reliable route.',
    'Close any OTHER MT5 terminals — the package attaches to one '
    'terminal, and with several open it may not be the one you mean.',
    'A terminal started in portable mode (/portable) will not accept '
    'IPC from a normally-started process.',
    'Python must be 64-bit to talk to a 64-bit terminal.',
    'If the terminal has only just opened, give it a few seconds to '
    'finish connecting and retry.',
]

INIT_ERRORS = {
    -10000: ('IPC general failure — Python could not talk to the terminal',
             _IPC_FIXES),
    -10001: ('IPC send failed — the terminal is not receiving from Python',
             _IPC_FIXES),
    -10002: ('IPC receive failed — the terminal is not answering Python',
             _IPC_FIXES),
    # -10003 is the one code in this family where "no terminal is
    # running" is a real possibility, so that advice leads here and
    # ONLY here. On -10001/-10002 the operator is looking at an open
    # terminal, and telling them to open one is what made the checklist
    # read as broken.
    -10003: ('IPC initialize failed — Python could not start or attach to '
             'a terminal', [
                 'Open the MT5 terminal yourself and log in, THEN retry — '
                 'attaching to a running terminal is far more reliable '
                 'than letting Python launch one',
             ] + _IPC_FIXES),
    -10004: ('No IPC connection — the terminal is not accepting API calls',
             _IPC_FIXES),
    -10005: ('IPC timeout — the terminal did not answer in time', [
        'The terminal may still be starting or reconnecting; wait and retry',
        'Close and reopen the terminal, then retry',
    ] + _IPC_FIXES),
    -8: ('Algorithmic trading is disabled in the terminal', [
        'Tools > Options > Expert Advisors > Allow algorithmic trading',
        'Restart the terminal after changing it',
    ]),
    -6: ('Authorization failed — login, password or server is wrong', [
        'Check the login number and server string EXACTLY as the broker '
        'gave them (server names are case- and space-sensitive)',
        'Check the password in .env — the Settings page writes it there',
        'An investor password connects but cannot trade; use the master '
        'password',
        # Live 2026-08-11: the terminal journal read "'100006':
        # authorization on MentoMarkets-Server failed (Invalid
        # account)" and "account ... has been deleted". The credentials
        # were typed correctly — the account was not on that server at
        # all. Two accounts at one broker do not have to share a
        # server: a demo usually lives on the broker's DEMO server, and
        # the live one answers "no demo/preliminary groups on server
        # side" when asked for one.
        'Confirm the account exists on THAT server — two accounts at '
        'one broker can be on different servers (demo vs live)',
        "Read the terminal's own Journal tab (View > Toolbox > "
        'Journal). "Invalid account" or "account has been deleted" '
        'there means the login does not exist on that server, which '
        'nothing on this side can fix — ask the broker',
    ]),
    -5: ('Unsupported terminal version', [
        'Update the MT5 terminal, or pin an older MetaTrader5 package',
    ]),
    -4: ('Not found — the terminal executable could not be located', [
        'Set terminal_path to the real terminal64.exe for this account, '
        'or clear it to attach to whatever terminal is already running',
    ]),
    -3: ('Out of memory', ['Close other applications and retry']),
    -2: ('Invalid parameters passed to initialize', [
        'Check login is a NUMBER and server is the exact string the '
        'broker published',
    ]),
    -1: ('Generic failure', _IPC_FIXES),
}


def parse_code(error):
    """Pull the numeric code out of whatever last_error() produced.

    Callers hand us a tuple `(code, description)`, the str() of one, or
    a bare message. Returns None when there is no code to find —
    guessing one would attach confident advice to an unknown fault.
    """
    if isinstance(error, (tuple, list)) and error:
        try:
            return int(error[0])
        except (TypeError, ValueError):
            return None
    if isinstance(error, int):
        return error
    text = str(error or '')
    # "(-10001, 'IPC send failed')" and "-10001" both appear in practice
    start = text.find('-')
    if start < 0:
        return None
    digits = ''
    for char in text[start + 1:]:
        if not char.isdigit():
            break
        digits += char
    return -int(digits) if digits else None


def explain(error):
    """(summary, [fixes]) for an MT5 error, or (None, []) if unknown.

    MT5's own description is kept in the summary — it is the part that
    is authoritative, and dropping it in favour of our wording would
    hide the exact string the operator can search for.
    """
    code = parse_code(error)
    if code is None or code not in INIT_ERRORS:
        return None, []
    summary, fixes = INIT_ERRORS[code]
    return summary, list(fixes)
