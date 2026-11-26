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


# -- OKX's own ledger: the fees, and the account's balance -------------------

def test_each_trade_is_checked_against_okxs_ledger_and_carries_the_balance():
    positions = [position('P1', 1000, 11, 12), position('P2', 2000, 21, 22)]
    journal = [{'position_id': 'P2', 'fees': 1.0},
               {'position_id': 'P1', 'fees': 1.0}]
    fills = [dict(fill('t1', 11, 'open', 'maker', -0.2), symbol='BTC-SWAP'),
             dict(fill('t2', 12, 'close', 'taker', -0.5), symbol='ETH-SWAP'),
             dict(fill('t3', 21, 'open', 'maker', -0.3), symbol='BTC-SWAP'),
             dict(fill('t4', 22, 'close', 'maker', -0.3), symbol='ETH-SWAP')]
    analysis.attach_fills(journal, positions, fills)
    bills = [
        {'account': 'main', 'bill_id': 'b0', 'ts_ms': 500, 'inst_id': '',
         'trade_id': '', 'fee': 0.0, 'bal': 3500.0, 'bal_chg': 3500.0},
        {'account': 'main', 'bill_id': 'b1', 'ts_ms': 900,
         'inst_id': 'BTC-SWAP', 'trade_id': 't1', 'fee': -0.2,
         'bal': 3499.8, 'bal_chg': -0.2},
        {'account': 'main', 'bill_id': 'b2', 'ts_ms': 1000,
         'inst_id': 'ETH-SWAP', 'trade_id': 't2', 'fee': -0.5,
         'bal': 3520.3, 'bal_chg': 20.5},
        # Funding between the trades: in the balance, in no trade.
        {'account': 'main', 'bill_id': 'b3', 'ts_ms': 1500, 'inst_id':
         'ETH-SWAP', 'trade_id': '', 'fee': 0.0, 'bal': 3519.9,
         'bal_chg': -0.4},
        {'account': 'main', 'bill_id': 'b4', 'ts_ms': 1900,
         'inst_id': 'BTC-SWAP', 'trade_id': 't3', 'fee': -0.3,
         'bal': 3519.6, 'bal_chg': -0.3},
        {'account': 'main', 'bill_id': 'b5', 'ts_ms': 2000,
         'inst_id': 'ETH-SWAP', 'trade_id': 't4', 'fee': -0.31,
         'bal': 3544.29, 'bal_chg': 24.69}]
    summary = analysis.attach_bills(journal, bills)
    p2, p1 = journal
    assert p1['bill_fees'] == 0.7 and p1['account_bal'] == 3520.3
    # The ledger says 0.61 where the fills said 0.60: shown, not hidden.
    assert p2['bill_fees'] == 0.61 and p2['account_bal'] == 3544.29
    assert summary['opening'] == 0.0 and summary['latest'] == 3544.29
    # Each fill: its own ledger fee beside it.
    assert [f['bill_fee'] for f in p1['fee_fills']] == [0.2, 0.5]


def test_each_fill_is_worked_through_contracts_size_price():
    trade = dict(position('P1', 1000, 11, 12),
                 leg_a={'position_tickets': [11], 'contract_size': 0.01},
                 leg_b={'position_tickets': [12], 'contract_size': 0.1})
    journal = [{'position_id': 'P1'}]
    fills = [dict(fill('d1', 11, 'open', 'maker', -0.2), volume=33.0,
                  price=60000.0, symbol='BTC-USDT-SWAP'),
             dict(fill('d2', 12, 'open', 'taker', -0.4), volume=4.0,
                  price=2000.0, symbol='ETH-USDT-SWAP')]
    analysis.attach_fills(journal, [trade], fills)
    first, second = journal[0]['fee_fills']
    assert first['leg'] == 'A' and first['end'] == 'in'
    assert first['notional'] == 19800.0              # 33 x 0.01 x 60,000
    assert first['bp'] == round(0.2 / 19800 * 1e4, 3)
    assert second['kind'] == 'taker' and second['notional'] == 800.0


def test_one_bill_for_an_orders_many_fills_is_matched_by_the_order():
    """OKX wrote ONE bill for an order filled in three pieces: matched by
    the order, the ledger's fee is the fills' fee - not a third of it."""
    positions = [position('P1', 1000, 11, 12)]
    journal = [{'position_id': 'P1', 'fees': 1.0}]
    fills = [dict(fill('t1', 11, 'open', 'taker', -0.30), order_id='o1',
                  symbol='BTC'),
             dict(fill('t2', 11, 'open', 'taker', -0.20), order_id='o1',
                  symbol='BTC'),
             dict(fill('t3', 11, 'open', 'taker', -0.10), order_id='o1',
                  symbol='BTC'),
             dict(fill('t4', 12, 'close', 'maker', -0.40), order_id='o2',
                  symbol='ETH')]
    analysis.attach_fills(journal, positions, fills)
    bills = [{'account': 'main', 'bill_id': 'b1', 'ts_ms': 900,
              'inst_id': 'BTC', 'ord_id': 'o1', 'trade_id': 't3',
              'fee': -0.60, 'bal': 3499.4, 'bal_chg': -0.6},
             {'account': 'main', 'bill_id': 'b2', 'ts_ms': 1000,
              'inst_id': 'ETH', 'ord_id': 'o2', 'trade_id': 't4',
              'fee': -0.40, 'bal': 3520.0, 'bal_chg': 20.6}]
    analysis.attach_bills(journal, bills)
    row = journal[0]
    assert row['fee'] == 1.0 and row['bill_fees'] == 1.0
    assert row['bills_matched'] == 2 and row['bill_orders'] == 2
    assert row['account_bal'] == 3520.0


# -- the ledger is the fee; the balance reconciled ---------------------------

def _ledger_case():
    trade = dict(position('P1', 1000, 11, 12, pnl=26.0),
                 entry_spread=10.0, exit_spread=13.0, spread_units=10.0)
    fills = [dict(fill('t1', 11, 'open', 'maker', -0.20), order_id='o1',
                  symbol='BTC'),
             dict(fill('t2', 12, 'open', 'maker', -0.20), order_id='o2',
                  symbol='ETH'),
             dict(fill('t3', 11, 'close', 'taker', -0.50), order_id='o3',
                  symbol='BTC'),
             dict(fill('t4', 12, 'close', 'maker', -0.30), order_id='o4',
                  symbol='ETH')]
    bills = [{'account': 'main', 'bill_id': f'b{i}', 'ts_ms': 900 + i,
              'type': '2', 'inst_id': sym, 'ord_id': o, 'trade_id': '',
              'fee': fee, 'bal': bal, 'bal_chg': fee}
             for i, (sym, o, fee, bal) in enumerate((
                 ('BTC', 'o1', -0.20, 3499.80), ('ETH', 'o2', -0.20, 3499.60),
                 ('BTC', 'o3', -0.52, 3514.08), ('ETH', 'o4', -0.30,
                                                 3528.78)))]
    # A manual click in the OKX app: in the ledger, in no trade.
    bills.append({'account': 'main', 'bill_id': 'b9', 'ts_ms': 950,
                  'type': '2', 'inst_id': 'SOL', 'ord_id': 'm1',
                  'trade_id': '', 'fee': -0.05, 'bal': 3528.73,
                  'bal_chg': -0.05})
    return trade, fills, bills


def test_the_ledgers_fee_is_the_trades_fee_and_its_net():
    trade, fills, bills = _ledger_case()
    [row] = analysis.with_okx_fees([trade], fills, bills)
    # Gross 30; the ledger charged 1.22 (the fills said 1.20).
    assert row['realized_pnl'] == pytest.approx(28.78)
    assert row['fees_from'] == 'OKX ledger'
    journal = analysis.journal([row])
    tally = analysis.attach_fills(journal, [row], fills, bills)
    assert journal[0]['fee'] == 1.22 and journal[0]['fills_fees'] == 1.2
    assert journal[0]['fees_source'] == 'OKX ledger'
    assert tally['ledger'] == 1
    # The period: 1.27 of trading fees; 1.22 on this trade; 0.05 on an
    # order that is no desk trade.
    assert tally['ledger_period'] == 1.27
    assert tally['ledger_matched'] == 1.22 and tally['ledger_other'] == 0.05


def test_the_journals_balance_is_reconciled_with_okxs_own():
    trade, fills, bills = _ledger_case()
    journal = analysis.journal([trade])
    analysis.attach_fills(journal, [trade], fills, bills)
    account = analysis.attach_bills(journal, bills)
    assert account['latest'] == 3528.73
    same = analysis.reconcile_balance(account, {'main': {
        'balance': 3528.73, 'equity': 3540.0, 'profit': 11.27}})
    assert same['ok'] and same['gap'] == 0.0
    assert same['okx_equity'] == 3540.0 and same['okx_open_pnl'] == 11.27
    off = analysis.reconcile_balance(account, {'main': {'balance': 3530.0}})
    assert not off['ok'] and off['gap'] == 1.27
    assert analysis.reconcile_balance(account, {}) is None


def test_the_gap_between_a_trades_balance_and_okx_now_is_explained():
    trade, fills, bills = _ledger_case()
    # After the trade: funding, a transfer in, and a bill in another
    # currency that must not be read as the account's dollars.
    bills += [{'account': 'main', 'bill_id': 'c1', 'ts_ms': 2000,
               'type': '8', 'inst_id': 'ETH', 'ccy': 'USDT', 'fee': 0.0,
               'bal': 3528.33, 'bal_chg': -0.40, 'trade_id': ''},
              {'account': 'main', 'bill_id': 'c2', 'ts_ms': 2100,
               'type': '1', 'inst_id': '', 'ccy': 'USDT', 'fee': 0.0,
               'bal': 10528.33, 'bal_chg': 7000.0, 'trade_id': ''},
              {'account': 'main', 'bill_id': 'c3', 'ts_ms': 2200,
               'type': '1', 'inst_id': '', 'ccy': 'AED', 'fee': 0.0,
               'bal': 150.0, 'bal_chg': 150.0, 'trade_id': ''}]
    journal = analysis.journal([trade])
    analysis.attach_fills(journal, [trade], fills, bills)
    account = analysis.attach_bills(journal, bills)
    assert '_bills' not in journal[0]
    assert journal[0]['account_bal'] == 3528.78
    assert account['latest'] == 10528.33                  # USDT, not AED
    after = account['after_last_trade']
    assert after['funding'] == -0.40 and after['transfers in / out'] == 7000
    rec = analysis.reconcile_balance(account, {'main': {
        'balance': 10528.33, 'equity': 10569.17, 'profit': 0.0}})
    assert rec['ok'] and rec['other_assets'] == 40.84
