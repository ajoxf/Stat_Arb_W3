"""The Analysis tab: the arithmetic, the trackers the engine feeds, and
that none of it can reach an order."""

import ast

import pytest

from okxtrader import analysis
from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import SpreadSide

from test_algo_desk import Clock


def closed(pnl, at, peak=None, trough=None, peak_min=None, **kw):
    return dict({'position_id': f'P{at}', 'pair_key': 'A|B', 'side': 'SELL',
                 'opened_at': at - 600, 'closed_at': at, 'realized_pnl': pnl,
                 'peak_pnl': peak, 'trough_pnl': trough, 'peak_min': peak_min,
                 'close_reason': 'manual', 'source': 'ALGO'}, **kw)


# -- the arithmetic ----------------------------------------------------------


def test_the_drawdown_is_the_worst_fall_from_a_peak_in_closing_order():
    rows = [closed(10, 1), closed(-4, 2), closed(-8, 3), closed(5, 4)]
    d = analysis.drawdown(rows)
    assert d['peak_equity'] == 10 and d['max'] == 12
    assert d['current'] == 7 and d['max_pct'] == 120.0


def test_no_closed_trade_is_no_drawdown_not_a_zero():
    d = analysis.drawdown([])
    assert d['max'] is None and d['peak_equity'] is None
    s = analysis.summary([])
    assert s['net_pnl'] is None and s['win_rate'] is None


def test_the_summary_leaves_an_unpriced_close_out_of_the_win_rate():
    rows = [closed(5, 1), closed(-2, 2), closed(None, 3),
            dict(closed(0, 4), closed_at=None)]
    s = analysis.summary(rows)
    assert s['count'] == 4 and s['closed'] == 2 and s['open'] == 1
    assert s['win_rate'] == 50.0 and s['net_pnl'] == 3


def test_a_trade_keeps_its_best_and_worst_point_and_when():
    class P:
        opened_at = 0.0
        peak_pnl = peak_min = trough_pnl = trough_min = None
    p = P()
    assert analysis.observe_extremes(p, -3.0, 60.0)
    assert analysis.observe_extremes(p, 7.5, 300.0)
    assert not analysis.observe_extremes(p, 2.0, 400.0)       # inside
    assert (p.peak_pnl, p.peak_min) == (7.5, 5.0)
    assert (p.trough_pnl, p.trough_min) == (-3.0, 1.0)
    # Unmeasured changes nothing.
    assert not analysis.observe_extremes(p, None, 500.0)


def test_mae_mfe_and_how_much_of_its_best_a_trade_kept():
    [row] = analysis.excursion_rows([closed(4.0, 1, peak=10.0, trough=-6.0,
                                            peak_min=12.0)])
    assert row['mae'] == 6.0 and row['mfe'] == 10.0
    assert row['utilisation_pct'] == 40.0 and row['peak_min'] == 12.0


def test_a_loser_has_no_kept_percentage():
    [row] = analysis.excursion_rows([closed(-4.0, 1, peak=10.0, trough=-6.0)])
    assert row['utilisation_pct'] is None


def test_a_trade_from_before_extremes_were_kept_is_left_out():
    assert analysis.excursion_rows([closed(4.0, 1)]) == []
    assert analysis.calibration([closed(4.0, 1)])['n'] == 0


# -- what was traded, and why it ended ---------------------------------------

#: The levels an Algo position is judged against, as the engine keeps
#: them on it. A SELL: break-even 71.20, stop ABOVE, target BELOW.
SELL_LEVELS = {'break_even': 71.20, 'sl': 71.40, 'tp': 70.55,
               'sl_money': -205.0, 'tp_money': 487.0,
               'stop_mode': 'PCT', 'target_mode': 'PCT',
               'stop_loss_pct': 2.0, 'tp_pct': 1.5,
               'entry_margin': 4720.0, 'entry_atr': 0.41}


def traded(reason, exit_spread, levels=None, side='SELL', **kw):
    return closed(-10.0, 1, peak=1.0, trough=-20.0, side=side,
                  close_reason=reason, entry_spread=71.00,
                  exit_spread=exit_spread, quantity=1.0,
                  exit_levels=SELL_LEVELS if levels is None else levels,
                  **dict({'leg_a': {'volume': 0.10},
                          'leg_b': {'volume': 0.25}}, **kw))


def test_the_journal_carries_the_numbers_behind_the_exit():
    """The journal already said WHAT was traded and at what. What it
    could not say is why it ended, in numbers."""
    [row] = analysis.journal([traded('Algo: stop loss', 71.46)])
    assert row['entry_spread'] == 71.00 and row['exit_spread'] == 71.46
    assert row['leg_a']['volume'] == 0.10 and row['leg_b']['volume'] == 0.25
    assert row['exit']['sl'] == 71.40
    assert row['cum_pnl'] == -10.0


def test_the_extremes_table_does_not_repeat_the_journals_columns():
    """One page saying the same thing twice is one page nobody reads
    twice: the journal carries the size, this table carries the
    extremes."""
    [row] = analysis.excursion_rows([traded('Algo: stop loss', 71.46)])
    assert 'quantity' not in row and 'exit' not in row
    assert row['mae'] == 20.0 and row['mfe'] == 1.0


def test_an_unrecorded_leg_is_no_lot_size_rather_than_zero():
    [row] = analysis.excursion_rows([traded('Algo: stop loss', 71.46,
                                            leg_a=None, leg_b=None)])
    assert row['lots_a'] is None and row['lots_b'] is None


def test_the_three_levels_are_columns_and_the_one_that_fired_is_marked():
    """They were one prose line under the row first. A journal is a
    table: a number worth printing is worth lining up down the page."""
    x = analysis.exit_columns(
        {'side': 'SELL', 'close_reason': 'Algo: stop loss',
         'exit_spread': 71.46, 'exit_levels': SELL_LEVELS})
    assert x['break_even'] == 71.20 and x['tp'] == 70.55 and x['sl'] == 71.40
    # Which of the three this exit was measured against - so the screen
    # marks that one cell rather than all three.
    assert x['on_level'] == 'sl'
    assert x['sized_by'] == '2% of margin (4,720.00/spread)'
    assert x['level_money'] == -205.0
    assert x['gap'] == 0.06 and x['gap_word'] == 'past'
    assert x['note'] is None


def test_how_far_past_is_measured_the_right_way_round_for_a_long():
    """A long's stop sits BELOW its break-even. Signed the other way,
    every stop reads as one that was never reached."""
    levels = dict(SELL_LEVELS, sl=70.90, tp=71.90)
    x = analysis.exit_columns(
        {'side': 'BUY', 'close_reason': 'Algo: stop loss',
         'exit_spread': 70.85, 'exit_levels': levels})
    assert x['on_level'] == 'sl'
    assert x['gap'] == 0.05 and x['gap_word'] == 'past'


def test_a_target_missed_by_a_tick_says_SHORT_rather_than_past():
    """That gap is the slippage the take-profit re-check exists for,
    and calling it 'past' would hide exactly that."""
    x = analysis.exit_columns(
        {'side': 'SELL', 'close_reason': 'Algo: profit target (after costs)',
         'exit_spread': 70.60, 'exit_levels': SELL_LEVELS})
    assert x['on_level'] == 'tp'
    assert x['gap'] == 0.05 and x['gap_word'] == 'short of'
    assert x['sized_by'] == '1.5% of margin (4,720.00/spread)'


def test_an_atr_stop_names_the_multiple_and_the_atr_it_was_sized_from():
    levels = dict(SELL_LEVELS, stop_mode='ATR', atr_stop_mult=2.5,
                  entry_atr=0.16, sl=71.60)
    x = analysis.exit_columns(
        {'side': 'SELL', 'close_reason': 'Algo: stop loss',
         'exit_spread': 71.62, 'exit_levels': levels})
    assert x['sized_by'] == '2.5 \u00d7 ATR 0.16'


def test_a_trade_closed_before_the_levels_were_kept_says_so():
    """Unmeasured is not zero, and it is not today's settings either:
    a level invented afterwards would read as the one it was stopped
    on."""
    x = analysis.exit_columns({'side': 'SELL',
                               'close_reason': 'Algo: stop loss',
                               'exit_spread': 71.46})
    assert x['sl'] is None and x['tp'] is None and x['break_even'] is None
    assert 'not recorded' in x['note']
    assert x['gap'] is None


def test_an_exit_that_was_not_a_level_marks_no_level():
    """A cutoff, a kill and a close by hand are not a level being
    touched. The levels are still shown - they were what the trade was
    being judged against - but none of them is marked as the one that
    fired, and there is no distance to one."""
    for reason in ('OVERNIGHT_CLOSE', 'closed by the trader',
                   'force-cleared: leg A not at the broker after 3 checks'):
        x = analysis.exit_columns({'side': 'SELL', 'close_reason': reason,
                                   'exit_spread': 71.46,
                                   'exit_levels': SELL_LEVELS})
        assert x['on_level'] is None
        assert x['gap'] is None and x['sized_by'] is None
        assert x['break_even'] == 71.20          # still worth seeing


def test_calibration_reads_the_peaks():
    rows = [closed(1, i, peak=float(i), peak_min=float(i)) for i in
            range(1, 11)]
    c = analysis.calibration(rows)
    assert c['n'] == 10 and c['peak_pctile']['50'] == pytest.approx(5.5)
    assert c['suggested_take'] == pytest.approx(analysis.percentile(
        [float(i) for i in range(1, 11)], 65))
    assert c['suggested_max_hold_min'] == pytest.approx(5.5)


def test_the_journal_runs_its_total_in_closing_order_newest_first():
    rows = [closed(5, 1), closed(-2, 2), closed(3, 3)]
    trips = analysis.journal(rows)
    assert [t['cum_pnl'] for t in trips] == [6, 3, 5]
    assert trips[0]['held_sec'] == 600


def test_the_algos_z_is_matched_to_its_position_at_both_ends():
    events = [
        {'kind': 'algo_signal', 'at': 100.0, 'pair_key': 'A|B',
         'detail': {'action': 'ENTER', 'z': 1.49}},
        {'kind': 'algo_order', 'at': 101.0, 'pair_key': 'A|B',
         'detail': {'action': 'ENTER', 'ok': True, 'position_id': 'P9'}},
        {'kind': 'algo_signal', 'at': 900.0, 'pair_key': 'A|B',
         'detail': {'action': 'EXIT', 'z': -1.58, 'position_id': 'P9'}},
    ]
    entry, exit_ = analysis.algo_z(events)
    assert entry == {'P9': 1.49} and exit_ == {'P9': -1.58}


def test_an_entry_order_long_after_the_signal_is_not_matched_to_it():
    events = [
        {'kind': 'algo_signal', 'at': 100.0, 'pair_key': 'A|B',
         'detail': {'action': 'ENTER', 'z': 1.49}},
        {'kind': 'algo_order', 'at': 500.0, 'pair_key': 'A|B',
         'detail': {'action': 'ENTER', 'ok': True, 'position_id': 'P9'}},
    ]
    assert analysis.algo_z(events)[0] == {}


# -- the z-score excursions --------------------------------------------------


def test_a_spread_riding_the_band_is_one_touch_not_a_hundred():
    ex = analysis.ZExcursions(now=0.0)
    for i in range(50):
        ex.tally(2.3, float(i))
    assert ex.counts['touch_2_up'] == 1


def test_the_CONTROL_back_inside_one_sigma_it_counts_again():
    ex = analysis.ZExcursions(now=0.0)
    for z in (2.3, 0.5, 2.1):
        ex.tally(z, 0.0)
    assert ex.counts['touch_2_up'] == 2


def test_a_two_sigma_stretch_that_comes_back_through_zero_is_a_reversion():
    ex = analysis.ZExcursions(now=0.0)
    for z in (1.0, 2.2, 3.1, 1.5, 0.2, -0.1):
        ex.tally(z, 0.0)
    assert ex.counts == {'touch_2_up': 1, 'touch_2_down': 0,
                         'touch_3_up': 1, 'touch_3_down': 0, 'reversions': 1}
    assert ex.max_z == 3.1
    # A small wobble through zero, never stretched, is not one.
    ex2 = analysis.ZExcursions(now=0.0)
    for z in (0.5, -0.5, 0.5):
        ex2.tally(z, 0.0)
    assert ex2.counts['reversions'] == 0


# -- the what-if-held shadow -------------------------------------------------


class Pos:
    position_id = 'P1'
    pair_key = 'A|B'
    side = SpreadSide.SELL
    entry_spread = 13.196
    spread_units = 50.0
    close_reason = 'Algo: stop loss'
    realized_pnl = -9.5


def test_a_shadow_marks_the_closed_trade_on_its_own_entry():
    w = analysis.arm_shadow(Pos(), fees=0.0, target_net=3.83, now=0.0)
    assert analysis.update_shadow(w, 13.30, 60.0)            # still losing
    assert not w['reverted_be']
    analysis.update_shadow(w, 13.19, 600.0)                  # back past BE
    assert w['reverted_be'] and w['be_min'] == 10.0
    analysis.update_shadow(w, 13.10, 900.0)                  # +4.80: target
    assert w['reverted_target'] and w['peak_net'] == pytest.approx(4.8)
    analysis.update_shadow(w, None, 3600.0)
    assert w['done']
    s = analysis.shadow_summary([w])
    assert s['revert_target_rate'] == 100.0 and s['active'] == 0


def test_a_clean_target_is_not_shadowed_and_the_CONTROL_a_stop_is():
    clean = Pos()
    clean.close_reason = 'Algo: profit target (after costs)'
    assert analysis.arm_shadow(clean, 0.0, None, 0.0) is None
    assert analysis.arm_shadow(Pos(), 0.0, None, 0.0) is not None


# -- the engine feeds it -----------------------------------------------------


def engine(config, legs, tmp_path):
    store = Store(str(tmp_path / 'trader.db'))
    clock = Clock()
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=clock, store=store)
    coordinator.start()
    coordinator.poll_once()
    return coordinator, store, clock


def opened(coordinator, pair, side=SpreadSide.BUY):
    md = coordinator.market[pair.key]
    position = coordinator.executor.market_entry(pair, side, md, 1.0).position
    coordinator.book.add_position(position)
    return position


def move_b(legs, pair, by):
    symbol = legs['acct_b'].broker.symbols[pair.symbol_b]
    symbol.quote(symbol.bid + by, symbol.ask + by)


def test_the_engine_keeps_each_open_trades_best_and_worst(config, pair, legs,
                                                         tmp_path):
    coordinator, store, clock = engine(config, legs, tmp_path)
    position = opened(coordinator, pair)                 # long the spread
    coordinator.poll_once()
    move_b(legs, pair, +2.0)                             # B up: it gains
    clock.now += 60
    coordinator.poll_once()
    move_b(legs, pair, -4.0)
    clock.now += 60
    coordinator.poll_once()
    assert position.peak_pnl > 0 > position.trough_pnl
    assert position.peak_min == 1.0 and position.trough_min == 2.0
    # Saved, so a restart and the tab both have them.
    [row] = store.open_positions()
    assert row['trough_pnl'] == position.trough_pnl


def test_the_levels_a_position_is_judged_against_are_kept_on_it(
        config, pair, legs, tmp_path):
    """And survive its close, which is the only moment they matter.

    The Analysis tab has to be able to say WHICH stop a trade was
    stopped on. Recomputing it afterwards from today's settings would
    print a level the trade was never measured against.
    """
    coordinator, store, clock = engine(config, legs, tmp_path)
    position = opened(coordinator, pair)
    coordinator.poll_once()
    levels = position.exit_levels
    assert levels and levels['break_even'] is not None
    # Saved with the position, not only held in memory.
    [row] = store.open_positions()
    assert row['exit_levels']['break_even'] == levels['break_even']

    coordinator.executor.close_position(pair, position,
                                        coordinator.market[pair.key],
                                        reason='Algo: stop loss')
    clock.now += 5
    coordinator.poll_once()
    closed_row = store.position(position.position_id)
    assert closed_row['closed_at'] is not None
    assert closed_row['exit_levels']['break_even'] == levels['break_even']


def test_a_ladder_that_cannot_be_priced_keeps_the_levels_it_had(
        config, pair, legs, tmp_path):
    """CONTROL. A pass with no market must not wipe them: the last set
    is the one the exit was decided on."""
    coordinator, store, clock = engine(config, legs, tmp_path)
    position = opened(coordinator, pair)
    coordinator.poll_once()
    before = dict(position.exit_levels)
    coordinator._track_positions(pair, None, {}, clock.now)
    assert position.exit_levels == before


def test_a_close_that_is_not_the_target_arms_a_shadow(config, pair, legs,
                                                       tmp_path):
    coordinator, store, clock = engine(config, legs, tmp_path)
    position = opened(coordinator, pair)
    coordinator.poll_once()
    coordinator.executor.close_position(pair, position,
                                        coordinator.market[pair.key],
                                        reason='manual')
    clock.now += 5
    coordinator.poll_once()
    [watch] = store.shadows()
    assert watch['position_id'] == position.position_id
    assert watch['done'] is False


def test_the_CONTROL_a_clean_target_arms_none(config, pair, legs, tmp_path):
    coordinator, store, clock = engine(config, legs, tmp_path)
    position = opened(coordinator, pair)
    coordinator.poll_once()
    coordinator.executor.close_position(
        pair, position, coordinator.market[pair.key],
        reason='Algo: profit target (after costs)')
    coordinator.poll_once()
    assert store.shadows() == []


def test_excursions_are_counted_on_the_algos_band_and_reset(config, pair, legs,
                                                            tmp_path):
    coordinator, store, clock = engine(config, legs, tmp_path)
    md = dict(coordinator.market[pair.key])
    mean = md['mid_spread']
    reading = {'z': (md['mid_spread'] - (mean - 0.25)) / 0.1,
               'mean': mean - 0.25, 'sigma': 0.1}
    coordinator._track_excursions(pair, md, reading, clock.now)
    saved = store.excursions()[pair.key]
    assert saved['counts']['touch_2_up'] == 1
    # Nothing MEASURED counts nothing - which is NOT the same as the
    # Algo being off. The counters are fed from the analysis band now
    # (see `_band_reading`), so they count with its Algo on or off.
    coordinator._track_excursions(pair, md, None, clock.now)
    coordinator._track_excursions(pair, md, {'z': None}, clock.now)
    assert store.excursions()[pair.key]['counts']['touch_2_up'] == 1
    coordinator.reset_excursions(pair.key)
    assert store.excursions()[pair.key]['counts']['touch_2_up'] == 0


def test_the_counters_survive_a_restart_through_the_engine(config, pair, legs,
                                                           tmp_path):
    """The engine-level control on the arming fix: the tracker is
    dropped and rebuilt from the database exactly as `_excursion` does
    at first use, and the parked spread is still one touch."""
    coordinator, store, clock = engine(config, legs, tmp_path)
    md = dict(coordinator.market[pair.key])
    mean = md['mid_spread'] - 0.25
    reading = {'z': 2.5, 'mean': mean, 'sigma': 0.1}
    for _ in range(4):
        coordinator._excursions.clear()
        coordinator._excursions_saved.clear()
        coordinator._track_excursions(pair, md, reading, clock.now)
    assert store.excursions()[pair.key]['counts']['touch_2_up'] == 1
    # CONTROL: a genuine new stretch, after a return to the mean, counts.
    coordinator._track_excursions(pair, md, {'z': 0.2, 'mean': mean,
                                             'sigma': 0.1}, clock.now)
    coordinator._track_excursions(pair, md, reading, clock.now)
    assert store.excursions()[pair.key]['counts']['touch_2_up'] == 2


def test_the_analysis_cannot_reach_an_order():
    path = analysis.__file__
    tree = ast.parse(open(path, encoding='utf-8').read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add((node.module or '') + ':' + ','.join(
                a.name for a in node.names))
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert imported <= {'math'}, imported


def test_the_journal_splits_each_trip_into_gross_and_fees():
    row = closed(4.0, 1, entry_spread=13.20, exit_spread=13.10,
                 spread_units=50.0)                    # SELL: +0.10 x 50
    [trip] = analysis.journal([row])
    assert trip['gross_pnl'] == 5.0 and trip['fees'] == 1.0
    # Unmeasured stays unmeasured, not zero.
    [trip] = analysis.journal([closed(4.0, 1)])
    assert trip['gross_pnl'] is None and trip['fees'] is None


def test_a_parked_spread_does_not_recount_its_touch_after_a_restart():
    """The counts were saved and the ARMING that made them safe to add
    to was not.

    A touch is counted on the OUTWARD crossing and then disarmed until
    z is back inside +-1. That arming lived only in memory, so every
    restart re-armed all four bands: a spread parked beyond +2 sigma
    logged a fresh touch on the first poll after each one. Six
    restarts, six touches, one stretch.
    """
    saved = None
    for poll in range(6):
        # A restart: the tracker is rebuilt from what was persisted.
        tracker = analysis.ZExcursions(saved, 0.0)
        tracker.tally(2.3, poll)
        saved = tracker.to_dict()
    assert saved['counts']['touch_2_up'] == 1


def test_control_a_genuine_second_stretch_is_still_counted():
    """The fix must not silence a real excursion: back inside +-1 and
    out again is two touches, restart or no restart."""
    tracker = analysis.ZExcursions(None, 0.0)
    for at, z in enumerate((2.3, 0.4, 2.4)):
        tracker.tally(z, at)
    assert tracker.counts['touch_2_up'] == 2
    # And across a restart, the same.
    saved = tracker.to_dict()
    again = analysis.ZExcursions(saved, 0.0)
    for at, z in enumerate((0.3, 2.5), start=10):
        again.tally(z, at)
    assert again.counts['touch_2_up'] == 3


def test_the_trade_journal_has_no_running_total_before_anything_is_priced():
    """The same rule as the signal journal: a first trade whose P&L
    could not be read printed $0.00, which reads as 'it broke even'."""
    rows = [closed(None, 1, peak=1.0, trough=-1.0),
            closed(None, 2, peak=1.0, trough=-1.0)]
    assert [r['cum_pnl'] for r in analysis.journal(rows)] == [None, None]


def test_and_a_real_total_from_the_first_priced_trade_on():
    rows = [closed(None, 1, peak=1.0, trough=-1.0),
            closed(-205.0, 2, peak=1.0, trough=-1.0),
            closed(None, 3, peak=1.0, trough=-1.0)]
    assert [r['cum_pnl'] for r in analysis.journal(rows)] == \
        [-205.0, -205.0, None]
