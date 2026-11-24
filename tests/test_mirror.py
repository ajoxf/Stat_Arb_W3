"""The MIRROR: the Algo's trades placed again in follower accounts
(OKX Managed Trading Sub-accounts), each a multiple of the trade.

Against OKX's simulator: the followers' orders go through the desk's
own OKX broker - real request building and response parsing - only the
network is gone. And wired into the desk: a LIVE Algo trade opens the
copy, the trade's close closes it.
"""

import json
import types

import pytest

from okxtrader import mirror as m
from okxtrader.models import OrderSide
from okxtrader.okx_api import OKXClient
from okxtrader.okx_broker import OKXBroker, OrderResult
from okxtrader.okx_sim import SimOKX


@pytest.fixture
def sim():
    s = SimOKX(seed=11, autostart=False)
    yield s
    s.stop()


def sim_broker(sim, tmp_path, name):
    b = OKXBroker(types.SimpleNamespace(name=name, demo=True),
                  client=OKXClient(transport=sim.handle),
                  ledger_path=tmp_path / name)
    assert b.initialize(), b.last_error
    return b


def trade(pid='POS-1', opened_at=1000.0, eth=2.0, btc=0.1):
    """An ETH vs BTC spread: SELL ETH, BUY BTC, in contracts."""
    return {'position_id': pid, 'pair_key': 'ethbtc', 'opened_at': opened_at,
            'legs': [{'symbol': 'ETH-USDT-SWAP', 'side': 'SELL',
                      'volume': eth},
                     {'symbol': 'BTC-USDT-SWAP', 'side': 'BUY',
                      'volume': btc}]}


def mirror_of(tmp_path, *followers, now=1000.0):
    return m.Mirror(list(followers), str(tmp_path / 'mirror_state.json'),
                    clock=lambda: now, background=False)


def held(broker):
    return sorted((p['symbol'], p['side'] if 'side' in p else p.get('type'),
                   round(p['volume'], 8))
                  for p in broker.positions_by_magic())


# -- sizing -----------------------------------------------------------------

def test_a_follower_is_sized_down_to_its_lot_step_never_up():
    assert m.round_down(2.0 * 0.5, 0.01, 0.01) == 1.0
    assert m.round_down(0.37 * 0.5, 0.01, 0.01) == 0.18     # not 0.185
    assert m.round_down(0.3, 0.1, 0.1) == 0.3                # no dust
    assert m.round_down(0.005, 0.01, 0.01) == 0.0            # under the min


# -- off unless asked ---------------------------------------------------------

def test_no_mirror_section_no_mirror(tmp_path):
    path = str(tmp_path / 's.json')
    assert m.Mirror.from_raw({}, path) is None
    assert m.Mirror.from_raw({'mirror': {'enabled': False, 'followers': [
        {'name': 'c1', 'multiple': 1}]}}, path) is None
    # Switched on but no usable follower: still nothing.
    assert m.Mirror.from_raw({'mirror': {'enabled': True, 'followers': [
        {'name': 'c1', 'multiple': 0}, {'multiple': 1}]}}, path) is None
    on = m.Mirror.from_raw({'mirror': {'enabled': True, 'followers': [
        {'name': 'c1', 'multiple': 0.5, 'pairs': ['ethbtc']}]}}, path)
    assert on.followers['c1'].multiple == 0.5
    assert on.followers['c1'].follows('ethbtc')
    assert not on.followers['c1'].follows('solbtc')


# -- open and close, against the simulator ------------------------------------

def test_a_trade_is_copied_at_its_multiple_and_closed_when_it_closes(
        sim, tmp_path):
    client = sim_broker(sim, tmp_path, 'c1')
    mirror = mirror_of(tmp_path, m.Follower('c1', 0.5, broker=client))
    mirror.observe([trade()])
    record = mirror.records['POS-1']['followers']['c1']
    assert record['status'] == m.OPEN, record
    legs = {leg['symbol']: leg for leg in record['legs']}
    assert legs['ETH-USDT-SWAP']['volume'] == 1.0          # 2.0 x 0.5
    assert legs['ETH-USDT-SWAP']['side'] == 'SELL'
    assert legs['BTC-USDT-SWAP']['volume'] == 0.05         # 0.1 x 0.5
    assert len(client.positions_by_magic()) == 2
    # Seen again: nothing more is sent.
    mirror.observe([trade()])
    assert len(client.positions_by_magic()) == 2
    # The trade has gone: the copy closes.
    mirror.observe([])
    assert mirror.records['POS-1']['followers']['c1']['status'] == m.CLOSED
    assert client.positions_by_magic() == []
    # ...and the record survives a restart.
    saved = json.load(open(tmp_path / 'mirror_state.json'))
    assert saved['records']['POS-1']['followers']['c1']['status'] == m.CLOSED


def test_each_follower_is_its_own_size_and_its_own_account(sim, tmp_path):
    one = sim_broker(sim, tmp_path, 'c1')
    two = sim_broker(sim, tmp_path, 'c2')
    mirror = mirror_of(tmp_path, m.Follower('c1', 1.0, broker=one),
                       m.Follower('c2', 3.0, broker=two))
    mirror.observe([trade()])
    sizes = {name: {leg['symbol']: leg['volume'] for leg in fr['legs']}
             for name, fr in mirror.records['POS-1']['followers'].items()}
    assert sizes['c1'] == {'ETH-USDT-SWAP': 2.0, 'BTC-USDT-SWAP': 0.1}
    assert sizes['c2'] == {'ETH-USDT-SWAP': 6.0, 'BTC-USDT-SWAP': 0.3}


def test_a_trade_first_seen_late_is_not_copied(sim, tmp_path):
    client = sim_broker(sim, tmp_path, 'c1')
    mirror = mirror_of(tmp_path, m.Follower('c1', 1.0, broker=client),
                       now=1000.0 + 300)
    mirror.observe([trade(opened_at=1000.0)])
    fr = mirror.records['POS-1']['followers']['c1']
    assert fr['status'] == m.SKIPPED and 'late' in fr['why']
    assert client.positions_by_magic() == []
    mirror.observe([])                       # and nothing to close
    assert client.positions_by_magic() == []


def test_too_small_for_the_follower_is_skipped_and_said(sim, tmp_path):
    client = sim_broker(sim, tmp_path, 'c1')
    mirror = mirror_of(tmp_path, m.Follower('c1', 0.01, broker=client))
    mirror.observe([trade(eth=0.5, btc=0.1)])     # BTC 0.001: under 0.01
    fr = mirror.records['POS-1']['followers']['c1']
    assert fr['status'] == m.SKIPPED and 'smallest size' in fr['why']
    assert client.positions_by_magic() == []


def test_never_one_leg_a_second_leg_that_fails_takes_the_first_off(
        sim, tmp_path):
    client = sim_broker(sim, tmp_path, 'c1')
    real = client.send_market_order

    def second_fails(symbol, side, volume, **kw):
        if symbol == 'BTC-USDT-SWAP':
            return OrderResult(False, error='51008 insufficient margin')
        return real(symbol, side, volume, **kw)
    client.send_market_order = second_fails
    mirror = mirror_of(tmp_path, m.Follower('c1', 1.0, broker=client))
    mirror.observe([trade()])
    fr = mirror.records['POS-1']['followers']['c1']
    assert fr['status'] == m.FAILED
    assert 'insufficient margin' in fr['why'] and 'closed again' in fr['why']
    assert client.positions_by_magic() == []          # no ETH left alone
    assert mirror.followers['c1'].last_error


def test_a_follower_that_fails_never_stops_the_others(sim, tmp_path):
    good = sim_broker(sim, tmp_path, 'c2')

    class Down:
        def symbol_info(self, symbol):
            raise RuntimeError('OKX 50113 invalid sign')
    mirror = mirror_of(tmp_path, m.Follower('c1', 1.0, broker=Down()),
                       m.Follower('c2', 1.0, broker=good))
    mirror.observe([trade()])
    followers = mirror.records['POS-1']['followers']
    assert followers['c1']['status'] == m.FAILED
    assert 'invalid sign' in followers['c1']['why']
    assert followers['c2']['status'] == m.OPEN
    assert len(good.positions_by_magic()) == 2


def test_switched_off_it_opens_nothing_new_but_still_closes(sim, tmp_path):
    client = sim_broker(sim, tmp_path, 'c1')
    follower = m.Follower('c1', 1.0, broker=client)
    mirror = mirror_of(tmp_path, follower)
    mirror.observe([trade('POS-1')])
    follower.enabled = False
    mirror.observe([trade('POS-1'), trade('POS-2')])
    assert mirror.records['POS-2']['followers']['c1']['status'] == m.SKIPPED
    mirror.observe([])
    assert mirror.records['POS-1']['followers']['c1']['status'] == m.CLOSED
    assert client.positions_by_magic() == []


def test_a_close_that_fails_is_tried_again(sim, tmp_path):
    client = sim_broker(sim, tmp_path, 'c1')
    clock = {'now': 1000.0}
    mirror = m.Mirror([m.Follower('c1', 1.0, broker=client)],
                      str(tmp_path / 'mirror_state.json'),
                      clock=lambda: clock['now'], background=False)
    mirror.observe([trade()])
    real = client.close_position_ticket
    client.close_position_ticket = lambda *a, **k: OrderResult(
        False, error='50001 service unavailable')
    mirror.observe([])
    fr = mirror.records['POS-1']['followers']['c1']
    assert fr['status'] == m.CLOSE_FAILED and 'unavailable' in fr['why']
    client.close_position_ticket = real
    mirror.observe([])                       # too soon: not yet
    assert fr['status'] == m.CLOSE_FAILED
    clock['now'] += m.CLOSE_RETRY_SEC
    mirror.observe([])
    assert mirror.records['POS-1']['followers']['c1']['status'] == m.CLOSED
    assert client.positions_by_magic() == []


def test_the_snapshot_says_who_follows_and_what_they_hold(sim, tmp_path):
    client = sim_broker(sim, tmp_path, 'c1')
    mirror = mirror_of(tmp_path, m.Follower('c1', 0.5, broker=client))
    mirror.observe([trade()])
    snap = mirror.snapshot()
    assert snap['on'] and snap['followers'][0]['name'] == 'c1'
    assert snap['trades'][0]['position_id'] == 'POS-1'
    assert snap['trades'][0]['followers']['c1']['status'] == m.OPEN


class SlowAccount:
    """A follower account where every order takes 0.2 s."""

    def __init__(self, fail_close=False):
        import threading
        self.sent, self.closed, self.next = [], [], 0
        self.fail_close = fail_close
        self.lock = threading.Lock()

    def symbol_info(self, symbol):
        return types.SimpleNamespace(volume_step=0.01, volume_min=0.01)

    def send_market_order(self, symbol, side, volume, **kw):
        import time
        time.sleep(0.2)
        with self.lock:
            self.next += 1
            self.sent.append((symbol, side.value, volume))
            return OrderResult(True, executed_price=1.0, ticket=self.next,
                               volume=volume)

    def close_position_ticket(self, symbol, ticket, volume, side, **kw):
        import time
        time.sleep(0.05)
        if self.fail_close:
            return OrderResult(False, error='50001 service unavailable')
        self.closed.append((symbol, ticket, volume))
        return OrderResult(True, ticket=ticket, volume=volume)


def test_fifteen_followers_trade_side_by_side_not_one_after_another(
        tmp_path):
    """Two legs at 0.2 s each is 0.4 s an account: fifteen in a row would
    be 6 s, the last account trading a price the first had left. Each has
    its own thread, so they all go on in well under 2 s - and every one
    gets both legs at its own multiple."""
    import time
    accounts = {f'c{i:02d}': SlowAccount() for i in range(15)}
    mirror = m.Mirror([m.Follower(name, 0.5 + i * 0.1, broker=acct)
                       for i, (name, acct) in enumerate(accounts.items())],
                      str(tmp_path / 'mirror_state.json'),
                      clock=lambda: 1000.0, background=True)
    started = time.time()
    mirror.observe([trade()])
    assert mirror.wait(10.0)
    assert time.time() - started < 2.0
    for i, (name, acct) in enumerate(accounts.items()):
        assert len(acct.sent) == 2, name
        eth = next(v for s, _side, v in acct.sent if s == 'ETH-USDT-SWAP')
        assert eth == pytest.approx(m.round_down(2.0 * (0.5 + i * 0.1),
                                                 0.01, 0.01))
        assert mirror.records['POS-1']['followers'][name]['status'] == m.OPEN
    mirror.observe([])
    assert mirror.wait(10.0)
    assert all(len(a.closed) == 2 for a in accounts.values())
    snap = mirror.snapshot()
    assert snap['summary']['followers'] == 15
    assert snap['summary']['holding'] == 0
    assert snap['trades'][0]['counts'] == {'CLOSED': 15}


def test_the_snapshot_counts_each_follower_and_flags_what_needs_a_person(
        tmp_path):
    ok, stuck = SlowAccount(), SlowAccount(fail_close=True)
    mirror = mirror_of(tmp_path, m.Follower('ok', 1.0, broker=ok),
                       m.Follower('stuck', 1.0, broker=stuck),
                       now=86400.0 * 10 + 100)
    mirror.observe([trade(opened_at=86400.0 * 10 + 90)])
    snap = mirror.snapshot()
    by = {f['name']: f for f in snap['followers']}
    assert by['ok']['holding'] == 1 and by['ok']['copied_today'] == 1
    assert snap['summary']['holding'] == 2 and snap['summary']['attention'] \
        == 0
    # The trade closes; one account's close fails - it still HOLDS a
    # copy of a trade that is gone. That is what a person must see.
    mirror.observe([])
    snap = mirror.snapshot()
    by = {f['name']: f for f in snap['followers']}
    assert by['ok']['holding'] == 0
    assert by['stuck']['failed_today'] == 1
    assert snap['summary']['attention'] == 1
    [alert] = [a for a in snap['attention'] if a['severity'] == 'high']
    assert alert['follower'] == 'stuck' and 'unavailable' in alert['why']


def test_old_finished_records_are_pruned_live_ones_never(tmp_path):
    acct = SlowAccount()
    acct.send_market_order = lambda s, side, v, **k: OrderResult(
        True, ticket=1, volume=v)
    acct.close_position_ticket = lambda *a, **k: OrderResult(True, volume=1)
    mirror = mirror_of(tmp_path, m.Follower('c1', 1.0, broker=acct))
    mirror.KEEP_TRADES = 5
    for i in range(8):
        mirror.observe([trade(f'P{i}', opened_at=1000.0 - 10 + i)])
        mirror.observe([])
    mirror.observe([trade('LIVE', opened_at=999.0)])
    assert 'LIVE' in mirror.records
    assert len(mirror.records) <= 6
