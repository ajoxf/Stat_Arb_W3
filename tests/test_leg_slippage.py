"""Which LEG slipped.

Live on spot gold against the gold future, three working orders in a row
cost 0.21, 0.20 and 0.19 - one pair, one direction, near-constant. The
report could only say the SPREAD went on worse than the level; it could
not say which leg did it, so it could not say why. Each leg now keeps
the touch MT5 was quoting when the order was SENT, beside its fill, at
both ends.
"""

import pytest

from okxtrader import slippage
from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import LegFill, SpreadPosition, SpreadSide


@pytest.fixture
def engine(config, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def round_trip(engine, pair, legs, worse_b=0.0, worse_a=0.0):
    legs['acct_b'].broker.fill_worse[pair.symbol_b] = worse_b
    legs['acct_a'].broker.fill_worse[pair.symbol_a] = worse_a
    md = engine.market[pair.key]
    position = engine.executor.market_entry(pair, SpreadSide.SELL, md,
                                            1.0).position
    engine.book.add_position(position)
    engine.executor.close_position(pair, position, md, reason='manual')
    return position


def test_the_future_filling_two_ticks_off_its_quote_shows_on_leg_b(
        engine, pair, legs):
    position = round_trip(engine, pair, legs, worse_b=0.20)
    [row] = slippage.report([position.to_dict()])['rows']
    assert row['entry_leg_b'] == pytest.approx(0.20)       # sold 0.20 low
    assert row['exit_leg_b'] == pytest.approx(0.20)        # bought 0.20 high
    assert row['entry_leg_a'] == pytest.approx(0.0)        # the spot: clean
    assert row['exit_leg_a'] == pytest.approx(0.0)
    assert row['symbol_b'] == pair.symbol_b
    # In money: 0.20 x 0.10 lots x 100 oz.
    assert row['entry_leg_b_money'] == pytest.approx(2.0)


def test_the_CONTROL_a_broker_filling_at_its_quote_shows_nothing(
        engine, pair, legs):
    position = round_trip(engine, pair, legs)
    [row] = slippage.report([position.to_dict()])['rows']
    for key in ('entry_leg_a', 'entry_leg_b', 'exit_leg_a', 'exit_leg_b'):
        assert row[key] == pytest.approx(0.0), key


def test_the_spot_slipping_is_told_apart_from_the_future(engine, pair, legs):
    position = round_trip(engine, pair, legs, worse_a=0.05)
    [row] = slippage.report([position.to_dict()])['rows']
    assert row['entry_leg_a'] == pytest.approx(0.05)
    assert row['entry_leg_b'] == pytest.approx(0.0)


def test_each_ladder_says_which_leg_it_comes_from(engine, pair, legs):
    rows = [round_trip(engine, pair, legs, worse_b=0.20).to_dict()
            for _ in range(3)]
    legs_ = slippage.report(rows)['by_pair'][pair.key]['legs']
    assert legs_['entry_leg_b']['points_mean'] == pytest.approx(0.20)
    assert legs_['entry_leg_b']['measured'] == 3
    assert legs_['entry_leg_a']['points_mean'] == pytest.approx(0.0)


def test_the_send_prices_survive_a_restart(engine, pair, legs, tmp_path):
    position = round_trip(engine, pair, legs, worse_b=0.20)
    store = Store(str(tmp_path / 'trader.db'))
    store.save_position(position)
    [row] = [r for r in store.closed_positions()
             if r['position_id'] == position.position_id]
    back = SpreadPosition.from_dict(row)
    assert back.leg_b.sent_price == pytest.approx(position.leg_b.sent_price)
    assert back.leg_b.exit_price == pytest.approx(position.leg_b.exit_price)


def test_a_fill_from_before_this_was_kept_is_unmeasured_not_zero():
    old = LegFill.from_dict({'account': 'a', 'symbol': 'GC', 'side': 'SELL',
                             'volume': 0.1, 'price': 4176.69})
    assert old.sent_price is None and old.exit_price is None
    row = {'position_id': 'P', 'pair_key': 'X', 'side': 'SELL',
           'quantity': 1.0, 'leg_a': None, 'leg_b': old.to_dict()}
    [out] = slippage.report([row])['rows']
    assert out['entry_leg_b'] is None and out['exit_leg_b'] is None
    summary = slippage.report([row])['by_pair']['X']['legs']
    assert summary['entry_leg_b']['measured'] == 0
    assert summary['entry_leg_b']['points_mean'] is None
