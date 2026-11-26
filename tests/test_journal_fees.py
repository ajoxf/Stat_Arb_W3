"""The trade journal's maker / taker per leg and its fee tally, from
OKX's own fills."""

import pytest

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


def test_an_old_fill_with_a_reused_ticket_is_not_charged_to_a_new_trade():
    """A reset broker ledger numbers its tickets from 1001 again: an old
    fill with the same ticket belongs to an old trade, not this one."""
    trade = dict(position('P9', 2_000_000_000, 1001, 1002),
                 opened_at=2_000_000_000 - 600)
    journal = [{'position_id': 'P9', 'fees': 2.0, 'notional': 40000.0}]
    ms = lambda at: int(at * 1000)
    fills = [dict(fill('new1', 1001, 'open', 'maker', -4.0),
                  broker_time_ms=ms(2_000_000_000 - 590)),
             dict(fill('new2', 1002, 'close', 'maker', -4.0),
                  broker_time_ms=ms(2_000_000_000 + 5)),
             # A month earlier, same tickets: someone else's trade.
             dict(fill('old1', 1001, 'open', 'taker', -10.0),
                  broker_time_ms=ms(2_000_000_000 - 30 * 86400)),
             dict(fill('old2', 1002, 'close', 'taker', -10.0),
                  broker_time_ms=ms(2_000_000_000 - 30 * 86400 + 60))]
    tally = analysis.attach_fills(journal, [trade], fills)
    assert journal[0]['fee'] == 8.0
    # 8 on 40,000 of notional: 2 bp.
    assert journal[0]['fees_bp_notional'] == 2.0
    assert tally['fees_bp_notional'] == 2.0
    assert (tally['maker_fills'], tally['taker_fills']) == (2, 0)


def test_net_pnl_is_worked_out_on_the_fees_okx_charged():
    """Gross 30 (the spread's move x the size), OKX charged 1.20: net
    28.80 - not the desk's own estimate of 26.00."""
    trade = dict(position('P1', 1000, 11, 12, pnl=26.0),
                 entry_spread=10.0, exit_spread=13.0, spread_units=10.0)
    fills = [fill('d1', 11, 'open', 'maker', -0.20),
             fill('d2', 12, 'open', 'maker', -0.20),
             fill('d3', 11, 'close', 'maker', -0.30),
             fill('d4', 12, 'close', 'taker', -0.50)]
    [row] = analysis.with_okx_fees([trade], fills)
    assert row['realized_pnl'] == pytest.approx(28.8)
    assert row['desk_pnl'] == 26.0 and row['fees_from'] == 'OKX'
    assert trade['realized_pnl'] == 26.0              # the record untouched
    # The journal then reads the same fees two ways, and they agree.
    journal = analysis.journal([row])
    tally = analysis.attach_fills(journal, [row], fills)
    assert journal[0]['fees'] == 1.2 and journal[0]['fee'] == 1.2
    assert journal[0]['pnl'] == 28.8 and tally['fees'] == 1.2


def test_a_trade_missing_a_legs_fills_keeps_the_desks_estimate():
    trade = dict(position('P1', 1000, 11, 12, pnl=26.0),
                 entry_spread=10.0, exit_spread=13.0, spread_units=10.0)
    # Leg B's close is not in the journal: OKX's fees are not all known.
    fills = [fill('d1', 11, 'open', 'maker', -0.20),
             fill('d2', 12, 'open', 'maker', -0.20),
             fill('d3', 11, 'close', 'maker', -0.30)]
    [row] = analysis.with_okx_fees([trade], fills)
    assert row['realized_pnl'] == 26.0 and 'fees_from' not in row


def test_each_trades_fees_break_down_by_leg_and_end():
    positions = [position('P1', 1000, 11, 12)]
    journal = [{'position_id': 'P1', 'fees': 2.0}]
    fills = [fill('d1', 11, 'open', 'maker', -0.20),
             fill('d1b', 11, 'open', 'taker', -0.90),
             fill('d2', 12, 'open', 'maker', -0.40),
             fill('d3', 11, 'close', 'taker', -1.00),
             fill('d4', 12, 'close', 'maker', -0.40)]
    analysis.attach_fills(journal, positions, fills)
    parts = journal[0]['fee_parts']
    assert parts['entry_a'] == {'maker': 0.2, 'taker': 0.9}
    assert parts['entry_b'] == {'maker': 0.4, 'taker': 0.0}
    assert parts['exit_a'] == {'maker': 0.0, 'taker': 1.0}
    assert parts['exit_b'] == {'maker': 0.4, 'taker': 0.0}
    assert journal[0]['fee'] == 2.9
