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
    # POST-ONLY, the original desk's June 2026 rule: a plain limit at
    # the touch races the book and is charged taker.
    assert len(live) == 2 and {o['ordType'] for o in live} == {'post_only'}
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


def test_not_filled_in_30s_is_a_clean_refusal(desk, caplog):
    import logging
    caplog.set_level(logging.INFO)
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    for _ in range(31):
        desk.tick()
    # Said in the log, not left to be guessed from silence.
    assert any('ended - not filled: timed out after 30s' in r.getMessage()
               for r in caplog.records)
    assert len(desk.done) == 1
    result = desk.done[0]
    assert not result.ok and result.refused and 'nothing went on' in \
        result.reason
    assert _live(desk.sim) == [] and desk.sim.positions == {}


def test_a_post_only_leg_okx_cancelled_is_placed_again(desk):
    """OKX cancels a post-only order that would have crossed
    (cancelSource 20 / 31). The original desk re-priced and re-sent it;
    so does this - the other leg is NOT pulled and nothing fails."""
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    job = desk.maker.jobs[desk.pair.key]
    first = job.legs['b'].live
    ord_id = desk.broker.ledger['orders'][str(first)]['ord_id']
    desk.sim.orders[ord_id]['state'] = 'canceled'
    desk.sim.orders[ord_id]['cancelSource'] = '31'
    desk.tick()
    b = job.legs['b']
    assert b.live is not None and b.live != first and b.replaced == 1
    assert 'cancelSource 31' in (b.cancel_reason or '')
    assert len(_live(desk.sim)) == 2 and not desk.done
    assert {o['ordType'] for o in _live(desk.sim)} == {'post_only'}


def test_CONTROL_a_leg_that_cannot_be_placed_again_pulls_the_other(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    job = desk.maker.jobs[desk.pair.key]
    ord_id = desk.broker.ledger['orders'][str(job.legs['b'].live)]['ord_id']
    desk.sim.orders[ord_id]['state'] = 'canceled'
    desk.maker._rest = lambda *a, **k: False       # OKX refuses it now
    desk.tick()
    assert _live(desk.sim) == []
    assert desk.done and not desk.done[0].ok


def test_a_click_at_a_level_never_rests_worse_than_it(desk):
    """A SELL clicked at a level the market has already fallen through
    rests its legs where the spread IS that level - further out, still
    post-only - instead of chasing the market down."""
    md = desk.md()
    level = md['short_spread'] + 5.0          # above the market: not there
    desk.maker.start(desk.pair, SpreadSide.SELL, md, 1, level=level,
                     source='trigger')
    job = desk.maker.jobs[desk.pair.key]
    a, b = job.legs['a'], job.legs['b']
    implied = b.price - desk.pair.hedge_ratio * a.price
    assert implied == pytest.approx(level, abs=0.02)
    assert job.held is True
    # Re-pegs keep holding it.
    for _ in range(3):
        desk.tick()
    implied = b.target - desk.pair.hedge_ratio * a.target
    assert implied >= level - 0.02


def test_CONTROL_a_level_already_better_rests_at_the_touch(desk):
    md = desk.md()
    level = md['short_spread'] - 5.0          # far below: already better
    desk.maker.start(desk.pair, SpreadSide.SELL, md, 1, level=level,
                     source='trigger')
    job = desk.maker.jobs[desk.pair.key]
    assert job.held is False


def test_a_held_click_not_filled_in_time_is_rearmed(desk):
    md = desk.md()
    level = md['short_spread'] + 5.0
    desk.maker.start(desk.pair, SpreadSide.SELL, md, 1, level=level,
                     source='trigger')
    job = desk.maker.jobs[desk.pair.key]
    for _ in range(31):
        desk.tick()
    assert desk.done and not desk.done[0].ok
    assert job.rearm is True and _live(desk.sim) == []


def test_CONTROL_a_fill_the_trader_cancelled_is_not_rearmed(desk):
    md = desk.md()
    desk.maker.start(desk.pair, SpreadSide.SELL, md, 1,
                     level=md['short_spread'] + 5.0, source='trigger')
    job = desk.maker.jobs[desk.pair.key]
    desk.maker.cancel(desk.pair.key)
    desk.tick()
    assert job.rearm is False


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
    fake = types.SimpleNamespace(legs={'okx': desk.leg}, maker=desk.maker,
                                 _actual_fee_bps=lambda pair, leg, c: None)
    desk.maker.config = {'FILL_STYLE': 'maker'}
    md = desk.md()
    Coordinator._okx_fees(fake, desk.pair, md)
    a = desk.pair.fee_settings['COMMISSION_PER_LOT_A']
    assert desk.pair.fee_basis['a'] == {'bps': 2.0, 'source': 'maker',
                                        'fills': 0}
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


def test_the_tp_is_priced_on_the_fees_the_ladder_actually_pays(desk):
    """With enough of its own fills, a leg's B/E and TP are priced at
    the rate it ACTUALLY paid - held between maker and taker."""
    from okxtrader.coordinator import Coordinator
    md = desk.md()
    desk.maker.config = {'FILL_STYLE': 'maker'}
    for paid, used in ((4.2, 4.2), (1.0, 2.0), (9.0, 5.0)):
        fake = types.SimpleNamespace(
            legs={'okx': desk.leg}, maker=desk.maker,
            _actual_fee_bps=lambda pair, leg, c, b=paid: {'bps': b,
                                                         'fills': 40})
        Coordinator._okx_fees(fake, desk.pair, md)
        assert desk.pair.fee_settings['COMMISSION_PER_LOT_A'] == \
            pytest.approx(used / 1e4 * 0.01 * md['leg_a_mid'])
        assert desk.pair.fee_basis['a']['source'] == 'actual'


def test_the_actual_fee_rate_is_read_from_the_ladders_own_fills():
    from okxtrader.coordinator import Coordinator
    now = 2_000_000_000.0
    # Leg A: two fills of 10 lots at 60,000 (0.01 BTC a lot): $12,000
    # traded, $5.04 paid: 4.2 bp. Leg B's fills are not counted for A.
    rows = ([{'leg': 'A', 'volume': 10, 'price': 60000.0, 'fee': -2.52}] * 2
            + [{'leg': 'B', 'volume': 50, 'price': 2000.0, 'fee': -9.0}])
    store = types.SimpleNamespace(fills=lambda **k: rows)
    fake = types.SimpleNamespace(store=store, clock=lambda: now,
                                 ACTUAL_FEE_DAYS=30, ACTUAL_FEE_MIN_FILLS=2,
                                 ACTUAL_FEE_EVERY_SEC=300.0)
    pair = types.SimpleNamespace(key='p')
    got = Coordinator._actual_fee_bps(fake, pair, 'a', 0.01)
    assert got['fills'] == 2 and got['bps'] == pytest.approx(4.2)
    # Too few fills: not believed.
    fake2 = types.SimpleNamespace(store=store, clock=lambda: now,
                                  ACTUAL_FEE_DAYS=30, ACTUAL_FEE_MIN_FILLS=8,
                                  ACTUAL_FEE_EVERY_SEC=300.0)
    assert Coordinator._actual_fee_bps(fake2, pair, 'a', 0.01) is None


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
    # POST-ONLY, the original desk's June 2026 rule: a plain limit at
    # the touch races the book and is charged taker.
    assert len(live) == 2 and {o['ordType'] for o in live} == {'post_only'}
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


# -- a leg on alone: 5 s as maker, then the rest crosses ----------------------

def test_a_leg_on_alone_crosses_at_market_after_five_seconds(desk):
    """Live: 31 s from an Algo signal to the second leg. One leg filled,
    the other gets MAKER_LEG_GAP_SEC (5 s) as maker, then what it is
    missing crosses at market - taker on that one piece only."""
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    desk.fill('a')
    for _ in range(4):
        desk.tick()
    assert not desk.done                       # still 4 s: maker
    desk.tick()
    desk.tick()
    assert desk.done and desk.done[0].ok
    position = desk.done[0].position
    assert position.leg_b.volume == pytest.approx(2.0)
    assert 'crossed at market' in desk.done[0].reason
    assert _live(desk.sim) == []               # nothing left resting


def test_a_part_filled_lead_is_matched_not_overdone(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    desk.fill('a', part=0.5)
    for _ in range(6):
        desk.tick()
    assert desk.done and desk.done[0].ok
    position = desk.done[0].position
    assert position.leg_a.volume == pytest.approx(0.5)
    assert position.leg_b.volume == pytest.approx(1.0)


def test_an_algo_entry_rests_five_seconds_not_thirty(desk):
    md = desk.md()
    desk.maker.start(desk.pair, SpreadSide.BUY, md, 1, source='algo')
    job = desk.maker.jobs[desk.pair.key]
    assert job.deadline - job.started == pytest.approx(5.0)
    for _ in range(6):
        desk.tick()
    assert desk.done and not desk.done[0].ok           # nothing filled
    assert _live(desk.sim) == []


def test_CONTROL_a_click_still_rests_thirty_seconds(desk):
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    job = desk.maker.jobs[desk.pair.key]
    assert job.deadline - job.started == pytest.approx(30.0)


def test_if_the_market_order_fails_recovery_still_covers_it(desk):
    """The fallback: a market order that cannot go leaves the old maker
    recovery (a plain limit, nudged to the market) and then the unwind -
    a leg is never left on alone."""
    desk.maker.start(desk.pair, SpreadSide.BUY, desk.md(), 1)
    desk.fill('a')
    desk.leg.order = lambda *a, **k: {'ok': False, 'filled_volume': 0.0,
                                      'error': 'refused'}
    for _ in range(6):
        desk.tick()
    job = desk.maker.jobs[desk.pair.key]
    assert job.phase == 'recovering' and job.recovery['leg'] == 'b'
    live = _live(desk.sim)
    assert len(live) == 1 and live[0]['ordType'] == 'limit'



# -- a take-profit close held to its level ----------------------------------

def test_a_take_profit_close_is_held_to_its_level(desk, closes):
    """Live: a take-profit at -8.85 closed, as maker, at -10.03 - the
    closing orders followed the spread back down. Held to its level,
    the close rests where the spread IS the target, and never worse."""
    from okxtrader.spread import executable_spread
    position = _open(desk)
    md = desk.md()
    closing = executable_spread(md, position.side, closing=True)
    level = closing + 5.0 if position.side.value == 'BUY' else closing - 5.0
    closes.start(desk.pair, position, md, 'Algo: profit target', level=level)
    job = closes.jobs[position.position_id]
    assert job.held is True
    price = {k: next(iter(leg['orders'].values()))['price']
             for k, leg in job.legs.items()}
    implied = price['b'] - desk.pair.hedge_ratio * price['a']
    if position.side.value == 'BUY':
        assert implied >= level - 0.02
    else:
        assert implied <= level + 0.02
    assert position.exit_sent_at is not None


def test_a_held_take_profit_not_filled_leaves_the_position_open(desk, closes):
    from okxtrader.spread import executable_spread
    position = _open(desk)
    md = desk.md()
    closing = executable_spread(md, position.side, closing=True)
    level = closing + 5.0 if position.side.value == 'BUY' else closing - 5.0
    closes.start(desk.pair, position, md, 'Algo: profit target', level=level)
    for _ in range(31):
        desk.clock.t += 1
        closes.step(desk.pair, desk.md())
    assert closes.done and closes.done[0].get('held') is True
    assert position.closed_at is None                # still on
    assert _live(desk.sim) == []                     # nothing resting
    assert desk.sim.positions                        # nothing closed


def test_a_close_with_one_leg_done_crosses_the_other_after_five_seconds(
        desk, closes):
    position = _open(desk)
    closes.start(desk.pair, position, desk.md(), 'closed by trader')
    _fill_close(desk, closes, position, leg_key='a')
    for _ in range(6):
        desk.clock.t += 1
        closes.step(desk.pair, desk.md())
    assert closes.done and closes.done[0]['ok']
    assert position.closed_at is not None
    assert desk.sim.positions == {} and _live(desk.sim) == []
    assert '5s' in (closes.done[0].get('note') or '')


def test_with_two_trades_on_the_exit_closes_its_own_trade_not_the_first(
        desk, closes):
    """Two trades on the SAME instruments - OKX holds them as one
    position per instrument. The second trade's take-profit closes the
    SECOND trade: its own tickets, its own size. The first stays on,
    tickets and all, at OKX and on the desk. Neither FIFO nor LIFO: the
    trade whose exit fired."""
    first = _open(desk)
    second = _open(desk)
    first_tickets = {k: list(getattr(first, 'leg_' + k).position_tickets)
                     for k in 'ab'}
    second_tickets = {k: list(getattr(second, 'leg_' + k).position_tickets)
                      for k in 'ab'}
    assert not set(first_tickets['a']) & set(second_tickets['a'])
    held_before = {k: dict(v) for k, v in desk.sim.positions.items()}

    closes.start(desk.pair, second, desk.md(), 'Algo: profit target')
    job = closes.jobs[second.position_id]
    # Every closing order names one of the SECOND trade's tickets.
    for key, leg in job.legs.items():
        named = {o['pos_ticket'] for o in leg['orders'].values()}
        assert named == {int(t) for t in second_tickets[key]}
    _fill_close(desk, closes, second)
    desk.clock.t += 1
    closes.step(desk.pair, desk.md())
    assert closes.done[-1]['ok']
    assert second.closed_at is not None
    assert first.closed_at is None and first.is_open
    # The first trade's tickets are still on the books, at full size.
    ledger = desk.broker.ledger['positions']
    for key in 'ab':
        for t in first_tickets[key]:
            assert str(t) in ledger
            assert ledger[str(t)]['volume'] == pytest.approx(
                getattr(first, 'leg_' + key).volume)
        for t in second_tickets[key]:
            assert str(t) not in ledger
    # ...and OKX - one position per instrument and side, averaged -
    # holds exactly the first trade's size.
    assert set(desk.sim.positions) == set(held_before)
    for key, row in desk.sim.positions.items():
        assert row['pos'] == pytest.approx(held_before[key]['pos'] / 2)
