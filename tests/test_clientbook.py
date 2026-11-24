"""The client's book: money in and out, P&L by month, and the profit
share with a high-water mark."""

import calendar

import pytest

from okxtrader import clientbook as cb


def at(year, month, day=10):
    return float(calendar.timegm((year, month, day, 12, 0, 0)))


def test_months_deposits_withdrawals_pnl_and_returns():
    flows = [{'at': at(2026, 9, 1), 'amount': 10000.0, 'kind': 'deposit'},
             {'at': at(2026, 10, 15), 'amount': 5000.0, 'kind': 'deposit'},
             {'at': at(2026, 11, 2), 'amount': -2000.0,
              'kind': 'withdrawal'}]
    closed = [{'closed_at': at(2026, 9, 20), 'pnl': 300.0},
              {'closed_at': at(2026, 10, 20), 'pnl': -100.0},
              {'closed_at': at(2026, 11, 20), 'pnl': 450.0}]
    b = cb.book(flows, closed, share_pct=20, now=at(2026, 11, 25),
                balance={'equity': 13700.0, 'at': at(2026, 11, 25)})
    sep, oct_, nov = b['months']
    assert [m['label'] for m in b['months']] == ['Sep 2026', 'Oct 2026',
                                                 'Nov 2026']
    assert (sep['deposits'], sep['pnl'], sep['return_pct']) == \
        (10000.0, 300.0, 3.0)
    # October: 10,300 at its start, 5,000 in: -100 on 15,300.
    assert oct_['opening'] == 10300.0
    assert oct_['return_pct'] == pytest.approx(-100 / 15300 * 100, abs=1e-3)
    assert nov['withdrawals'] == 2000.0 and nov['closing'] == 13650.0
    s = b['summary']
    assert (s['deposited'], s['withdrawn'], s['net_invested']) == \
        (15000.0, 2000.0, 13000.0)
    assert s['pnl'] == 650.0 and s['balance'] == 13700.0
    assert s['book_value'] == 13650.0
    assert s['pnl_month'] == 450.0
    assert b['flows'][0]['amount'] == -2000.0          # newest first


def test_the_profit_share_has_a_high_water_mark():
    """+1,000, then -400, then +600: the share is on the 1,000, nothing
    on the 400 won back, and on the 200 above the old best."""
    closed = [{'closed_at': at(2026, 8), 'pnl': 1000.0},
              {'closed_at': at(2026, 9), 'pnl': -400.0},
              {'closed_at': at(2026, 10), 'pnl': 600.0}]
    b = cb.book([{'at': at(2026, 8, 1), 'amount': 10000.0}], closed,
                share_pct=20, now=at(2026, 11))
    aug, sep, oct_, nov = b['months']
    assert (aug['above_hwm'], aug['share'], aug['client_net']) == \
        (1000.0, 200.0, 800.0)
    assert (sep['hwm'], sep['share'], sep['client_net']) == (1000.0, 0.0,
                                                              -400.0)
    assert (oct_['hwm'], oct_['above_hwm'], oct_['share']) == \
        (1000.0, 200.0, 40.0)
    assert aug['status'] == 'due' and sep['status'] == '-'
    assert nov['status'] == 'accruing' and nov['share'] == 0.0
    s = b['summary']
    assert s['share_total'] == 240.0 and s['share_due'] == 240.0
    assert s['client_net_pnl'] == 1200.0 - 240.0
    assert s['hwm'] == 1200.0


def test_this_months_share_is_accruing_not_due():
    b = cb.book([{'at': at(2026, 11, 1), 'amount': 1000.0}],
                [{'closed_at': at(2026, 11, 5), 'pnl': 100.0}],
                share_pct=25, now=at(2026, 11, 20))
    [nov] = b['months']
    assert nov['status'] == 'accruing' and nov['share'] == 25.0
    assert b['summary']['share_accruing'] == 25.0
    assert b['summary']['share_due'] == 0.0


def test_nothing_recorded_is_an_empty_book_not_zeros_invented():
    b = cb.book(now=at(2026, 11))
    assert b['months'] == [] and b['summary']['balance'] is None
    assert b['summary']['return_pct'] is None
