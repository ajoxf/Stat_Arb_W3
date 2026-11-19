"""Each side's own stop and target.

A ladder's exits applied to both sides alike. H to L and L to H can now
each carry their own - sizing mode, SL %, TP %, ATR multiples - and a
blank one is the ladder's own, never a zero.
"""

import pytest

from okxtrader import algo, backtest
from okxtrader.models import SpreadSide

from test_algo_signal import feed, touch_signal
from test_atr_levels import atr_mode
from test_backtest import COST, LEVELS, PLAIN, ranging
from test_tp_sl_size import MARGIN, engine, opened


def test_a_side_lays_its_own_values_over_the_ladders():
    p = algo.clean_params({'atr_stop_mult': 2.0, 'atr_stop_mult_sell': 3.0,
                           'stop_mode_buy': 'ATR', 'tp_pct_buy': 6})
    sell, buy = algo.for_side(p, 'SELL'), algo.for_side(p, 'BUY')
    assert sell['atr_stop_mult'] == 3.0 and buy['atr_stop_mult'] == 2.0
    assert buy['stop_mode'] == 'ATR' and sell['stop_mode'] == 'MARGIN'
    assert buy['tp_pct'] == 6.0 and sell['tp_pct'] is None


def test_a_blank_or_zero_side_value_is_the_ladders_never_zero():
    p = algo.clean_params({'atr_stop_mult_sell': '', 'stop_loss_pct_buy': 0})
    assert p['atr_stop_mult_sell'] is None and p['stop_loss_pct_buy'] is None
    assert algo.for_side(p, 'BUY')['stop_loss_pct'] == p['stop_loss_pct']
    assert algo.check_params({'stop_loss_pct_buy': 0})
    assert algo.check_params({'stop_mode_sell': 'WIDE'})


def two_positions(config, pair, legs, **params):
    coordinator = engine(config, legs)
    atr_mode(pair, **params)
    coordinator.algos.atr = lambda key, period: 0.10
    sell = opened(coordinator, pair, 1.0, side=SpreadSide.SELL)
    buy = opened(coordinator, pair, 1.0, side=SpreadSide.BUY)
    rows = {r['position_id']: r for r in coordinator._algo_positions(
        pair, coordinator.market[pair.key])}
    assert sell.spread_units == buy.spread_units
    K[0] = sell.spread_units                       # k, one spread
    return rows[sell.position_id], rows[buy.position_id]


K = [None]


def test_each_side_gets_its_own_atr_stop(config, pair, legs):
    sell, buy = two_positions(config, pair, legs, atr_stop_mult_sell=3.0)
    assert sell['sl'] == pytest.approx(sell['break_even'] + 0.30)
    assert buy['sl'] == pytest.approx(buy['break_even'] - 0.20)


def test_the_CONTROL_with_no_side_values_both_sides_are_alike(config, pair,
                                                               legs):
    sell, buy = two_positions(config, pair, legs)
    assert sell['sl'] - sell['break_even'] == pytest.approx(
        buy['break_even'] - buy['sl'])


def test_one_side_by_margin_the_other_by_atr(config, pair, legs):
    sell, buy = two_positions(config, pair, legs, stop_mode_buy='MARGIN',
                              stop_loss_pct_buy=5.0)
    assert sell['sl'] == pytest.approx(sell['break_even'] + 0.20)    # ATR
    money = (buy['break_even'] - buy['sl']) * K[0]
    assert money == pytest.approx(0.05 * MARGIN)                     # 5 %


def test_each_side_gets_its_own_tp_percent(config, pair, legs):
    sell, buy = two_positions(config, pair, legs, target_mode='MARGIN',
                              tp_pct_buy=6.0)
    gained = (buy['tp'] - buy['break_even']) * K[0]
    assert gained == pytest.approx(0.06 * MARGIN)
    lost = (sell['break_even'] - sell['tp']) * K[0]
    assert lost != pytest.approx(0.06 * MARGIN)       # the ladder's own


def test_the_entry_check_is_per_side(config, pair, legs):
    coordinator = engine(config, legs)
    atr_mode(pair, atr_stop_mult_sell=0.1)           # H to L: inside bid-ask
    coordinator.algos.atr = lambda key, period: 1.0  # ladder's: 2.0, clear
    gate = coordinator._levels_gate(pair, coordinator.market[pair.key])
    assert 'inside the bid-ask' in gate['SELL'] and gate['BUY'] is None
    # The CONTROL: alike, one answer for both.
    atr_mode(pair)
    assert coordinator._levels_gate(pair, coordinator.market[pair.key]) \
        is None


def test_a_held_side_does_not_hold_the_other():
    gates = {'levels': {'SELL': 'levels: the stop is inside the bid-ask',
                        'BUY': None}}
    body = feed(touch_signal(), 10.26, 10.30, 3, gates=gates)  # an H to L
    assert body['intents'] == [] and 'bid-ask' in body['blocked']
    body = feed(touch_signal(), 9.70, 9.74, 3, gates=gates)    # an L to H
    assert body['intents'][0]['side'] == 'BUY'


def test_the_backtest_sizes_each_side_from_its_own():
    """A side given a stop inside the bid-ask has its entries held, as
    live; the OTHER side's stop does not hold it. (This history only
    ever stretches upward, so its trades are all H to L.)"""
    tiny = dict(LEVELS, stop_points=0.001)               # inside 0.01
    held = backtest.run(ranging(), PLAIN, 0.01, COST,
                        dict(LEVELS, by_side={'SELL': tiny, 'BUY': LEVELS}))
    assert held['trades'] == []
    assert any('bid-ask' in reason for reason in held['held'])
    # The CONTROL: the same stop on the OTHER side leaves H to L trading.
    free = backtest.run(ranging(), PLAIN, 0.01, COST,
                        dict(LEVELS, by_side={'SELL': LEVELS, 'BUY': tiny}))
    plain = backtest.run(ranging(), PLAIN, 0.01, COST, LEVELS)
    assert free['trades'] and len(free['trades']) == len(plain['trades'])
