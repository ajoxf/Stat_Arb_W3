"""Maker or taker, the gap between the legs, slippage in bp, and OKX's
own orders read back for reconciling."""

import types

import pytest

from okxtrader import slippage
from okxtrader.okx_api import OKXClient
from okxtrader.okx_broker import OKXBroker
from okxtrader.okx_sim import SimOKX

PAIR = 'BTC-USDT-SWAP|ETH-USDT-SWAP'
RATES = {PAIR: {'a': {'maker': 2.0, 'taker': 5.0},
                'b': {'maker': 2.0, 'taker': 5.0}}}


def fill(leg, kind, fee, at, ticket, entry='open', volume=0.1, ours=1):
    return {'pair_key': PAIR, 'leg': leg, 'entry': entry,
            'order_type': kind, 'fee': fee, 'volume': volume,
            'broker_time_ms': at, 'position_ticket': str(ticket),
            'is_ours': ours, 'symbol': 'X'}


# The live trade of 2026-11-19: BTC sold as maker 16 s after the ETH
# leg, most of which crossed and paid taker.
FILLS = [fill('B', 'maker', -0.0003, 1000, 1002, volume=0.01),
         fill('B', 'maker', -0.0003, 1000, 1002, volume=0.01),
         fill('B', 'taker', -0.0288, 1000, 1002, volume=0.33),
         fill('A', 'maker', -0.0123, 17000, 1001, volume=0.10)]
POSITION = {'position_id': 'p1', 'pair_key': PAIR, 'side': 'BUY',
            'opened_at': 1.0,
            'leg_a': {'position_tickets': [1001]},
            'leg_b': {'position_tickets': [1002]}}


def test_a_taker_fill_is_counted_and_priced_against_maker():
    q = slippage.fill_quality(FILLS, [POSITION], RATES)
    b = [r for r in q['liquidity'] if r['leg'] == 'B'][0]
    assert (b['maker_fills'], b['taker_fills']) == (2, 1)
    assert b['taker_share'] == pytest.approx(0.33 / 0.35)
    # 3 bp of the 5 paid was the cost of going in as taker.
    assert b['extra'] == pytest.approx(0.0288 * 3 / 5)
    a = [r for r in q['liquidity'] if r['leg'] == 'A'][0]
    assert a['taker_fills'] == 0 and a['extra'] == 0.0


def test_CONTROL_unknown_rates_say_so_instead_of_zero():
    q = slippage.fill_quality(FILLS, [POSITION], {})
    b = [r for r in q['liquidity'] if r['leg'] == 'B'][0]
    assert b['extra'] is None


def test_the_gap_between_the_legs_and_which_went_first():
    q = slippage.fill_quality(FILLS, [POSITION], RATES)
    gap = q['gaps'][0]
    assert gap['entry_gap_ms'] == 16000 and gap['entry_first'] == 'B'
    assert gap['exit_gap_ms'] is None          # still open
    assert q['entry_gap']['worst_ms'] == 16000


def test_the_trader_s_own_fills_are_left_out():
    q = slippage.fill_quality([fill('A', 'taker', -1.0, 1, 9, ours=0)],
                              [], RATES)
    assert q['liquidity'] == []


def test_leg_slip_is_also_given_in_basis_points_of_its_price():
    row = slippage._leg_slips({'leg_a': {
        'side': 'SELL', 'sent_price': 61613.4, 'price': 61593.2,
        'volume': 0.1, 'contract_size': 0.01}})
    assert row['entry_leg_a'] == pytest.approx(20.2)
    assert row['entry_leg_a_bps'] == pytest.approx(20.2 / 61613.4 * 1e4)


def test_okx_orders_come_back_with_the_desk_ticket(tmp_path):
    sim = SimOKX(seed=3, autostart=False)
    b = OKXBroker(types.SimpleNamespace(name='okx', demo=True),
                  client=OKXClient(transport=lambda m, p, params=None,
                                   body=None: sim.handle(m, p, params, body)),
                  ledger_path=tmp_path)
    assert b.initialize()
    tick = b.symbol_tick('ETH-USDT-SWAP')
    from okxtrader.models import OrderSide
    placed = b.place_pending_limit('ETH-USDT-SWAP', OrderSide.BUY, 0.35,
                                   round(tick.bid * 0.99, 2),
                                   comment='LADDER-MK:B')
    assert placed['ok']
    orders = b.okx_orders()
    mine = [o for o in orders if o['desk_ticket'] == placed['ticket']]
    assert len(mine) == 1
    o = mine[0]
    assert o['ours'] and o['state'] == 'live' and o['size'] == 0.35
    assert o['symbol'] == 'ETH-USDT-SWAP' and o['comment'] == 'LADDER-MK:B'
    sim.stop()
