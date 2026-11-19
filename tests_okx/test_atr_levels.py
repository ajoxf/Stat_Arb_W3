"""The stop and the target sized by the spread's ATR.

A % of margin is the same distance on a quiet day and a wild one. In
ATR mode the stop is a multiple of the spread's average true range -
close to close, on the Algo's own candles - frozen when the trade
opens. An entry waits for an ATR to exist, and a stop inside the
bid-ask (the trade would open already stopped) is refused. Every guard
has its control.
"""

import pytest

from okxtrader import algo, algofilters, backtest
from okxtrader.database import Store
from okxtrader.models import SpreadPosition, SpreadSide

from test_algo_signal import feed, touch_signal
from test_backtest import COST, LEVELS, PLAIN, ranging
from test_tp_sl_size import engine, opened


# -- the ATR --------------------------------------------------------------------


def test_atr_is_the_average_close_to_close_move_wilder_smoothed():
    closes = [10.0, 10.2, 10.1, 10.4, 10.3]          # moves .2 .1 .3 .1
    assert algofilters.atr(closes, 2) == pytest.approx(
        ((0.15 * 1 + 0.3) / 2 * 1 + 0.1) / 2)
    assert algofilters.atr(closes, 4) == pytest.approx(0.175)


def test_too_few_candles_is_no_atr_not_zero():
    assert algofilters.atr([10.0, 10.1], 14) is None
    assert algofilters.atr([], 14) is None


def test_the_settings():
    p = algo.clean_params({'stop_mode': 'atr', 'target_mode': 'ATR',
                           'atr_period': 21})
    assert (p['stop_mode'], p['target_mode'], p['atr_period']) == \
        ('ATR', 'ATR', 21)
    assert algo.clean_params({})['stop_mode'] == 'MARGIN'     # as before
    assert algo.clean_params({})['atr_period'] == 14
    assert algo.check_params({'stop_mode': 'WIDE'})
    assert algo.check_params({'atr_period': 1})
    assert algo.check_params({'atr_stop_mult': 0})
    assert algo.check_params({'stop_mode': 'ATR', 'atr_period': 21,
                              'atr_stop_mult': 2.5}) == []


# -- the levels on an open position -----------------------------------------------------


def atr_mode(pair, **extra):
    pair.algo_params = dict({'stop_mode': 'ATR', 'target_mode': 'ATR',
                             'atr_period': 14, 'atr_stop_mult': 2.0,
                             'atr_target_mult': 1.5}, **extra)


def test_atr_levels_are_multiples_of_the_atr_from_break_even(config, pair,
                                                            legs):
    coordinator = engine(config, legs)
    atr_mode(pair)
    coordinator.algos.atr = lambda key, period: 0.10
    position = opened(coordinator, pair, 1.0, side=SpreadSide.BUY)
    [row] = coordinator._algo_positions(pair, coordinator.market[pair.key])
    assert row['sl'] == pytest.approx(row['break_even'] - 0.20)
    assert row['tp'] == pytest.approx(row['break_even'] + 0.15)
    assert row['entry_atr'] == pytest.approx(0.10)
    assert position.entry_atr == pytest.approx(0.10)


def test_the_atr_is_frozen_when_the_trade_opens(config, pair, legs):
    coordinator = engine(config, legs)
    atr_mode(pair)
    coordinator.algos.atr = lambda key, period: 0.10
    opened(coordinator, pair, 1.0, side=SpreadSide.SELL)
    md = coordinator.market[pair.key]
    [before] = coordinator._algo_positions(pair, md)
    coordinator.algos.atr = lambda key, period: 0.40      # the market moves
    [after] = coordinator._algo_positions(pair, md)
    assert (after['tp'], after['sl']) == (before['tp'], before['sl'])
    assert before['sl'] == pytest.approx(before['break_even'] + 0.20)


def test_the_CONTROL_margin_mode_is_what_it_was(config, pair, legs):
    coordinator = engine(config, legs)
    pair.algo_params = {'stop_loss_pct': 5.0}
    coordinator.algos.atr = lambda key, period: 0.10
    position = opened(coordinator, pair, 1.0, side=SpreadSide.BUY)
    [row] = coordinator._algo_positions(pair, coordinator.market[pair.key])
    money = (row['sl'] - row['break_even']) * position.spread_units
    assert money == pytest.approx(-0.05 * 500.0)          # 5% of margin
    assert position.entry_atr is None                     # not needed


def test_the_atr_is_saved_with_the_position(config, pair, legs, tmp_path):
    store = Store(str(tmp_path / 'trader.db'))
    coordinator = engine(config, legs, store=store)
    atr_mode(pair)
    coordinator.algos.atr = lambda key, period: 0.10
    opened(coordinator, pair, 1.0)
    coordinator._algo_positions(pair, coordinator.market[pair.key])
    [row] = store.open_positions()
    assert SpreadPosition.from_dict(row).entry_atr == pytest.approx(0.10)


# -- the entry guard ---------------------------------------------------------------------


def test_no_entry_until_the_atr_is_measured(config, pair, legs):
    coordinator = engine(config, legs)
    atr_mode(pair)
    coordinator.algos.atr = lambda key, period: None
    reason = coordinator._levels_check(pair, coordinator.market[pair.key])
    assert 'ATR(14) not measured yet' in reason
    coordinator.algos.atr = lambda key, period: 0.50       # the control
    assert coordinator._levels_check(pair,
                                     coordinator.market[pair.key]) is None


def test_no_entry_with_a_stop_inside_the_bid_ask(config, pair, legs):
    coordinator = engine(config, legs)
    atr_mode(pair)
    md = coordinator.market[pair.key]
    width = md['long_spread'] - md['short_spread']
    coordinator.algos.atr = lambda key, period: width / 4   # stop = w / 2
    assert 'inside the bid-ask' in coordinator._levels_check(pair, md)
    coordinator.algos.atr = lambda key, period: width * 2  # the control
    assert coordinator._levels_check(pair, md) is None


def test_the_CONTROL_with_the_stop_off_only_the_atr_is_asked_for(config, pair,
                                                                 legs):
    coordinator = engine(config, legs)
    atr_mode(pair, stop_loss_on=False)
    md = coordinator.market[pair.key]
    coordinator.algos.atr = lambda key, period: 1e-6
    assert coordinator._levels_check(pair, md) is None


def test_the_algo_does_not_enter_past_the_levels_guard():
    body = feed(touch_signal(), 10.26, 10.30, 3,
                gates={'levels': 'levels: ATR(14) not measured yet'})
    assert body['intents'] == [] and 'ATR(14)' in body['blocked']
    body = feed(touch_signal(), 10.26, 10.30, 3, gates={'levels': None})
    assert body['intents'][0]['action'] == 'ENTER'          # the control


# -- the backtest -------------------------------------------------------------------------


def test_the_backtest_sizes_each_trade_from_the_atr_at_its_entry():
    params = dict(PLAIN, stop_mode='ATR', target_mode='ATR', atr_period=14,
                  atr_stop_mult=2.0, atr_target_mult=1.5)
    result = backtest.run(ranging(), params, 0.01, COST, LEVELS)
    assert result['summary']['trades'] >= 1


def test_the_backtest_waits_for_the_atr_like_live():
    params = dict(PLAIN, stop_mode='ATR', atr_period=500)   # never measured
    result = backtest.run(ranging(), params, 0.01, COST, LEVELS)
    assert result['trades'] == []
    assert any('ATR not measured' in reason for reason in result['held'])
    # The CONTROL: the same history in margin mode trades.
    assert backtest.run(ranging(), PLAIN, 0.01, COST, LEVELS)['trades']
