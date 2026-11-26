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
    assert broker.symbol_info('BTC-USD_UM-261225').expiry > 0


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
    from tests.conftest import FakeBroker, FakeSymbol
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


def test_funding_reads_as_mirrors_swap(broker):
    """A perpetual's funding is money per lot per day in deposit currency
    (MT5 swap mode 4); spot and dated futures charge none (mode 0)."""
    perp = broker.symbol_info('BTC-USDT-SWAP')
    assert perp.swap_mode == 4
    assert perp.swap_long == pytest.approx(-perp.swap_short)
    rate = broker.funding('BTC-USDT-SWAP')['rate']
    mid = broker.symbol_tick('BTC-USDT-SWAP').bid
    assert abs(perp.swap_short) == pytest.approx(abs(rate) * 3 * 0.01 * mid,
                                                 rel=1e-3)
    for spot_or_future in ('BTC-USDT', 'BTC-USD_UM-261225'):
        info = broker.symbol_info(spot_or_future)
        assert (info.swap_mode, info.swap_long, info.swap_short) == (0, 0, 0)


def test_okx_fills_reach_the_journal(broker, tmp_path):
    """The journal keeps DEAL rows only. An OKX fill labelled with its
    instrument type was silently skipped, and the Fills tab stayed
    empty with trades on."""
    from okxtrader.database import Store
    broker.send_market_order('BTC-USDT-SWAP', OrderSide.SELL, 0.2,
                             comment='LADDER-X:A')
    rows = broker.order_log(1)
    assert rows and all(r['inst_type'] == 'DEAL' for r in rows)
    store = Store(str(tmp_path / 'j.db'))
    assert store.record_fills('okx', rows) == len(rows)
    [fill] = store.fills()
    assert fill['symbol'] == 'BTC-USDT-SWAP' and fill['is_ours'] == 1
    assert fill['commission'] < 0


# -- a close reduces its own position, it never opens the opposite one -------

def _orders_sent(sim):
    return len(sim.orders)


def test_a_spot_long_whose_coin_is_gone_is_not_sold_short(broker, sim):
    """Selling coin that is not there BORROWS it. A close of a spot long
    whose coin was sold elsewhere must send nothing at all."""
    r = broker.send_market_order('BTC-USDT', OrderSide.BUY, 0.003)
    sim.balances['BTC'] = 0.0                    # sold on the OKX app
    broker._bal_cache = None
    before = _orders_sent(sim)
    closed = broker.close_position_ticket('BTC-USDT', r.ticket, 0.003,
                                          OrderSide.BUY)
    assert not closed.success and 'already flat' in closed.error
    assert _orders_sent(sim) == before           # nothing went out
    assert sim.balances['BTC'] == 0.0            # and nothing was borrowed


def test_a_spot_long_closes_only_the_coin_still_held(broker, sim):
    r = broker.send_market_order('BTC-USDT', OrderSide.BUY, 0.003)
    sim.balances['BTC'] = 0.001                  # part sold elsewhere
    broker._bal_cache = None
    broker.close_position_ticket('BTC-USDT', r.ticket, 0.003, OrderSide.BUY)
    assert sim.balances['BTC'] == pytest.approx(0.0)   # flat, not short


def test_a_spot_short_buys_back_only_what_is_owed(broker, sim):
    r = broker.send_market_order('BTC-USDT', OrderSide.SELL, 0.003)
    sim.balances['BTC'] = -0.001                 # part repaid elsewhere
    broker._bal_cache = None
    broker.close_position_ticket('BTC-USDT', r.ticket, 0.003,
                                 OrderSide.SELL)
    assert sim.balances['BTC'] == pytest.approx(0.0)   # flat, not long


def test_a_perp_close_is_capped_to_what_okx_holds_on_that_side(broker, sim):
    r = broker.send_market_order('BTC-USDT-SWAP', OrderSide.BUY, 0.5)
    sim.positions[('BTC-USDT-SWAP', 'long')]['pos'] = 0.2   # cut elsewhere
    broker._pos_cache = None
    closed = broker.close_position_ticket('BTC-USDT-SWAP', r.ticket, 0.5,
                                          OrderSide.BUY)
    assert closed.success or closed.volume == pytest.approx(0.2)
    assert ('BTC-USDT-SWAP', 'long') not in sim.positions
    assert ('BTC-USDT-SWAP', 'short') not in sim.positions   # never opened


def test_a_resting_close_leaves_no_room_for_a_second_one(broker, sim):
    """A maker close already resting on the whole long: a second close
    on top would be an order with nothing left to reduce."""
    r = broker.send_market_order('BTC-USDT-SWAP', OrderSide.BUY, 0.5)
    tick = broker.symbol_tick('BTC-USDT-SWAP')
    first = broker.place_pending_limit('BTC-USDT-SWAP', OrderSide.SELL, 0.5,
                                       tick.ask + 50, position_ticket=r.ticket,
                                       post_only=False)
    assert first['ok']
    broker._pos_cache = None
    second = broker.place_pending_limit('BTC-USDT-SWAP', OrderSide.SELL, 0.5,
                                        tick.ask + 60,
                                        position_ticket=r.ticket,
                                        post_only=False)
    assert not second['ok'] and 'already flat' in second['error']


def test_a_spot_resting_close_with_no_coin_is_refused(broker, sim):
    r = broker.send_market_order('BTC-USDT', OrderSide.BUY, 0.003)
    sim.balances['BTC'] = 0.0
    broker._bal_cache = None
    tick = broker.symbol_tick('BTC-USDT')
    placed = broker.place_pending_limit('BTC-USDT', OrderSide.SELL, 0.003,
                                        tick.ask, position_ticket=r.ticket,
                                        post_only=False)
    assert not placed['ok'] and 'already flat' in placed['error']


def test_CONTROL_unreadable_okx_sends_no_close(broker):
    r = broker.send_market_order('BTC-USDT-SWAP', OrderSide.BUY, 0.2)

    def down(*a, **k):
        raise OKXError('OKX did not answer')
    broker._pos_cache = None
    broker.client.transport = down
    closed = broker.close_position_ticket('BTC-USDT-SWAP', r.ticket, 0.2,
                                          OrderSide.BUY)
    assert not closed.success and 'nothing was sent' in closed.error


def test_an_inverse_contract_is_refused_and_says_why(sim, tmp_path):
    """OKX's liquid dated futures (BTC-USD-261225) are COIN-margined:
    sized in USD, settled in coin. The desk's maths is linear, so they
    are refused by name - never mis-sized."""
    sim.instruments_extra = None
    b = OKXBroker(types.SimpleNamespace(name='okx', demo=True),
                  client=OKXClient(transport=sim.handle), ledger_path=tmp_path)
    assert b.initialize()
    b._inverse.add('BTC-USD-261225')            # as OKX lists it
    report = b.symbol_report('BTC-USD-261225')
    assert not report['found']
    assert 'coin-margined (inverse)' in report['error']
    assert 'BTC-USD_UM-261225' in report['error']   # what to use instead


def test_candles_through_okxs_rate_limit_are_asked_again(broker, sim):
    """OKX allows about 20 history-candle requests per 2 s. A 15m chart
    is two dozen pages; a refusal for the rate (50011) cut a leg's
    history short. It is now waited out and asked again."""
    real = broker.client.get
    refused = []

    def limited(path, params=None, private=False):
        if path.endswith('history-candles') and len(refused) < 2:
            refused.append(path)
            raise OKXError('OKX 50011: Too Many Requests', code='50011')
        return real(path, params, private)
    broker.client.get = limited
    bars = broker.rates('BTC-USDT-SWAP', 5, 600)
    assert len(refused) == 2
    assert len(bars) == 600
    assert all('open' in b for b in bars)


def test_candles_carry_their_volume_in_usdt(broker):
    """OKX's 8th candle field, volCcyQuote: the leg's traded value in
    USDT - the same unit on both legs, for the volume profile."""
    bars = broker.rates('BTC-USDT-SWAP', 5, 10)
    assert bars and all(b['volume'] == 7800000.0 for b in bars)


def test_each_perpetual_family_is_priced_on_its_own_fee_group(broker):
    """OKX prices families in fee groups of their own: ETH's rate is
    asked for ETH, never taken from BTC's answer an hour old."""
    real = broker.client.get
    asked = []

    def get(path, params=None, private=False):
        if path == '/api/v5/account/trade-fee':
            asked.append(params.get('instFamily'))
            maker = {'BTC-USDT': '-0.0002', 'ETH-USDT': '-0.00015'}[
                params['instFamily']]
            return [{'makerU': maker, 'takerU': '-0.0005'}]
        return real(path, params, private=private)
    broker.client.get = get
    broker._fee_cache.clear()
    assert broker.fees_bps('BTC-USDT-SWAP') == (2.0, 5.0)
    assert broker.fees_bps('ETH-USDT-SWAP') == (1.5, 5.0)
    assert broker.fees_bps('BTC-USDT-SWAP') == (2.0, 5.0)   # cached
    assert asked == ['BTC-USDT', 'ETH-USDT']


def test_the_account_ledger_is_read_page_by_page_and_from_the_archive(broker):
    import time as _time
    calls = []

    def get(path, params=None, private=False):
        calls.append((path, params.get('after')))
        if path == '/api/v5/account/bills':
            if params.get('after') is None:
                return [{'billId': str(1000 - i), 'ts': '1', 'bal': '1'}
                        for i in range(100)]
            return [{'billId': '1', 'ts': '1', 'bal': '1'}]
        return [{'billId': 'a1', 'ts': '1', 'bal': '1'}]
    broker.client.get = get
    rows = broker.account_bills((_time.time() - 30 * 86400) * 1000)
    assert len(rows) == 102
    assert calls[0] == ('/api/v5/account/bills', None)
    assert calls[1] == ('/api/v5/account/bills', '901')
    assert calls[2][0] == '/api/v5/account/bills-archive'
