"""Seventeen digits where the trader typed two.

`0.01 / 0.1` is `0.09999999999999999` in binary floating point, and a
LIMIT fill computes a position's size exactly that way — leg B's lots
divided by what one Qty is worth. Three 0.01 buys covered by three 0.01
sells is `-3.469446951953614e-18`, not zero.

Both reached the screen. The Net column printed the seventeen digits,
and the ladder footer printed the tiny one where it should have said
FLAT — because a number that small is still not zero to an `if`. The
trader who reported "sometimes 8.235121223000000... , sometimes
0.000000001122411, when 3-4 positions are open" was looking at this.

Nothing was wrong with any trade. The arithmetic was right and the
sweeping was missing. `sizing.tidy` is the convention this codebase
already uses for it — nine decimal places, which changes no quantity
anybody could type.
"""

import pytest

from okxtrader import sizing
from okxtrader.book import Book
from okxtrader.coordinator import Coordinator
from okxtrader.models import LegFill, OrderSide, OrderType, SpreadPosition
from okxtrader.models import SpreadSide


def a_position(side, quantity, entry_spread=8.2351):
    leg_a = LegFill('acct_a', 'XAUUSD_', OrderSide.BUY, 0.10, 4292.0,
                    contract_size=100.0)
    leg_b = LegFill('acct_b', 'GC1226', OrderSide.SELL, 0.10, 4351.0,
                    contract_size=100.0)
    return SpreadPosition('XAUUSD_|GC1226', side, quantity, leg_a, leg_b,
                          entry_spread, OrderType.LIMIT, 10.0)


def test_a_size_that_arrives_as_a_division_is_swept(engine_free=None):
    """THE SOURCE. A limit fill sizes a position as lots / clip lots."""
    dusty = 0.01 / 0.1
    assert repr(dusty) == '0.09999999999999999', 'the fixture is not dusty'

    position = a_position(SpreadSide.BUY, dusty)

    assert position.quantity == 0.1
    assert repr(position.quantity) == '0.1'


def test_CONTROL_a_clean_size_is_untouched(engine_free=None):
    """The control. Tidying must not move a number anybody typed."""
    for typed in (1.0, 0.01, 12.5, 100.0, 0.001):
        assert a_position(SpreadSide.BUY, typed).quantity == typed


def test_a_net_of_three_in_and_three_out_reads_FLAT(engine_free=None):
    """The other half, and the one that printed on the ladder: a
    running sum of quantities that cancel does not land on zero."""
    book = Book()
    for _ in range(3):
        book.add_position(a_position(SpreadSide.BUY, 0.01))
    for _ in range(3):
        book.add_position(a_position(SpreadSide.SELL, 0.01))

    net, _avg = book.net_position('XAUUSD_|GC1226')

    assert net == 0.0
    assert not net, 'a flat ladder did not read as flat'
    # ...and never a negative nothing, which renders as "-0".
    assert repr(net) == '0.0'


def test_CONTROL_a_net_that_is_really_there_survives(engine_free=None):
    """The control: sweeping must not sweep away a real position."""
    book = Book()
    for _ in range(3):
        book.add_position(a_position(SpreadSide.BUY, 0.01))
    book.add_position(a_position(SpreadSide.SELL, 0.01))

    net, _avg = book.net_position('XAUUSD_|GC1226')

    assert net == pytest.approx(0.02)
    assert repr(net) == '0.02'


def test_the_average_entry_is_swept_too(engine_free=None):
    """It is a weighted division, so it collects dust the same way —
    and it is the number beside the net on the ladder footer."""
    book = Book()
    book.add_position(a_position(SpreadSide.BUY, 0.01, entry_spread=8.235))
    book.add_position(a_position(SpreadSide.BUY, 0.01, entry_spread=8.236))
    book.add_position(a_position(SpreadSide.BUY, 0.01, entry_spread=8.237))

    _net, avg = book.net_position('XAUUSD_|GC1226')

    assert avg == pytest.approx(8.236)
    assert len(repr(avg).split('.')[1]) <= 9, repr(avg)


def test_an_average_of_no_trades_is_still_None(engine_free=None):
    """Unmeasured is not zero, and the sweeping must not change that."""
    book = Book()
    book.add_position(a_position(SpreadSide.BUY, 0.01, entry_spread=None))

    net, avg = book.net_position('XAUUSD_|GC1226')

    assert avg is None
    assert net == pytest.approx(0.01)


def test_the_whole_way_through_a_real_engine(config, pair, legs):
    """End to end, the way the trader met it: clicks in, net out, and
    nothing on the snapshot carrying more decimals than a person
    typed."""
    pair.clip_lots_b = 0.1
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    pair.order_type = OrderType.MARKET
    md = coordinator.market[pair.key]
    for _ in range(4):
        coordinator.click(pair.key, SpreadSide.BUY, md['long_spread'],
                          quantity=0.1)

    row = coordinator.snapshot()['pairs'][pair.key]

    assert repr(row['net_position']) == repr(sizing.tidy(row['net_position']))
    for position in row['positions']:
        assert repr(position['quantity']) == \
            repr(sizing.tidy(position['quantity'])), position['quantity']


def test_tidy_leaves_nine_decimals_alone(engine_free=None):
    """The boundary, said out loud: the sweep is at nine places, so
    nothing a lot size or a Qty box can hold is moved by it."""
    assert sizing.tidy(0.000000001) == 0.000000001
    assert sizing.tidy(0.09999999999999999) == 0.1
