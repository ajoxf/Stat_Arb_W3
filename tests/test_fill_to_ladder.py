"""A fill is filed under the ladder whose position it belongs to - by
ticket, not by symbol.

On OKX one account carries every ladder, and one instrument (say
BTC-USDT-SWAP) is a leg of several. Matching by symbol filed a fill
under whichever ladder came first in the config.
"""

import pytest

from okxtrader.config import PairConfig, TraderConfig
from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import LegFill, SpreadPosition, SpreadSide


def _pairs():
    basis = PairConfig('BTC-USDT|BTC-USDT-SWAP', name='BTC basis',
                       leg_a={'account': 'okx', 'symbol': 'BTC-USDT'},
                       leg_b={'account': 'okx', 'symbol': 'BTC-USDT-SWAP'})
    ethbtc = PairConfig('BTC-USDT-SWAP|ETH-USDT-SWAP', name='ETH vs BTC',
                        leg_a={'account': 'okx', 'symbol': 'BTC-USDT-SWAP'},
                        leg_b={'account': 'okx', 'symbol': 'ETH-USDT-SWAP'})
    return basis, ethbtc


@pytest.fixture
def desk(tmp_path):
    basis, ethbtc = _pairs()
    config = TraderConfig(pairs={basis.key: basis, ethbtc.key: ethbtc})
    store = Store(str(tmp_path / 'desk.db'))
    coordinator = Coordinator(config, {}, status_path=str(tmp_path / 's.json'),
                              store=store, sleep=lambda s: None)
    return coordinator, basis, ethbtc, store


def _position(pair, ticket_a, ticket_b):
    leg_a = LegFill('okx', pair.symbol_a, 'SELL', 1.0, 65000.0,
                    position_tickets=[ticket_a])
    leg_b = LegFill('okx', pair.symbol_b, 'BUY', 1.86, 3510.0,
                    position_tickets=[ticket_b])
    return SpreadPosition(pair.key, SpreadSide.BUY, 1.0, leg_a, leg_b, 0.45,
                          'LIMIT', 1.0)


def test_a_fill_goes_to_the_ladder_whose_position_holds_its_ticket(desk):
    coordinator, basis, ethbtc, _ = desk
    coordinator.book.add_position(_position(ethbtc, 1001, 1002))
    # BTC-USDT-SWAP is leg B of the basis and leg A of ETH vs BTC: the
    # ticket says which.
    assert coordinator.resolve_leg('okx', 'BTC-USDT-SWAP', '1001') == \
        (ethbtc.key, 'A')
    assert coordinator.resolve_leg('okx', 'ETH-USDT-SWAP', 1002) == \
        (ethbtc.key, 'B')


def test_a_closed_positions_ticket_is_found_in_our_own_ledger(desk):
    coordinator, basis, ethbtc, store = desk
    store.remember_tickets([('okx', '2001', basis.key)])
    assert coordinator.resolve_leg('okx', 'BTC-USDT-SWAP', '2001') == \
        (basis.key, 'B')


def test_CONTROL_an_instrument_on_two_ladders_with_no_ticket_is_not_guessed(
        desk):
    coordinator, *_ = desk
    assert coordinator.resolve_leg('okx', 'BTC-USDT-SWAP', None) == \
        (None, None)


def test_CONTROL_an_instrument_on_ONE_ladder_still_maps_by_symbol(desk):
    coordinator, basis, ethbtc, _ = desk
    assert coordinator.resolve_leg('okx', 'BTC-USDT', None) == (basis.key, 'A')
    assert coordinator.resolve_leg('okx', 'ETH-USDT-SWAP', '9999') == \
        (ethbtc.key, 'B')


def test_the_journal_files_each_fill_by_its_ticket(desk):
    coordinator, basis, ethbtc, store = desk
    coordinator.book.add_position(_position(ethbtc, 1001, 1002))
    coordinator.book.add_position(_position(basis, 3001, 3002))
    rows = [{'inst_type': 'DEAL', 'deal_id': d, 'symbol': 'BTC-USDT-SWAP',
             'position_id': t, 'fill_qty': 1, 'fill_price': 65000,
             'filled_at': 1} for d, t in (('d1', 1001), ('d2', 3002))]
    store.record_fills('okx', rows, coordinator.resolve_leg)
    filed = {f['deal_id']: (f['pair_key'], f['leg'])
             for f in store.fills()}
    assert filed == {'d1': (ethbtc.key, 'A'), 'd2': (basis.key, 'B')}


# -- what a position's P&L is a percent OF ----------------------------------------

def test_a_positions_return_is_a_share_of_notional_and_of_margin():
    from okxtrader.coordinator import position_returns
    basis, ethbtc = _pairs()
    leg_a = LegFill('okx', 'BTC-USDT-SWAP', 'SELL', 1.0, 65000.0,
                    position_tickets=[1], contract_size=0.01)
    leg_b = LegFill('okx', 'ETH-USDT-SWAP', 'BUY', 1.86, 3500.0,
                    position_tickets=[2], contract_size=0.1)
    position = SpreadPosition(ethbtc.key, SpreadSide.BUY, 1.0, leg_a, leg_b,
                              0.45, 'LIMIT', 0.186)
    got = position_returns(position, net_pnl=2.0, closing_spread=0.55,
                           margin_per_spread=400.0)
    face = 1.0 * 0.01 * 65000.0 + 1.86 * 0.1 * 3500.0       # 650 + 651
    assert got['spread_move'] == pytest.approx(0.10)       # a BUY, it rose
    assert got['notional'] == pytest.approx(face)
    assert got['pnl_pct_notional'] == pytest.approx(2.0 / face * 100)
    assert got['pnl_pct_margin'] == pytest.approx(0.5)     # 2 of 400


def test_CONTROL_an_unmeasured_return_is_None_not_zero():
    from okxtrader.coordinator import position_returns
    basis, _ = _pairs()
    leg = LegFill('okx', 'BTC-USDT', 'BUY', 0.01, 65000.0,
                  position_tickets=[1], contract_size=1.0)
    position = SpreadPosition(basis.key, SpreadSide.SELL, 1.0, leg, leg,
                              30.0, 'LIMIT', 0.01)
    got = position_returns(position, net_pnl=None, closing_spread=None,
                           margin_per_spread=None)
    assert got == {'spread_move': None, 'notional': 1300.0, 'margin': None,
                   'pnl_pct_notional': None, 'pnl_pct_margin': None}


def test_a_ladders_open_pnl_is_a_share_of_all_its_positions():
    from okxtrader.coordinator import ladder_returns
    rows = [{'notional': 1000.0, 'margin': 100.0},
            {'notional': 3000.0, 'margin': 300.0}]
    got = ladder_returns(rows, open_pnl=-4.0)
    assert got['open_pnl_pct_notional'] == pytest.approx(-0.1)
    assert got['open_pnl_pct_margin'] == pytest.approx(-1.0)
    # CONTROL: one position with no margin measured makes the margin
    # share unknown, not a share of the others alone.
    rows[1]['margin'] = None
    assert ladder_returns(rows, -4.0)['open_pnl_pct_margin'] is None


def test_a_combined_position_reads_its_total_over_its_total_bases():
    from okxtrader import combined
    rows = [{'position_id': 'P1', 'side': 'BUY', 'quantity': 1.0,
             'opened_at': 1.0, 'net_pnl': 3.0, 'notional': 1000.0,
             'margin': 100.0},
            {'position_id': 'P2', 'side': 'BUY', 'quantity': 1.0,
             'opened_at': 2.0, 'net_pnl': -1.0, 'notional': 1000.0,
             'margin': 100.0}]
    [row] = combined.combine(rows)
    assert row['pnl_pct_notional'] == pytest.approx(0.1)   # 2 of 2000
    assert row['pnl_pct_margin'] == pytest.approx(1.0)     # 2 of 200


def test_the_algo_window_is_handed_the_positions_return():
    """The Algo passes only a named list of position fields to its
    window; the % figures must be on it or the panel never sees them."""
    from okxtrader import algo
    for name in ('notional', 'margin', 'pnl_pct_notional', 'pnl_pct_margin'):
        assert name in algo._POSITION_DISPLAY
