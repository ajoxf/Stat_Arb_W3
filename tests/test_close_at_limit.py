"""Closing at a PRICE, not at the market.

CLOSE ALL crosses now. This is the other half: name a spread level and
rest one working order per open position, each carrying that position's
ticket — on a hedging account an opposite order without the ticket
OPENS a second position instead of closing the first.

It shares AutoRouting's machinery, and that is exactly where it can go
wrong: turning AutoRouting off pulls what AutoRouting armed, and an
exit the TRADER rested by hand must survive that. `auto_armed` is the
whole of the distinction, so most of this file is about it.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def market_entry(coordinator, pair, side=SpreadSide.BUY):
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, side, md['long_spread'])
    assert answer.get('ok'), answer
    return coordinator.book.positions(pair.key)[0]


def away(coordinator, pair):
    """A level the market cannot reach on this poll.

    A resting close is held HERE and fired when the market gets to it,
    so a level the market is already through fires at once — which is
    the behaviour of a different test from most of these.
    """
    return coordinator.market[pair.key]['short_spread'] + 5.0


def reach(coordinator, pair, gold_symbols, level):
    """Walk leg B until the spread can be SOLD at or over `level`."""
    _spot, future = gold_symbols
    move = (level - coordinator.market[pair.key]['short_spread']) + 0.01
    future.bid += move
    future.ask += move
    coordinator.poll_once()


# -- what it places --------------------------------------------------------

def test_it_rests_one_order_per_position_at_the_price_asked(engine, pair):
    coordinator = engine
    position = market_entry(coordinator, pair)

    answer = coordinator.close_at_limit(pair.key, 12.5)

    assert answer['ok'] and len(answer['armed']) == 1
    resting = coordinator.book.orders_for_position(position.position_id)
    assert len(resting) == 1
    assert resting[0].level == 12.5
    # A long is closed by SELLING the spread.
    assert resting[0].side is SpreadSide.SELL


def test_it_closes_BY_TICKET_and_rests_nothing_at_either_broker(
        engine, pair, legs, gold_symbols):
    """The level is held HERE. MT5 ignores `position` on a pending, so
    an opposite limit rested at the broker OPENS a second position on a
    hedging account instead of closing the first — that is exactly what
    happened live. When the market reaches the level both legs are
    closed by ticket."""
    coordinator = engine
    position = market_entry(coordinator, pair)
    level = away(coordinator, pair)
    coordinator.close_at_limit(pair.key, level)
    coordinator.poll_once()

    assert coordinator.book.orders_for_position(position.position_id)
    assert not legs['acct_a'].broker.pendings
    assert not legs['acct_b'].broker.pendings

    reach(coordinator, pair, gold_symbols, level)

    assert position.is_open is False
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []
    closes = [e for e in legs['acct_a'].broker.sent if e['action'] == 'close']
    assert closes and closes[-1]['ticket'] == \
        position.leg_a.position_tickets[0]


def test_it_is_marked_as_the_TRADER_s_order(engine, pair):
    coordinator = engine
    position = market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)
    order = coordinator.book.orders_for_position(position.position_id)[0]
    assert order.auto_armed is False
    # ...and the orphan sweep still sees it: a closing order left
    # against a position that has gone fills into nothing.
    assert coordinator.book.auto_armed_for(position.position_id) == []


def test_nothing_crosses_now(engine, pair, legs):
    """A target, and no stop — and above all, no market order. The
    control against this quietly becoming CLOSE ALL."""
    coordinator = engine
    market_entry(coordinator, pair)
    before = len(legs['acct_b'].broker.open_positions())
    coordinator.close_at_limit(pair.key, away(coordinator, pair))
    coordinator.poll_once()
    assert len(legs['acct_b'].broker.open_positions()) == before


# -- what it refuses -------------------------------------------------------

def test_flat_is_refused_and_says_so(engine, pair):
    answer = engine.close_at_limit(pair.key, 12.5)
    assert answer['ok'] is False
    assert 'flat' in answer['reason']


def test_a_price_that_is_not_a_number_is_refused(engine, pair):
    coordinator = engine
    market_entry(coordinator, pair)
    for value in (None, '', 'later'):
        answer = coordinator.close_at_limit(pair.key, value)
        assert answer['ok'] is False, value
        assert 'needs a price' in answer['reason']


def test_an_unknown_pair_is_refused(engine):
    assert engine.close_at_limit('nothing|here', 1.0)['ok'] is False


def test_a_position_already_working_an_exit_is_not_given_a_second(engine,
                                                                  pair):
    """Two closing orders on one position is a position closed twice —
    the second fill opens the opposite side."""
    coordinator = engine
    position = market_entry(coordinator, pair)
    assert coordinator.close_at_limit(pair.key, 12.5)['ok']

    again = coordinator.close_at_limit(pair.key, 13.5)

    assert again['ok'] is False
    assert again['already_working'] == [position.position_id]
    assert 'pull it first' in again['reason']
    assert len(coordinator.book.orders_for_position(
        position.position_id)) == 1


# -- and what must NOT take it away ----------------------------------------

def test_the_tidy_up_leaves_the_trader_s_exit_alone(engine, pair):
    """The one that matters. Every poll pulls the closing orders that
    must not stay; a hand-rested exit is the trader's, and taking it
    away leaves a live position with no target and nobody told."""
    coordinator = engine
    position = market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)

    coordinator.tidy_closing_orders(pair)

    resting = coordinator.book.orders_for_position(position.position_id)
    assert len(resting) == 1 and resting[0].level == 12.5


def test_an_AutoRouting_target_left_from_before_is_pulled(engine, pair):
    """The control: AutoRouting has been removed, and a target it armed
    before the upgrade must not be left resting — that order would
    still fill."""
    coordinator = engine
    position = market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)
    # As one would come back from before the upgrade: a target marked as
    # automation's, resting against the same position.
    from okxtrader.models import OrderType
    leftover = coordinator.book.add_order(
        pair, position.side.opposite, 13.0, position.quantity,
        order_type=OrderType.LIMIT, position_id=position.position_id,
        auto_armed=True)
    assert leftover.auto_armed is True
    assert len(coordinator.book.orders_for_position(
        position.position_id)) == 2

    coordinator.tidy_closing_orders(pair)

    resting = coordinator.book.orders_for_position(position.position_id)
    assert [o.level for o in resting] == [12.5]       # the trader's stays


def test_a_closing_order_still_goes_when_its_POSITION_goes(engine, pair):
    """Whoever rested it. An exit whose position is gone would fill
    into nothing and open a naked one."""
    coordinator = engine
    position = market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)

    position.closed_at = 1_700_000_000.0
    coordinator.tidy_closing_orders(pair)

    assert coordinator.book.orders_for_position(position.position_id) == []


def test_it_is_written_to_the_session_log(engine, pair):
    coordinator = engine
    market_entry(coordinator, pair)
    coordinator.close_at_limit(pair.key, 12.5)
    assert any(event['action'] == 'close_at_limit'
               for event in coordinator.session_events)
