"""One leg connection, many threads: one request at a time, and a
connection that fails opens again by itself.

2026-11-18, live: the pair chart asked a leg for 3,300 one-minute bars
on the command thread while the price poll asked the same leg for a
tick. The two read each other's replies - "Expecting ':' delimiter:
line 1 column 196611" - every reply after that was read out of step,
three oil ladders went dark, the reconciler could not read the account,
and the leg stayed that way until the engine was restarted for an
unrelated reason.
"""

import contextlib
import threading

import pytest

from conftest import FakeBroker, FakeSymbol
from okxtrader.leg_runner import LegServer
from okxtrader.legs import RemoteLeg


@pytest.fixture
def server():
    symbol = FakeSymbol('USOIL_F7', 92.06, 92.10, contract_size=1000.0,
                        volume_min=0.01, volume_step=0.01)
    broker = FakeBroker('cfi', [symbol])
    broker.initialize()
    # A big answer, the size a chart's history is.
    broker.bars['USOIL_F7'] = [
        {'time': 1_800_000_000 + 60 * i, 'close': 92.0 + (i % 50) / 100.0,
         'spread': 3, 'point': 0.001} for i in range(5000)]
    srv = LegServer(broker, '127.0.0.1', 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.stop()


def hammer(leg, rounds=25):
    """The chart thread and the poll thread, on one leg at once."""
    wrong = []

    def chart():
        for _ in range(rounds):
            try:
                bars = leg.rates('USOIL_F7', 1, 5000)
            except Exception as e:    # the race can raise outright too
                wrong.append(f'rates raised {e!r}')
                continue
            if not bars or len(bars) != 5000:
                wrong.append('rates')

    def poll():
        for _ in range(rounds * 8):
            try:
                tick = leg.tick('USOIL_F7')
            except Exception as e:
                wrong.append(f'tick raised {e!r}')
                continue
            if not tick or tick.get('bid') != 92.06:
                wrong.append('tick')

    threads = [threading.Thread(target=chart), threading.Thread(target=poll)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return wrong


def test_two_threads_on_one_leg_each_get_their_own_answer(server):
    leg = RemoteLeg('cfi', f'127.0.0.1:{server.port}', timeout=5.0)
    assert leg.connect(retries=1, delay=0)
    assert hammer(leg) == []


def test_CONTROL_without_the_lock_they_read_each_others_replies(server):
    """The incident, reproduced: the same two threads with the lock
    taken away. Without this, a lock that did nothing would pass."""
    leg = RemoteLeg('cfi', f'127.0.0.1:{server.port}', timeout=5.0)
    assert leg.connect(retries=1, delay=0)
    leg._io = contextlib.nullcontext()
    leg.RECONNECT_SEC = 3600          # and no healing, as it was
    assert hammer(leg) != []


def test_a_failed_connection_opens_again_by_itself(server):
    leg = RemoteLeg('cfi', f'127.0.0.1:{server.port}', timeout=5.0)
    assert leg.connect(retries=1, delay=0)
    leg.close()                       # what an IPC failure leaves behind
    assert leg.tick('USOIL_F7')['bid'] == 92.06


def test_it_is_not_retried_on_every_request(server):
    """A runner that is really gone is asked again every couple of
    seconds, not on every one of the poll's requests."""
    leg = RemoteLeg('cfi', f'127.0.0.1:{server.port}', timeout=5.0)
    assert leg.connect(retries=1, delay=0)
    leg.close()
    tries = []
    leg.connect = lambda **kw: tries.append(1) or False
    for _ in range(20):
        assert leg.tick('USOIL_F7') is None
    assert len(tries) == 1
