"""Size the Algo by TOTAL margin: one figure for both legs, split between
them by beta, turned into the Algo's Qty on every poll from live prices
and OKX's leverage - rounded down, so a trade never ties up more."""

import pytest

from okxtrader import algo
from okxtrader.coordinator import Coordinator


def test_the_qty_a_total_margin_buys_rounded_down():
    # $181.28 for 30 Qty at $6.0426 each: 180 buys 29.78.
    assert algo.qty_for_margin(180.0, 6.0426) == pytest.approx(29.78)
    assert algo.qty_for_margin(6.0426 * 30, 6.0426) == pytest.approx(30.0)
    # Never more than it was given.
    assert algo.qty_for_margin(180.0, 6.0426) * 6.0426 <= 180.0
    # Too little for one step, unknown, or nothing: no size.
    assert algo.qty_for_margin(0.05, 6.0) is None
    assert algo.qty_for_margin(180.0, None) is None
    assert algo.qty_for_margin(0, 6.0) is None


def test_size_by_is_qty_unless_asked_and_checked():
    assert algo.clean_params({})['algo_size_by'] == 'QTY'
    assert algo.clean_params({'algo_size_by': 'margin'})['algo_size_by'] \
        == 'MARGIN'
    assert algo.clean_params({'algo_size_by': 'lots'})['algo_size_by'] \
        == 'QTY'
    assert algo.check_params({'algo_size_by': 'lots'})
    assert algo.check_params({'algo_size_by': 'MARGIN',
                              'algo_margin': 180}) == []
    assert algo.check_params({'algo_margin': -1})


def _coordinator(config, legs, per_qty):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.margin_detail = lambda pair, side=None: {
        'money': per_qty, 'source': 'OKX'}
    return coordinator


def test_the_algo_qty_follows_the_total_margin(config, pair, legs):
    pair.algo_params = {'algo_size_by': 'MARGIN', 'algo_margin': 180.0,
                        'algo_qty': 1.0}
    coordinator = _coordinator(config, legs, 6.0426)
    coordinator._algo_size_from_margin(pair)
    assert algo.clean_params(pair.algo_params)['algo_qty'] == \
        pytest.approx(29.78)
    sizing = coordinator._algo_sizing[pair.key]
    assert sizing['qty'] == pytest.approx(29.78) and sizing['note'] is None
    # Leverage halved: the margin per Qty doubles, the Qty halves.
    coordinator.margin_detail = lambda pair, side=None: {'money': 12.0852}
    coordinator._algo_size_from_margin(pair)
    assert pair.algo_params['algo_qty'] == pytest.approx(14.89)


def test_sized_by_qty_the_qty_is_the_traders(config, pair, legs):
    pair.algo_params = {'algo_qty': 3.0, 'algo_margin': 180.0}
    coordinator = _coordinator(config, legs, 6.0)
    coordinator._algo_size_from_margin(pair)
    assert pair.algo_params['algo_qty'] == 3.0
    assert pair.key not in coordinator._algo_sizing


def test_unpriced_or_too_small_keeps_the_last_qty_and_says_why(
        config, pair, legs):
    pair.algo_params = {'algo_size_by': 'MARGIN', 'algo_margin': 180.0,
                        'algo_qty': 5.0}
    coordinator = _coordinator(config, legs, None)
    coordinator._algo_size_from_margin(pair)
    assert pair.algo_params['algo_qty'] == 5.0
    assert 'not priced' in coordinator._algo_sizing[pair.key]['note']
    pair.algo_params['algo_margin'] = 0.01
    coordinator = _coordinator(config, legs, 6.0)
    coordinator._algo_size_from_margin(pair)
    assert pair.algo_params['algo_qty'] == 5.0
    assert 'less than' in coordinator._algo_sizing[pair.key]['note']


def test_a_poll_sizes_the_algo_from_its_margin(config, pair, legs):
    pair.algo_params = {'algo_size_by': 'MARGIN', 'algo_margin': 100.0}
    coordinator = _coordinator(config, legs, 4.0)
    coordinator.start()
    coordinator.poll_once()
    assert pair.algo_params['algo_qty'] == pytest.approx(25.0)
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['algo_sizing']['qty'] == pytest.approx(25.0)
    assert row['algo_params']['algo_qty'] == pytest.approx(25.0)


def test_the_add_qty_follows_its_own_total_margin(config, pair, legs):
    """Add size by Total margin: its own Qty, worked out like the
    trade's - and the trade's Qty is left alone."""
    pair.algo_params = {'algo_qty': 3.0, 'add_size_by': 'MARGIN',
                        'add_margin': 60.0}
    coordinator = _coordinator(config, legs, 6.0426)
    coordinator._algo_size_from_margin(pair)
    params = algo.clean_params(pair.algo_params)
    assert params['add_qty'] == pytest.approx(9.92)
    assert params['algo_qty'] == 3.0
    assert algo.add_quantity(params) == pytest.approx(9.92)
    # Too small to buy the smallest size: no add size, so no add.
    pair.algo_params['add_margin'] = 0.01
    coordinator._algo_size_from_margin(pair)
    assert algo.add_quantity(algo.clean_params(pair.algo_params)) is None


def test_add_size_by_is_checked_and_same_by_default():
    assert algo.DEFAULT_PARAMS['add_size_by'] == 'SAME'
    assert algo.add_quantity(algo.clean_params({'algo_qty': 4})) == 4
    assert algo.check_params({'add_size_by': 'LOTS'})
    assert algo.clean_params({'add_size_by': 'margin'})['add_size_by'] == \
        'MARGIN'
