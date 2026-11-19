"""A take-profit is confirmed on prices read NOW. Nothing else waits.

Live on oil: one poll showed the closing spread 0.16 past the target,
the Algo closed, and the close filled 0.235 back from it - a +$3.90
target banked +$0.10, and the trade's "peak" was that same bad print.
The stop loss, the cutoff, a kill and every manual close are never
re-checked: waiting there adds risk. Waiting on a target costs at most
the opportunity, and the stop still stands meanwhile.
"""

import pytest

from okxtrader.models import SpreadSide

from test_atr_levels import atr_mode
from test_tp_sl_size import engine, opened


def seen_at_target(coordinator, pair, position):
    """A poll whose closing price is just past the take-profit - while
    the broker's own quotes have not moved."""
    md = coordinator.market[pair.key]
    [row] = coordinator._algo_positions(pair, md)
    coordinator.market[pair.key] = dict(md, long_spread=row['tp'] - 0.01)
    return row['tp']


@pytest.fixture
def holding(config, pair, legs):
    coordinator = engine(config, legs)
    atr_mode(pair)
    coordinator.algos.atr = lambda key, period: 0.10
    position = opened(coordinator, pair, 1.0, side=SpreadSide.SELL)
    return coordinator, position


def test_a_target_seen_on_one_bad_poll_is_not_banked(holding, pair, legs):
    coordinator, position = holding
    seen_at_target(coordinator, pair, position)
    answer = coordinator.algo_exit(pair, position.position_id,
                                   'PROFIT_TARGET')
    assert answer['ok'] is False and answer['held'] is True
    assert 'short of the take-profit' in answer['reason']
    assert position.is_open
    assert legs['acct_b'].broker.open_positions()          # still on


def test_the_CONTROL_a_target_the_market_really_reached_is_banked(
        holding, pair, legs):
    coordinator, position = holding
    seen_at_target(coordinator, pair, position)
    symbol = legs['acct_b'].broker.symbols[pair.symbol_b]
    symbol.quote(symbol.bid - 1.0, symbol.ask - 1.0)        # it is there
    answer = coordinator.algo_exit(pair, position.position_id,
                                   'PROFIT_TARGET')
    assert answer['ok'] is True and not position.is_open


def test_a_stop_loss_is_never_re_checked(holding, pair, legs):
    coordinator, position = holding
    seen_at_target(coordinator, pair, position)       # fresh says "no"
    answer = coordinator.algo_exit(pair, position.position_id, 'STOP_LOSS')
    assert answer['ok'] is True and not position.is_open


def test_the_CONTROL_with_the_recheck_off_the_bad_poll_is_banked(
        holding, pair, legs):
    coordinator, position = holding
    coordinator.config.settings['RECHECK_TAKE_PROFIT'] = False
    seen_at_target(coordinator, pair, position)
    answer = coordinator.algo_exit(pair, position.position_id,
                                   'PROFIT_TARGET')
    assert answer['ok'] is True and not position.is_open


def test_a_leg_that_cannot_be_read_holds_the_target_not_the_stop(
        holding, pair, legs):
    coordinator, position = holding
    seen_at_target(coordinator, pair, position)
    legs['acct_a'].tick = lambda symbol: None
    held = coordinator.algo_exit(pair, position.position_id, 'PROFIT_TARGET')
    assert held['ok'] is False and 'could not be read' in held['reason']
    stop = coordinator.algo_exit(pair, position.position_id, 'STOP_LOSS')
    assert stop['ok'] is True and not position.is_open
