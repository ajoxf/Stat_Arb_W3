"""Size in dollars: one Qty is a round amount of money, both legs
together, and the leg lots follow the live prices and beta."""

import types

import pytest

from okxtrader import sizing
from okxtrader.config import PairConfig, clean_usd
from okxtrader.coordinator import Coordinator

BETA = 0.028402
BTC, ETH = 61600.0, 1743.0


def test_one_qty_is_the_money_asked_for_and_hedged_at_beta():
    lots_a, lots_b = sizing.lots_for_dollars(1000, BETA, BTC, ETH, 0.01, 0.1)
    value_a, value_b = lots_a * 0.01 * BTC, lots_b * 0.1 * ETH
    assert value_a + value_b == pytest.approx(1000.0)
    # The hedge: beta units of BTC per unit of ETH.
    assert lots_a * 0.01 == pytest.approx(BETA * lots_b * 0.1)
    assert lots_a == pytest.approx(0.813, abs=0.001)
    assert lots_b == pytest.approx(2.863, abs=0.001)


def test_CONTROL_missing_prices_size_nothing():
    assert sizing.lots_for_dollars(1000, BETA, None, ETH, 0.01, 0.1) is None
    assert clean_usd('') is None and clean_usd(0) is None
    assert clean_usd('1000') == 1000.0


def pair(**over):
    p = PairConfig('BTC-USDT-SWAP|ETH-USDT-SWAP',
                   leg_a={'account': 'okx', 'symbol': 'BTC-USDT-SWAP'},
                   leg_b={'account': 'okx', 'symbol': 'ETH-USDT-SWAP'},
                   hedge_ratio=BETA, clip_lots_a=0.1, clip_lots_b=0.35,
                   **over)
    p.meta_a, p.meta_b = {'contract_size': 0.01}, {'contract_size': 0.1}
    return p


def coord():
    return Coordinator.__new__(Coordinator)


def test_the_engine_sets_the_lots_from_the_dollars():
    p = pair(usd_per_qty=1000)
    coord()._size_in_dollars(p, {'leg_a_mid': BTC, 'leg_b_mid': ETH})
    assert p.clip_lots_a == pytest.approx(0.813, abs=0.001)
    assert p.clip_lots_b == pytest.approx(2.863, abs=0.001)
    assert p.to_dict()['usd_per_qty'] == 1000.0


def test_the_lots_follow_the_prices():
    p = pair(usd_per_qty=1000)
    c = coord()
    c._size_in_dollars(p, {'leg_a_mid': BTC, 'leg_b_mid': ETH})
    first = p.clip_lots_a
    c._size_in_dollars(p, {'leg_a_mid': BTC * 1.2, 'leg_b_mid': ETH * 1.2})
    assert p.clip_lots_a == pytest.approx(first / 1.2, rel=1e-3)


def test_CONTROL_without_dollars_the_typed_lots_stand():
    p = pair()
    coord()._size_in_dollars(p, {'leg_a_mid': BTC, 'leg_b_mid': ETH})
    assert (p.clip_lots_a, p.clip_lots_b) == (0.1, 0.35)


def test_an_order_is_rounded_to_okx_lot_step():
    p = pair(usd_per_qty=1000)
    coord()._size_in_dollars(p, {'leg_a_mid': BTC, 'leg_b_mid': ETH})
    meta = {'contract_size': 0.01, 'volume_step': 0.01, 'volume_min': 0.01,
            'volume_max': 10000}
    meta_b = dict(meta, contract_size=0.1)
    plan = sizing.clip_plan(p, meta, meta_b, BTC, ETH, 100)
    assert plan.get('reason') is None
    assert plan["leg_a_lots"] == pytest.approx(81.33, abs=0.02)
    assert plan["leg_b_lots"] == pytest.approx(286.32, abs=0.02)
