"""The slippage report over a session.

The rules being pinned here are the ones that make a report worth
reading: unmeasured never becomes zero, positive is a cost at BOTH ends,
the session is cut on the broker's clock, and money comes from the same
`k` every other conversion uses.
"""

import time
from datetime import datetime, timedelta

import pytest

from okxtrader import slippage
from okxtrader.database import Store


def position(**overrides):
    row = {'position_id': 'POS1', 'pair_key': 'X|Y', 'side': 'BUY',
           'quantity': 1.0, 'spread_units': 1000.0, 'order_type': 'MARKET',
           'opened_at': 1000.0, 'closed_at': 2000.0,
           'entry_slippage': 0.01, 'exit_slippage': 0.02,
           'click_to_on_ms': 40.0, 'realized_pnl': 5.0}
    row.update(overrides)
    return row


def test_money_uses_the_one_multiplier_and_nothing_else():
    """`points x spread_units` - the same conversion as the mark, where
    a position's spread_units is its WHOLE size already. Multiplying by
    the quantity again (as this report did) put the slippage on a scale
    `quantity` times the P&L's: 0.23 points on 30 spreads read $7.34
    where it cost $0.24."""
    assert slippage.money(0.01, position(spread_units=1000.0, quantity=2.0)) \
        == pytest.approx(10.0)
    from okxtrader.models import SpreadPosition
    mark = SpreadPosition.mark
    assert 'spread_units' in mark.__code__.co_names or \
        'spread_units' in str(mark.__code__.co_names)


def test_money_is_unmeasured_when_the_size_is_unknown():
    assert slippage.money(0.01, position(spread_units=None)) is None
    assert slippage.money(None, position()) is None


def test_an_unmeasured_fill_is_never_averaged_in_as_zero():
    """The whole reason the report is trustworthy. One good fill and one
    that could not be priced is a mean of the good one, over ONE
    measurement — not half of it over two."""
    body = slippage.report([position(entry_slippage=0.02),
                            position(position_id='POS2',
                                     entry_slippage=None)])

    entry = body['overall']['entry']
    assert entry['measured'] == 1
    assert entry['unmeasured'] == 1
    assert entry['points_mean'] == pytest.approx(0.02)


def test_with_nothing_measured_every_figure_is_none():
    """Rendered as "—". A report of 0.0000 across the board reads as a
    session of perfect fills."""
    body = slippage.report([position(entry_slippage=None,
                                     exit_slippage=None)])
    entry = body['overall']['entry']
    assert entry['measured'] == 0
    assert entry['points_mean'] is None
    assert entry['money_total'] is None


def test_an_open_position_has_no_exit_and_is_not_an_unmeasured_one():
    """A trade that is still on has not exited. Counting it as an
    unmeasured exit would make every live position look like a hole in
    the record."""
    body = slippage.report([position(closed_at=None, exit_slippage=None)])
    assert body['overall']['exit']['measured'] == 0
    assert body['overall']['exit']['unmeasured'] == 0
    assert body['counts']['open'] == 1


def test_a_round_turn_needs_both_ends_measured():
    """One end plus a missing one is half a round turn, and reporting it
    as a whole one halves the cost of trading."""
    body = slippage.report([position(exit_slippage=None)])
    assert body['overall']['round_trip']['measured'] == 0
    assert body['overall']['round_trip']['unmeasured'] == 1

    both = slippage.report([position()])
    assert both['overall']['round_trip']['points_mean'] == pytest.approx(0.03)
    assert both['overall']['round_trip']['money_total'] == pytest.approx(30.0)


def test_positive_is_a_cost_at_both_ends_and_the_report_only_adds():
    """`executor.slippage()` already flipped the sign for the exit. If
    this module flipped it again, an exit that cost money would read as
    a gain."""
    body = slippage.report([position(entry_slippage=0.01,
                                     exit_slippage=0.02)])
    assert body['overall']['entry']['money_total'] == pytest.approx(10.0)
    assert body['overall']['exit']['money_total'] == pytest.approx(20.0)


def test_an_improvement_is_negative_and_counted_apart_from_a_cost():
    body = slippage.report([position(entry_slippage=-0.01),
                            position(position_id='P2', entry_slippage=0.03)])
    entry = body['overall']['entry']
    assert entry['paid'] == 1 and entry['earned'] == 1
    assert entry['points_worst'] == pytest.approx(0.03)
    assert entry['points_best'] == pytest.approx(-0.01)
    assert entry['points_mean'] == pytest.approx(0.01)


def test_market_and_limit_are_reported_apart():
    """The one split the report exists for: a peg that is not beating a
    market click on the same ladder is costing time for nothing."""
    body = slippage.report([
        position(order_type='MARKET', entry_slippage=0.04),
        position(position_id='P2', order_type='LIMIT',
                 entry_slippage=-0.01)])

    assert body['by_order_type']['MARKET']['entry']['points_mean'] == \
        pytest.approx(0.04)
    assert body['by_order_type']['LIMIT']['entry']['points_mean'] == \
        pytest.approx(-0.01)


def test_the_worst_entries_are_listed_worst_first_by_money():
    """Ranked in money, not in points: two ladders with different `k`
    are not comparable in spread points, and the money is what was
    actually paid."""
    body = slippage.report([
        position(position_id='SMALL', entry_slippage=0.05,
                 spread_units=10.0),
        position(position_id='BIG', entry_slippage=0.02,
                 spread_units=10000.0)])

    assert [row['position_id'] for row in body['worst']] == ['BIG', 'SMALL']


def test_a_pair_is_reported_under_its_own_name():
    body = slippage.report([position(pair_key='X|Y')], names={'X|Y': 'Gold sp'})
    assert body['by_pair']['X|Y']['name'] == 'Gold sp'


# -- the session window ---------------------------------------------------


def _epoch(text):
    return datetime.strptime(text, '%Y-%m-%d %H:%M').timestamp()


def test_the_session_is_cut_on_the_brokers_clock():
    """The deals are stamped in the broker's day. A window cut on this
    machine's clock, hours away, splits one session across two
    reports."""
    now = _epoch('2026-08-27 14:00')          # 17:00 at a broker on +3h
    window = slippage.session_window(now, 3 * 3600, 16, 55)

    # The broker is past its 16:55 cutoff, so this is a NEW session that
    # began five minutes ago — not one that began yesterday.
    assert window['clock'] == 'broker'
    assert now - window['from'] == pytest.approx(5 * 60)


def test_before_the_cutoff_the_session_is_still_yesterdays():
    now = _epoch('2026-08-27 10:00')
    window = slippage.session_window(now, 0, 16, 55)
    started = datetime.fromtimestamp(window['from'])
    assert (started.day, started.hour, started.minute) == (26, 16, 55)


def test_an_unmeasured_broker_clock_still_reports_and_says_which_clock():
    """Refusing to open the report would teach nobody anything; showing
    it silently on the wrong clock would mislead. It opens, and says."""
    window = slippage.session_window(_epoch('2026-08-27 10:00'), None, 16, 55)
    assert window['clock'] == 'machine'
    assert 'has not been measured' in window['note']


# -- the store, which is where a real session's positions come from -------


def test_positions_are_gathered_by_when_they_were_PUT_ON(tmp_path):
    """A position carried overnight belongs to the session it was opened
    in: that is the session whose click its entry slippage was measured
    against."""
    store = Store(str(tmp_path / 'db.sqlite'))
    with store._connect() as connection:
        for pid, opened, closed in (('OLD', 100.0, 150.0),
                                    ('IN', 1000.0, None),
                                    ('LATER', 5000.0, None)):
            connection.execute(
                'INSERT INTO positions (position_id, pair_key, side, '
                'quantity, opened_at, closed_at) VALUES (?,?,?,?,?,?)',
                (pid, 'X|Y', 'BUY', 1.0, opened, closed))

    rows = store.positions_between(500.0, 4000.0)

    assert [row['position_id'] for row in rows] == ['IN']


def test_the_journal_is_counted_over_the_same_window(tmp_path):
    """The coverage check: our positions against the deals the account
    actually saw. Disagreement is shown, not reconciled away."""
    store = Store(str(tmp_path / 'db.sqlite'))
    rows = [{'inst_type': 'DEAL', 'deal_id': '1', 'fill_qty': 1.0,
             'filled_at': 1000, 'is_bot': True},
            {'inst_type': 'DEAL', 'deal_id': '2', 'fill_qty': 2.0,
             'filled_at': 9000, 'is_bot': True},
            {'inst_type': 'DEAL', 'deal_id': '3', 'fill_qty': 4.0,
             'filled_at': 1500, 'is_bot': False}]
    store.record_fills('acct', rows)

    ours = store.fills_between(500, 2000, ours_only=True)
    assert ours['fills'] == 1 and ours['volume'] == pytest.approx(1.0)

    # A manual terminal click is in the journal and NOT ours: the count
    # the report checks itself against has to be the same population.
    everything = store.fills_between(500, 2000, ours_only=False)
    assert everything['fills'] == 2


def test_the_report_runs_over_the_store_end_to_end(tmp_path):
    """The path the endpoint takes: positions out of the database,
    through the window, into the report."""
    store = Store(str(tmp_path / 'db.sqlite'))
    now = time.time()
    with store._connect() as connection:
        connection.execute(
            'INSERT INTO positions (position_id, pair_key, side, quantity, '
            'spread_units, order_type, opened_at, entry_slippage) '
            'VALUES (?,?,?,?,?,?,?,?)',
            ('POS1', 'X|Y', 'BUY', 1.0, 1000.0, 'MARKET', now - 60, 0.01))

    window = slippage.session_window(now, 0, 0, 0)
    body = slippage.report(store.positions_between(window['from'],
                                                   window['to']),
                           window=window)

    assert body['counts']['positions'] == 1
    assert body['overall']['entry']['money_total'] == pytest.approx(10.0)


def _leg(side, sent, price, exit_sent=None, exit_price=None, volume=0.35,
         contract=0.1, symbol='ETH-USDT-SWAP'):
    return {'side': side, 'sent_price': sent, 'price': price,
            'exit_sent_price': exit_sent, 'exit_price': exit_price,
            'volume': volume, 'contract_size': contract, 'symbol': symbol}


def test_every_fill_against_the_price_it_was_sent_at():
    """One row per leg per end: sent at, filled at, the slip in price,
    bp and money - positive a cost. A maker fill at its own price is
    0, said as 'exact'; a send price not kept is unmeasured, not 0."""
    pos = position(
        leg_a=_leg('SELL', 61000.0, 61000.0, 60900.0, 60905.0, volume=0.1,
                   contract=0.01, symbol='BTC-USDT-SWAP'),
        leg_b=_leg('BUY', 1709.30, 1709.40, None, 1712.0))
    body = slippage.fills([pos])
    rows = {(r['end'], r['leg']): r for r in body['rows']}
    # Leg A entry: a maker fill at its own price.
    assert rows[('entry', 'A')]['slip'] == 0.0
    # Leg B entry: bought 0.10 above the price sent - a cost.
    b = rows[('entry', 'B')]
    assert b['slip'] == pytest.approx(0.10)
    assert b['slip_bps'] == pytest.approx(0.10 / 1709.30 * 1e4)
    assert b['slip_money'] == pytest.approx(0.10 * 0.35 * 0.1)
    # Leg A exit: a SELL's close is a BUY - paid 5 above what was sent.
    assert rows[('exit', 'A')]['side'] == 'BUY'
    assert rows[('exit', 'A')]['slip'] == pytest.approx(5.0)
    # Leg B exit: the send price was not kept - unmeasured, not 0.
    assert rows[('exit', 'B')]['slip'] is None
    s = body['summary']
    assert (s['fills'], s['measured'], s['exact'], s['worse'], s['better'],
            s['unmeasured']) == (4, 3, 1, 2, 0, 1)


def test_an_open_position_has_its_entry_fills_only():
    pos = position(closed_at=None,
                   leg_a=_leg('SELL', 61000.0, 61000.0),
                   leg_b=_leg('BUY', 1709.3, 1709.3))
    body = slippage.fills([pos])
    assert [r['end'] for r in body['rows']] == ['entry', 'entry']
    assert body['summary']['exact'] == 2


def test_the_report_carries_the_fills():
    body = slippage.report([position(leg_a=_leg('SELL', 1.0, 1.0),
                                     leg_b=_leg('BUY', 2.0, 2.0))])
    assert body['fills']['summary']['measured'] == 2
