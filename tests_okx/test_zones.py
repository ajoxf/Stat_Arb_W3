"""The hypothetical levels a signal would have been given.

The sigma ZONE track these tests were written for is gone - it wrote
16,562 rows on a live desk in one day and nobody read them. What is
left is the arithmetic the signals need, and it still has to be right:
a long recovers its costs UP and a short DOWN, and a level that could
not be priced is None rather than zero.
"""

from okxtrader import zones


def test_the_entry_side_is_the_one_the_algo_would_take():
    """It sells a spread stretched high and buys one stretched low."""
    assert zones.entry_side(2.5) == 'SELL'
    assert zones.entry_side(-2.5) == 'BUY'
    assert zones.entry_side(0.0) is None
    assert zones.entry_side(None) is None


def test_a_buy_puts_the_target_above_break_even_and_the_stop_below():
    body = zones.levels('BUY', entry_spread=10.0, fee_points=0.20,
                        target_points=1.0, stop_points=0.5)
    assert body['entry'] == 10.0
    assert body['break_even'] == 10.20          # costs are recovered UP
    assert body['tp'] == 11.20
    assert body['sl'] == 9.70


def test_a_sell_mirrors_it():
    body = zones.levels('SELL', entry_spread=10.0, fee_points=0.20,
                        target_points=1.0, stop_points=0.5)
    assert body['entry'] == 10.0
    assert body['break_even'] == 9.80           # costs are recovered DOWN
    assert body['tp'] == 8.80
    assert body['sl'] == 10.30


def test_prices_are_rounded_to_the_spreads_own_step():
    body = zones.levels('BUY', entry_spread=10.004, fee_points=0.203,
                        target_points=1.0, stop_points=0.5, step=0.01)
    assert body['entry'] == 10.0
    assert body['break_even'] == 10.21
    assert body['tp'] == 11.21
    assert body['sl'] == 9.71


def test_an_unpriceable_level_is_none_and_never_zero():
    """A stop of 0.00 reads as 'get out at break-even', which is a
    different instruction from 'this could not be priced'."""
    body = zones.levels('BUY', 10.0, fee_points=0.2,
                        target_points=None, stop_points=None)
    assert body['break_even'] == 10.2
    assert body['tp'] is None
    assert body['sl'] is None
    no_costs = zones.levels('BUY', 10.0, fee_points=None,
                            target_points=1.0, stop_points=0.5)
    assert no_costs['entry'] == 10.0
    assert no_costs['break_even'] is None
    assert no_costs['tp'] is None and no_costs['sl'] is None


def test_no_side_and_no_entry_price_nothing():
    assert zones.levels(None, 10.0, 0.2, 1.0, 0.5)['entry'] is None
    assert zones.levels('BUY', None, 0.2, 1.0, 0.5)['entry'] is None
