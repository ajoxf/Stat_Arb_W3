"""OKX's WebSocket price feed, and the parallel REST fallback.

The feed is tested with a stand-in for the socket (this box cannot open
a WebSocket to OKX); the messages are OKX's own shapes, as its v5
public channel sends them.
"""

import json
import threading
import time
import types

import pytest

from okxtrader.okx_api import OKXClient
from okxtrader.okx_broker import OKXBroker
from okxtrader.okx_sim import SimOKX
from okxtrader.okx_ws import STALE_SEC, PriceStream


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ticker(inst, bid, ask, ts=1795100000000):
    return json.dumps({'arg': {'channel': 'tickers', 'instId': inst},
                       'data': [{'instId': inst, 'bidPx': str(bid),
                                 'askPx': str(ask), 'last': str(bid),
                                 'ts': str(ts)}]})


class FakeApp:
    """Stands in for websocket.WebSocketApp: records what was sent."""

    def __init__(self, stream):
        self.stream = stream
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def close(self):
        pass


def connected_stream(clock):
    s = PriceStream('wss://test', clock=clock, app_factory=FakeApp)
    s._app = FakeApp(s)
    s.on_open()
    return s


def test_a_pushed_ticker_is_the_price(tmp_path):
    clock = Clock()
    s = connected_stream(clock)
    s.watch({'BTC-USDT-SWAP'})
    sub = json.loads(s._app.sent[-1])
    assert sub == {'op': 'subscribe', 'args': [
        {'channel': 'tickers', 'instId': 'BTC-USDT-SWAP'},
        {'channel': 'books5', 'instId': 'BTC-USDT-SWAP'}]}
    s.on_message(json.dumps({'event': 'subscribe', 'arg': {
        'channel': 'tickers', 'instId': 'BTC-USDT-SWAP'}}))
    s.on_message(ticker('BTC-USDT-SWAP', 63400.1, 63400.2))
    assert s.row('BTC-USDT-SWAP')['bidPx'] == '63400.1'


def test_a_quiet_instrument_on_a_live_connection_is_still_believed():
    clock = Clock()
    s = connected_stream(clock)
    s.watch({'ETH-USDT-SWAP'})
    s.on_message(ticker('ETH-USDT-SWAP', 1796.3, 1796.4))
    clock.t += STALE_SEC - 5
    s.on_message('pong')                       # the connection is alive
    clock.t += 10
    assert s.row('ETH-USDT-SWAP') is not None


def test_CONTROL_a_feed_heard_nothing_from_is_not_believed():
    clock = Clock()
    s = connected_stream(clock)
    s.watch({'ETH-USDT-SWAP'})
    s.on_message(ticker('ETH-USDT-SWAP', 1796.3, 1796.4))
    clock.t += STALE_SEC + 1
    assert s.row('ETH-USDT-SWAP') is None      # -> REST


def test_a_dropped_connection_is_not_believed_and_resubscribes_on_return():
    clock = Clock()
    s = connected_stream(clock)
    s.watch({'BTC-USDT-SWAP', 'BTC-USDT'})
    s.on_message(ticker('BTC-USDT-SWAP', 1, 2))
    s.on_close()
    assert s.row('BTC-USDT-SWAP') is None
    s._app = FakeApp(s)
    s.on_open()
    args = json.loads(s._app.sent[-1])['args']
    assert {a['instId'] for a in args} == {'BTC-USDT-SWAP', 'BTC-USDT'}


def test_an_okx_refusal_is_kept_in_okxs_words():
    s = connected_stream(Clock())
    s.on_message(json.dumps({'event': 'error', 'code': '60018',
                             'msg': "Wrong URL or channel:tickers,instId:X "
                                    "doesn't exist."}))
    assert s.last_error.startswith('OKX 60018')


# -- the broker: feed first, REST when the feed cannot be believed ---------

@pytest.fixture
def broker(tmp_path):
    sim = SimOKX(seed=4, autostart=False)
    b = OKXBroker(types.SimpleNamespace(name='okx', demo=True),
                  client=OKXClient(transport=sim.handle), ledger_path=tmp_path)
    assert b.initialize()
    yield b, sim
    sim.stop()


def test_the_broker_reads_the_feed_and_makes_no_rest_call(broker):
    b, sim = broker
    b.stream = connected_stream(time.time)
    b.symbol_tick('BTC-USDT-SWAP')             # watched, subscribed
    b.stream.on_message(ticker('BTC-USDT-SWAP', 61000.0, 61000.5))
    calls = []
    b.client.transport, real = (lambda *a: calls.append(a) or real(*a),
                                b.client.transport)
    tick = b.symbol_tick('BTC-USDT-SWAP')
    assert (tick.bid, tick.ask) == (61000.0, 61000.5)
    assert calls == []


def test_CONTROL_a_dead_feed_falls_back_to_rest(broker):
    b, sim = broker
    b.stream = connected_stream(time.time)
    b.symbol_tick('BTC-USDT-SWAP')
    b.stream.on_message(ticker('BTC-USDT-SWAP', 61000.0, 61000.5))
    b.stream.on_close()
    b._row_cache = {}
    tick = b.symbol_tick('BTC-USDT-SWAP')
    bid, _ = sim._touch('BTC-USDT-SWAP')
    assert tick.bid == pytest.approx(bid)       # the exchange's, not 61000


def test_rest_fetches_every_watched_instrument_at_once(tmp_path):
    """Four instruments at 200 ms each: one round, not four."""
    sim = SimOKX(seed=5, autostart=False)
    lock = threading.Lock()

    def slow(method, path, params=None, body=None):
        if path == '/api/v5/market/ticker':
            time.sleep(0.2)
        with lock:
            return sim.handle(method, path, params, body)
    b = OKXBroker(types.SimpleNamespace(name='okx', demo=True),
                  client=OKXClient(transport=slow), ledger_path=tmp_path)
    assert b.initialize()
    b.parallel = True
    symbols = ['BTC-USDT', 'ETH-USDT', 'BTC-USDT-SWAP', 'ETH-USDT-SWAP']
    for s in symbols:
        b._watched.add(s)
    t0 = time.time()
    ticks = [b.symbol_tick(s) for s in symbols]
    assert all(t and t.bid > 0 for t in ticks)
    assert time.time() - t0 < 0.6, time.time() - t0
    sim.stop()


def test_a_pushed_book_is_the_ladders_depth(broker):
    b, sim = broker
    b.stream = connected_stream(time.time)
    b.depth('BTC-USDT-SWAP')                   # watched -> subscribed
    sent = [json.loads(m) for m in b.stream._app.sent]
    channels = {a['channel'] for m in sent for a in m.get('args', [])}
    assert channels == {'tickers', 'books5'}
    b.stream.on_message(json.dumps({
        'arg': {'channel': 'books5', 'instId': 'BTC-USDT-SWAP'},
        'data': [{'asks': [['61000.5', '12', '0', '3']],
                  'bids': [['61000.4', '7', '0', '2']], 'ts': '1'}]}))
    calls = []
    b.client.transport, real = (lambda *a: calls.append(a) or real(*a),
                                b.client.transport)
    assert b.depth('BTC-USDT-SWAP') == [
        {'type': 'ask', 'price': 61000.5, 'volume': 12.0},
        {'type': 'bid', 'price': 61000.4, 'volume': 7.0}]
    assert calls == []
