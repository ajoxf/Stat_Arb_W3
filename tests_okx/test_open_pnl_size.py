"""Size is in `spread_units` once, and must not be charged twice.

Reported from the desk with the screen beside MT5:

    Net  10  entry 45.6300  mark 46.1800  ->  -$55.00   (should be -$5.50)
    Net 100  entry 45.5800  mark 46.1800  -> -$6,000.00 (should be -$60.00)
    our total -$6,055.00      MT5's own -$56.70

`SpreadPosition.mark` returned `move * spread_units * quantity`. But
every place a position is built passes `spread_units(leg_b.volume,
contract_b)`, and `leg_b.volume` is the volume of the WHOLE position —
1.0 lots for 100 spreads, not the 0.01 one spread costs. The size was
already in there.

WHY IT SURVIVED EVERYTHING: at Qty 1 the double count is x1.
`spread_units` is 0.01 x 100 = 1.0, `quantity` is 1, and the wrong
formula gives the right answer. Every check made on this system, mine
included, was made at Qty 1.

So every test here uses a size GREATER THAN ONE.
"""

import pytest

from okxtrader.models import SpreadPosition, SpreadSide


def position(side, quantity, entry, leg_b_lots, contract_b=100.0):
    p = SpreadPosition.__new__(SpreadPosition)
    p.side = side
    p.quantity = quantity
    p.entry_spread = entry
    # Exactly what the three construction sites pass: the WHOLE
    # position's leg-B volume, times its contract size.
    p.spread_units = leg_b_lots * contract_b
    return p


def test_the_desks_own_numbers_ten_spreads():
    p = position(SpreadSide.SELL, 10, 45.63, 0.1)
    assert p.mark(46.18) == pytest.approx(-5.50)


def test_the_desks_own_numbers_one_hundred_spreads():
    p = position(SpreadSide.SELL, 100, 45.58, 1.0)
    assert p.mark(46.18) == pytest.approx(-60.00)


def test_pnl_is_LINEAR_in_size():
    """The property the double count broke: ten times the size is ten
    times the money, not a hundred times."""
    one = position(SpreadSide.BUY, 1, 45.00, 0.01).mark(46.00)
    ten = position(SpreadSide.BUY, 10, 45.00, 0.10).mark(46.00)
    hundred = position(SpreadSide.BUY, 100, 45.00, 1.00).mark(46.00)
    assert one == pytest.approx(1.00)
    assert ten == pytest.approx(10.0 * one)
    assert hundred == pytest.approx(100.0 * one)


def test_qty_one_is_unchanged():
    """The CONTROL, and the reason this hid for so long. At Qty 1 the
    old formula and the new one agree, so this must still pass — if it
    did not, the fix would have moved something it should not."""
    assert position(SpreadSide.BUY, 1, 45.00, 0.01).mark(45.55) == \
        pytest.approx(0.55)
    assert position(SpreadSide.SELL, 1, 45.00, 0.01).mark(45.55) == \
        pytest.approx(-0.55)


def test_a_sell_makes_money_when_the_spread_falls():
    """Direction is untouched by the fix."""
    assert position(SpreadSide.SELL, 10, 46.00, 0.1).mark(45.00) == \
        pytest.approx(10.00)
    assert position(SpreadSide.BUY, 10, 46.00, 0.1).mark(45.00) == \
        pytest.approx(-10.00)


def test_the_money_is_one_multiplication_only():
    """Pins the shape, so quantity cannot creep back in."""
    import inspect
    body = inspect.getsource(SpreadPosition.mark)
    code = '\n'.join(line.split('#')[0] for line in body.splitlines())
    assert 'self.quantity' not in code, (
        'size is being charged twice again')
    assert 'move * self.spread_units' in code
