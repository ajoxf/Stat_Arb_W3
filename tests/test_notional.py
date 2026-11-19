"""Notional volume traded: lots x contract size x price, on the broker's
own deals - today, over the last 7 days and over the last 30.

Unmeasured is not zero: a deal whose contract size is not known is
counted and said, never priced as nothing.
"""

import json
import time

import pytest

from okxtrader import analysis
from okxtrader.database import Store
from okxtrader.webapp import create_app

NOW = 1_800_000_000.0          # a UTC instant
OFFSET = 2 * 3600              # the broker runs at UTC+2


def deal(hours_ago, volume, price, account='CFI', symbol='USOIL_F7',
         pair_key='USOIL_F7|UKOIL_F7', leg='A'):
    utc = NOW - hours_ago * 3600.0
    return {'account': account, 'symbol': symbol, 'volume': volume,
            'price': price, 'pair_key': pair_key, 'leg': leg,
            'broker_time_ms': int((utc + OFFSET) * 1000),
            'server_offset_s': OFFSET}


SIZES = {('CFI', 'USOIL_F7'): 1000.0, ('AC', 'UKOIL_F7'): 100.0}


def test_a_deal_is_lots_times_contract_size_times_price():
    out = analysis.notional_traded([deal(1, 0.5, 60.0)], SIZES, NOW)
    [row] = out['rows']
    assert row['today']['notional'] == pytest.approx(0.5 * 1000 * 60.0)
    assert row['today']['a'] == pytest.approx(30000.0)
    assert row['today']['lots'] == 0.5 and row['today']['fills'] == 1


def test_both_legs_add_up_and_each_is_kept_apart():
    out = analysis.notional_traded(
        [deal(1, 1.0, 60.0),
         deal(1, 2.0, 62.0, account='AC', symbol='UKOIL_F7', leg='B')],
        SIZES, NOW)
    today = out['rows'][0]['today']
    assert today['a'] == pytest.approx(60000.0)
    assert today['b'] == pytest.approx(12400.0)
    assert today['notional'] == pytest.approx(72400.0)


def test_each_deal_lands_in_the_windows_it_falls_in():
    out = analysis.notional_traded(
        [deal(1, 1.0, 10.0), deal(3 * 24, 1.0, 10.0),
         deal(20 * 24, 1.0, 10.0), deal(40 * 24, 1.0, 10.0)],
        SIZES, NOW)
    row = out['rows'][0]
    assert row['today']['fills'] == 1
    assert row['7d']['fills'] == 2
    assert row['30d']['fills'] == 3            # 40 days ago is out


def test_today_is_the_brokers_day_not_the_last_24_hours():
    """At 01:00 on the broker's clock, a deal at 23:00 the broker's
    yesterday is not today - though it is within 24 hours."""
    broker_now = NOW + OFFSET
    hours_into_day = (broker_now % 86400.0) / 3600.0
    late_yesterday = deal(hours_into_day + 1, 1.0, 10.0)
    early_today = deal(max(hours_into_day - 0.5, 0.01), 1.0, 10.0)
    out = analysis.notional_traded([late_yesterday, early_today], SIZES,
                                   NOW)
    assert out['rows'][0]['today']['fills'] == 1
    assert out['rows'][0]['7d']['fills'] == 2


def test_a_deal_with_no_contract_size_is_counted_not_priced():
    out = analysis.notional_traded(
        [deal(1, 1.0, 60.0),
         deal(1, 1.0, 90.0, account='GONE', symbol='BRENT',
              pair_key='BRENT|UKOIL_F7')], SIZES, NOW)
    total = out['total']['today']
    assert total['fills'] == 2 and total['unpriced'] == 1
    assert total['notional'] == pytest.approx(60000.0)   # not + a guess
    # Control: with its size known, it is priced.
    sizes = dict(SIZES)
    sizes[('GONE', 'BRENT')] = 100.0
    out = analysis.notional_traded(
        [deal(1, 1.0, 90.0, account='GONE', symbol='BRENT',
              pair_key='BRENT|UKOIL_F7')], sizes, NOW)
    assert out['total']['today']['unpriced'] == 0
    assert out['total']['today']['notional'] == pytest.approx(9000.0)


def test_contract_sizes_come_from_the_ladders_and_from_old_positions():
    """A ladder deleted since still prices its deals, from the size
    frozen on a position that traded on it."""
    pairs = {'USOIL_F7|UKOIL_F7': {'account_a': 'CFI', 'symbol_a': 'USOIL_F7',
                                   'contract_size_a': 1000.0,
                                   'account_b': 'AC', 'symbol_b': 'UKOIL_F7',
                                   'contract_size_b': None}}
    positions = [{'leg_a': {'account': 'CFI', 'symbol': 'BRENT',
                            'contract_size': 100.0},
                  'leg_b': {'account': 'AC', 'symbol': 'UKOIL_F7',
                            'contract_size': 100.0}}]
    sizes = analysis.contract_sizes(pairs, positions)
    assert sizes == {('CFI', 'USOIL_F7'): 1000.0, ('CFI', 'BRENT'): 100.0,
                     ('AC', 'UKOIL_F7'): 100.0}


def test_nothing_traded_is_no_rows_not_a_row_of_zeros():
    out = analysis.notional_traded([], SIZES, NOW)
    assert out['rows'] == []
    assert out['total']['30d']['fills'] == 0


# -- the Analysis page --------------------------------------------------------

def test_the_analysis_page_carries_the_notional(tmp_path):
    db_path = str(tmp_path / 'trader.db')
    store = Store(db_path)
    now = time.time()
    # Ten minutes ago - but never before the broker's midnight, or in the
    # first ten minutes of its day this deal would be yesterday's.
    broker_now = now + OFFSET
    day_start = broker_now - broker_now % 86400.0 - OFFSET
    at = max(now - 600, day_start + 1)
    store.record_fills('CFI', [{
        'inst_type': 'DEAL', 'deal_id': '501', 'symbol': 'USOIL_F7',
        'side': 'buy', 'pos_side': 'open', 'fill_qty': 2.0,
        'fill_price': 61.5, 'filled_at': int((at + OFFSET) * 1000),
        'server_offset_sec': OFFSET, 'is_bot': True}],
        resolve=lambda account, symbol: ('USOIL_F7|UKOIL_F7', 'A'))
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({'at': now, 'pairs': {'USOIL_F7|UKOIL_F7': {
        'account_a': 'CFI', 'symbol_a': 'USOIL_F7',
        'contract_size_a': 1000.0}}}), encoding='utf-8')
    (tmp_path / 'config.json').write_text('{"accounts": {}, "pairs": {}}',
                                          encoding='utf-8')
    app = create_app(str(status), str(tmp_path / 'c.jsonl'),
                     str(tmp_path / 'r.json'), str(tmp_path / 'config.json'),
                     db_path=db_path)
    body = app.test_client().get('/api/analysis').get_json()
    [row] = body['notional']['rows']
    assert row['pair_key'] == 'USOIL_F7|UKOIL_F7'
    assert row['today']['notional'] == pytest.approx(2.0 * 1000 * 61.5)
    assert row['30d']['fills'] == 1


def test_a_picked_day_is_the_brokers_day_and_only_that_day():
    import datetime
    picked = datetime.datetime.fromtimestamp(
        NOW + OFFSET - 3 * 86400, datetime.timezone.utc).date().isoformat()
    def midnight(day):
        return int(datetime.datetime.strptime(day, '%Y-%m-%d').replace(
            tzinfo=datetime.timezone.utc).timestamp() * 1000)
    out = analysis.notional_traded(
        [deal(1, 1.0, 10.0), deal(3 * 24, 2.0, 10.0),
         deal(45 * 24, 1.0, 10.0)], SIZES, NOW, date=picked,
        date_ms=(midnight(picked), midnight(picked) + 86400000))
    assert [p['key'] for p in out['periods']] == ['today', '7d', '30d',
                                                  'date']
    assert out['periods'][-1]['label'] == picked
    row = out['rows'][0]
    assert row['date']['fills'] == 1 and row['date']['lots'] == 2.0
    # A day older than the 30-day window is still answered.
    old = datetime.datetime.fromtimestamp(
        NOW + OFFSET - 45 * 86400, datetime.timezone.utc).date().isoformat()
    out = analysis.notional_traded([deal(45 * 24, 1.0, 10.0)], SIZES, NOW,
                                   date=old, date_ms=(midnight(old),
                                                      midnight(old) +
                                                      86400000))
    assert out['rows'][0]['date']['fills'] == 1
    # A RANGE: from the old day to the picked one, both included.
    out = analysis.notional_traded(
        [deal(1, 1.0, 10.0), deal(3 * 24, 2.0, 10.0),
         deal(45 * 24, 1.0, 10.0)], SIZES, NOW, date=old + ' to ' + picked,
        date_ms=(midnight(old), midnight(picked) + 86400000))
    assert out['rows'][0]['date']['fills'] == 2
    # Control: no day picked, no day column.
    assert 'date' not in [p['key'] for p in analysis.notional_traded(
        [deal(1, 1.0, 10.0)], SIZES, NOW)['periods']]


def test_the_volume_is_split_by_account_too():
    out = analysis.notional_traded(
        [deal(1, 1.0, 60.0), deal(2, 1.0, 61.0),
         deal(1, 2.0, 62.0, account='AC', symbol='UKOIL_F7', leg='B')],
        SIZES, NOW)
    by = {r['account']: r for r in out['accounts']}
    assert by['CFI']['today']['notional'] == pytest.approx(121000.0)
    assert by['AC']['today']['notional'] == pytest.approx(12400.0)
    assert by['CFI']['today']['fills'] == 2


def test_the_page_names_each_accounts_login_and_broker_and_takes_a_day(
        tmp_path):
    db_path = str(tmp_path / 'trader.db')
    store = Store(db_path)
    now = time.time()
    stamp = int((now - 600 + OFFSET) * 1000)
    store.record_fills('CFI', [{
        'inst_type': 'DEAL', 'deal_id': '501', 'symbol': 'USOIL_F7',
        'side': 'buy', 'pos_side': 'open', 'fill_qty': 1.0,
        'fill_price': 60.0, 'filled_at': stamp,
        'server_offset_sec': OFFSET, 'is_bot': True}],
        resolve=lambda account, symbol: ('USOIL_F7|UKOIL_F7', 'A'))
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({
        'at': now,
        'accounts': {'CFI': {'login': 19514405, 'server': 'CFI3-Real',
                             'company': 'CFI Financial Group'}},
        'pairs': {'USOIL_F7|UKOIL_F7': {'account_a': 'CFI',
                                        'symbol_a': 'USOIL_F7',
                                        'contract_size_a': 1000.0}}}),
        encoding='utf-8')
    (tmp_path / 'config.json').write_text('{"accounts": {}, "pairs": {}}',
                                          encoding='utf-8')
    app = create_app(str(status), str(tmp_path / 'c.jsonl'),
                     str(tmp_path / 'r.json'), str(tmp_path / 'config.json'),
                     db_path=db_path)
    import datetime
    day = datetime.datetime.fromtimestamp(
        stamp / 1000.0, datetime.timezone.utc).date().isoformat()
    body = app.test_client().get(
        f'/api/analysis?volume_from={day}&volume_to={day}').get_json()[
        'notional']
    assert body['periods'][-1]['label'] == day
    assert body['account_info']['CFI'] == {'login': 19514405,
                                           'broker': 'CFI Financial Group'}
    [account] = body['accounts']
    assert account['account'] == 'CFI'
    assert account['date']['notional'] == pytest.approx(60000.0)
    # A date that is not a date is ignored, not an error.
    # A range given backwards is read forwards; one end alone is a day.
    for query in (f'volume_from=2099-01-01&volume_to={day}',
                  f'volume_to={day}'):
        got = app.test_client().get('/api/analysis?' + query).get_json()
        assert got['notional']['from'] == day, query
    bad = app.test_client().get('/api/analysis?volume_from=yesterday')
    assert bad.status_code == 200
    assert 'date' not in [p['key'] for p in
                          bad.get_json()['notional']['periods']]
