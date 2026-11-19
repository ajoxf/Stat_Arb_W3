"""TP and SL for a position that is not one spread, and the margin they
are a % of, frozen when the position is first priced.

The fault: a position's `spread_units` is its WHOLE size (0.05 lots x
1,000 = 50 for half a spread of 0.1 lots), and the TP/SL maths divided
by the quantity again. At 0.5 spreads the levels sat twice as far away
as the % asked for; at 3 spreads, a third as far. (The fake future's
minimum is 0.1 lots, so these tests use 2 and 3 spreads.)
"""

import pytest

from okxtrader import takeprofit
from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import SpreadPosition, SpreadSide

SETTINGS = {'TP_TARGET_PCT_OF_MARGIN': 2.0}
MARGIN = 500.0                       # per ONE spread


def engine(config, legs, store=None):
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0       # 0.1 lots: 300+200
    coordinator = Coordinator(config, legs, sleep=lambda s: None, store=store)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def opened(coordinator, pair, quantity, side=SpreadSide.SELL):
    md = coordinator.market[pair.key]
    position = coordinator.executor.market_entry(pair, side, md,
                                                 quantity).position
    coordinator.book.add_position(position)
    return position


@pytest.mark.parametrize('quantity', [1.0, 2.0, 3.0])
def test_the_target_is_the_percent_of_margin_THIS_position_ties_up(
        config, pair, legs, quantity):
    """In money, the TP is TP% of the margin the position ties up —
    what a trader works out from MT5 by hand. 1.0 is the control: it
    was right before, and must stay right."""
    coordinator = engine(config, legs)
    position = opened(coordinator, pair, quantity)
    body = takeprofit.for_position(position, coordinator.market[pair.key],
                                   pair, SETTINGS, MARGIN)
    money_at_tp = body['target_points'] * position.spread_units
    assert money_at_tp == pytest.approx(0.02 * MARGIN * quantity)


@pytest.mark.parametrize('quantity', [1.0, 2.0, 3.0])
def test_the_algo_stop_is_the_percent_of_margin_THIS_position_ties_up(
        config, pair, legs, quantity):
    coordinator = engine(config, legs)
    pair.algo_params = {'stop_loss_on': True, 'stop_loss_pct': 5.0}
    position = opened(coordinator, pair, quantity)
    [row] = coordinator._algo_positions(pair, coordinator.market[pair.key])
    money_at_sl = (row['sl'] - row['break_even']) * position.spread_units
    assert money_at_sl == pytest.approx(0.05 * MARGIN * quantity)


def test_the_margin_is_frozen_when_the_position_is_first_priced(
        config, pair, legs):
    coordinator = engine(config, legs)
    position = opened(coordinator, pair, 2.0)
    md = coordinator.market[pair.key]
    [before] = coordinator._algo_positions(pair, md)
    assert position.entry_margin == pytest.approx(MARGIN)

    # The terminals' margin moves with the price...
    legs['acct_a'].broker.margin_per_lot = 6000.0
    coordinator._margin_cache.clear()
    [after] = coordinator._algo_positions(pair, md)
    # ...the open position's TP and SL do not.
    assert after['tp'] == pytest.approx(before['tp'])
    assert after['sl'] == pytest.approx(before['sl'])

    # CONTROL: a position opened now is priced on the margin now.
    later = opened(coordinator, pair, 2.0)
    coordinator._algo_positions(pair, md)
    assert later.entry_margin == pytest.approx(800.0)        # 600 + 200


def test_the_frozen_margin_survives_a_restart(config, pair, legs, tmp_path):
    store = Store(str(tmp_path / 'trader.db'))
    coordinator = engine(config, legs, store=store)
    position = opened(coordinator, pair, 2.0)
    coordinator._algo_positions(pair, coordinator.market[pair.key])

    [row] = store.open_positions()
    again = SpreadPosition.from_dict(row)
    assert again.entry_margin == pytest.approx(MARGIN)
    assert again.position_id == position.position_id


def test_a_zero_margin_is_never_frozen(config, pair, legs):
    """A terminal answering 0 has not priced it: nothing is frozen, so
    the first real figure is the one kept — never a 0 that would put
    the TP and SL at break-even."""
    coordinator = engine(config, legs)
    position = opened(coordinator, pair, 2.0)
    legs['acct_a'].broker.margin_per_lot = 0.0
    legs['acct_b'].broker.margin_per_lot = 0.0
    coordinator._margin_cache.clear()
    assert coordinator.entry_margin(pair, position) is None
    assert position.entry_margin is None
    # CONTROL: once the terminals price it, that is what is frozen.
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    coordinator._margin_cache.clear()
    assert coordinator.entry_margin(pair, position) == pytest.approx(MARGIN)


# -- the levels in money, on the panel ------------------------------------------


@pytest.mark.parametrize('side', [SpreadSide.BUY, SpreadSide.SELL])
def test_each_level_says_what_closing_there_is_worth_net(config, pair, legs,
                                                         side):
    """The $ beside the TP and SL is the net P&L of the whole position
    closing at that price - checked against the mark the desk itself
    uses, not against the formula that made it."""
    from okxtrader.executor import mark_position
    coordinator = engine(config, legs)
    pair.algo_params = {'stop_loss_on': True, 'stop_loss_pct': 5.0}
    position = opened(coordinator, pair, 2.0, side=side)
    md = coordinator.market[pair.key]
    [row] = coordinator._algo_positions(pair, md)
    assert row['sl_money'] == pytest.approx(-0.05 * MARGIN * 2.0)
    assert row['tp_money'] > 0
    settings = pair.exit_settings(coordinator.config.settings)
    for level, worth in ((row['tp'], row['tp_money']),
                         (row['sl'], row['sl_money'])):
        there = dict(md, short_spread=level, long_spread=level)
        _gross, net, _ = mark_position(position, there, settings)
        assert net == pytest.approx(worth, abs=0.01)


def test_the_CONTROL_with_the_stop_off_there_is_no_stop_money(config, pair,
                                                               legs):
    coordinator = engine(config, legs)
    pair.algo_params = {'stop_loss_on': False}
    opened(coordinator, pair, 1.0)
    [row] = coordinator._algo_positions(pair, coordinator.market[pair.key])
    assert row['sl'] is None and row['sl_money'] is None


def test_the_money_reaches_the_algos_panel_row():
    from test_algo_signal import STATS, touch_signal
    held = {'position_id': 'p1', 'side': 'BUY', 'entry_spread': 9.80,
            'tp': 10.5, 'sl': 9.5, 'break_even': 9.82, 'quantity': 0.5,
            'tp_money': 4.0, 'sl_money': -10.0}
    body = touch_signal().evaluate(0.0, {'short_spread': 9.90,
                                         'long_spread': 9.92,
                                         'mid_spread': 9.91, 'quote_id': 1},
                                   STATS, [held], {})
    [row] = body['positions']
    assert (row['tp_money'], row['sl_money']) == (4.0, -10.0)
