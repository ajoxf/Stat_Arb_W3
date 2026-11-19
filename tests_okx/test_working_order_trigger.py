"""A working order is held here and crossed when the SPREAD gets there.

The fault it replaces, live on spot gold against the gold future: SELL
clicked at 28.30, on at 26.84. The old working order was a real limit
on ONE leg, and two legs that are the same metal move together - the
limit filled on a move in gold, and the leg crossed after it had moved
the same way. A held order fires only when the executable spread is at
its level, and both legs cross at market together.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import OrderState, OrderType, SpreadSide


@pytest.fixture
def engine(config, pair, legs):
    pair.order_type = OrderType.LIMIT
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def move(legs, symbol_leg, by):
    broker = legs[symbol_leg].broker
    [symbol] = broker.symbols.values()
    symbol.quote(symbol.bid + by, symbol.ask + by)


def sell_above(coordinator, pair, ticks=20):
    md = coordinator.market[pair.key]
    return round(md['short_spread'] + ticks * pair.increment, 10)


def nothing_at_the_broker(legs):
    return all(leg.pending_orders() == [] and
               leg.broker.open_positions() == [] for leg in legs.values())


def test_a_working_order_rests_nothing_at_the_broker(engine, pair, legs):
    level = sell_above(engine, pair)
    answer = engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    assert answer['ok']
    assert nothing_at_the_broker(legs)
    [quote] = engine.quoter.snapshot(pair.key)
    assert quote['held'] is True and quote['leg'] == 'BOTH'
    assert quote['ticket'] is None and 'symbol' not in quote
    [order] = engine.book.orders(pair.key)
    assert order.state is OrderState.WORKING


def test_it_crosses_both_legs_when_the_sell_spread_reaches_it(engine, pair,
                                                              legs):
    level = sell_above(engine, pair)
    engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    move(legs, 'acct_b', +0.30)                    # the spread widens
    engine.poll_once()
    [position] = engine.book.positions(pair.key)
    assert position.side is SpreadSide.SELL
    assert position.order_type is OrderType.LIMIT
    assert legs['acct_a'].broker.open_positions()
    assert legs['acct_b'].broker.open_positions()
    # On at the level or better, and scored against the level named.
    assert position.entry_spread >= level - 1e-9
    assert position.entry_slippage == pytest.approx(level -
                                                    position.entry_spread)
    assert engine.book.orders(pair.key) == []
    assert engine.quoter.snapshot(pair.key) == []
    # Never a pending, at any point.
    assert not [e for leg in legs.values() for e in leg.broker.sent
                if e['action'] == 'pending']


def test_the_gold_case_both_legs_rising_together_fills_nothing(engine, pair,
                                                               legs):
    """The live fault: gold rises $1.46, both legs with it. The spread
    did not move, so a SELL above it is not reached - nothing goes on."""
    level = sell_above(engine, pair, ticks=10)
    engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    move(legs, 'acct_a', +1.46)
    move(legs, 'acct_b', +1.46)
    engine.poll_once()
    assert engine.book.positions(pair.key) == []
    assert nothing_at_the_broker(legs)
    assert engine.book.orders(pair.key)[0].is_working


@pytest.mark.quote
def test_the_CONTROL_the_old_one_leg_quote_DID_fill_on_that_move(engine, pair,
                                                                 legs):
    """The same click, the same move, on the old path: the limit on the
    future is reached by gold's move alone, and the spread it went on at
    is worse than the level by exactly that move."""
    level = sell_above(engine, pair, ticks=10)
    engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    [pending] = legs['acct_b'].pending_orders()
    move(legs, 'acct_a', +1.46)
    # The future reaches the resting limit on the same move.
    legs['acct_b'].broker.fill_pending(pending['ticket'])
    engine.poll_once()
    [position] = engine.book.positions(pair.key)
    assert level - position.entry_spread == pytest.approx(1.46, abs=0.25)


def test_a_buy_reads_the_buy_spread(engine, pair, legs):
    md = engine.market[pair.key]
    level = round(md['long_spread'] - 20 * pair.increment, 10)
    engine.click(pair.key, SpreadSide.BUY, level)
    engine.poll_once()
    assert engine.book.positions(pair.key) == []
    move(legs, 'acct_b', -0.30)                    # the spread narrows
    engine.poll_once()
    [position] = engine.book.positions(pair.key)
    assert position.side is SpreadSide.BUY
    assert position.entry_spread <= level + 1e-9


def test_a_stale_price_holds_the_order_and_the_CONTROL_fires_it(engine, pair,
                                                                legs):
    level = sell_above(engine, pair)
    engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    move(legs, 'acct_b', +0.30)
    md = dict(engine.market[pair.key], guard_reason='leg A stale 30s')
    engine.quoter.work(pair, md)
    assert engine.book.positions(pair.key) == []
    assert 'stale' in engine.quoter.snapshot(pair.key)[0]['reason']
    engine.poll_once()                             # the price is good again
    assert len(engine.book.positions(pair.key)) == 1


def test_a_cancelled_order_never_fires(engine, pair, legs):
    level = sell_above(engine, pair)
    answer = engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    engine.cancel_order(answer['order']['order_id'])
    move(legs, 'acct_b', +0.30)
    engine.poll_once()
    assert engine.book.positions(pair.key) == []
    assert nothing_at_the_broker(legs)


def test_a_refusal_at_the_level_ends_the_order_in_the_brokers_words(
        config, pair, legs, tmp_path):
    pair.order_type = OrderType.LIMIT
    store = Store(str(tmp_path / 'trader.db'))
    engine = Coordinator(config, legs, sleep=lambda s: None, store=store)
    engine.start()
    engine.poll_once()
    level = sell_above(engine, pair)
    answer = engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    for leg in legs.values():
        for name in leg.broker.symbols:
            leg.broker.reject_orders[name] = \
                '10027 - AutoTrading disabled by client'
    move(legs, 'acct_b', +0.30)
    engine.poll_once()
    order = engine.book.order(answer['order']['order_id'])
    assert order.state is OrderState.REJECTED
    assert '10027' in order.reason and f'{level:g}' in order.reason
    assert engine.book.orders(pair.key) == []        # not re-fired
    assert nothing_at_the_broker(legs)
    [refused] = store.events('refused')
    assert '10027' in refused['detail']['reason']
    # Once, not three times a second.
    engine.poll_once()
    assert len(store.events('refused')) == 1


def test_the_ladder_does_not_count_a_held_order_as_missing_at_the_broker(
        engine, pair):
    engine.click(pair.key, SpreadSide.SELL, sell_above(engine, pair))
    engine.poll_once()
    row = engine.snapshot()['pairs'][pair.key]
    assert row['working_orders'] == 'TRIGGER'
    assert row['held_entries'] == 1 and row['broker_pendings'] == 0


# -- the re-check before sending ------------------------------------------------


def flicker(engine, pair, legs, level):
    """The poll saw the spread at the level; by the time the orders would
    go, the broker's prices are back where they were."""
    seen = dict(engine.market[pair.key])
    seen['short_spread'] = level + 0.01
    return seen


def test_a_level_the_spread_only_flickered_to_is_not_sent(engine, pair, legs):
    level = sell_above(engine, pair)
    engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    events = engine.quoter.work(pair, flicker(engine, pair, legs, level))
    assert [e['action'] for e in events] == ['recheck_missed']
    assert engine.book.positions(pair.key) == []
    assert nothing_at_the_broker(legs)
    [quote] = engine.quoter.snapshot(pair.key)
    assert quote['rechecks_missed'] == 1
    assert engine.book.orders(pair.key)[0].is_working   # still working
    # When the spread really is there, it goes.
    move(legs, 'acct_b', +0.30)
    engine.poll_once()
    assert len(engine.book.positions(pair.key)) == 1


def test_the_CONTROL_with_the_recheck_off_the_flicker_is_traded(engine, pair,
                                                                legs):
    engine.config.settings['RECHECK_BEFORE_SEND'] = False
    level = sell_above(engine, pair)
    engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    engine.quoter.work(pair, flicker(engine, pair, legs, level))
    [position] = engine.book.positions(pair.key)
    # ...and it went on below the level: the bad fill the re-check stops.
    assert position.entry_spread < level


def test_it_is_sent_on_the_fresh_prices_not_the_polls(engine, pair, legs):
    level = sell_above(engine, pair)
    engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    seen = flicker(engine, pair, legs, level)
    move(legs, 'acct_b', +0.50)               # further still, by now
    engine.quoter.work(pair, seen)
    [position] = engine.book.positions(pair.key)
    assert position.entry_spread > level + 0.1


def test_a_leg_that_cannot_be_re_read_sends_nothing(engine, pair, legs):
    level = sell_above(engine, pair)
    engine.click(pair.key, SpreadSide.SELL, level)
    engine.poll_once()
    move(legs, 'acct_b', +0.30)
    seen = dict(engine.market[pair.key], short_spread=level + 0.3)
    legs['acct_a'].tick = lambda symbol: None
    engine.quoter.work(pair, seen)
    assert engine.book.positions(pair.key) == []
    assert 'could not re-read' in engine.quoter.snapshot(pair.key)[0]['reason']
