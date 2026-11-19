"""The spread's definition, and the rules that came out of live losses."""

import pytest

from okxtrader.models import SpreadSide
from okxtrader.spread import (closing_prices, compute_spread,
                              executable_spread)


def md(pair, bid_a=100.0, ask_a=100.2, bid_b=110.0, ask_b=110.4, beta=1.0):
    return compute_spread(pair,
                          {'bid': bid_a, 'ask': ask_a, 'last': 999.0, 'time': 1},
                          {'bid': bid_b, 'ask': ask_b, 'last': 999.0, 'time': 1},
                          beta)


def test_mid_comes_from_the_book_never_from_last(pair):
    """`last` is a TRADE print. A print above the ask puts the "mid"
    above the long spread — above the best price anyone can buy at."""
    snapshot = md(pair)
    assert snapshot['leg_a_mid'] == pytest.approx(100.1)
    assert snapshot['leg_b_mid'] == pytest.approx(110.2)
    assert snapshot['spread'] == pytest.approx(10.1)


def test_short_never_above_mid_never_above_long(pair):
    snapshot = md(pair)
    assert snapshot['short_spread'] <= snapshot['spread'] <= \
        snapshot['long_spread']
    assert snapshot['short_spread'] == pytest.approx(110.0 - 100.2)
    assert snapshot['long_spread'] == pytest.approx(110.4 - 100.0)


def test_spread_cost_is_exactly_one_round_turn_of_both_books(pair):
    """`long - short` IS both legs' bid-ask, in spread units. It is one
    quantity seen two ways, never two costs to be added together."""
    snapshot = md(pair, beta=2.0)
    both_widths = (110.4 - 110.0) + 2.0 * (100.2 - 100.0)
    assert snapshot['spread_cost'] == pytest.approx(both_widths)


def test_a_level_reads_the_side_the_market_offers(pair):
    """Arming at a level and firing when the MID touched it fires on a
    price the market never offered, and fills half a round turn worse."""
    snapshot = md(pair)
    assert executable_spread(snapshot, SpreadSide.SELL) == \
        snapshot['short_spread']
    assert executable_spread(snapshot, SpreadSide.BUY) == \
        snapshot['long_spread']
    # The control: reading the mid for both would make them equal, which
    # is the bug this replaced.
    assert snapshot['short_spread'] != snapshot['long_spread']


def test_a_position_reads_a_different_touch_at_each_end(pair):
    """A short ENTERS on short_spread and EXITS on long_spread. Reading
    the favourable side at both ends makes every trade look like it
    cleared its costs — worse than using the mid, not better."""
    snapshot = md(pair)
    entry = executable_spread(snapshot, SpreadSide.SELL)
    exit_ = executable_spread(snapshot, SpreadSide.SELL, closing=True)
    assert entry == snapshot['short_spread']
    assert exit_ == snapshot['long_spread']
    assert exit_ > entry            # closing a short costs the round turn


@pytest.mark.parametrize('side', [SpreadSide.BUY, SpreadSide.SELL])
def test_leg_marks_agree_with_the_closing_spread(pair, side):
    """`B - beta x A` of the two closing touches IS the closing
    executable spread. A mark that disagreed with the level it is
    compared against is the fault this pins."""
    beta = 1.5
    snapshot = md(pair, beta=beta)
    price_a, price_b = closing_prices(snapshot, side)
    assert price_b - beta * price_a == pytest.approx(
        executable_spread(snapshot, side, closing=True))
