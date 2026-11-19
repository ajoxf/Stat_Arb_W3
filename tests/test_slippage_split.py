"""Where slippage came from, which account it came from, and what it
took out of the P&L.

The split adds up by construction: BROKER FILL is each leg against the
price it was sent at; MARKET MOVED WHILE SENDING is the rest of the
spread's slip. Unmeasured is never zero: a leg without its send price
leaves the split unmeasured.
"""

import pytest

from okxtrader import slippage
from test_leg_slippage import engine, round_trip          # noqa: F401


def position(entry_slip=0.002, exit_slip=0.008, sent_a=82.0, sent_b=92.0,
             fill_a=82.0, fill_b=92.0, realized=124.30, closed=True,
             click_ms=752.0, opened=1000.0):
    """One SELL spread of 1, k = 1,000 per 1.00 of spread: sell B, buy A."""
    leg = lambda acct, sym, side, sent, fill: {
        'account': acct, 'symbol': sym, 'side': side, 'volume': 1.0,
        'contract_size': 1000.0, 'price': fill, 'sent_price': sent,
        'exit_price': fill, 'exit_sent_price': sent}
    return {'position_id': 'P%d' % opened, 'pair_key': 'OIL', 'side': 'SELL',
            'quantity': 1.0, 'order_type': 'LIMIT', 'spread_units': 1000.0,
            'opened_at': opened, 'closed_at': opened + 60 if closed else None,
            'entry_slippage': entry_slip,
            'exit_slippage': exit_slip if closed else None,
            'realized_pnl': realized if closed else None,
            'click_to_on_ms': click_ms,
            'leg_a': leg('CFI', 'USOIL_F7', 'BUY', sent_a, fill_a),
            'leg_b': leg('AC', 'UKOIL_F7', 'SELL', sent_b, fill_b)}


def test_legs_filled_at_the_price_sent_put_it_all_on_the_market_moving():
    [row] = slippage.report([position()])['rows']
    assert row['entry_broker_money'] == pytest.approx(0.0)
    assert row['entry_moved_money'] == pytest.approx(2.0)
    assert row['entry_moved_points'] == pytest.approx(0.002)
    assert row['exit_moved_money'] == pytest.approx(8.0)


def test_a_leg_filled_worse_than_it_was_sent_is_the_broker():
    # Leg B sold 0.001 below what it was sent at: 0.001 x 1 x 1,000.
    [row] = slippage.report([position(fill_b=91.999)])['rows']
    assert row['entry_broker_money'] == pytest.approx(1.0)
    assert row['entry_moved_money'] == pytest.approx(1.0)      # 2.0 - 1.0
    assert row['entry_broker_money'] + row['entry_moved_money'] == \
        pytest.approx(row['entry_money'])                      # adds up


def test_a_leg_with_no_send_price_leaves_the_split_unmeasured():
    p = position()
    p['leg_a']['sent_price'] = None
    [row] = slippage.report([p])['rows']
    assert row['entry_broker_money'] is None
    assert row['entry_moved_money'] is None                    # not zero
    assert row['entry_money'] == pytest.approx(2.0)            # still there


def test_the_split_on_the_real_engine(engine, pair, legs):
    """Not a hand-made dict: a broker filling 0.20 off its quote is all
    broker, and the control - a clean fill - is none of it."""
    dirty = round_trip(engine, pair, legs, worse_b=0.20).to_dict()
    [row] = slippage.report([dirty])['rows']
    assert row['entry_broker_money'] == pytest.approx(2.0)
    clean = round_trip(engine, pair, legs).to_dict()
    [row] = slippage.report([clean])['rows']
    assert row['entry_broker_money'] == pytest.approx(0.0)


def test_each_account_and_symbol_is_summed_on_its_own():
    out = slippage.report([position(fill_b=91.999), position(opened=2000.0)])
    by = {(r['account'], r['symbol']): r for r in out['by_account']}
    b = by[('AC', 'UKOIL_F7')]
    assert b['fills'] == 4 and b['legs'] == 'B'
    # Sold 0.001 under the send (a cost); bought back 0.001 under it
    # too (a gain): one paid, one earned, and they cancel.
    assert b['entry_points_mean'] == pytest.approx(0.0005)
    assert b['money_total'] == pytest.approx(0.0)
    assert b['points_worst'] == pytest.approx(0.001)
    assert b['paid'] == 1 and b['earned'] == 1
    a = by[('CFI', 'USOIL_F7')]
    assert a['money_total'] == pytest.approx(0.0) and a['paid'] == 0


def test_the_session_summary_adds_the_parts_and_the_latency():
    out = slippage.report([position(click_ms=700.0, opened=1.0),
                           position(click_ms=800.0, opened=2.0),
                           position(click_ms=None, opened=3.0)])
    assert out['split']['entry']['moved_money'] == pytest.approx(6.0)
    assert out['split']['entry']['broker_money'] == pytest.approx(0.0)
    assert out['click_to_on_median_ms'] == pytest.approx(750.0)


def test_slippage_is_set_against_the_pnl_it_came_out_of():
    out = slippage.report([position()])
    pnl = out['pnl']
    assert pnl['slippage'] == pytest.approx(10.0)
    assert pnl['net'] == pytest.approx(124.30)
    assert pnl['before'] == pytest.approx(134.30)
    assert pnl['share'] == pytest.approx(10.0 / 134.30)


def test_a_share_of_a_loss_is_not_a_share():
    out = slippage.report([position(realized=-50.0)])
    assert out['pnl']['share'] is None and out['pnl']['slippage'] == 10.0
    # Control: an open trade is not counted at all.
    out = slippage.report([position(closed=False)])
    assert out['pnl']['trades'] == 0 and out['pnl']['share'] is None
