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


# -- limits, maker / taker, P&L and reports ----------------------------------

class Account:
    """A scripted follower account: resting orders fill after `fill_after`
    looks (None: never), market orders fill at the touch."""

    def __init__(self, fill_after=1, bid=100.0, ask=100.1, margin=50.0,
                 fees=(2.0, 5.0)):
        self.fill_after, self.bid, self.ask = fill_after, bid, ask
        self.margin, self.fees = margin, fees
        self.next, self.orders, self.market, self.closes = 0, {}, [], []
        self.amends, self.cancels = [], []

    def symbol_info(self, symbol):
        return types.SimpleNamespace(volume_step=0.01, volume_min=0.01,
                                     contract_size=0.1)

    def symbol_tick(self, symbol):
        return types.SimpleNamespace(bid=self.bid, ask=self.ask)

    def margin_for(self, symbol, side, volume, price=None):
        return self.margin * volume

    def fees_bps(self, symbol):
        return self.fees

    def place_pending_limit(self, symbol, side, volume, price, comment='',
                            position_ticket=None, post_only=True):
        self.next += 1
        self.orders[self.next] = {'symbol': symbol, 'side': side.value,
                                  'volume': volume, 'price': price, 'looks': 0,
                                  'close': position_ticket}
        return {'ok': True, 'ticket': self.next, 'price': price}

    def order_fill_state(self, ticket):
        o = self.orders[ticket]
        o['looks'] += 1
        done = self.fill_after is not None and o['looks'] >= self.fill_after
        return {'ok': True, 'filled_volume': o['volume'] if done else 0.0,
                'price': o['price'] if done else None,
                'position_tickets': [ticket] if done else [],
                'still_open': not done}

    def modify_pending(self, ticket, price):
        self.amends.append((ticket, price))
        self.orders[ticket]['price'] = price
        return {'ok': True}

    def cancel_pending(self, ticket):
        self.cancels.append(ticket)
        return {'ok': True, 'filled_volume': 0.0, 'price': None,
                'position_tickets': [], 'still_open': False}

    def send_market_order(self, symbol, side, volume, **kw):
        self.next += 1
        price = self.ask if side is OrderSide.BUY else self.bid
        self.market.append((symbol, side.value, volume, price))
        return OrderResult(True, executed_price=price, ticket=self.next,
                           volume=volume)

    def close_position_ticket(self, symbol, ticket, volume, side, **kw):
        price = self.bid if side is OrderSide.BUY else self.ask
        self.closes.append((symbol, ticket, volume, price))
        return OrderResult(True, executed_price=price, ticket=ticket,
                           volume=volume)


def quiet_mirror(tmp_path, *followers, now=86400.0 * 100 + 50):
    return m.Mirror(list(followers), str(tmp_path / 'mirror_state.json'),
                    clock=lambda: now, background=False,
                    sleep=lambda s: None)


def maker_trade(pid='POS-1', opened_at=86400.0 * 100 + 40):
    return dict(trade(pid, opened_at=opened_at), style='MAKER')


def test_a_maker_trade_is_copied_as_maker_first_leg_rests_second_hedges(
        tmp_path):
    acct = Account(fill_after=2)
    mirror = quiet_mirror(tmp_path, m.Follower('c1', 1.0, broker=acct))
    mirror.observe([maker_trade()])
    fr = mirror.records['POS-1']['followers']['c1']
    assert fr['status'] == m.OPEN and fr['style'] == 'MAKER'
    # ETH (the first leg) rested - a SELL at this account's offer...
    [order] = acct.orders.values()
    assert (order['symbol'], order['side'], order['price']) == \
        ('ETH-USDT-SWAP', 'SELL', 100.1)
    # ...and only BTC crossed.
    assert [mk[0] for mk in acct.market] == ['BTC-USDT-SWAP']
    kinds = {p['symbol']: p['kind'] for p in fr['legs']}
    assert kinds == {'ETH-USDT-SWAP': 'maker', 'BTC-USDT-SWAP': 'taker'}


def test_a_maker_leg_follows_the_touch_and_is_never_chased_on_entry(
        tmp_path):
    acct = Account(fill_after=None)              # it never fills
    follower = m.Follower('c1', 1.0, broker=acct, maker_timeout_sec=3)
    mirror = quiet_mirror(tmp_path, follower)
    real = acct.symbol_tick
    looks = {'n': 0}

    def moving(symbol):
        looks['n'] += 1
        acct.ask = 100.1 + 0.1 * looks['n']      # the offer walks up
        return real(symbol)
    acct.symbol_tick = moving
    mirror.observe([maker_trade()])
    fr = mirror.records['POS-1']['followers']['c1']
    assert fr['status'] == m.SKIPPED and 'never chases' in fr['why']
    assert acct.amends                            # re-priced to the touch
    assert acct.cancels and acct.market == []     # cancelled, nothing crossed


def test_execution_TAKER_crosses_even_when_the_trade_was_maker(tmp_path):
    acct = Account()
    mirror = quiet_mirror(tmp_path, m.Follower('c1', 1.0, broker=acct,
                                               execution='TAKER'))
    mirror.observe([maker_trade()])
    assert acct.orders == {} and len(acct.market) == 2


def test_a_maker_exit_rests_then_whatever_is_left_crosses(tmp_path):
    acct = Account(fill_after=1)
    follower = m.Follower('c1', 1.0, broker=acct, maker_timeout_sec=2)
    mirror = quiet_mirror(tmp_path, follower)
    mirror.observe([trade(opened_at=86400.0 * 100 + 40)])    # taker in
    acct.fill_after = None                                   # exit: no fill
    mirror.observe([], close_style=lambda pid: 'MAKER')
    fr = mirror.records['POS-1']['followers']['c1']
    assert fr['status'] == m.CLOSED and fr['style_out'] == 'MAKER'
    closes = [o for o in acct.orders.values() if o['close']]
    assert closes and closes[0]['symbol'] == 'ETH-USDT-SWAP'
    # Nothing filled resting: it CROSSED - a copy always gets out.
    assert {c[0] for c in acct.closes} == {'ETH-USDT-SWAP', 'BTC-USDT-SWAP'}


def test_the_pnl_of_a_copy_is_its_own_fills_less_its_fees(tmp_path):
    acct = Account()
    mirror = quiet_mirror(tmp_path, m.Follower('c1', 1.0, broker=acct))
    mirror.observe([trade(opened_at=86400.0 * 100 + 40)])
    # ETH sold at 100.0 (bid); BTC bought at 100.1 (ask). Out: ETH bought
    # back at 99.0, BTC sold at 101.0.
    acct.bid, acct.ask = 101.0, 99.0
    mirror.observe([])
    fr = mirror.records['POS-1']['followers']['c1']
    # ETH: -1 x (99 - 100) x 2 x 0.1 = +0.2; BTC: +1 x (101 - 100.1) x 0.1
    # x 0.1 = +0.009. Fees 5 bp taker on every fill.
    assert fr['gross'] == pytest.approx(0.209)
    fees = 5e-4 * 0.1 * (100.0 * 2 + 100.1 * 0.1 + 99.0 * 2 + 101.0 * 0.1)
    assert fr['fees'] == pytest.approx(fees, abs=1e-6)
    assert fr['pnl'] == pytest.approx(0.209 - fees, abs=1e-6)
    [kept] = mirror.closed['c1']
    assert kept['pnl'] == pytest.approx(fr['pnl'])


def test_max_margin_holds_a_copy_that_would_go_over(tmp_path):
    acct = Account(margin=50.0)          # 2.0 + 0.1 lots = 105 a copy
    mirror = quiet_mirror(tmp_path, m.Follower('c1', 1.0, broker=acct,
                                               max_margin=150.0))
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40)])
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40),
                    trade('P2', opened_at=86400.0 * 100 + 45)])
    assert mirror.records['P1']['followers']['c1']['status'] == m.OPEN
    p2 = mirror.records['P2']['followers']['c1']
    assert p2['status'] == m.SKIPPED and 'margin' in p2['why']
    assert mirror.margin_held('c1') == pytest.approx(105.0)


def test_the_daily_loss_limit_stops_new_copies_until_tomorrow(tmp_path):
    clock = {'now': 86400.0 * 100 + 50}
    acct = Account()
    mirror = m.Mirror([m.Follower('c1', 1.0, broker=acct,
                                  daily_loss_limit=0.1)],
                      str(tmp_path / 'mirror_state.json'),
                      clock=lambda: clock['now'], background=False,
                      sleep=lambda s: None)
    mirror.observe([trade('P1', opened_at=clock['now'] - 5)])
    acct.bid, acct.ask = 99.0, 101.0                  # a losing exit
    mirror.observe([])
    assert mirror.closed['c1'][0]['pnl'] < -0.1
    mirror.observe([trade('P2', opened_at=clock['now'] - 1)])
    p2 = mirror.records['P2']['followers']['c1']
    assert p2['status'] == m.SKIPPED and 'daily loss limit' in p2['why']
    # The next day: copies again.
    clock['now'] += 86400.0
    mirror.observe([trade('P3', opened_at=clock['now'] - 1)])
    assert mirror.records['P3']['followers']['c1']['status'] == m.OPEN


def test_max_trades_a_day(tmp_path):
    acct = Account()
    mirror = quiet_mirror(tmp_path, m.Follower('c1', 1.0, broker=acct,
                                               max_trades_day=1))
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40)])
    mirror.observe([])
    mirror.observe([trade('P2', opened_at=86400.0 * 100 + 45)])
    assert 'limit is 1' in mirror.records['P2']['followers']['c1']['why']


def test_halt_resume_and_close_copies_from_the_screen(tmp_path):
    acct = Account()
    mirror = quiet_mirror(tmp_path, m.Follower('c1', 1.0, broker=acct),
                          m.Follower('c2', 1.0, broker=Account()))
    assert mirror.control('c1', 'HALT')['ok']
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40)])
    assert 'halted' in mirror.records['P1']['followers']['c1']['why']
    assert mirror.records['P1']['followers']['c2']['status'] == m.OPEN
    mirror.control('c1', 'RESUME')
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40),
                    trade('P2', opened_at=86400.0 * 100 + 45)])
    assert mirror.records['P2']['followers']['c1']['status'] == m.OPEN
    # Close copies: c1 out now (and halted); c2 keeps its copies.
    answer = mirror.control('c1', 'CLOSE_COPIES')
    assert answer['closing'] == 1
    assert mirror.records['P2']['followers']['c1']['status'] == m.CLOSED
    assert mirror.records['P2']['followers']['c2']['status'] == m.OPEN
    assert mirror.halted['c1']
    assert mirror.closed['c1'][0]['forced'] is True
    # The trade closes later: nothing left to do for c1.
    mirror.observe([])
    assert mirror.records['P2']['followers']['c2']['status'] == m.CLOSED
    assert mirror.control('nobody', 'HALT')['ok'] is False


def test_the_snapshot_carries_each_followers_pnl_limits_and_halts(tmp_path):
    acct = Account()
    mirror = quiet_mirror(tmp_path, m.Follower('c1', 1.0, broker=acct,
                                               capital=1000.0,
                                               max_margin=500.0))
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40)])
    acct.bid, acct.ask = 101.0, 99.0
    mirror.observe([])
    mirror.control('c1', 'HALT')
    [f] = mirror.snapshot()['followers']
    assert f['pnl_today'] == pytest.approx(mirror.closed['c1'][0]['pnl'],
                                           abs=0.01)
    assert f['return_month_pct'] == pytest.approx(
        100 * f['pnl_month'] / 1000.0, abs=0.01)
    assert f['halted'] and 'halted' in f['blocked']
    assert f['max_margin'] == 500.0 and f['closed_count'] == 1


def test_the_report_rows_are_closed_copies_with_their_pnl_only(tmp_path):
    acct = Account()
    mirror = quiet_mirror(tmp_path, m.Follower('c1', 1.0, broker=acct))
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40)])
    mirror.observe([])
    closed = m.load_closed(str(tmp_path / 'mirror_state.json'))
    [row] = m.report_positions(closed, 'c1')
    assert row['realized_pnl'] == pytest.approx(closed['c1'][0]['pnl'])
    assert row['quantity'] is None and row['side'] is None
    assert m.report_positions(closed, 'nobody') == []


def test_each_follower_has_a_trader_and_a_client_report(tmp_path):
    """/mirror/report: the follower's own copies - the Trader's with the
    trades, the Client's with returns on its capital only."""
    from okxtrader.webapp import create_app
    acct = Account()
    status = tmp_path / 'status.json'
    status.write_text('{}')
    mirror = m.Mirror([m.Follower('client a', 1.0, broker=acct)],
                      str(tmp_path / 'mirror_state.json'),
                      clock=lambda: 1_790_000_000.0, background=False,
                      sleep=lambda s: None)
    mirror.observe([trade('P1', opened_at=1_790_000_000.0 - 5)])
    acct.bid, acct.ask = 101.0, 99.0
    mirror.observe([])
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'accounts': {}, 'pairs': {}, 'mirror': {
        'enabled': True, 'followers': [{'name': 'client a', 'multiple': 1,
                                        'capital': 1000}]}}))
    app = create_app(str(status), str(tmp_path / 'c.jsonl'),
                     str(tmp_path / 'r.json'), str(config),
                     str(tmp_path / 'db.sqlite'))
    app.config.update(TESTING=True)
    client = app.test_client()
    trader = client.get('/mirror/report?follower=client%20a&days=0')
    assert trader.status_code == 200
    page = trader.get_data(as_text=True)
    assert 'client a' in page and 'Trade journal' in page
    view = client.get('/mirror/report?follower=client%20a&view=client&days=0')
    page = view.get_data(as_text=True)
    assert view.status_code == 200 and 'Return on capital' in page
    assert 'Trade journal' not in page and 'ETH-USDT-SWAP' not in page
    assert client.get('/mirror/report?follower=nobody').status_code == 404


def test_the_mirror_buttons_reach_the_engine_as_a_command(tmp_path):
    from okxtrader import commands

    class Desk:
        def __init__(self):
            self.asked = []

        def mirror_control(self, follower, action):
            self.asked.append((follower, action))
            return {'ok': True, 'follower': follower, 'action': action}
    desk = Desk()
    processor = commands.CommandRunner(desk, str(tmp_path / 'c.jsonl'),
                                       str(tmp_path / 'r.json'))
    result = processor.execute({'id': '1', 'kind': 'mirror_control',
                                'payload': {'follower': 'c1',
                                            'action': 'HALT'}})
    assert result['ok'] and desk.asked == [('c1', 'HALT')]


# -- MANAGED ACCOUNTS: set up from the screen, taken live ---------------------

def managed_app(tmp_path, status=None):
    from okxtrader.webapp import create_app
    (tmp_path / 'status.json').write_text(json.dumps(status or {}))
    (tmp_path / 'config.json').write_text(json.dumps(
        {'accounts': {}, 'pairs': {'ethbtc': {'name': 'ETH vs BTC'}}}))
    app = create_app(str(tmp_path / 'status.json'), str(tmp_path / 'c.jsonl'),
                     str(tmp_path / 'r.json'), str(tmp_path / 'config.json'),
                     str(tmp_path / 'db.sqlite'))
    app.config.update(TESTING=True)
    return app.test_client()


def test_a_managed_account_is_added_from_the_screen_keys_to_env_only(
        tmp_path, monkeypatch):
    for key in ('OKX_API_KEY_MIRROR_CLIENT_A', 'OKX_SECRET_KEY_MIRROR_CLIENT_A',
                'OKX_PASSPHRASE_MIRROR_CLIENT_A'):
        monkeypatch.delenv(key, raising=False)
    client = managed_app(tmp_path)
    answer = client.post('/api/mirror/accounts/client a', json={
        'new': True, 'multiple': '0.5', 'capital': '10000',
        'max_margin': '1000', 'daily_loss_limit': '150',
        'max_trades_day': '10', 'execution': 'MIRROR', 'demo': True,
        'enabled': True, 'pairs': ['ethbtc'], 'api_key': 'k-123',
        'secret': 's-456', 'passphrase': 'p-789'}).get_json()
    assert answer['ok'], answer
    raw = json.loads((tmp_path / 'config.json').read_text())
    [row] = raw['mirror']['followers']
    assert row['multiple'] == 0.5 and row['max_trades_day'] == 10
    assert row['pairs'] == ['ethbtc']
    text = (tmp_path / 'config.json').read_text()
    assert 'k-123' not in text and 's-456' not in text        # not here
    env = (tmp_path / '.env').read_text()
    assert 'OKX_API_KEY_MIRROR_CLIENT_A="k-123"' in env       # here
    listed = client.get('/api/mirror/accounts').get_json()
    [shown] = listed['followers']
    assert shown['key_set'] and shown['secret_set'] and shown['passphrase_set']
    assert 'k-123' not in json.dumps(listed)                  # never echoed
    assert listed['pairs'] == [{'key': 'ethbtc', 'name': 'ETH vs BTC'}]
    # Saved again with the key fields blank: the key is KEPT.
    client.post('/api/mirror/accounts/client a', json={'multiple': 0.75})
    assert 'k-123' in (tmp_path / '.env').read_text()
    # A second account of the same name is refused.
    assert client.post('/api/mirror/accounts/client a', json={
        'new': True, 'multiple': 1}).status_code == 409


def test_bad_input_is_refused_in_words(tmp_path):
    client = managed_app(tmp_path)
    r = client.post('/api/mirror/accounts/x', json={'new': True,
                                                    'multiple': '0'})
    assert r.status_code == 400 and 'multiple' in r.get_json()['error']
    r = client.post('/api/mirror/accounts/x', json={'multiple': 1,
                                                    'max_margin': 'lots'})
    assert 'not a number' in r.get_json()['error']
    r = client.post('/api/mirror/accounts/bad%2Fname', json={'multiple': 1})
    assert r.status_code == 400
    r = client.post('/api/mirror/settings', json={'open_window_sec': 0})
    assert r.status_code == 400


def test_copying_is_switched_on_and_off_from_the_screen(tmp_path):
    client = managed_app(tmp_path)
    assert client.post('/api/mirror/settings', json={
        'enabled': True, 'open_window_sec': 45}).get_json()['ok']
    raw = json.loads((tmp_path / 'config.json').read_text())
    assert raw['mirror']['enabled'] is True
    assert raw['mirror']['open_window_sec'] == 45


def test_an_account_holding_copies_cannot_be_removed(tmp_path):
    client = managed_app(tmp_path, status={'mirror': {'followers': [
        {'name': 'c1', 'holding': 2}]}})
    client.post('/api/mirror/accounts/c1', json={'new': True, 'multiple': 1})
    r = client.delete('/api/mirror/accounts/c1')
    assert r.status_code == 409 and 'still holds 2' in r.get_json()['error']
    (tmp_path / 'b').mkdir()
    client2 = managed_app(tmp_path / 'b')          # nothing held here
    client2.post('/api/mirror/accounts/c2', json={'new': True,
                                                   'multiple': 1})
    assert client2.delete('/api/mirror/accounts/c2').get_json()['ok']


def test_test_connection_says_when_there_is_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv('OKX_API_KEY_MIRROR_C9', raising=False)
    client = managed_app(tmp_path)
    client.post('/api/mirror/accounts/c9', json={'new': True, 'multiple': 1})
    body = client.get('/api/mirror/accounts/c9/test').get_json()
    assert body['connected'] is False
    assert 'no API key' in body['problems'][0]


def test_a_change_on_the_screen_is_taken_in_place(tmp_path, monkeypatch):
    acct, other = Account(), Account()
    one = m.Follower('c1', 1.0, broker=acct)
    mirror = quiet_mirror(tmp_path, one)
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40)])
    # New settings for c1, a new account c2, copying still on.
    mirror.reconfigure({'enabled': True, 'open_window_sec': 30,
                        'followers': [{'name': 'c1', 'multiple': 2,
                                       'max_trades_day': 5},
                                      {'name': 'c2', 'multiple': 1}]})
    assert mirror.followers['c1'] is one                    # same object
    assert one.multiple == 2 and one.max_trades_day == 5
    assert one._broker is acct                              # not reconnected
    assert 'c2' in mirror.followers and mirror.open_window_sec == 30
    # A new KEY for c1: it reconnects on its next order.
    monkeypatch.setenv('OKX_API_KEY_MIRROR_C1', 'a-new-key')
    mirror.reconfigure({'enabled': True, 'followers': [
        {'name': 'c1', 'multiple': 2}, {'name': 'c2', 'multiple': 1}]})
    assert one._broker is None
    one._broker = acct
    mirror.followers['c2']._broker = other
    # c1 taken off while it holds P1: closes only, then goes.
    mirror.reconfigure({'enabled': True, 'followers': [
        {'name': 'c2', 'multiple': 1}]})
    assert one.removed and not one.enabled
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40)])
    mirror.observe([])
    assert 'c1' not in mirror.followers
    assert mirror.closed['c1'][0]['pnl'] is not None


def test_switched_off_no_new_copy_but_what_is_on_still_closes(tmp_path):
    acct = Account()
    mirror = quiet_mirror(tmp_path, m.Follower('c1', 1.0, broker=acct))
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40)])
    mirror.reconfigure({'enabled': False, 'followers': [
        {'name': 'c1', 'multiple': 1}]})
    assert mirror.snapshot()['paused'] is True
    mirror.observe([trade('P1', opened_at=86400.0 * 100 + 40),
                    trade('P2', opened_at=86400.0 * 100 + 45)])
    assert 'switched off' in mirror.records['P2']['followers']['c1']['why']
    mirror.observe([])
    assert mirror.records['P1']['followers']['c1']['status'] == m.CLOSED
