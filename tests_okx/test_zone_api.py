"""The signal track over the API: one ladder, one day, filtered in the
QUERY rather than by handing the browser everything.
"""

import json
import time

import pytest

from okxtrader.database import Store
from okxtrader.webapp import create_app

DAY = 86400.0


@pytest.fixture
def desk(tmp_path):
    paths = {'status': str(tmp_path / 'status.json'),
             'commands': str(tmp_path / 'commands.jsonl'),
             'results': str(tmp_path / 'results.json'),
             'config': str(tmp_path / 'config.json'),
             'db': str(tmp_path / 'trader.db')}
    with open(paths['config'], 'w', encoding='utf-8') as handle:
        json.dump({'pairs': {'A|B': {'name': 'A vs B'},
                             'C|D': {'name': 'C vs D'}}}, handle)
    store = Store(paths['db'])
    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'], paths['db'])
    app.config.update(TESTING=True)
    return app.test_client(), store


def write(store, pair_key, age_days, zone, now, **kw):
    store.save_zone_event(zones.event_row(
        pair_key, now - age_days * DAY, kw.pop('z', 2.6), zone,
        kw.pop('previous', zones.MEAN), 13.0, 0.1,
        {'entry_z': 2.5, 'max_entry_z': 3.5, 'stop_z': 4.0, 'length': 20},
        timeframe_sec=900.0, **kw))


# -- the date picker, and the signals ----------------------------------------

def write_signal(store, pair_key, day, basis='ALGO', outcome='TP',
                 armed_at=None, **kw):
    row = dict({
        'pair_key': pair_key, 'basis': basis, 'side': 'SELL',
        'broker_day': day, 'threshold': 2.0, 'state': 'CLOSED',
        'outcome': outcome, 'armed_at': armed_at or time.time(),
        'armed_z': 2.6, 'entered_at': armed_at or time.time(),
        'entered_z': 2.6, 'entry': 13.25, 'break_even': 13.05,
        'tp': 12.55, 'sl': 13.55, 'units': 50.0, 'reverted': True,
        'revert_min': 22.0, 'held_min': 40.0, 'entry_z': 2.5}, **kw)
    return store.save_signal(row)


def test_a_particular_day_can_be_asked_for_on_its_own(desk):
    """'How many signals on 5 Nov for Gold' - the question asked."""
    client, store = desk
    write_signal(store, 'A|B', '2026-11-05')
    write_signal(store, 'A|B', '2026-11-05')
    write_signal(store, 'A|B', '2026-11-04')
    body = client.get('/api/analysis?date=2026-11-05').get_json()
    assert body['ok'] and body['date'] == '2026-11-05'
    assert len(body['signals']) == 2


def test_a_day_and_an_instrument_together(desk):
    client, store = desk
    write_signal(store, 'A|B', '2026-11-05')
    write_signal(store, 'C|D', '2026-11-05')
    body = client.get('/api/analysis?date=2026-11-05&pair=A%7CB').get_json()
    assert [r['pair_key'] for r in body['signals']] == ['A|B']


def test_the_day_beats_the_rolling_window_when_both_are_given(desk):
    """A day and 'the last 24 hours' are different questions, and
    different answers near a boundary."""
    client, store = desk
    write_signal(store, 'A|B', '2020-01-02',
                 armed_at=time.time() - 900 * DAY)
    body = client.get('/api/analysis?zone_days=1&date=2020-01-02').get_json()
    assert len(body['signals']) == 1       # the rolling day would show none


def test_a_date_that_is_not_a_date_is_refused_in_words(desk):
    client, _store = desk
    body = client.get('/api/analysis?date=5-Nov').get_json()
    assert body['ok'] is False and 'YYYY-MM-DD' in body['error']


def test_the_days_that_have_signals_are_published_for_the_picker(desk):
    client, store = desk
    write_signal(store, 'A|B', '2026-11-05')
    write_signal(store, 'A|B', '2026-11-05')
    write_signal(store, 'A|B', '2026-11-03')
    days = client.get('/api/analysis').get_json()['signal_days']
    assert days == [{'day': '2026-11-05', 'rows': 2},
                    {'day': '2026-11-03', 'rows': 1}]


def test_the_tally_counts_what_the_tiles_show(desk):
    client, store = desk
    for outcome, net in (('TP', 30.0), ('TP', 18.0), ('SL', -44.0)):
        write_signal(store, 'A|B', '2026-11-05', outcome=outcome,
                     net_pnl=net, reverted=outcome == 'TP')
    write_signal(store, 'A|B', '2026-11-05', outcome='NO_ENTRY',
                 state='CLOSED', entered_at=None, entered_z=None)
    body = client.get('/api/analysis?date=2026-11-05').get_json()
    tally = body['signal_tally']
    assert tally['armed'] == 4 and tally['entered'] == 3
    assert tally['no_entry'] == 1
    # Won and lost are counted in MONEY, not by which level was touched.
    assert tally['tp'] == 2 and tally['sl'] == 1
    assert tally['net_pnl'] == pytest.approx(4.0)
    assert tally['win_rate'] == pytest.approx(66.7)


def test_a_day_with_nothing_is_an_empty_list_not_an_error(desk):
    client, _store = desk
    body = client.get('/api/analysis?date=1999-01-01').get_json()
    assert body['ok'] and body['signals'] == []


def test_the_api_serves_the_signals_in_journal_shape(desk):
    """The page reads the same shape for signals as for trades: newest
    first, each row saying why it ended, with a running total."""
    client, store = desk
    write_signal(store, 'A|B', '2026-11-05', outcome='SL', net_pnl=-205.0,
                 closed_at=time.time(), break_even=71.20, tp=70.55,
                 sl=71.40, exit_spread=71.46, quantity=1.0,
                 leg_a_side='BUY', leg_a_entry=101.118,
                 leg_b_side='SELL', leg_b_entry=115.554)
    write_signal(store, 'A|B', '2026-11-05', outcome='BLOCKED',
                 net_pnl=None, closed_at=time.time() + 60,
                 block_reason='edge filter: capture is 0.02x',
                 edge_ratio=0.02, edge_required=1.5)
    rows = client.get('/api/analysis?date=2026-11-05').get_json()['signals']
    assert [r['outcome'] for r in rows] == ['BLOCKED', 'SL']
    # The refusal carries no money, so the total does not move for it.
    assert [r['cum_pnl'] for r in rows] == [-205.0, -205.0]
    # The levels are COLUMNS on the row, not a sentence under it.
    assert rows[1]['sl'] == 71.40 and rows[1]['break_even'] == 71.20
    assert rows[1]['leg_a_entry'] == 101.118
    # ...and the WHY column keeps only what is not a number.
    assert 'edge 0.02x' in rows[0]['why']
