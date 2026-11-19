"""The OKX broker against the simulated exchange.

The simulator answers OKX's own v5 envelope, so these drive the broker's
real request building and response parsing — only the network is gone.
"""

import types

import pytest

from okxtrader.models import MAGIC_NUMBER, OrderSide
from okxtrader.okx_api import OKXClient, OKXError
from okxtrader.okx_broker import OKXBroker, fmt_price, fmt_size
from okxtrader.okx_sim import SimOKX


@pytest.fixture
def sim():
    s = SimOKX(seed=11, autostart=False)
    yield s
    s.stop()


@pytest.fixture
def broker(sim, tmp_path):
    b = OKXBroker(types.SimpleNamespace(name='okx', demo=True),
                  client=OKXClient(transport=sim.handle),
                  ledger_path=tmp_path)
    assert b.initialize(), b.last_error
    return b


def test_instrument_specs_are_okxs_own(broker):
    spot = broker.symbol_info('BTC-USDT')
    perp = broker.symbol_info('ETH-USDT-SWAP')
    assert (spot.contract_size, spot.volume_min) == (1.0, 0.00001)
    assert (perp.contract_size, perp.volume_step, perp.tick_size) == \
        (0.1, 0.01, 0.01)
    assert broker.symbol_info('BTC-USDT-261225').expiry > 0


def test_sizes_and_prices_are_legal():
    assert fmt_size(0.12345, '0.01') == '0.12'
    assert fmt_size(0.3, '0.1') == '0.3'            # no 0.29999 dust
    assert fmt_price(65000.06, '0.1') == '65000.1'


def test_the_fee_tier_is_the_accounts(broker, sim):
    assert broker.fees_bps('BTC-USDT-SWAP') == (2.0, 5.0)
    assert broker.fees_bps('BTC-USDT') == (8.0, 10.0)
    sim.vip = 'VIP1'
    broker._fee_cache.clear()
    assert broker.fees_bps('BTC-USDT-SWAP') == (1.0, 3.0)


def test_every_click_is_its_own_ticket(broker):
    a = broker.send_market_order('BTC-USDT-SWAP', OrderSide.SELL, 0.2)
    b = broker.send_market_order('BTC-USDT-SWAP', OrderSide.SELL, 0.1)
    assert a.success and b.success and a.ticket != b.ticket
    held = {p['ticket']: p['volume'] for p in broker.positions_by_magic()}
    assert held == {a.ticket: 0.2, b.ticket: 0.1}


def test_closing_one_ticket_leaves_the_other(broker):
    a = broker.send_market_order('BTC-USDT-SWAP', OrderSide.BUY, 0.2)
    b = broker.send_market_order('BTC-USDT-SWAP', OrderSide.BUY, 0.3)
    closed = broker.close_position_ticket('BTC-USDT-SWAP', a.ticket, 0.2,
                                          OrderSide.BUY)
    assert closed.success
    assert [p['ticket'] for p in broker.positions_by_magic()] == [b.ticket]
    assert broker._exchange_positions() == {('BTC-USDT-SWAP', 'long'):
                                            pytest.approx(0.3)}


def test_a_spot_short_is_a_borrow_and_closes(broker, sim):
    r = broker.send_market_order('BTC-USDT', OrderSide.SELL, 0.003)
    assert r.success and sim.balances['BTC'] == pytest.approx(-0.003)
    c = broker.close_position_ticket('BTC-USDT', r.ticket, 0.003,
                                     OrderSide.SELL)
    assert c.success and sim.balances['BTC'] == pytest.approx(0.0)


def test_post_only_never_takes(broker):
    tick = broker.symbol_tick('ETH-USDT-SWAP')
    refused = broker.place_pending_limit('ETH-USDT-SWAP', OrderSide.BUY,
                                         0.5, tick.ask + 1)
    assert not refused['ok'] and '51124' in refused['error']


def test_a_resting_order_fills_into_a_position(broker, sim):
    tick = broker.symbol_tick('ETH-USDT-SWAP')
    placed = broker.place_pending_limit('ETH-USDT-SWAP', OrderSide.BUY, 0.5,
                                        tick.bid)
    assert placed['ok']
    sim.maker_fill_prob = 1.0
    sim.step()
    state = broker.order_fill_state(placed['ticket'])
    assert state['filled_volume'] == pytest.approx(0.5)
    assert state['position_tickets'] == [placed['ticket']]
    assert [p['ticket'] for p in broker.positions_by_magic()] == \
        [placed['ticket']]


def test_a_position_closed_on_the_okx_app_reads_as_gone(broker, sim,
                                                       monkeypatch):
    monkeypatch.setattr(OKXBroker, 'TRIM_GRACE_SEC', 0.0)
    monkeypatch.setattr(OKXBroker, 'TRIM_PERSIST_SEC', 0.0)
    r = broker.send_market_order('BTC-USDT-SWAP', OrderSide.SELL, 0.2)
    sim.handle('POST', '/api/v5/trade/order', {}, {
        'instId': 'BTC-USDT-SWAP', 'tdMode': 'cross', 'side': 'buy',
        'posSide': 'short', 'ordType': 'market', 'sz': '0.2'})
    broker._pos_cache = None
    assert broker.positions_by_magic() == []
    assert r.ticket


def test_CONTROL_a_fresh_shortfall_is_not_believed(broker, sim):
    """The same outside close, but only just seen: OKX's view can trail a
    fill, so one read is not enough to throw a position away."""
    r = broker.send_market_order('BTC-USDT-SWAP', OrderSide.SELL, 0.2)
    sim.handle('POST', '/api/v5/trade/order', {}, {
        'instId': 'BTC-USDT-SWAP', 'tdMode': 'cross', 'side': 'buy',
        'posSide': 'short', 'ordType': 'market', 'sz': '0.2'})
    broker._pos_cache = None
    assert [p['ticket'] for p in broker.positions_by_magic()] == [r.ticket]


def test_unreadable_positions_are_unknown_not_flat(broker):
    broker.send_market_order('BTC-USDT-SWAP', OrderSide.SELL, 0.2)

    def down(*a, **k):
        raise OKXError('OKX did not answer')
    broker.client.transport = down
    broker._pos_cache = None
    assert broker.positions_by_magic() is None


def test_an_already_flat_close_says_so(broker, sim, monkeypatch):
    r = broker.send_market_order('BTC-USDT-SWAP', OrderSide.SELL, 0.2)
    sim.positions.clear()                     # liquidated, say
    broker._pos_cache = None
    closed = broker.close_position_ticket('BTC-USDT-SWAP', r.ticket, 0.2,
                                          OrderSide.SELL)
    assert not closed.success and 'already flat' in closed.error


def test_the_ledger_survives_a_restart(broker, sim, tmp_path):
    r = broker.send_market_order('BTC-USDT-SWAP', OrderSide.BUY, 0.2)
    again = OKXBroker(types.SimpleNamespace(name='okx', demo=True),
                      client=OKXClient(transport=sim.handle),
                      ledger_path=tmp_path)
    assert again.initialize()
    assert [p['ticket'] for p in again.positions_by_magic()] == [r.ticket]


def test_orders_a_crash_left_behind_are_found(broker, sim):
    """An order of ours at OKX that the ledger lost is still ours: the
    startup sweep must be able to cancel it."""
    tick = broker.symbol_tick('ETH-USDT-SWAP')
    sim.handle('POST', '/api/v5/trade/order', {}, {
        'instId': 'ETH-USDT-SWAP', 'tdMode': 'cross', 'side': 'buy',
        'posSide': 'long', 'ordType': 'post_only', 'px': str(tick.bid - 5),
        'sz': '0.5', 'clOrdId': 'nx999abc'})
    sim.handle('POST', '/api/v5/trade/order', {}, {
        'instId': 'ETH-USDT-SWAP', 'tdMode': 'cross', 'side': 'buy',
        'posSide': 'long', 'ordType': 'post_only', 'px': str(tick.bid - 6),
        'sz': '0.5', 'clOrdId': 'manual1'})           # the trader's own
    found = broker.pending_orders_by_magic()
    assert len(found) == 1 and found[0]['comment'] == 'recovered'
    assert broker.cancel_pending(found[0]['ticket'])['cancelled']


def test_the_journal_rows_have_the_engines_keys(broker):
    from tests_okx.conftest import FakeBroker, FakeSymbol
    broker.send_market_order('BTC-USDT-SWAP', OrderSide.SELL, 0.2,
                             comment='ours')
    rows = broker.order_log(1)
    fake = FakeBroker('f', [FakeSymbol('X', 1.0, 1.1)])
    fake.initialize()
    fake.send_market_order('X', OrderSide.BUY, 0.1)
    # 'account' is stamped by LocalLeg on top of any broker's rows.
    assert set(rows[0]) == set(fake.order_log(1)[0]) - {'account'}
    assert rows[0]['magic'] == MAGIC_NUMBER and rows[0]['commission'] < 0


def test_refusals_carry_okxs_words(broker):
    bad = broker.send_market_order('BTC-USDT-SWAP', OrderSide.BUY, 0.001)
    assert not bad.success and 'rounds to zero' in bad.error
