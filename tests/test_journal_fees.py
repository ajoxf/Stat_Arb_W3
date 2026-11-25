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
