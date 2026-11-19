"""Keeping the Algo out of a trend.

A mean-reversion Algo that sells every touch of the upper band is run
over by a spread that climbs along it — the oil spread of 2 October did
exactly that for ten hours. Two protections, each with a control:

- RE-ENTRY: armed by the stretch, it enters only when the spread comes
  back inside the band. A spread riding the band never comes back.
- TREND DIRECTION: no entry against a middle line that has moved more
  than a sigma over the last two hours. The other side stays open.
"""

import pytest

from okxtrader import algo
from okxtrader.algodesk import judge_filters, trend_drift

from test_algo_signal import STATS, feed

# The band: mean 10.00, sigma 0.10. Entry z 2.0 arms at 10.20 (sell
# side); re-entry 0.5 back enters at z 1.5, i.e. 10.15.
REENTRY = {'entry_z': 2.0, 'reentry_on': True, 'reentry_back': 0.5,
           'confirm_ticks': 1, 'max_entry_z': 0, 'cutoff_buffer_min': 0}


def test_the_defaults_wait_for_the_way_back_in_and_watch_the_trend():
    assert algo.DEFAULT_PARAMS['reentry_on'] is True
    assert algo.DEFAULT_PARAMS['trend_on'] is True


# -- re-entry -------------------------------------------------------------------


def test_a_stretch_that_stays_out_is_never_sold():
    """A trend riding the band: z stays at 2.5 — armed, never entered."""
    signal = algo.AlgoSignal(REENTRY)
    body = None
    for i in range(10):
        body = feed(signal, 10.25, 10.27, 1, start=i)
        assert body['intents'] == []
    assert body['armed']['SELL'] is True
    assert body['signal'] is None


def test_the_CONTROL_entering_on_the_touch_would_have_sold_it():
    signal = algo.AlgoSignal(dict(REENTRY, reentry_on=False))
    body = feed(signal, 10.25, 10.27, 1)
    assert body['intents'][0]['side'] == 'SELL'


def test_the_way_back_inside_is_the_entry():
    signal = algo.AlgoSignal(REENTRY)
    feed(signal, 10.25, 10.27, 1, start=0)            # armed at z +2.5
    body = feed(signal, 10.18, 10.20, 1, start=1)     # z +1.8: not yet
    assert body['intents'] == []
    body = feed(signal, 10.14, 10.16, 1, start=2)     # z +1.4: back in
    [intent] = body['intents']
    assert intent['side'] == 'SELL'
    assert intent['z'] == pytest.approx(1.4)
    # The arming is spent: back out and in again is a NEW arming.
    assert body['armed']['SELL'] is False


def test_reaching_the_mean_first_disarms_it():
    signal = algo.AlgoSignal(REENTRY)
    feed(signal, 10.25, 10.27, 1, start=0)            # armed
    feed(signal, 9.99, 10.01, 1, start=1)             # z <= 0: disarmed
    body = feed(signal, 10.14, 10.16, 1, start=2)     # z +1.4, not armed
    assert body['intents'] == []
    assert body['armed']['SELL'] is False


def test_the_buy_side_mirrors_it():
    signal = algo.AlgoSignal(REENTRY)
    feed(signal, 9.73, 9.75, 1, start=0)              # offer z -2.5: armed
    body = feed(signal, 9.84, 9.86, 1, start=1)       # offer z -1.4: in
    assert body['intents'][0]['side'] == 'BUY'


def test_re_entry_cannot_be_set_at_or_past_the_mean():
    p = algo.clean_params({'entry_z': 1.0, 'reentry_back': 5})
    assert 0 < p['entry_z'] - p['reentry_back'] < p['entry_z']
    assert algo.check_params({'reentry_back': 0})


# -- the re-entry window -----------------------------------------------------------
#
# Live: oil armed past +2.00, came back, and was still an "entry" at
# +0.05 - with nothing left to revert. The window now ends half-way
# back: entry 2.0, back 0.5, window 50 % enters between +1.50 and +0.75.


def test_the_window_is_half_way_back_by_default():
    p = algo.clean_params(REENTRY)
    assert algo.reentry_window(p) == pytest.approx((1.5, 0.75))


def test_a_signal_near_the_mean_is_not_an_entry():
    signal = algo.AlgoSignal(REENTRY)
    feed(signal, 10.25, 10.27, 1, start=0)            # armed at z +2.5
    body = feed(signal, 10.005, 10.02, 1, start=1)    # z +0.05
    assert body['intents'] == []
    assert body['armed']['SELL'] is False             # through the window


def test_the_CONTROL_the_open_window_still_takes_it():
    signal = algo.AlgoSignal(dict(REENTRY, reentry_window_pct=100))
    feed(signal, 10.25, 10.27, 1, start=0)
    body = feed(signal, 10.005, 10.02, 1, start=1)
    assert body['intents'][0]['side'] == 'SELL'


def test_inside_the_window_it_enters():
    signal = algo.AlgoSignal(REENTRY)
    feed(signal, 10.25, 10.27, 1, start=0)
    body = feed(signal, 10.09, 10.11, 1, start=1)     # z +0.9: in window
    assert body['intents'][0]['z'] == pytest.approx(0.9)


def test_a_jump_through_the_window_disarms_and_never_comes_back_to_it():
    signal = algo.AlgoSignal(REENTRY)
    feed(signal, 10.25, 10.27, 1, start=0)            # armed
    feed(signal, 10.05, 10.07, 1, start=1)            # z +0.5: through
    body = feed(signal, 10.12, 10.14, 1, start=2)     # z +1.2: not armed
    assert body['intents'] == [] and body['armed']['SELL'] is False


def test_the_buy_side_is_the_mirror():
    signal = algo.AlgoSignal(REENTRY)
    feed(signal, 9.73, 9.75, 1, start=0)              # buy z -2.5: armed
    body = feed(signal, 9.97, 9.99, 1, start=1)       # buy z -0.1
    assert body['intents'] == [] and body['armed']['BUY'] is False
    signal = algo.AlgoSignal(REENTRY)
    feed(signal, 9.73, 9.75, 1, start=0)
    body = feed(signal, 9.88, 9.90, 1, start=1)       # buy z -1.0: inside
    assert body['intents'][0]['side'] == 'BUY'


def test_the_window_setting_is_checked():
    assert algo.check_params({'reentry_window_pct': 0})
    assert algo.check_params({'reentry_window_pct': 120})
    assert algo.check_params({'reentry_window_pct': 50}) == []


# -- trend direction ------------------------------------------------------------


def climbing(n=40, step=0.02):
    return [10.0 + step * i for i in range(n)]


PARAMS = algo.clean_params({'trend_on': True, 'trend_sigma': 1.0,
                            'trend_lookback_min': 120, 'length': 20,
                            'edge_on': False, 'regime_on': False})


def check_for(closes, params=PARAMS):
    stats = {'ready': True, 'mean': None, 'sigma': 0.1}
    return judge_filters(params, None, stats, closes, {})


def test_the_drift_is_measured_in_sigma_and_says_which_way():
    assert trend_drift(climbing(), 20, 8, 0.1) > 1.0
    assert trend_drift(climbing(step=-0.02), 20, 8, 0.1) < -1.0
    assert trend_drift(climbing(n=10), 20, 8, 0.1) is None   # too short


def test_a_rising_middle_blocks_selling_into_it_and_not_buying():
    filters, check = check_for(climbing())
    assert filters['trend']['state'] == 'UP'
    assert 'ROSE' in check('SELL', 1.5)
    assert check('BUY', -1.5) is None             # with the trend: open


def test_a_falling_middle_blocks_buying_into_it():
    filters, check = check_for(climbing(step=-0.02))
    assert 'FELL' in check('BUY', -1.5)
    assert check('SELL', 1.5) is None


def test_the_CONTROL_a_flat_middle_blocks_nothing():
    flat = [10.0 + (0.05 if i % 2 else -0.05) for i in range(40)]
    filters, check = check_for(flat)
    assert filters['trend']['state'] == 'FLAT'
    assert check('SELL', 1.5) is None and check('BUY', -1.5) is None


def test_the_CONTROL_switched_off_it_blocks_nothing():
    filters, check = check_for(climbing(), dict(PARAMS, trend_on=False))
    assert check('SELL', 1.5) is None


def test_unmeasured_blocks_rather_than_passes():
    filters, check = check_for(climbing(n=10))
    assert 'not enough candles' in check('SELL', 1.5)
