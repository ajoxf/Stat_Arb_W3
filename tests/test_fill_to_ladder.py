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
