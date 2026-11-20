"""The ladder's Fill: maker both legs (the default), one maker and one
taker (Mirror's quote), or taker both legs - per ladder."""

import types

from okxtrader import maker as maker_module
from okxtrader.config import PairConfig, clean_fill_style
from okxtrader.quoter import QuoteGroup, Quoter, passive_clamp


def pair(**over):
    return PairConfig('BTC-USDT-SWAP|ETH-USDT-SWAP',
                      leg_a={'account': 'okx', 'symbol': 'BTC-USDT-SWAP'},
                      leg_b={'account': 'okx', 'symbol': 'ETH-USDT-SWAP'},
                      **over)


def test_a_ladder_keeps_its_own_fill():
    p = pair(fill_style='quote')
    assert p.fill_style == 'quote'
    assert p.to_dict()['fill_style'] == 'quote'
    assert maker_module.fill_style(p, {'FILL_STYLE': 'maker'}) == 'quote'


def test_CONTROL_no_fill_of_its_own_is_the_desks():
    assert maker_module.fill_style(pair(), {'FILL_STYLE': 'maker'}) == 'maker'
    assert maker_module.fill_style(pair(), {}) == 'maker'
    assert clean_fill_style('nonsense') is None


def test_only_a_quote_ladder_quotes():
    quoter = Quoter.__new__(Quoter)
    quoter.config = {'FILL_STYLE': 'maker', 'WORKING_ORDERS': 'TRIGGER'}
    assert quoter.triggers(pair(fill_style='quote')) is False
    assert quoter.triggers(pair(fill_style='maker')) is True
    assert quoter.triggers(pair(fill_style='taker')) is True
    assert quoter.triggers(pair()) is True


def test_a_quote_through_the_market_joins_its_own_touch():
    md = {'leg_b_bid': 100.0, 'leg_b_ask': 100.1}
    assert passive_clamp(md, 'b', 'SELL', 99.0) == (100.1, True)
    assert passive_clamp(md, 'b', 'BUY', 101.0) == (100.0, True)
    # Inside the bid-ask, or behind it: left where the level puts it.
    assert passive_clamp(md, 'b', 'SELL', 100.5) == (100.5, False)
    assert passive_clamp(md, 'b', 'BUY', 99.0) == (99.0, False)


def test_a_quote_okx_cancelled_is_placed_again_not_amended_for_ever():
    quoter = Quoter.__new__(Quoter)
    quoter._unreadable_said = {}
    group = QuoteGroup('K', 'SELL', -3.0, 'b')
    group.ticket, group.price = 1001, 1700.0
    leg = types.SimpleNamespace(order_state=lambda t: {
        'filled_volume': 0.0, 'still_open': False})
    quoter.legs = {'okx': leg}
    p = types.SimpleNamespace(key='K', account_a='okx', account_b='okx')
    assert quoter._check_fill(p, {}, group) is None
    assert group.ticket is None and 'placing it again' in group.reason
