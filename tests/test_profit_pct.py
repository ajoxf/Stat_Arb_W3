"""Profit % on margin, on the desktop: net P&L over the margin BOTH
legs tied up at entry - the base the TP % and SL % are set on - and over
both legs' notional. Unmeasured is not zero: no margin, no %.
"""

import pytest

from okxtrader import analysis


def trip(pnl=124.30, margin=1740.0, qty=1.0, source='terminal',
         closed=True):
    leg = lambda p: {'volume': 1.0, 'contract_size': 1000.0, 'price': p,
                     'side': 'BUY', 'exit_price': p, 'symbol': 'X'}
    return {'position_id': 'P', 'pair_key': 'OIL', 'side': 'SELL',
            'quantity': qty, 'opened_at': 1.0,
            'closed_at': 100.0 if closed else None, 'realized_pnl': pnl,
            'entry_margin': margin, 'entry_margin_source': source,
            'leg_a': leg(82.0), 'leg_b': leg(92.0)}


def test_profit_is_a_share_of_both_legs_margin_times_the_size():
    out = analysis.returns(trip(pnl=-280.0, qty=2.0), -280.0)
    assert out['margin'] == pytest.approx(3480.0)
    assert out['pct_margin'] == pytest.approx(-280.0 / 3480.0 * 100, abs=1e-4)
    assert out['notional'] == pytest.approx(174000.0)
    assert out['pct_notional'] == pytest.approx(-280.0 / 174000.0 * 100, abs=1e-6)
    assert out['margin_estimated'] is False


def test_no_margin_is_no_percentage_not_zero():
    out = analysis.returns(trip(margin=None), 124.30)
    assert out['margin'] is None and out['pct_margin'] is None
    assert out['pct_notional'] is not None          # its own base stands


def test_an_estimated_margin_is_marked():
    assert analysis.returns(trip(source='leverage'),
                            1.0)['margin_estimated'] is True


def test_the_journal_carries_each_trades_percentage():
    [row] = analysis.journal([trip()])
    assert row['pct_margin'] == pytest.approx(124.30 / 1740.0 * 100, abs=1e-4)


def test_return_on_margin_counts_only_trades_whose_margin_was_priced():
    s = analysis.summary([trip(pnl=124.30), trip(pnl=-502.0),
                          trip(pnl=999.0, margin=None)])
    assert s['margin_trades'] == 2
    assert s['return_on_margin'] == pytest.approx(
        (124.30 - 502.0) / (2 * 1740.0) * 100, abs=1e-4)
    # Control: nothing priced, no figure - never 0%.
    assert analysis.summary([trip(margin=None)])['return_on_margin'] is None


def test_each_trade_is_read_against_the_capital_managed():
    """Return on CAPITAL and the capital a trade used: its net, its
    margin and its face value, each over the capital the trader manages
    (CAPITAL_MANAGED)."""
    [row] = analysis.journal([trip(pnl=70.0)], capital=3500.0)
    assert row['pct_capital'] == pytest.approx(2.0)          # 70 / 3,500
    assert row['pct_capital_used'] == pytest.approx(
        1740.0 / 3500.0 * 100, abs=0.01)
    assert row['exposure'] == pytest.approx(174000.0 / 3500.0, abs=0.001)


def test_no_capital_set_is_a_dash_not_zero():
    [row] = analysis.journal([trip()])
    assert row['pct_capital'] is None
    assert row['pct_capital_used'] is None and row['exposure'] is None
    s = analysis.summary([trip()])
    assert s['return_on_capital'] is None and s['capital_used_avg'] is None


def test_the_summary_reads_the_window_against_the_capital():
    s = analysis.summary([trip(pnl=124.30), trip(pnl=-54.30, margin=870.0),
                          trip(pnl=10.0, margin=None)], capital=10000.0)
    assert s['capital'] == 10000.0
    assert s['return_on_capital'] == pytest.approx(0.8)      # 80 / 10,000
    assert s['capital_used_avg'] == pytest.approx((17.4 + 8.7) / 2, abs=0.01)
    assert s['capital_used_max'] == pytest.approx(17.4)
