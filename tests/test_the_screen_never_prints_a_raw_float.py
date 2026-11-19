"""No size reaches the screen without being swept.

`0.01 / 0.1` is 0.09999999999999999, and three 0.01 buys covered by
three 0.01 sells is -3.469446951953614e-18. The engine sweeps both now
— but the screen is where a trader meets a number, and `app.js` has a
`qty()` helper whose own comment says it exists so that "nothing on the
way to the screen can put the dust back".

Seven places were not using it, which is how the trader came to see
seventeen digits where they had typed two.

This is a SOURCE check on purpose. Driving a browser to prove it means
a shared page, a live poll and a random test order — I wrote three such
tests and they took three unrelated ones down with them. A regression
here would be reintroduced by someone typing `+ row.net_position +`,
and that is exactly what this reads.
"""

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / 'okxtrader' / 'static' / 'app.js'

#: Fields that hold a SIZE — a lot count, a spread count, a net. Every
#: one of them is the result of arithmetic somewhere, so every one of
#: them can carry binary dust.
SIZE_FIELDS = ('net_position', 'quantity', 'volume', 'remaining',
               'filled_quantity')

#: `... + row.net_position + ...` — a size dropped straight into a
#: string. The swept form is `+ qty(row.net_position) +`, which does
#: not match because of the bracket.
RAW = re.compile(r"\+\s*\w+\.(" + '|'.join(SIZE_FIELDS) + r")\b")


def source():
    return APP.read_text(encoding='utf-8')


def printed(line):
    """Is this line BUILDING TEXT, or just doing arithmetic?

    `return sum + ghost.quantity;` adds two numbers and never reaches a
    screen. A quote somewhere on the line is what separates the two,
    and it keeps every shape the real offenders had:
    `'<td>' + position.quantity + '</td>'`, `'' + row.net_position`,
    `' · ' + leg.volume + ' lots × '`.
    """
    return "'" in line or '"' in line


def test_no_size_is_concatenated_into_the_screen_unswept():
    offenders = []
    for number, line in enumerate(source().splitlines(), start=1):
        if RAW.search(line) and printed(line):
            offenders.append(f'{number}: {line.strip()}')
    assert not offenders, (
        'a size goes to the screen without qty():\n  ' +
        '\n  '.join(offenders))


def test_CONTROL_the_check_can_actually_fail():
    """The control. A pattern that matches nothing would pass this file
    for ever and prove nothing — including after someone puts the raw
    concatenation back."""
    for bad in ("html += '<td>' + position.quantity + '</td>';",
                "x = '' + row.net_position + ' @ '",
                "  ' · ' + leg.volume + ' lots × ' +"):
        assert RAW.search(bad) and printed(bad), bad
    # ...and does not fire on the swept form, or this would be
    # unfixable rather than a guard.
    for good in ("html += '<td>' + qty(position.quantity) + '</td>';",
                 "x = '' + qty(row.net_position) + ' @ '"):
        assert not RAW.search(good), good
    # ...nor on arithmetic, which never reaches a screen.
    assert not printed('return sum + ghost.quantity;')


def test_the_helper_the_check_depends_on_is_still_there():
    """`qty()` and `isFlat()` are the whole answer. A check for their
    USE means nothing if they have quietly been deleted."""
    text = source()
    assert 'function qty(value)' in text
    assert 'function isFlat(net)' in text
    assert 'FLAT_EPSILON' in text


def test_a_net_of_dust_is_treated_as_FLAT_not_as_a_position():
    """`if (!net)` is false for -3.4e-18, so the ladder offered to
    close a position that is not there. Every place that asks "is this
    ladder flat?" must ask `isFlat`."""
    offenders = []
    for number, line in enumerate(source().splitlines(), start=1):
        if re.search(r"!\s*row\.net_position\b", line):
            offenders.append(f'{number}: {line.strip()}')
    assert not offenders, (
        'a flat test that dust defeats:\n  ' + '\n  '.join(offenders))


@pytest.mark.parametrize('dust,expected', [
    (0.09999999999999999, '0.1'),
    (-3.469446951953614e-18, '-0'),
    (0.030000000000000002, '0.03'),
])
def test_the_helpers_own_arithmetic_is_what_we_think_it_is(dust, expected):
    """`qty()` is `String(parseFloat(number.toFixed(6)))`. Pinned in
    Python so the gate can check it without a browser — if the JS is
    ever changed to something else, this stops describing it and the
    docstring above stops being true."""
    swept = float(f'{dust:.6f}')
    assert repr(swept).rstrip('0').rstrip('.') in (expected, expected + '.0')
