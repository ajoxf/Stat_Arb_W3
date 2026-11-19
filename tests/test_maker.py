"""The W3 maker fill against the simulated OKX: both legs rest at the
touch, re-peg on 1 bp, time out at 30 s, recover a lagging leg as
maker, and unwind at market only when recovery runs out."""

import types

import pytest

from okxtrader.config import PairConfig
from okxtrader.executor import PairExecutor
from okxtrader.legs import LocalLeg
from okxtrader.maker import MakerFills, passive_price
from okxtrader.models import SpreadSide
from okxtrader.okx_api import OKXClient
from okxtrader.okx_broker import OKXBroker
from okxtrader.okx_sim import SimOKX
from okxtrader.spread import compute_spread


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def desk(tmp_path):
    sim = SimOKX(seed=3, autostart=False, maker_fill_prob=0.0)
    broker = OKXBroker(types.SimpleNamespace(name='okx', demo=True),
                       client=OKXClient(transport=sim.handle),
                       ledger_path=tmp_path)
    assert broker.initialize()
    leg = LocalLeg(broker)
    pair = PairConfig('BTC-USDT-SWAP|ETH-USDT-SWAP', name='ETH vs BTC',
                      leg_a={'account': 'okx', 'symbol': 'BTC-USDT-SWAP'},
                      leg_b={'account': 'okx', 'symbol': 'ETH-USDT-SWAP'},
                      hedge_ratio=0.05,
                      hedge_ratio_for='BTC-USDT-SWAP|ETH-USDT-SWAP',
                      increment=0.05, clip_lots_a=1.0, clip_lots_b=2.0)
    for side, sym in (('a', 'BTC-USDT-SWAP'), ('b', 'ETH-USDT-SWAP')):
        info = broker.symbol_info(sym)
        setattr(pair, f'meta_{side}', {
            'contract_size': info.contract_size,
            'volume_step': info.volume_step, 'volume_min': info.volume_min,
            'volume_max': info.volume_max})
    clock = Clock()
    from okxtrader.config import TraderConfig
    config = TraderConfig(pairs={pair.key: pair})
    config.settings.update({'MARKET_PROTECTION_TICKS': 0,
                            'FILL_STYLE': 'maker'})
    executor = PairExecutor(config, {'okx': leg}, clock=clock,
                            sleep=lambda s: None)
    maker = MakerFills(config, {'okx': leg}, executor, clock=clock)
    done = []
    maker.on_done = lambda job, result: done.append(result)
    d = types.SimpleNamespace(sim=sim, broker=broker, leg=leg, pair=pair,
                              clock=clock, maker=maker, done=done)

    def md():
        broker._row_cache = {}
        a = leg.tick('BTC-USDT-SWAP')
        b = leg.tick('ETH-USDT-SWAP')
        return compute_spread(pair, a, b, pair.hedge_ratio, clock=clock)
    d.md = md

    def fill(leg_key, part=1.0):
        ml = maker.jobs[pair.key].legs[leg_key]
        order = broker.ledger['orders'][str(ml.live)]
        o = sim.orders[order['ord_id']]
        sim._fill(o, (o['sz'] - o['accFillSz']) * part, float(o['px']),
                  taker=False)
    d.fill = fill

    def tick(seconds=1.0):
        clock.t += seconds
        return maker.step(pair, md())
    d.tick = tick
    yield d
    sim.stop()


def _live(sim):
    return [o for o in sim.orders.values()
            if o['state'] in ('live', 'partially_filled')]


def test_the_maker_price_is_w3s():
    # Wide book: bid improved by 1 bp. One-tick book: held 0.5 bp off
    # the ask, so it never touches the other side.
    assert passive_price('BUY', 100.0, 101.0, 1.0, 0.5) == pytest.approx(100.01)
    assert passive_price('BUY', 100.0, 100.001, 1.0, 0.5) == \
        pytest.approx(100.001 * (1 - 0.5e-4))
    assert passive_price('SELL', 100.0, 101.0, 1.0, 0.5) == \
        pytest.approx(101 * (1 - 1e-4))


def test_both_legs_rest_passive_and_book_when_both_fill(desk):
    started = desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    assert started.ok and started.working
    live = _live(desk.sim)
    assert len(live) == 2 and {o['ordType'] for o in live} == {'limit'}
    for o in live:                                  # passive: it makes
        bid, ask = desk.sim._touch(o['instId'])
        assert (float(o['px']) < ask) if o['side'] == 'buy' \
            else (float(o['px']) > bid)
    desk.fill('a')
    desk.fill('b')
    desk.tick()
    assert len(desk.done) == 1 and desk.done[0].ok
    position = desk.done[0].position
    assert position.order_type.value == 'LIMIT'
    assert position.leg_a.volume == pytest.approx(1.0)
    assert position.leg_b.volume == pytest.approx(2.0)
    assert not desk.maker.busy(desk.pair.key)


def test_maker_fills_pay_the_vip_maker_fee(desk):
    desk.maker.start(desk.pair, SpreadSide.SELL, desk.md(), 1)
    desk.fill('a')
    desk.fill('b')
    desk.tick()
    fees = [f for f in desk.sim.fills]
    assert fees and all(f['execType'] == 'M' for f in fees)


def test_it_re_pegs_by_amend_when_the_touch_moves_over_1bp(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    job = desk.maker.jobs[desk.pair.key]
    ticket = job.legs['a'].live
    before = job.legs['a'].price
    desk.sim.index['BTC'] *= 1.0003                  # 3 bp
    desk.tick()
    assert job.legs['a'].amends == 1 and job.legs['a'].live == ticket
    assert job.legs['a'].price > before
    # Under 1 bp: left alone, keeping its place in the queue.
    desk.sim.index['BTC'] *= 1.00004
    desk.tick()
    assert job.legs['a'].amends == 1


def test_not_filled_in_30s_is_a_clean_refusal(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    for _ in range(31):
        desk.tick()
    assert len(desk.done) == 1
    result = desk.done[0]
    assert not result.ok and result.refused and 'nothing went on' in \
        result.reason
    assert _live(desk.sim) == [] and desk.sim.positions == {}


def test_one_leg_cancelled_at_okx_pulls_the_other(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    job = desk.maker.jobs[desk.pair.key]
    ord_id = desk.broker.ledger['orders'][str(job.legs['b'].live)]['ord_id']
    desk.sim.orders[ord_id]['state'] = 'canceled'
    desk.tick()
    assert _live(desk.sim) == []
    assert desk.done and not desk.done[0].ok


def test_a_leg_on_alone_is_recovered_as_maker(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    desk.fill('a')
    for _ in range(31):
        desk.tick()
    job = desk.maker.jobs[desk.pair.key]
    assert job.phase == 'recovering' and job.recovery['leg'] == 'b'
    assert job.recovery['want'] == pytest.approx(2.0)
    desk.fill('b')
    desk.tick()
    assert desk.done and desk.done[0].ok
    assert desk.done[0].position.leg_b.volume == pytest.approx(2.0)


def test_recovery_nudges_toward_the_market(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    desk.fill('a')
    for _ in range(31):
        desk.tick()
    ml = desk.maker.jobs[desk.pair.key].legs['b']
    first = ml.price
    for _ in range(7):                               # past one 6 s step
        desk.tick()
    assert ml.price > first                          # a BUY moves up


def test_recovery_out_of_time_closes_the_leg_that_went_on(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    desk.fill('a')
    for _ in range(31 + 61):
        desk.tick()
    assert desk.done and not desk.done[0].ok
    assert 'closed at market' in desk.done[0].reason
    assert desk.done[0].naked is None
    assert desk.sim.positions == {} and _live(desk.sim) == []


def test_one_fill_at_a_time_per_pair(desk):
    assert desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1).ok
    again = desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    assert not again.ok and 'already working' in again.reason


def test_the_reconciler_is_told_what_is_in_flight(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    job = desk.maker.jobs[desk.pair.key]
    tickets = desk.maker.in_flight_tickets()
    assert ('okx', str(job.legs['a'].live)) in tickets
    assert ('okx', str(job.legs['b'].live)) in tickets


def test_cancel_pulls_both_legs(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    assert desk.maker.cancel(desk.pair.key)['ok']
    desk.tick()
    assert _live(desk.sim) == [] and desk.done and not desk.done[0].ok


def test_commission_is_priced_from_the_vip_tier(desk):
    """The ladder's commission comes from OKX's own fee for this account:
    maker at both ends under the W3 fill, taker under crossing."""
    from okxtrader import config as cfg_module
    from okxtrader.coordinator import Coordinator
    fake = types.SimpleNamespace(legs={'okx': desk.leg}, maker=desk.maker)
    desk.maker.config = {'FILL_STYLE': 'maker'}
    md = desk.md()
    Coordinator._okx_fees(fake, desk.pair, md)
    a = desk.pair.fee_settings['COMMISSION_PER_LOT_A']
    # Lv1 swaps: maker 2 bp, taker 5 bp; BTC-USDT-SWAP is 0.01 BTC a lot.
    assert a == pytest.approx(2 / 1e4 * 0.01 * md['leg_a_mid'])
    merged = desk.pair.exit_settings(cfg_module.DEFAULT_SETTINGS)
    assert merged['COMMISSION_PER_LOT_A'] == a
    desk.pair.commission_per_lot_a = 0.5          # typed by the trader
    assert desk.pair.exit_settings({})['COMMISSION_PER_LOT_A'] == 0.5
    desk.maker.config = {'FILL_STYLE': 'taker'}
    Coordinator._okx_fees(fake, desk.pair, md)
    assert desk.pair.fee_settings['COMMISSION_PER_LOT_A'] == pytest.approx(
        5 / 1e4 * 0.01 * md['leg_a_mid'])


# -- maker CLOSES -------------------------------------------------------------


def _open(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    desk.fill('a')
    desk.fill('b')
    desk.tick()
    return desk.done.pop().position


@pytest.fixture
def closes(desk):
    from okxtrader.maker import MakerCloses
    mc = MakerCloses({}, {'okx': desk.leg}, desk.maker.executor,
                     clock=desk.clock)
    done = []
    mc.on_done = lambda job, answer: done.append(answer)
    mc.done = done
    return mc


def _close_orders(desk, mc, position):
    job = mc.jobs[position.position_id]
    return [(leg, t) for leg in job.legs.values() for t in leg['orders']]


def _fill_close(desk, mc, position, leg_key=None):
    job = mc.jobs[position.position_id]
    for key, leg in job.legs.items():
        if leg_key and key != leg_key:
            continue
        for t in leg['orders']:
            o = desk.sim.orders[desk.broker.ledger['orders'][str(t)]['ord_id']]
            desk.sim._fill(o, o['sz'] - o['accFillSz'], float(o['px']),
                           taker=False)


def test_a_maker_close_rests_by_ticket_and_settles(desk, closes):
    position = _open(desk)
    started = closes.start(desk.pair, position, desk.md(), 'closed by trader')
    assert started['ok'] and started['working']
    live = _live(desk.sim)
    assert len(live) == 2 and {o['ordType'] for o in live} == {'limit'}
    _fill_close(desk, closes, position)
    desk.clock.t += 1
    closes.step(desk.pair, desk.md())
    assert closes.done and closes.done[0]['ok']
    assert position.closed_at is not None
    assert position.realized_pnl is not None
    assert desk.sim.positions == {} and _live(desk.sim) == []
    assert all(f['execType'] == 'M' for f in desk.sim.fills)


def test_a_maker_close_re_pegs_on_1bp(desk, closes):
    position = _open(desk)
    closes.start(desk.pair, position, desk.md(), 'closed by trader')
    desk.sim.index['ETH'] *= 1.0005
    desk.clock.t += 1
    closes.step(desk.pair, desk.md())
    job = closes.jobs[position.position_id]
    assert sum(o['amends'] for o in job.legs['b']['orders'].values()) == 1


def test_a_maker_close_not_filled_closes_the_rest_at_market(desk, closes):
    position = _open(desk)
    closes.start(desk.pair, position, desk.md(), 'closed by trader')
    _fill_close(desk, closes, position, 'a')        # A made, B never
    for _ in range(31):
        desk.clock.t += 1
        closes.step(desk.pair, desk.md())
    assert closes.done and closes.done[0]['ok']
    assert position.closed_at is not None
    assert desk.sim.positions == {} and _live(desk.sim) == []
    kinds = {f['instId']: f['execType'] for f in desk.sim.fills[-2:]}
    assert kinds['ETH-USDT-SWAP'] == 'T'            # B crossed at the end


def test_abandon_pulls_the_resting_closes(desk, closes):
    position = _open(desk)
    closes.start(desk.pair, position, desk.md(), 'closed by trader')
    closes.abandon(position.position_id, 'CLOSE ALL')
    assert _live(desk.sim) == [] and not closes.jobs
    assert position.closed_at is None               # nothing settled here
