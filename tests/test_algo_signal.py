"""The Algo's arithmetic: the band, the entry, the gates and the exits.

Pure — no coordinator, no broker. Every gate here has a CONTROL beside
it that turns the gate off and shows the signal coming through, because
a test that only ever sees silence passes on a signal that never fires.
"""

import math

import pytest

from okxtrader import algo, bands

TF = 900.0                                   # 15-minute candles


# -- the band ------------------------------------------------------------------



def touch_signal(params=None):
    """The Algo entering on the TOUCH of the band — what these tests are
    about. Re-entry confirmation is tested on its own
    (test_algo_protections)."""
    return algo.AlgoSignal(dict({'reentry_on': False}, **(params or {})))


def test_the_middle_line_is_pines_ema_seeded_on_the_sma():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    seed = (1 + 2 + 3) / 3.0
    alpha = 2.0 / 4.0
    expected = seed
    for close in (4.0, 5.0, 6.0):
        expected = alpha * close + (1 - alpha) * expected
    assert bands.ema(values, 3) == pytest.approx(expected)


def test_sigma_is_the_population_stdev_of_the_last_n():
    assert bands.population_stdev([1.0, 3.0]) == pytest.approx(1.0)
    candles = bands.SpreadCandles(TF, 3)
    candles.seed([(0, 100.0), (900, 1.0), (1800, 3.0)])
    candles.observe(2700 + 10, 5.0)          # the forming candle counts
    stats = candles.stats()
    assert stats['ready']
    assert stats['sigma'] == pytest.approx(bands.population_stdev(
        [1.0, 3.0, 5.0]))


def test_fewer_than_n_candles_is_not_a_band_and_says_how_many():
    candles = bands.SpreadCandles(TF, 20)
    candles.seed([(i * TF, 1.0 + i * 0.01) for i in range(7)])
    stats = candles.stats()
    assert not stats['ready']
    assert stats['mean'] is None and stats['sigma'] is None
    assert (stats['count'], stats['needed']) == (7, 20)


def test_a_flat_spread_has_no_band():
    candles = bands.SpreadCandles(TF, 3)
    candles.seed([(0, 1.0), (900, 1.0), (1800, 1.0)])
    stats = candles.stats()
    assert not stats['ready'] and stats['sigma'] is None


def test_a_candle_closes_when_the_next_bucket_starts():
    candles = bands.SpreadCandles(TF, 3)
    assert candles.observe(10, 1.0) is None
    assert candles.observe(800, 1.5) is None           # same candle
    closed = candles.observe(905, 2.0)
    assert closed == (0.0, 1.5)                         # its LAST price
    assert candles.closes() == [1.5, 2.0]


def test_history_never_overwrites_a_candle_we_watched_close():
    candles = bands.SpreadCandles(TF, 3)
    candles.observe(10, 1.5)
    candles.observe(905, 2.0)                    # bucket 0 closed at 1.5
    added = candles.seed([(0.0, 9.9), (900.0, 9.9), (-900.0, 1.0)])
    assert candles.closed[0.0] == 1.5            # ours kept
    assert 900.0 not in candles.closed           # still forming: skipped
    assert candles.closed[-900.0] == 1.0 and added == 1


def test_bars_from_two_brokers_are_matched_in_utc_and_moved_to_the_mid():
    # Broker A runs at UTC+2, broker B at UTC+3: the SAME UTC candle is
    # stamped an hour apart.
    utc = 1_700_000_100 - (1_700_000_100 % 900)
    bars_a = [{'time': utc + 7200, 'close': 100.0, 'spread': 20,
               'point': 0.01}]
    bars_b = [{'time': utc + 10800, 'close': 110.0, 'spread': 40,
               'point': 0.01},
              # A candle leg A never traded: dropped, not filled in.
              {'time': utc + 10800 + 900, 'close': 111.0, 'spread': 0,
               'point': 0.01}]
    rows = bands.spread_bars(bars_a, bars_b, 1.0, 900, 7200, 10800)
    assert len(rows) == 1
    bucket, spread = rows[0]
    assert bucket == utc
    # Each bid moved up half its own bar's spread: 110.20 - 100.10.
    assert spread == pytest.approx(10.10)


def test_bars_whose_hour_cannot_be_placed_are_not_used():
    bars = [{'time': 900, 'close': 1.0, 'spread': 0, 'point': 0.01}]
    assert bands.spread_bars(bars, bars, 1.0, 900, None, 0) == []
    # The control: both clocks measured, and the bar is used.
    assert len(bands.spread_bars(bars, bars, 1.0, 900, 0, 0)) == 1


def test_a_brokers_offset_is_rounded_to_its_quarter_hour():
    assert bands.round_offset(10799) == 10800
    assert bands.round_offset(19802) == 19800       # GMT+5:30
    assert bands.round_offset(None) is None


# -- the signal ----------------------------------------------------------------

STATS = {'ready': True, 'count': 20, 'needed': 20, 'mean': 10.0,
         'sigma': 0.1}


def market(short, long_, quote=None):
    return {'short_spread': short, 'long_spread': long_,
            'mid_spread': (short + long_) / 2.0, 'quote_id': quote}


def run(signal, md, now=0.0, positions=(), gates=None, stats=STATS):
    return signal.evaluate(now, md, stats, positions, gates)


def feed(signal, short, long_, ticks, start=0, now=0.0, **kw):
    body = None
    for i in range(ticks):
        body = run(signal, market(short, long_, quote=start + i), now=now,
                   **kw)
    return body


def test_sell_signal_on_the_BID_side_z_after_three_fresh_quotes():
    signal = touch_signal()
    # Bid-side spread 10.26 is z +2.6; the offer side is further out.
    body = feed(signal, 10.26, 10.30, 2)
    assert body['state'] == 'CONFIRMING' and body['signal'] is None
    body = feed(signal, 10.26, 10.30, 1, start=2)
    assert body['state'] == 'SIGNAL' and body['signal'] == 'SELL'
    assert body['z_sell'] == pytest.approx(2.6)
    [intent] = body['intents']
    assert intent['action'] == 'ENTER' and intent['side'] == 'SELL'
    assert intent['spread'] == 10.26            # what a sell receives


def test_the_same_quote_polled_again_is_not_a_second_tick():
    signal = touch_signal()
    for _ in range(5):
        body = run(signal, market(10.26, 10.30, quote='same'))
    assert body['signal'] is None
    # The control: three DIFFERENT quotes confirm it.
    body = feed(signal, 10.26, 10.30, 3, start=100)
    assert body['signal'] == 'SELL'


def test_buy_is_judged_on_the_OFFER_side_not_the_mid():
    signal = touch_signal()
    # Mid 9.745 is z -2.55, but a buy PAYS 9.76: z -2.4, not a signal.
    body = feed(signal, 9.73, 9.76, 5)
    assert body['z_mid'] <= -2.5 and body['signal'] is None
    # The control: the offer itself through the band.
    body = feed(signal, 9.72, 9.74, 3, start=10)
    assert body['signal'] == 'BUY'
    assert body['intents'][0]['spread'] == 9.74


def test_a_signal_is_recorded_ONCE_while_it_holds():
    signal = touch_signal()
    feed(signal, 10.26, 10.30, 3)
    body = feed(signal, 10.27, 10.30, 4, start=3)
    assert body['signal'] == 'SELL' and body['intents'] == []


def test_a_price_that_cannot_be_trusted_holds_the_entry():
    signal = touch_signal()
    body = feed(signal, 10.26, 10.30, 3,
                gates={'health': 'leg B quote 20s old'})
    assert body['state'] == 'BLOCKED' and body['intents'] == []
    assert 'quote' in body['blocked']
    # The control: the same market, healthy.
    body = feed(signal, 10.26, 10.30, 1, start=3, gates={'health': None})
    assert body['signal'] == 'SELL'


def test_no_band_no_signal_and_the_count_is_said():
    signal = touch_signal()
    warming = {'ready': False, 'count': 7, 'needed': 20}
    body = feed(signal, 10.26, 10.30, 5, stats=warming)
    assert body['signal'] is None and body['z_sell'] is None
    assert body['state'] == 'BLOCKED' and '7/20' in body['blocked']


def test_the_last_minutes_before_the_cutoff_hold_the_entry():
    signal = touch_signal({'cutoff_buffer_min': 20})
    body = feed(signal, 10.26, 10.30, 3, gates={'cutoff_min': 12})
    assert body['state'] == 'BLOCKED' and 'cutoff' in body['blocked']
    # The control: the buffer off.
    signal = touch_signal({'cutoff_buffer_min': 0})
    body = feed(signal, 10.26, 10.30, 3, gates={'cutoff_min': 12})
    assert body['signal'] == 'SELL'


def test_a_blow_out_past_the_cap_is_not_an_entry():
    signal = touch_signal({'max_entry_z': 3.5})
    body = feed(signal, 10.40, 10.42, 3)                 # z +4.0
    assert body['state'] == 'BLOCKED' and 'cap' in body['blocked']
    # The control: no cap.
    signal = touch_signal({'max_entry_z': 0})
    assert feed(signal, 10.40, 10.42, 3)['signal'] == 'SELL'


def position(side='SELL', tp=10.0, be=10.2, opened_at=0.0, pid='POS-1',
             sl=None, entry=10.26):
    return {'position_id': pid, 'side': side, 'entry_spread': entry,
            'opened_at': opened_at, 'break_even': be, 'tp': tp, 'sl': sl,
            'net_pnl': 1.0}


def test_a_close_starts_the_cooldown():
    signal = touch_signal({'cooldown_min': 5})
    run(signal, market(10.10, 10.12, 1), now=0, positions=[position()])
    body = feed(signal, 10.26, 10.30, 3, start=10, now=60)   # it closed
    assert body['state'] == 'BLOCKED' and 'cooldown' in body['blocked']
    # The control: five minutes later the same stretch signals.
    body = feed(signal, 10.26, 10.30, 1, start=20, now=60 + 301)
    assert body['signal'] == 'SELL'


def test_no_entry_signal_while_the_ladder_holds_a_position():
    signal = touch_signal({'reversion_on': False})
    body = feed(signal, 9.72, 9.74, 3, positions=[position(tp=None)])
    assert body['state'] == 'IN_POSITION'
    assert not [i for i in body['intents'] if i['action'] == 'ENTER']


def test_exit_at_break_even_after_costs_plus_the_target():
    signal = touch_signal()
    # A SHORT closes at the OFFER: 10.01 is short of a 10.00 target.
    body = run(signal, market(9.98, 10.01, 1), positions=[position(tp=10.0)])
    assert body['positions'][0]['exit'] is None
    body = run(signal, market(9.97, 10.00, 2), positions=[position(tp=10.0)])
    assert body['state'] == 'EXIT'
    [intent] = body['intents']
    assert intent['action'] == 'EXIT' and intent['reason'] == 'PROFIT_TARGET'
    assert intent['position_id'] == 'POS-1' and intent['spread'] == 10.00


def test_a_long_exits_on_the_BID():
    signal = touch_signal({'reversion_on': False})
    held = [position(side='BUY', tp=10.10, be=10.0)]
    assert run(signal, market(10.09, 10.20, 1),
               positions=held)['positions'][0]['exit'] is None
    assert run(signal, market(10.10, 10.20, 2),
               positions=held)['positions'][0]['exit'] == 'PROFIT_TARGET'


def test_no_target_priced_means_no_profit_exit():
    """Unmeasured is not zero: break-even is not the target."""
    signal = touch_signal({'reversion_on': False})
    body = run(signal, market(5.0, 5.01, 1), positions=[position(tp=None)])
    assert body['positions'][0]['exit'] is None and body['intents'] == []


def test_a_gate_never_holds_back_an_exit():
    signal = touch_signal()
    body = run(signal, market(9.97, 10.00, 1), positions=[position(tp=10.0)],
               gates={'health': 'leg A quote 20s old', 'cutoff_min': -5})
    assert body['positions'][0]['exit'] == 'PROFIT_TARGET'


def test_the_optional_exits_are_OFF_until_a_ladder_asks():
    held = [position(side='SELL', tp=9.0, be=10.2, opened_at=0.0)]
    stretched = market(10.44, 10.45, 1)           # offer z +4.5
    signal = touch_signal()
    body = run(signal, stretched, now=10 ** 6, positions=held)
    assert body['positions'][0]['exit'] is None

    on = touch_signal({'stop_z_on': True, 'stop_z': 4.0})
    assert run(on, stretched, positions=held)['positions'][0]['exit'] \
        == 'Z_STOP'

    timed = touch_signal({'time_stop_on': True, 'time_stop_candles': 2})
    assert run(timed, market(10.1, 10.11, 1), now=1799,
               positions=held)['positions'][0]['exit'] is None
    assert run(timed, market(10.1, 10.11, 2), now=1800,
               positions=held)['positions'][0]['exit'] == 'TIME_STOP'


def test_back_to_the_mean_only_in_profit():
    held = [position(side='SELL', tp=9.0, be=10.0)]
    signal = touch_signal({'reversion_on': True})
    # Offer at z -0.5 but ABOVE break-even: not in profit, no exit.
    assert run(signal, market(9.94, 10.05, 1), stats=dict(
        STATS, mean=10.10), positions=held)['positions'][0]['exit'] is None
    # The control: back through the mean AND under break-even.
    assert run(signal, market(9.90, 9.95, 2), positions=held)[
        'positions'][0]['exit'] == 'MEAN_REVERSION'


def test_blank_settings_are_the_defaults_and_bad_ones_are_named():
    params = algo.clean_params({'entry_z': '', 'timeframe_min': 7,
                                'length': '30', 'stop_z_on': 'true'})
    assert params['entry_z'] == 2.5
    assert params['timeframe_min'] == 15            # 7 is not a timeframe
    assert params['length'] == 30 and params['stop_z_on'] is True
    problems = algo.check_params({'timeframe_min': 7, 'entry_z': 0,
                                  'nonsense': 1})
    assert len(problems) == 3
    assert algo.check_params({'entry_z': 2.0, 'length': ''}) == []


def test_the_defaults_are_the_ones_the_desk_agreed():
    d = algo.DEFAULT_PARAMS
    assert (d['entry_z'], d['timeframe_min'], d['length'],
            d['confirm_ticks']) == (2.5, 15, 20, 3)
    assert (d['max_entry_z'], d['cutoff_buffer_min'],
            d['cooldown_min']) == (3.5, 20, 5)
    assert not (d['stop_z_on'] or d['time_stop_on'])
    # Back to mean is ON: a mean short of the TP is where the trade
    # comes off, in profit.
    assert d['reversion_on'] is True
    assert math.isclose(d['stop_z'], 4.0)
    # The stop loss is ON, at the take-profit's own 2% of margin.
    assert d['stop_loss_on'] is True and d['stop_loss_pct'] == 2.0
    assert d['progress_bar'] is True


def test_the_stop_loss_exits_a_short_on_the_OFFER():
    signal = touch_signal()
    held = [position(side='SELL', tp=10.0, sl=10.40)]
    # A short closes at the offer: 10.39 has not reached a 10.40 stop.
    assert run(signal, market(10.37, 10.39, 1),
               positions=held)['positions'][0]['exit'] is None
    body = run(signal, market(10.38, 10.40, 2), positions=held)
    assert body['positions'][0]['exit'] == 'STOP_LOSS'
    [intent] = body['intents']
    assert intent['reason'] == 'STOP_LOSS' and intent['sl'] == 10.40


def test_a_long_is_stopped_on_the_BID_and_no_gate_holds_it():
    signal = touch_signal()
    held = [position(side='BUY', tp=10.5, sl=9.80, be=10.1, entry=10.1)]
    body = run(signal, market(9.80, 9.90, 1), positions=held,
               gates={'health': 'leg A quote 20s old', 'cutoff_min': -5})
    assert body['positions'][0]['exit'] == 'STOP_LOSS'


def test_no_stop_priced_means_no_stop_signal():
    """The stop off — or no margin to price it — is None, never 0: a
    stop of 0 points would sit AT break-even and fire on the entry."""
    signal = touch_signal()
    held = [position(side='BUY', tp=10.5, sl=None, be=10.1, entry=10.1)]
    assert run(signal, market(1.0, 1.1, 1),
               positions=held)['positions'][0]['exit'] is None


def test_progress_runs_from_the_stop_to_the_target():
    # A long in at 10.0, stop 9.8 (0.2 under), target 10.4 (0.4 over).
    assert algo.progress('BUY', 10.0, 10.0, 10.4, 9.8) == 0.0
    assert algo.progress('BUY', 10.0, 10.2, 10.4, 9.8) == pytest.approx(0.5)
    assert algo.progress('BUY', 10.0, 9.9, 10.4, 9.8) == pytest.approx(-0.5)
    assert algo.progress('BUY', 10.0, 11.0, 10.4, 9.8) == 1.0     # clamped
    assert algo.progress('BUY', 10.0, 9.0, 10.4, 9.8) == -1.0
    # A short mirrors it: in at 10.0, target 9.6, stop 10.2.
    assert algo.progress('SELL', 10.0, 9.8, 9.6, 10.2) == pytest.approx(0.5)
    assert algo.progress('SELL', 10.0, 10.1, 9.6, 10.2) == pytest.approx(-0.5)
    # Unmeasured is not zero: no stop, and the losing half is unknown.
    assert algo.progress('BUY', 10.0, 9.9, 10.4, None) is None
    assert algo.progress('BUY', 10.0, None, 10.4, 9.8) is None


def test_each_position_row_carries_its_entry_and_progress():
    signal = touch_signal()
    body = run(signal, market(10.10, 10.16, 1),
               positions=[position(side='SELL', tp=9.86, sl=10.46,
                                   entry=10.26)])
    row = body['positions'][0]
    assert row['entry_spread'] == 10.26 and row['sl'] == 10.46
    assert row['progress'] == pytest.approx(0.25)



def test_a_ladder_set_to_one_direction_enters_only_that_way():
    """H to L only: a stretch DOWN is shown, never entered. The control:
    Both, and the same stretch is a BUY."""
    down = (9.72, 9.74)                          # offer z about -2.6
    one_way = touch_signal({'direction': 'H_TO_L'})
    body = feed(one_way, *down, 3)
    assert body['signal'] is None and body['intents'] == []
    assert body['z_buy'] <= -2.5                 # still measured and shown
    both = touch_signal({'direction': 'BOTH'})
    assert feed(both, *down, 3)['signal'] == 'BUY'


def test_L_to_H_only_never_sells_and_still_buys():
    one_way = touch_signal({'direction': 'L_TO_H'})
    assert feed(one_way, 10.26, 10.30, 3)['signal'] is None
    assert feed(one_way, 9.72, 9.74, 3, start=10)['signal'] == 'BUY'


def test_direction_never_holds_back_an_exit():
    """A long on an H-to-L-only ladder (opened before the setting
    changed) still gets out."""
    signal = touch_signal({'direction': 'H_TO_L'})
    held = [position(side='BUY', tp=10.10, be=10.0, entry=10.0)]
    assert run(signal, market(10.10, 10.20, 1),
               positions=held)['positions'][0]['exit'] == 'PROFIT_TARGET'


def test_a_direction_that_does_not_exist_is_named():
    assert algo.clean_params({'direction': 'sideways'})['direction'] == 'BOTH'
    assert algo.check_params({'direction': 'sideways'})
    assert algo.check_params({'direction': 'l_to_h'}) == []


def test_the_panel_sees_the_positions_own_size_age_and_legs():
    """A 0.5-spread position read "1.00 spread(s)" on the panel: the
    signal dropped its size, so the screen fell back to 1."""
    held = {'position_id': 'p1', 'side': 'SELL', 'entry_spread': 10.15,
            'tp': 9.9, 'sl': 10.5, 'break_even': 10.15, 'net_pnl': -8.45,
            'quantity': 0.5, 'age_sec': 75.0, 'opened_at': 1.0,
            'leg_a_side': 'BUY', 'leg_a_entry': 89.08, 'leg_a_now': 89.17,
            'leg_b_side': 'SELL', 'leg_b_entry': 102.28, 'leg_b_now': 102.54}
    body = run(touch_signal(), market(10.20, 10.22, 1), positions=[held])
    [row] = body['positions']
    assert row['quantity'] == 0.5
    assert row['age_sec'] == 75.0
    assert row['leg_a_entry'] == 89.08 and row['leg_b_side'] == 'SELL'


def test_one_position_at_a_time_no_entry_while_holding():
    """A stretch that would enter when flat does nothing while a
    position is on — and the CONTROL: flat, the same stretch enters."""
    held = {'position_id': 'p1', 'side': 'SELL', 'entry_spread': 10.15,
            'quantity': 0.5}
    body = feed(touch_signal(), 10.26, 10.30, 3, positions=[held])
    assert body['state'] == 'IN_POSITION' and body['intents'] == []
    body = feed(touch_signal(), 10.26, 10.30, 3)
    assert body['intents'][0]['action'] == 'ENTER'


def test_a_mean_short_of_the_target_is_where_a_long_comes_off():
    """ETH vs BTC, 23 Nov: long, the mean below the TP, the spread
    touched the mean and turned. Back to mean (on by default) takes it
    there - on the BID, and only at or past break-even."""
    signal = touch_signal()
    body = run(signal, market(10.0, 10.02, 1))
    mean = body['mean']
    held = [position(side='BUY', tp=mean + 1.0, be=mean - 1.0)]
    # The bid short of the mean: holds.
    assert run(signal, market(mean - 0.01, mean + 0.05, 2),
               positions=held)['positions'][0]['exit'] is None
    # The bid at the mean, past break-even, before the TP: out.
    assert run(signal, market(mean + 0.001, mean + 0.05, 3),
               positions=held)['positions'][0]['exit'] == 'MEAN_REVERSION'
    # A mean not yet past break-even is no exit: TP or SL decide.
    signal = touch_signal()
    run(signal, market(10.0, 10.02, 1))
    under = [position(side='BUY', tp=mean + 2.0, be=mean + 1.0)]
    assert run(signal, market(mean + 0.001, mean + 0.05, 2),
               positions=under)['positions'][0]['exit'] is None


# -- the ADD: one more trade on the same side, on the re-entry -------------

def test_an_add_goes_on_once_the_spread_is_two_sigma_past_the_entry():
    """Short from 10.00; mean 10.00, sigma 0.1. A fresh SELL signal at
    10.26 is 2.6 sigma past it: the add goes on, on the same side."""
    signal = touch_signal()
    held = [position(entry=10.00, tp=9.9, be=10.05)]
    body = feed(signal, 10.26, 10.30, 3, positions=held)
    assert body['state'] == 'SIGNAL' and body['signal'] == 'SELL'
    [intent] = body['intents']
    assert intent['action'] == 'ENTER' and intent['side'] == 'SELL'
    assert intent['add'] is True and intent['spread'] == 10.26


def test_no_add_short_of_the_distance_and_it_says_why():
    """Short from 10.10: an add needs the bid at 10.30 or more."""
    signal = touch_signal()
    held = [position(entry=10.10, tp=9.9, be=10.15)]
    body = feed(signal, 10.26, 10.30, 3, positions=held)
    assert body['state'] == 'IN_POSITION' and body['intents'] == []
    assert body['add']['beyond'] == pytest.approx(10.30)
    assert 'sigma past the entry' in body['add']['waiting']
    assert 'blocked_side' not in body              # not a blocked signal


def test_one_add_at_most_and_never_the_other_side():
    signal = touch_signal()
    two = [position(entry=10.00, tp=9.9, be=10.05),
           position(entry=10.26, tp=10.1, be=10.31, pid='POS-2')]
    body = feed(signal, 10.40, 10.42, 3, positions=two,
                gates=None)
    assert body['state'] == 'IN_POSITION' and body['intents'] == []
    assert 'the most' in body['add']['why']
    # A long on: a SELL stretch is not an add to it.
    signal = touch_signal()
    long_ = [position(side='BUY', entry=10.00, tp=10.1, be=9.95)]
    body = feed(signal, 10.26, 10.30, 3, positions=long_)
    assert body['intents'] == []


def test_adding_switched_off_holds_one_trade():
    signal = touch_signal({'add_on': False})
    held = [position(entry=10.00, tp=9.9, be=10.05)]
    body = feed(signal, 10.26, 10.30, 3, positions=held)
    assert body['state'] == 'IN_POSITION' and body['intents'] == []


def test_the_add_is_taken_on_the_reentry_not_on_the_spike():
    """ETH vs BTC, 23 Nov: short, then a spike far past the band. The
    add is NOT sold into the spike; it arms there and is taken when the
    spread comes back inside the band (2.5 armed, 0.5 back: z 2.0)."""
    signal = algo.AlgoSignal({'max_entry_z': 0})
    held = [position(entry=9.95, tp=9.85, be=10.0, sl=None)]
    body = feed(signal, 10.45, 10.47, 3, positions=held)     # z +4.5
    assert body['intents'] == [] and body['armed']['SELL']
    body = feed(signal, 10.30, 10.32, 3, start=10, positions=held)
    assert body['intents'] == []                    # still outside
    body = feed(signal, 10.19, 10.21, 3, start=20, positions=held)
    [intent] = body['intents']
    assert intent['side'] == 'SELL' and intent['add'] is True
    assert intent['spread'] == 10.19


def test_add_settings_are_read_and_checked():
    p = algo.clean_params({'add_on': 'false', 'add_sigma': '1.5',
                           'max_adds': '2'})
    assert (p['add_on'], p['add_sigma'], p['max_adds']) == (False, 1.5, 2)
    assert algo.DEFAULT_PARAMS['add_on'] is True
    assert algo.check_params({'add_sigma': 0})
