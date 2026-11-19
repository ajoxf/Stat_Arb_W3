"""Signals: the bookkeeping of the signal journal's rows.

WHAT IS NOT HERE ANY MORE, and why. This file used to test an
arm-then-enter machine and a stop-and-target check that lived in
`signals.py` as a copy of the Algo's rules. The copy did not match the
Algo - on a live desk it showed dozens of trades in an afternoon in
which the real Algo took none - and it is gone: what arms, what enters
and what exits is decided by the Algo's own code (`shadow.ShadowAlgo`,
tested in `test_signal_journal.py`), and its rules are tested where
they live, in `test_algo_signal.py`. What remains here is arithmetic on
rows: a refusal noted, a stretch settled, a trade marked and closed,
the tally and the journal.
"""

import pytest

from okxtrader import algo as algo_module, signals


PARAMS = dict(algo_module.DEFAULT_PARAMS)      # entry_z 2.5, back 0.5, 50%


# -- the row's life, decided elsewhere ---------------------------------------

def entered(side='SELL', be=13.0, tp=12.5, sl=13.4, units=50.0, at=0.0):
    """A row the Algo entered: armed, then entered at break-even."""
    sig = signals.new_signal('A|B', signals.ALGO, side, at, 2.6, PARAMS)
    signals.enter(sig, at + 60.0, 1.8,
                  {'entry': be, 'break_even': be, 'tp': tp, 'sl': sl},
                  units)
    return sig


def test_a_row_is_stamped_as_decided_by_the_algos_own_code():
    """The column the purge hangs on: a row without it came from the
    copy of the rules that did not match the Algo."""
    sig = signals.new_signal('A|B', signals.ALGO, 'SELL', 0.0, 2.6, PARAMS)
    assert sig['decided_by'] == signals.DECIDED_BY == 'algo'


def test_a_refusal_is_noted_and_the_row_stays_armed():
    """A block can clear before the stretch is over - a cooldown runs
    out, a feed comes back - and then the entry is what counts. So a
    refusal is NOTED, not settled."""
    sig = signals.new_signal('A|B', signals.ALGO, 'SELL', 0.0, 2.6, PARAMS)
    signals.note_block(sig, 'cooldown 4:40')
    assert sig['state'] == signals.ARMED and sig['outcome'] is None
    assert sig['block_reason'] == 'cooldown 4:40'
    assert signals.why(sig) == 'held back: cooldown 4:40'


def test_a_stretch_that_ends_held_back_settles_as_blocked():
    sig = signals.new_signal('A|B', signals.ALGO, 'SELL', 0.0, 2.6, PARAMS)
    signals.note_block(sig, 'edge filter: capture 0.34x the cost, under '
                            'the 1x required', {'ratio': 0.34,
                                                'required': 1.0})
    signals.settle_unentered(sig, 60.0, 'never confirmed')
    assert sig['outcome'] == signals.BLOCKED
    assert sig['edge_ratio'] == 0.34 and sig['edge_required'] == 1.0
    assert sig['net_pnl'] is None              # a refusal is never a loss
    assert 'edge 0.34x of 1.00x' in signals.why(sig)


def test_CONTROL_a_stretch_nothing_refused_settles_as_not_confirmed():
    """...and says why in the Algo's terms, not as a refusal."""
    sig = signals.new_signal('A|B', signals.ALGO, 'SELL', 0.0, 2.6, PARAMS)
    signals.settle_unentered(sig, 60.0, 'came back through the window '
                                        'before a fresh price confirmed it')
    assert sig['outcome'] == signals.NO_ENTRY
    assert signals.why(sig).startswith('came back through the window')


def test_marking_decides_nothing():
    """A price past the stop does NOT close the row here. When a trade
    comes off is the Algo's decision; this used to carry its own stop
    check, and a copy of a rule is a rule that drifts."""
    sig = entered('SELL', be=13.0, tp=12.5, sl=13.4)
    signals.mark(sig, 13.9, 3.5, 120.0)        # far past the stop
    assert sig['state'] == signals.OPEN and sig['outcome'] is None
    assert sig['trough_net'] == -45.0          # but the worst is kept


def test_the_best_and_worst_point_are_both_kept():
    sig = entered('SELL', be=13.0)
    signals.mark(sig, 12.8, 1.0, 120.0)        # +10
    signals.mark(sig, 13.2, 2.2, 180.0)        # -10
    signals.mark(sig, 13.0, 1.6, 240.0)
    assert sig['peak_net'] == 10.0 and sig['trough_net'] == -10.0


def test_coming_back_to_the_mean_is_recorded_beside_the_outcome():
    sig = entered('SELL', at=0.0)
    signals.mark(sig, 12.9, 0.4, 600.0)
    assert not sig['reverted']
    signals.mark(sig, 12.8, -0.1, 1380.0)      # crossed: 22 min after entry
    assert sig['reverted'] and sig['revert_min'] == 22.0


def test_an_unpriced_trade_is_marked_without_inventing_a_number():
    sig = entered('SELL', be=None, units=None)
    signals.mark(sig, 12.8, 0.4, 120.0)
    assert sig['peak_net'] is None and sig['trough_net'] is None


def test_the_algos_exit_closes_the_row_in_its_own_words():
    sig = entered('SELL', be=13.0)
    signals.close(sig, 660.0, 'PROFIT_TARGET', 12.4, 0.1)
    assert sig['outcome'] == signals.TP
    assert sig['exit_reason'] == 'profit target (after costs)'
    assert sig['exit_spread'] == 12.4
    # break-even 13.0, sold, closed at 12.4, k of 50
    assert sig['net_pnl'] == 30.0
    assert sig['held_min'] == 10.0


def test_only_the_target_and_the_stop_say_a_level_was_reached_live():
    """The Algo's other exits closed the trade for another reason
    entirely; calling them a take-profit would overstate the page."""
    for reason, outcome in (('STOP_LOSS', signals.SL),
                            ('Z_STOP', signals.EXIT),
                            ('MEAN_REVERSION', signals.EXIT),
                            ('TIME_STOP', signals.EXIT)):
        sig = entered('SELL')
        signals.close(sig, 660.0, reason, 12.9, 0.4)
        assert sig['outcome'] == outcome, reason
    assert sig['exit_reason'] == 'time stop'


def test_the_tally_counts_armed_entered_and_how_they_ended():
    rows = []
    for outcome, net in ((signals.TP, 30.0), (signals.TP, 18.0),
                         (signals.SL, -44.0), (signals.EXIT, -3.0)):
        sig = entered('SELL', tp=12.5, sl=13.4)
        sig['state'], sig['outcome'] = signals.CLOSED, outcome
        sig['net_pnl'] = net
        sig['reverted'] = outcome != signals.SL
        rows.append(sig)
    never = signals.new_signal('A|B', signals.ALGO, 'SELL', 0.0, 2.6,
                               PARAMS)
    signals.settle_unentered(never, 10.0, 'not confirmed')
    rows.append(never)
    body = signals.tally(rows)
    assert body['armed'] == 5 and body['entered'] == 4
    assert body['no_entry'] == 1 and body['settled'] == 4
    # Won and lost are by MONEY: the trade the Algo's time stop closed
    # at -$3 is a loss, however it got out.
    assert body['tp'] == 2 and body['sl'] == 2
    assert body['win_rate'] == 50.0
    assert body['reverted'] == 3 and body['revert_rate'] == 75.0


def test_nothing_settled_is_a_dash_and_never_a_zero_per_cent():
    """A win rate of 0% on no trades is a statement nobody measured."""
    sig = entered('SELL', tp=12.5, sl=13.4)
    body = signals.tally([sig])
    assert body['entered'] == 1 and body['settled'] == 0
    assert body['win_rate'] is None and body['revert_rate'] is None


def test_nothing_at_all_still_reports_every_figure():
    body = signals.tally([])
    assert body['armed'] == 0 and body['entered'] == 0
    assert body['win_rate'] is None and body['revert_rate'] is None


# -- derived from history ----------------------------------------------------

def replay_trade(**kw):
    return dict({'side': 'SELL', 'opened_at': 1000.0, 'entry': 13.2,
                 'entry_z': 2.61, 'closed_at': 4600.0, 'exit': 12.7,
                 'reason': 'PROFIT_TARGET', 'pnl': 24.5}, **kw)


def test_a_replayed_trade_becomes_a_signal_that_says_it_is_derived():
    row = signals.from_replay(replay_trade(), 'A|B', PARAMS,
                              broker_day='2026-11-05', units=50.0)
    assert row['source'] == signals.REPLAY
    assert row['basis'] == signals.ALGO
    assert row['broker_day'] == '2026-11-05'
    assert row['state'] == signals.CLOSED
    assert row['outcome'] == signals.TP
    assert row['net_pnl'] == 24.5
    assert row['held_min'] == 60.0
    assert row['armed_z'] == 2.61 and row['entered_z'] == 2.61


def test_only_the_target_and_the_stop_say_a_level_was_reached():
    """The Algo's other exits closed the trade for another reason
    entirely; calling them a take-profit would overstate the page."""
    assert signals.from_replay(replay_trade(reason='PROFIT_TARGET'),
                               'A|B', PARAMS)['outcome'] == signals.TP
    assert signals.from_replay(replay_trade(reason='STOP_LOSS'),
                               'A|B', PARAMS)['outcome'] == signals.SL
    for other in ('Z_STOP', 'MEAN_REVERSION', 'TIME_STOP'):
        row = signals.from_replay(replay_trade(reason=other), 'A|B', PARAMS)
        assert row['outcome'] == signals.EXIT
        assert row['exit_reason'] == signals.EXIT_WORDS[other]
        # Decided by the Algo's own code: the backtest runs AlgoSignal.
        assert row['decided_by'] == signals.DECIDED_BY


def test_a_replay_that_ran_out_of_candles_is_open_not_a_result():
    row = signals.from_replay(
        replay_trade(closed_at=None, pnl=None, reason='still open'),
        'A|B', PARAMS)
    assert row['state'] == signals.OPEN
    assert row['outcome'] is None and row['net_pnl'] is None


def test_won_and_lost_are_measured_in_money_not_by_the_level_touched():
    """An exit on the mean reversion in profit is a win however it got
    out. Counting only PROFIT_TARGET would under-report exactly the
    trades the Algo's own exits exist to take."""
    rows = [signals.from_replay(replay_trade(reason=r, pnl=p), 'A|B', PARAMS)
            for r, p in (('PROFIT_TARGET', 30.0), ('MEAN_REVERSION', 12.0),
                         ('STOP_LOSS', -40.0), ('TIME_STOP', -5.0))]
    body = signals.tally(rows)
    assert body['tp'] == 2 and body['sl'] == 2
    assert body['win_rate'] == 50.0
    assert body['net_pnl'] == -3.0


def test_a_trade_whose_pnl_was_never_measured_is_in_neither_count():
    rows = [signals.from_replay(replay_trade(pnl=None), 'A|B', PARAMS),
            signals.from_replay(replay_trade(pnl=10.0), 'A|B', PARAMS)]
    body = signals.tally(rows)
    assert body['settled'] == 2 and body['priced'] == 1
    assert body['tp'] == 1 and body['sl'] == 0
    assert body['win_rate'] == 100.0


def test_the_tally_says_how_many_rows_were_measured_and_how_many_derived():
    live = entered('SELL', tp=12.5, sl=13.4)
    live['state'], live['outcome'], live['net_pnl'] = 'CLOSED', 'TP', 20.0
    rows = [live, signals.from_replay(replay_trade(), 'A|B', PARAMS)]
    body = signals.tally(rows)
    assert body['live'] == 1 and body['replayed'] == 1


# -- the signal journal ------------------------------------------------------

def a_signal(**kw):
    row = {'pair_key': 'A|B', 'side': 'SELL', 'source': signals.LIVE,
           'state': signals.CLOSED, 'outcome': signals.SL,
           'armed_at': 100.0, 'armed_z': 2.61, 'entered_at': 110.0,
           'closed_at': 200.0,
           'closed_z': 3.12, 'entry': 71.0, 'exit_spread': 71.46,
           'break_even': 71.20, 'tp': 70.55, 'sl': 71.40,
           'net_pnl': -205.0, 'reverted': False}
    row.update(kw)
    return row


def test_the_journal_runs_a_total_in_the_order_things_closed():
    rows = [a_signal(closed_at=300.0, net_pnl=50.0),
            a_signal(closed_at=100.0, net_pnl=-20.0),
            a_signal(closed_at=200.0, net_pnl=10.0)]
    out = signals.journal(rows)
    # Newest first on the screen, but the total runs in CLOSING order.
    assert [r['closed_at'] for r in out] == [300.0, 200.0, 100.0]
    assert [r['cum_pnl'] for r in out] == [40.0, -10.0, -20.0]


def test_a_refusal_carries_no_money_and_does_not_move_the_total():
    """A signal the Algo declined is not a loss, and a page that let it
    move the running total would report money nobody could have lost."""
    rows = [a_signal(closed_at=100.0, net_pnl=-20.0),
            a_signal(closed_at=150.0, outcome=signals.BLOCKED, net_pnl=None,
                     block_reason='edge filter: capture is 0.02x',
                     edge_ratio=0.02, edge_required=1.5)]
    out = signals.journal(rows)
    assert [r['cum_pnl'] for r in out] == [-20.0, -20.0]


def test_the_levels_are_columns_on_the_row_itself():
    """They were a prose line under the row first. The signal already
    carries its three levels, so the journal prints them as columns
    and the WHY column keeps only what is not a number."""
    [row] = signals.journal([a_signal()])
    assert row['break_even'] == 71.20
    assert row['tp'] == 70.55 and row['sl'] == 71.40
    assert row['why'] == 'never came back to the mean'


def test_a_signal_that_came_back_says_when():
    [row] = signals.journal([a_signal(reverted=True, revert_min=22.0)])
    assert row['why'] == 'came back to the mean after 22.0 min'


def test_a_refusal_says_the_filter_and_the_reading_behind_it():
    [row] = signals.journal([a_signal(
        outcome=signals.BLOCKED, net_pnl=None, entered_at=None,
        block_reason='edge filter: capture is 0.02x the round trip',
        edge_ratio=0.02, edge_required=1.5)])
    assert 'edge filter' in row['why']
    assert 'edge 0.02x of 1.50x' in row['why']


def test_a_stretch_that_never_came_back_names_the_window():
    [row] = signals.journal([a_signal(
        outcome=signals.NO_ENTRY, net_pnl=None, entered_at=None,
        entry_z=2.0, reentry_back=0.5)])
    assert 'never back inside the window' in row['why']
    assert 'enters 2.00' in row['why'] and 'back 0.50' in row['why']


def test_a_signal_still_running_has_nothing_to_explain():
    """CONTROL: the WHY column is for how it ENDED. One that has not
    ended yet is a dash, not a sentence invented for it."""
    [row] = signals.journal([a_signal(state=signals.OPEN, outcome=None,
                                      entered_at=None, net_pnl=None)])
    assert row['why'] is None


def test_a_close_records_the_spread_it_came_off_at():
    """The journal prints 'spread in -> out'. Without this the out half
    was a dash on every row."""
    signal = signals.new_signal('A|B', signals.ALGO, 'SELL', 10.0, 2.6,
                                {'entry_z': 2.0})
    signals.enter(signal, 11.0, 2.6, {'entry': 71.0, 'break_even': 70.9,
                                      'tp': 70.0, 'sl': 71.5}, 10.0,
                  {'leg_a_side': 'BUY', 'leg_a_entry': 101.118,
                   'leg_b_side': 'SELL', 'leg_b_entry': 115.554}, 1.0)
    assert signal['leg_a_entry'] == 101.118 and signal['quantity'] == 1.0
    signals.close(signal, 12.0, 'STOP_LOSS', 71.6, 3.1)
    assert signal['outcome'] == signals.SL
    assert signal['exit_spread'] == 71.6


# -- how often the z was triggered -------------------------------------------

def test_the_z_count_is_split_by_side_and_names_its_threshold():
    """'How many times did it trigger' is the question the page is for.
    Above and below are different trades, so they are counted apart."""
    rows = [a_signal(armed_z=2.61, entry_z=2.5, side='SELL'),
            a_signal(armed_z=3.12, entry_z=2.5, side='SELL'),
            a_signal(armed_z=-2.63, entry_z=2.5, side='BUY')]
    t = signals.tally(rows)
    assert t['triggered'] == 3
    assert t['triggered_up'] == 2 and t['triggered_down'] == 1
    assert t['threshold_z'] == 2.5
    assert t['peak_z'] == 3.12 and t['trough_z'] == -2.63


def test_a_refusal_and_a_stretch_that_never_entered_still_triggered():
    """The z reached the band: that is the trigger. What the Algo did
    about it afterwards is a different column, and leaving these out
    would under-count exactly the stretches worth knowing about."""
    rows = [a_signal(armed_z=2.61, entry_z=2.5, outcome=signals.BLOCKED,
                     net_pnl=None, entered_at=None),
            a_signal(armed_z=2.70, entry_z=2.5, outcome=signals.NO_ENTRY,
                     net_pnl=None, entered_at=None)]
    t = signals.tally(rows)
    assert t['triggered'] == 2 and t['entered'] == 0


def test_two_ladders_that_disagree_quote_no_threshold():
    """Printing either one would be wrong on half the rows."""
    rows = [a_signal(armed_z=2.61, entry_z=2.5),
            a_signal(armed_z=2.10, entry_z=2.0)]
    assert signals.tally(rows)['threshold_z'] is None
    # The COUNT still works: it is the threshold that cannot be named.
    assert signals.tally(rows)['triggered'] == 2


def test_nothing_triggered_is_no_furthest_z_rather_than_zero():
    t = signals.tally([])
    assert t['triggered'] == 0
    assert t['peak_z'] is None and t['trough_z'] is None


# A second poke at the band is not a second trigger: that is the Algo's
# own arming, and it is tested through the engine, on the Algo's own
# code, in test_signal_journal.py.


def test_a_page_of_refusals_has_no_running_total_rather_than_zero():
    """UNMEASURED IS NOT ZERO. Three signals, none of them entered:
    $0.00 down the Running column reads as 'it broke even', which is a
    measurement nobody made. Seen live on a desk whose edge filter
    refused everything."""
    rows = [a_signal(outcome=signals.BLOCKED, net_pnl=None,
                     entered_at=None, closed_at=100.0),
            a_signal(outcome=signals.NO_ENTRY, net_pnl=None,
                     entered_at=None, closed_at=200.0)]
    assert [r['cum_pnl'] for r in signals.journal(rows)] == [None, None]


def test_the_total_is_real_from_the_first_priced_row_on():
    """CONTROL. Once something has been priced the total is a
    measurement, and a later unpriced row leaves it where it was
    rather than blanking it again."""
    rows = [a_signal(net_pnl=None, outcome=signals.BLOCKED,
                     entered_at=None, closed_at=100.0),
            a_signal(net_pnl=-205.0, closed_at=200.0),
            a_signal(net_pnl=None, outcome=signals.BLOCKED,
                     entered_at=None, closed_at=300.0)]
    # Newest first on the screen.
    assert [r['cum_pnl'] for r in signals.journal(rows)] == \
        [-205.0, -205.0, None]


def test_a_blocked_signal_is_never_counted_as_a_loss():
    """147 refusals read as 147 losses and -$53,245 of P&L that never
    happened. A refusal is not a trade."""
    rows = []
    for _ in range(3):
        row = signals.new_signal('A|B', signals.ALGO, 'SELL', 0.0, 2.6, {})
        signals.note_block(row, 'edge filter: capture 0.02x the cost',
                           {'ratio': 0.02, 'required': 1.5})
        signals.settle_unentered(row, 10.0)
        rows.append(row)
    body = signals.tally(rows)
    assert body['armed'] == 3 and body['blocked'] == 3
    assert body['entered'] == 0 and body['settled'] == 0
    assert body['sl'] == 0 and body['tp'] == 0
    assert body['net_pnl'] is None            # never measured, never zero
    assert body['win_rate'] is None
