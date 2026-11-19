"""The margin a position's TP %, SL % and profit % are measured on.

Both legs, each priced by its own terminal ON THE SIDE IT IS ACTUALLY
ON: a BUY spread buys B and sells A, a SELL spread the reverse. And
where it came from is kept, so an estimate is never shown as MT5's own
figure.
"""

import pytest

from okxtrader import telegram as tg
from okxtrader.coordinator import Coordinator
from okxtrader.models import OrderType, SpreadPosition


@pytest.fixture
def engine(config, pair, legs):
    co = Coordinator(config, legs, sleep=lambda s: None)
    co.start()
    co.poll_once()
    return co


def spy(legs, per_lot, engine=None):
    """Each fake terminal prices margin, and records the side asked."""
    asked = []
    if engine is not None:
        engine._margin_cache.clear()      # priced again, from here
    for name, leg in legs.items():
        broker = leg.broker
        broker.margin_per_lot = per_lot[name]

        def margin_for(symbol, side, volume, price=None, _b=broker,
                       _n=name):
            asked.append((_n, side))
            return float(_b.margin_per_lot) * float(volume)
        broker.margin_for = margin_for
    return asked


def test_a_sell_spread_is_priced_on_the_sides_it_is_on(engine, pair, legs):
    asked = spy(legs, {'acct_a': 100.0, 'acct_b': 200.0}, engine)
    detail = engine.margin_detail(pair, 'SELL')
    assert dict(asked) == {'acct_a': 'BUY', 'acct_b': 'SELL'}
    assert detail['money'] == pytest.approx(
        100.0 * pair.clip_lots_a + 200.0 * pair.clip_lots_b)
    assert detail['source'] == 'terminal'


def test_CONTROL_a_buy_spread_and_no_side_keep_the_old_sides(engine, pair,
                                                             legs):
    asked = spy(legs, {'acct_a': 100.0, 'acct_b': 200.0}, engine)
    engine.margin_detail(pair, 'BUY')
    assert dict(asked) == {'acct_a': 'SELL', 'acct_b': 'BUY'}
    asked.clear()
    engine._margin_cache.clear()
    engine.margin_detail(pair)
    assert dict(asked) == {'acct_a': 'SELL', 'acct_b': 'BUY'}


def test_a_position_freezes_its_own_direction_and_the_source(engine, pair,
                                                             legs):
    asked = spy(legs, {'acct_a': 100.0, 'acct_b': 200.0}, engine)
    md = engine.market[pair.key]
    pair.order_type = OrderType.MARKET
    assert engine.click(pair.key, 'SELL', md['short_spread'], 1)['ok']
    pair.order_type = OrderType.LIMIT
    [position] = engine.book.positions(pair.key)
    asked.clear()
    position.entry_margin = None
    engine.entry_margin(pair, position)
    assert dict(asked) == {'acct_a': 'BUY', 'acct_b': 'SELL'}
    assert position.entry_margin_source == 'terminal'
    again = SpreadPosition.from_dict(position.to_dict())
    assert again.entry_margin_source == 'terminal'           # saved


# -- what Telegram says -----------------------------------------------------

POSITION = {'quantity': 2.0, 'entry_margin': 500.0,
            'leg_a': {'volume': 1.0, 'contract_size': 1000, 'price': 82.0},
            'leg_b': {'volume': 1.0, 'contract_size': 1000, 'price': 92.0}}


def test_an_estimated_margin_is_said_to_be_one():
    est = dict(POSITION, entry_margin_source='leverage')
    assert 'estimated from leverage' in tg.margin_text(est)
    assert 'of margin (estimated)' in tg.pnl_shares(100.0, est)


def test_CONTROL_the_terminals_own_margin_carries_no_note():
    own = dict(POSITION, entry_margin_source='terminal')
    assert 'estimated' not in tg.margin_text(own)
    assert 'estimated' not in tg.pnl_shares(100.0, own)
    # Both legs, times the size: 500 per spread x 2.
    assert '$1,000' in tg.margin_text(own) or '1,000' in tg.margin_text(own)
    assert '+10.00% of margin' in tg.pnl_shares(100.0, own)
