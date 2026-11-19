"""Two costs break-even and the stop used to leave out.

- A stop exits at MARKET: taker on both legs, where break-even priced
  the maker exit. The stop is brought in by that difference, so what it
  loses is the % of margin asked for, not that plus a fee.
- Selling SPOT the account does not hold borrows the coin, and OKX
  charges interest every hour. Break-even for the side that sells the
  spot leg carries it; the other side does not.
"""

import types

import pytest

from okxtrader import takeprofit
from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide


class Maker:
    def __init__(self, on):
        self.on = on

    def enabled(self, pair):
        return self.on


def bare(maker_on=True):
    c = Coordinator.__new__(Coordinator)
    c.maker = Maker(maker_on)
    c.market = {}
    c.legs = {}
    return c


def eth_btc():
    return types.SimpleNamespace(
        key='BTC-USDT-SWAP|ETH-USDT-SWAP', clip_lots_a=1.0, clip_lots_b=3.52,
        meta_a={'contract_size': 0.01}, meta_b={'contract_size': 0.1},
        fee_bps={'a': {'maker': 2.0, 'taker': 5.0},
                 'b': {'maker': 2.0, 'taker': 5.0}},
        account_a='okx', account_b='okx',
        symbol_a='BTC-USDT-SWAP', symbol_b='ETH-USDT-SWAP')


def position(qty=1.0):
    return types.SimpleNamespace(quantity=qty, spread_units=0.352 * qty,
                                 side=SpreadSide.BUY)


MD = {'leg_a_mid': 62000.0, 'leg_b_mid': 1760.0}


def test_a_stop_is_brought_in_by_the_taker_difference():
    # 3 bp more on $620 + $619.52 of closing value = $0.3719,
    # over k 0.352 = 1.0564 points.
    extra = bare().stop_taker_points(eth_btc(), MD, position())
    assert extra == pytest.approx((620.0 + 619.52) * 3e-4 / 0.352)


def test_it_scales_with_size_in_money_not_in_points():
    one = bare().stop_taker_points(eth_btc(), MD, position(1.0))
    five = bare().stop_taker_points(eth_btc(), MD, position(5.0))
    assert five == pytest.approx(one)          # points per spread: same


def test_CONTROL_under_the_taker_fill_there_is_no_difference():
    """Break-even already priced taker at both ends."""
    assert bare(maker_on=False).stop_taker_points(
        eth_btc(), MD, position()) == 0.0


def test_unknown_fees_add_nothing_rather_than_guess():
    pair = eth_btc()
    pair.fee_bps = None
    assert bare().stop_taker_points(pair, MD, position()) == 0.0


# -- borrow interest -------------------------------------------------------

class Broker:
    def __init__(self, rate=0.00000114):
        self.rate = rate

    def inst_type(self, symbol):
        return 'SPOT' if symbol.count('-') == 1 else 'SWAP'

    def borrow_rate(self, ccy):
        return self.rate


def basis(rate=0.00000114):
    c = bare()
    c.legs = {'okx': types.SimpleNamespace(broker=Broker(rate))}
    pair = types.SimpleNamespace(
        key='BTC-USDT|BTC-USDT-SWAP', account_a='okx', account_b='okx',
        symbol_a='BTC-USDT', symbol_b='BTC-USDT-SWAP', clip_lots_a=0.01,
        clip_lots_b=1.0, meta_a={'contract_size': 1.0},
        meta_b={'contract_size': 0.01})
    c.market = {pair.key: {'leg_a_mid': 62000.0, 'leg_b_mid': 62030.0}}
    return c, pair


def test_buying_the_spread_sells_spot_and_pays_borrow_interest():
    c, pair = basis()
    cost = c.borrow_cost(pair, 'BUY', nights=1)
    # 0.01 BTC x 62,000 x 0.000114%/h x 24 h
    assert cost['money'] == pytest.approx(0.01 * 62000 * 0.00000114 * 24)
    assert cost['ccy'] == 'BTC' and cost['hours'] == 24


def test_CONTROL_selling_the_spread_buys_spot_and_borrows_nothing():
    c, pair = basis()
    assert c.borrow_cost(pair, 'SELL', nights=1) is None


def test_an_intraday_trade_still_pays_the_first_hour():
    c, pair = basis()
    assert c.borrow_cost(pair, 'BUY', nights=0)['hours'] == 1.0


def test_borrow_interest_moves_break_even_out():
    c, pair = basis()
    pair.clip_lots_a = 0.01
    terms_plain = takeprofit.break_even_terms(
        types.SimpleNamespace(clip_lots_a=0.01, clip_lots_b=1.0), {}, {},
        1.0, 0.01, 1, {'money': 0.0})
    borrow = c.borrow_cost(pair, 'BUY', nights=1)
    terms = takeprofit.break_even_terms(
        types.SimpleNamespace(clip_lots_a=0.01, clip_lots_b=1.0), {}, {},
        1.0, 0.01, 1, {'money': 0.0, 'borrow': borrow})
    assert terms['borrow_money'] == pytest.approx(borrow['money'])
    assert terms['added_money'] - terms_plain['added_money'] == \
        pytest.approx(borrow['money'])


def test_an_unreadable_borrow_rate_is_said_not_zeroed():
    c, pair = basis(rate=None)
    borrow = c.borrow_cost(pair, 'BUY', nights=1)
    terms = takeprofit.break_even_terms(
        types.SimpleNamespace(clip_lots_a=0.01, clip_lots_b=1.0), {}, {},
        1.0, 0.01, 1, {'money': 0.0, 'borrow': borrow})
    assert terms['added_money'] is None
    assert 'borrow rate' in terms['note']
