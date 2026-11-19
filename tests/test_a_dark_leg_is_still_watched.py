"""NO PRICE IS NOT NOTHING TO DO.

2026-09-16, live: a trader's leg filled at 18:47:12 and was hedged at
19:00:03. Twelve minutes and fifty-one seconds naked, in silence, on a
pair whose orders the system was supposedly working.

Nothing was broken at the broker. The pending was a real order and the
broker filled it exactly as asked. What failed was here: one leg's tick
went missing, the coordinator's poll said

    if not tick_a or not tick_b:
        continue

and that `continue` skipped EVERYTHING below it — including the only
call that asks whether a resting order has filled. The instant a tick
came back, the fill was noticed and hedged in one second.

So the rule this file pins is a single sentence: a price we cannot see
is a reason to stop PLACING and the opposite of a reason to stop
WATCHING. A pending is a real order at a real broker and it fills
whether or not this process can price it.

Every guard here has a CONTROL that turns the darkness off and asserts
the opposite, because a test that only ever sees the dark path cannot
tell "withheld correctly" from "never worked at all".
"""

import logging

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide


#: The one-leg quote (WORKING_ORDERS = QUOTE); see conftest.
pytestmark = pytest.mark.quote


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def shouted(caplog):
    """Every CRITICAL line, rendered. A fault nobody can find in the log
    is a fault nobody finds at all — these lines are the record the desk
    reads back the next morning."""
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.CRITICAL]


def go_dark(legs, account):
    """That leg has no price at all — the shape of every cause.

    A dropped Market Watch subscription, the broker, the LP, the line,
    the PC: they all arrive here as `tick()` returning nothing, and the
    consequence for an order already at the broker is identical. The
    leg stays TRADABLE, because it usually is: the terminal answers
    order requests while the feed is down, and a test that killed the
    whole leg could not tell the hedge path from the unwind path.
    """
    legs[account].tick = lambda symbol: None


def rest_one(coordinator, pair, legs, side=SpreadSide.BUY, offset=-5):
    """One click, rested as a real pending on the quoting leg (B)."""
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] + offset * pair.increment, 10)
    coordinator.click(pair.key, side, level)
    coordinator.poll_once()
    pendings = legs['acct_b'].pending_orders()
    assert len(pendings) == 1, pendings
    return level, pendings[0]['ticket']


# -- the incident itself ---------------------------------------------------

def test_a_pending_that_fills_while_a_leg_is_dark_is_hedged_anyway(
        engine, pair, legs):
    """THE 2026-09-16 FAULT, reproduced and closed.

    Leg A goes dark; the broker fills leg B's pending while it is dark.
    Before the fix nothing looked, and the hedge waited for the feed.
    """
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)

    go_dark(legs, 'acct_a')
    legs['acct_b'].broker.fill_pending(ticket)

    coordinator.poll_once()

    # ONE poll, not one tick: the hedge is on.
    hedged = legs['acct_a'].broker.open_positions()
    assert len(hedged) == 1, 'leg A was left naked while the pair was dark'
    assert hedged[0]['volume'] == pytest.approx(0.10)
    assert len(legs['acct_b'].broker.open_positions()) == 1
    # And it is BOOKED, so the ladder and the reconciler both know.
    assert len(coordinator.book.positions(pair.key)) == 1


def test_CONTROL_the_same_fill_is_hedged_when_the_pair_can_be_priced(
        engine, pair, legs):
    """The control for the test above: with the feed up, that fill
    hedges. Without this, a hedge that never worked at all would read
    as the fix working."""
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)

    legs['acct_b'].broker.fill_pending(ticket)
    coordinator.poll_once()

    assert len(legs['acct_a'].broker.open_positions()) == 1
    assert len(coordinator.book.positions(pair.key)) == 1


def test_a_fill_that_cannot_be_hedged_is_unwound_BY_TICKET_not_left_naked(
        engine, pair, legs, caplog):
    """The question the trader actually asked: one leg on, and the
    second cannot be placed.

    The dark leg refuses the hedge. The answer is not to wait and not to
    hope — it is to take the leg that DID go on straight back off, by
    ticket, and end flat. Seconds, not minutes.
    """
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)

    go_dark(legs, 'acct_a')
    legs['acct_a'].broker.reject_orders['XAUUSD_'] = \
        '10027 AutoTrading disabled by client'
    legs['acct_b'].broker.fill_pending(ticket)

    with caplog.at_level(logging.CRITICAL):
        coordinator.poll_once()

    assert legs['acct_a'].broker.open_positions() == [], 'leg A dealt anyway'
    assert legs['acct_b'].broker.open_positions() == [], \
        'the filled leg was left ON with no hedge — this is the naked leg'
    # Unwound BY TICKET. On a hedging account an opposite market order
    # opens a SECOND position and leaves the first one exactly where it
    # was, twice the size.
    closes = [e for e in legs['acct_b'].broker.sent if e['action'] == 'close']
    assert len(closes) == 1 and closes[0]['ticket'] == ticket
    assert not [e for e in legs['acct_b'].broker.sent
                if e['action'] == 'market'], 'unwound with an opposite order'
    # The refusal carries the BROKER'S OWN WORDS, not "check the log" —
    # on the order the trader is looking at, and in the log.
    rejected = [o for o in coordinator.book.orders(pair.key,
                                                   working_only=False)
                if o.reason and '10027' in o.reason]
    assert rejected, 'the broker\'s own refusal never reached the trader'
    said = shouted(caplog)
    assert any('10027' in line for line in said)
    assert not [line for line in said if 'UNWIND FAILED' in line]


def test_CONTROL_without_the_refusal_the_dark_fill_ends_with_both_legs_on(
        engine, pair, legs):
    """The control: the unwind above is the REJECTION's doing, not
    something the dark path does to every fill."""
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)

    go_dark(legs, 'acct_a')
    legs['acct_b'].broker.fill_pending(ticket)
    coordinator.poll_once()

    assert len(legs['acct_a'].broker.open_positions()) == 1
    assert len(legs['acct_b'].broker.open_positions()) == 1
    assert not [e for e in legs['acct_b'].broker.sent
                if e['action'] == 'close']


# -- what a dark pair does with what is already resting --------------------

def test_a_resting_pending_is_PULLED_while_the_pair_is_dark(engine, pair,
                                                            legs):
    """We cannot hedge what we cannot price, so an order that might fill
    into that should not be resting. The cost is queue position; the
    alternative is a fill with no hedge."""
    coordinator = engine
    rest_one(coordinator, pair, legs)

    go_dark(legs, 'acct_a')
    coordinator.poll_once()

    assert legs['acct_b'].pending_orders() == []
    # Pulled, not forgotten: the synthetic is still working and comes
    # back when the feed does (next test).
    assert len(coordinator.book.orders(pair.key)) == 1


def test_CONTROL_a_lit_pair_leaves_the_same_pending_resting(engine, pair,
                                                            legs):
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)

    coordinator.poll_once()

    resting = legs['acct_b'].pending_orders()
    assert len(resting) == 1 and resting[0]['ticket'] == ticket


def test_the_order_is_re_rested_at_the_same_level_when_the_feed_returns(
        engine, pair, legs):
    """Pulled is not cancelled. The synthetic is still working; only the
    broker-side order went away, and it comes back where it was."""
    coordinator = engine
    level, _ticket = rest_one(coordinator, pair, legs)
    priced = legs['acct_b'].pending_orders()[0]['price']

    go_dark(legs, 'acct_a')
    coordinator.poll_once()
    assert legs['acct_b'].pending_orders() == []

    del legs['acct_a'].tick            # the feed comes back
    coordinator.poll_once()

    back = legs['acct_b'].pending_orders()
    assert len(back) == 1
    assert back[0]['price'] == pytest.approx(priced)
    assert len(coordinator.book.orders(pair.key)) == 1
    assert coordinator.book.orders(pair.key)[0].level == pytest.approx(level)


# -- a guard must never prevent a close ------------------------------------

def test_a_dark_pair_does_not_touch_a_resting_CLOSE(engine, pair, legs):
    """A closing order rests NOTHING at the broker — it is a level this
    process watches and a close by ticket when the market reaches it.
    There is nothing to pull, and pulling it would delete a trader's
    exit because a feed hiccuped."""
    coordinator = engine
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    coordinator.click(pair.key, SpreadSide.BUY, md['long_spread'])
    position = coordinator.book.positions(pair.key)[0]
    assert coordinator.close_at_limit(pair.key, md['short_spread'] + 5.0)['ok']

    go_dark(legs, 'acct_a')
    coordinator.poll_once()

    still = coordinator.book.orders_for_position(position.position_id)
    assert len(still) == 1 and still[0].is_working, \
        'the trader\'s exit was pulled because a feed went quiet'
    assert coordinator.book.positions(pair.key)[0].is_open


def test_CONTROL_that_same_resting_close_still_fires_when_the_market_arrives(
        engine, pair, legs, gold_symbols):
    """The control: the exit above survived because nothing pulled it,
    not because it was already dead."""
    coordinator = engine
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    coordinator.click(pair.key, SpreadSide.BUY, md['long_spread'])
    level = md['short_spread'] + 5.0
    coordinator.close_at_limit(pair.key, level)

    _spot, future = gold_symbols
    move = (level - coordinator.market[pair.key]['short_spread']) + 0.01
    future.bid += move
    future.ask += move
    coordinator.poll_once()

    assert coordinator.book.positions(pair.key) == []
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []


def test_a_close_by_hand_works_while_the_pair_is_dark(engine, pair, legs):
    """Spec §8, and the rule this whole file could most easily have
    broken: a guard may withhold an ORDER, never a CLOSE. A trader who
    wants out of a position while the screen shows dashes gets out."""
    coordinator = engine
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    coordinator.click(pair.key, SpreadSide.BUY, md['long_spread'])
    position = coordinator.book.positions(pair.key)[0]

    go_dark(legs, 'acct_a')
    coordinator.poll_once()
    closed = coordinator.executor.close_position(pair, position, None)

    assert closed.get('ok'), closed
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []


# -- saying so -------------------------------------------------------------

def test_the_ladder_is_told_WHICH_leg_is_dark_and_why(engine, pair, legs):
    """Dashes alone read as a quiet market. A blind one has to say so,
    and name the leg, because that is the difference between waiting and
    going to look at a terminal."""
    coordinator = engine
    go_dark(legs, 'acct_a')
    coordinator.poll_once()

    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['market'] is None
    reason = row['dark_reason']
    assert reason and 'XAUUSD_' in reason
    assert 'GC1226' not in reason, 'blamed the leg that was answering'


def test_CONTROL_a_priced_pair_carries_no_dark_reason(engine, pair):
    coordinator = engine
    coordinator.poll_once()
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['market'] is not None
    assert row['dark_reason'] is None


def test_the_reason_clears_the_moment_the_feed_comes_back(engine, pair, legs):
    coordinator = engine
    go_dark(legs, 'acct_a')
    coordinator.poll_once()
    assert coordinator.snapshot()['pairs'][pair.key]['dark_reason']

    del legs['acct_a'].tick
    coordinator.poll_once()

    assert coordinator.snapshot()['pairs'][pair.key]['dark_reason'] is None


def test_going_dark_is_logged_at_CRITICAL_and_re_subscribes_itself(
        engine, pair, legs, caplog):
    """A dropped subscription is the commonest cause and re-subscribing
    is what fixes it. The stale path already did this; withholding it
    from the WORSE case made no sense."""
    coordinator = engine
    tried = []
    original = coordinator.refresh_feed
    coordinator.refresh_feed = lambda key: tried.append(key) or original(key)

    go_dark(legs, 'acct_a')
    with caplog.at_level(logging.CRITICAL):
        coordinator.poll_once()

    assert tried == [pair.key]
    assert any('XAUUSD_' in line for line in shouted(caplog))


def test_CONTROL_a_lit_pair_neither_shouts_nor_re_subscribes(engine, pair,
                                                             legs, caplog):
    coordinator = engine
    tried = []
    original = coordinator.refresh_feed
    coordinator.refresh_feed = lambda key: tried.append(key) or original(key)

    with caplog.at_level(logging.CRITICAL):
        coordinator.poll_once()

    assert tried == []
    assert shouted(caplog) == []


# -- nothing new goes on while the pair is blind ---------------------------

def test_no_order_is_PLACED_while_a_leg_has_no_price(engine, pair, legs):
    """The other half of the rule. Watching what is already at the
    broker is compulsory; adding to it is the opposite. Both modes
    refuse, and the refusal says why in words a trader can act on."""
    coordinator = engine
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] - 0.05, 10)
    go_dark(legs, 'acct_a')
    coordinator.poll_once()

    limit = coordinator.click(pair.key, SpreadSide.BUY, level)
    pair.order_type = pair.order_type.__class__('MARKET')
    market = coordinator.click(pair.key, SpreadSide.BUY, level)

    for answer in (limit, market):
        assert answer['ok'] is False and answer['refused']
        assert 'no price' in answer['reason']
    assert legs['acct_b'].pending_orders() == []
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []
    assert coordinator.book.orders(pair.key) == []


def test_CONTROL_the_same_two_clicks_are_taken_when_both_legs_are_priced(
        engine, pair, legs):
    coordinator = engine
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] - 0.05, 10)

    assert coordinator.click(pair.key, SpreadSide.BUY, level)['ok']
    pair.order_type = pair.order_type.__class__('MARKET')
    assert coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread'])['ok']

    coordinator.poll_once()
    assert len(legs['acct_b'].pending_orders()) == 1
    assert len(coordinator.book.positions(pair.key)) == 1


# -- it does not matter WHICH leg went dark -------------------------------

def test_the_QUOTING_leg_going_dark_is_watched_the_same_way(engine, pair,
                                                            legs):
    """The pending rests on leg B. When it is B that goes blind, the
    order we cannot price is the order we are holding — the case where
    doing nothing is most tempting and least safe."""
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)

    go_dark(legs, 'acct_b')
    legs['acct_b'].broker.fill_pending(ticket)
    coordinator.poll_once()

    assert len(legs['acct_a'].broker.open_positions()) == 1
    assert len(coordinator.book.positions(pair.key)) == 1


def test_BOTH_legs_going_dark_names_both_and_still_pulls(engine, pair, legs):
    """The whole terminal, the whole line, the whole PC. Nothing left to
    price with and everything still resting at the broker."""
    coordinator = engine
    rest_one(coordinator, pair, legs)

    go_dark(legs, 'acct_a')
    go_dark(legs, 'acct_b')
    coordinator.poll_once()

    reason = coordinator.snapshot()['pairs'][pair.key]['dark_reason']
    assert 'XAUUSD_' in reason and 'GC1226' in reason
    assert legs['acct_b'].pending_orders() == []
    assert len(coordinator.book.orders(pair.key)) == 1
