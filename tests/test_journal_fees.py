"""The trade journal's maker / taker per leg and its fee tally, from
OKX's own fills."""

from okxtrader import analysis


def position(pid, closed_at, ta, tb, pnl=10.0, gross=12.0):
    return {'position_id': pid, 'pair_key': 'K', 'side': 'BUY',
            'quantity': 1.0, 'opened_at': closed_at - 600,
            'closed_at': closed_at, 'realized_pnl': pnl,
            'leg_a': {'position_tickets': [ta]},
            'leg_b': {'position_tickets': [tb]}}


def fill(deal, ticket, entry, kind, fee, account='main'):
    return {'deal_id': deal, 'account': account, 'position_ticket': str(ticket),
            'entry': entry, 'order_type': kind, 'fee': fee}


def test_each_trade_says_maker_or_taker_per_leg_and_okxs_fees():
    positions = [position('P1', 1000, 11, 12), position('P2', 2000, 21, 22)]
    journal = [{'position_id': 'P2', 'fees': 3.0},
               {'position_id': 'P1', 'fees': 2.0}]
    fills = [fill('d1', 11, 'open', 'maker', -0.20),
             fill('d2', 12, 'open', 'taker', -1.00),
             fill('d3', 11, 'close', 'maker', -0.25),
             fill('d4', 12, 'close', 'maker', -0.30),
             fill('d5', 12, 'close', 'taker', -0.50),
             fill('d6', 21, 'open', 'maker', 0.05)]   # a maker rebate
    tally = analysis.attach_fills(journal, positions, fills)
    p2, p1 = journal
    assert p1['fills'] == {'entry_a': 'M', 'entry_b': 'T',
                           'exit_a': 'M', 'exit_b': 'M+T'}
    assert p1['fee'] == 2.25 and p1['fees_source'] == 'OKX'
    assert p1['maker_fees'] == 0.75 and p1['taker_fees'] == 1.5
    assert (p1['maker_fills'], p1['taker_fills']) == (3, 2)
    assert p2['fee'] == -0.05 and p2['fills']['entry_b'] is None
    # The running tally, in close order.
    assert p1['cum_fees'] == 2.25 and p2['cum_fees'] == 2.2
    assert tally['fees'] == 2.2 and tally['measured'] == 2
    assert tally['maker_fees'] == 0.7 and tally['taker_fees'] == 1.5
    assert (tally['maker_fills'], tally['taker_fills']) == (4, 2)


def test_a_trade_without_its_fills_keeps_the_estimate_said_as_one():
    journal = [{'position_id': 'P1', 'fees': 2.0}]
    tally = analysis.attach_fills(journal, [position('P1', 1000, 1, 2)], [])
    assert journal[0]['fee'] == 2.0
    assert journal[0]['fees_source'] == 'estimate'
    assert journal[0]['cum_fees'] == 2.0
    assert tally['estimated'] == 1 and tally['measured'] == 0


def test_a_fill_shared_by_two_trades_is_counted_once():
    positions = [position('P1', 1000, 5, 6), position('P2', 2000, 5, 7)]
    journal = [{'position_id': 'P2'}, {'position_id': 'P1'}]
    fills = [fill('d1', 5, 'open', 'taker', -1.0)]
    tally = analysis.attach_fills(journal, positions, fills)
    assert tally['fees'] == 1.0


# -- the capital: the account balance unless one is typed ---------------------

import json
import time

from okxtrader.database import Store
from okxtrader.webapp import create_app


def _app(tmp_path, settings):
    db_path = str(tmp_path / 'trader.db')
    Store(db_path)
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({
        'at': time.time(),
        'accounts': {'okx': {'equity': 3744.0}, 'other': {'equity': 99.0}},
        'pairs': {'A|B': {'leg_a_account': 'okx',
                          'leg_b_account': 'okx'}}}), encoding='utf-8')
    (tmp_path / 'config.json').write_text(json.dumps(
        {'accounts': {}, 'pairs': {}, 'settings': settings}),
        encoding='utf-8')
    return create_app(str(status), str(tmp_path / 'c.jsonl'),
                      str(tmp_path / 'r.json'),
                      str(tmp_path / 'config.json'), db_path=db_path)


def test_with_nothing_typed_the_capital_is_the_account_balance(tmp_path):
    body = _app(tmp_path, {}).test_client().get(
        '/api/analysis').get_json()
    # The accounts the ladders trade, not every account the desk knows.
    assert body['summary']['capital'] == 3744.0
    assert body['summary']['capital_source'] == 'balance'


def test_a_typed_capital_wins(tmp_path):
    body = _app(tmp_path, {'CAPITAL_MANAGED': 5000}).test_client().get(
        '/api/analysis').get_json()
    assert body['summary']['capital'] == 5000.0
    assert body['summary']['capital_source'] == 'set'


def test_each_excursion_row_carries_the_margin_it_is_read_on():
    row = analysis.excursion_row({
        'position_id': 'P1', 'realized_pnl': 10.0, 'peak_pnl': 30.0,
        'trough_pnl': -20.0, 'entry_margin': 5.0, 'quantity': 300.0})
    assert row['margin'] == 1500.0
    assert row['mae'] == 20.0 and row['mfe'] == 30.0


def test_the_analysis_tab_and_the_report_both_match_okxs_fills(tmp_path):
    client = _app(tmp_path, {}).test_client()
    assert client.get('/api/analysis').get_json()['fee_tally'] is not None
    assert client.get('/report').status_code == 200


def test_fees_are_read_as_a_share_of_the_gross():
    journal = [{'position_id': 'P2', 'fees': 5.0, 'gross_pnl': -10.0},
               {'position_id': 'P1', 'fees': 4.0, 'gross_pnl': 40.0}]
    tally = analysis.attach_fills(journal, [], [])
    p2, p1 = journal
    assert p1['fees_pct_gross'] == 10.0
    # A move that lost has no share: a dash, not a negative percent.
    assert p2['fees_pct_gross'] is None
    assert tally['gross'] == 30.0 and tally['fees_pct_gross'] == 30.0
