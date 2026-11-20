"""LIMIT mode: quote one leg, cross the other.

The peg is anchored on the OTHER leg — the fault that produced 13.96
seconds from click to pair-on, and +0.4700 of slippage, was a peg
chasing its own leg's book.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import OrderState, OrderType, SpreadSide
from okxtrader.quoter import implied_spread, peg_price, quoting_leg


#: The one-leg quote (WORKING_ORDERS = QUOTE); see conftest.
pytestmark = pytest.mark.quote


@pytest.fixture
def engine(config, pair, legs):
    # These tests are about the QUOTING mechanics - several orders
    # resting on one ladder, summed into one pending. The position
    # limit (tests/test_position_limit.py) is off here so it does not
    # stand in front of what is under test.
    pair.max_positions = 0
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def level_of(coordinator, pair, offset_ticks=-5):
    """A level below the market, so the pending rests rather than fills."""
    md = coordinator.market[pair.key]
    return round(md['short_spread'] + offset_ticks * pair.increment, 10)


def test_a_click_rests_a_real_pending_on_the_quoting_leg(engine, pair, legs):
    coordinator = engine
    level = level_of(coordinator, pair)

    coordinator.click(pair.key, SpreadSide.BUY, level)
    coordinator.poll_once()

    quotes = coordinator.quoter.snapshot(pair.key)
    assert len(quotes) == 1
    quote = quotes[0]
    # Leg B has the wider book here, so it is the leg that quotes — that
    # is the spread being earned.
    assert quote['leg'] == 'B'
    pendings = legs['acct_b'].pending_orders()
    assert len(pendings) == 1
    assert pendings[0]['ticket'] == quote['ticket']
    # And nothing crossed: a working order is not a trade.
    assert legs['acct_a'].broker.open_positions() == []


def test_the_pending_price_is_the_one_that_makes_the_clicked_spread(
        engine, pair, legs):
    coordinator = engine
    md = coordinator.market[pair.key]
    level = level_of(coordinator, pair, offset_ticks=5)   # SELL: above the market (OKX post-only rests, never crosses)

    coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.poll_once()

    resting = legs['acct_b'].pending_orders()[0]['price']
    # SELL the spread quoting B: P_B = S + beta x ask_A, because leg A
    # is the one we will have to LIFT when this fills.
    assert resting == pytest.approx(level + pair.hedge_ratio * md['leg_a_ask'])
    assert implied_spread(pair, md, SpreadSide.SELL, 'b',
                          resting) == pytest.approx(level)


def test_the_peg_chases_the_other_leg_not_its_own(engine, pair, legs):
    """The 13.96-second incident: the peg was anchored on its own leg's
    book, so it chased the market away from itself and never filled."""
    coordinator = engine
    level = level_of(coordinator, pair, offset_ticks=5)   # SELL: above the market (OKX post-only rests, never crosses)
    coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.poll_once()
    first = legs['acct_b'].pending_orders()[0]['price']

    # Leg A rises 4.00. Leg B has not moved at all. (Rises: with the
    # SELL resting above the market, a FALL in A would bring the level
    # to B's own bid, and OKX's post-only would join the touch instead.)
    legs['acct_a'].broker.quote('XAUUSD_', 4296.00, 4296.20)
    coordinator.poll_once()

    moved = legs['acct_b'].pending_orders()[0]['price']
    assert moved == pytest.approx(first + 4.0)
    md = coordinator.market[pair.key]
    assert implied_spread(pair, md, SpreadSide.SELL, 'b',
                          moved) == pytest.approx(level)
    assert coordinator.quoter.snapshot(pair.key)[0]['repegs'] == 1


def test_a_move_inside_the_dead_band_does_not_touch_the_order(engine, pair,
                                                              legs):
    """Every MODIFY loses queue position. Re-pricing three times a second
    guarantees you are never at the front of a queue, which defeats the
    entire point of quoting."""
    coordinator = engine
    coordinator.click(pair.key, SpreadSide.SELL,
                      level_of(coordinator, pair, offset_ticks=5))
    coordinator.poll_once()
    modifies = [e for e in legs['acct_b'].broker.sent if e['action'] == 'modify']

    # Half an increment of drift: inside the band.
    legs['acct_a'].broker.quote('XAUUSD_', 4292.005, 4292.205)
    coordinator.poll_once()

    assert [e for e in legs['acct_b'].broker.sent
            if e['action'] == 'modify'] == modifies


def test_three_clicks_at_one_level_are_one_pending_at_the_summed_size(
        engine, pair, legs):
    coordinator = engine
    level = level_of(coordinator, pair)
    for _ in range(3):
        coordinator.click(pair.key, SpreadSide.BUY, level)
    coordinator.poll_once()

    pendings = legs['acct_b'].pending_orders()
    assert len(pendings) == 1
    assert pendings[0]['volume'] == pytest.approx(0.3)   # 3 x 0.1
    # Still three separate, individually cancellable synthetics.
    assert len(coordinator.book.orders(pair.key)) == 3


def test_cancelling_one_resizes_the_pending_and_the_last_one_pulls_it(
        engine, pair, legs):
    coordinator = engine
    level = level_of(coordinator, pair)
    orders = [coordinator.click(pair.key, SpreadSide.BUY, level)['order']
              for _ in range(3)]
    coordinator.poll_once()

    coordinator.cancel_order(orders[0]['order_id'])
    coordinator.poll_once()
    assert legs['acct_b'].pending_orders()[0]['volume'] == pytest.approx(0.2)

    for order in orders[1:]:
        coordinator.cancel_order(order['order_id'])
    coordinator.poll_once()
    assert legs['acct_b'].pending_orders() == []


def test_a_fill_crosses_the_other_leg_immediately(engine, pair, legs):
    """The crossing order goes IMMEDIATELY on fill — the deadline is a
    failure-escalation window, not a patience window."""
    coordinator = engine
    level = level_of(coordinator, pair)
    coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.poll_once()
    ticket = coordinator.quoter.snapshot(pair.key)[0]['ticket']

    legs['acct_b'].broker.fill_pending(ticket)
    coordinator.poll_once()

    positions = coordinator.book.positions(pair.key)
    assert len(positions) == 1
    position = positions[0]
    assert position.order_type is OrderType.LIMIT
    # Leg A was crossed at market, for the hedge to what actually filled.
    assert legs['acct_a'].broker.open_positions()[0]['volume'] == \
        pytest.approx(0.1)
    # The entry is the two REAL fills: the quoted price on B, the touch
    # we paid on A.
    assert position.entry_spread == pytest.approx(
        position.leg_b.price - pair.hedge_ratio * position.leg_a.price)
    # A maker fill is scored against the level the trader NAMED.
    assert position.entry_slippage is not None
    assert coordinator.quoter.hedge_times


def test_the_synthetic_is_marked_filled_and_stops_working(engine, pair, legs):
    coordinator = engine
    level = level_of(coordinator, pair)
    order_id = coordinator.click(pair.key, SpreadSide.SELL,
                                 level)['order']['order_id']
    coordinator.poll_once()
    legs['acct_b'].broker.fill_pending(
        coordinator.quoter.snapshot(pair.key)[0]['ticket'])
    coordinator.poll_once()

    order = coordinator.book.order(order_id)
    assert order.state is OrderState.FILLED
    assert coordinator.book.orders(pair.key) == []


def test_a_partial_fill_hedges_what_filled_not_what_rested(engine, pair,
                                                           legs):
    coordinator = engine
    level = level_of(coordinator, pair)
    for _ in range(3):
        coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.poll_once()
    ticket = coordinator.quoter.snapshot(pair.key)[0]['ticket']

    legs['acct_b'].broker.fill_pending(ticket, volume=0.1)   # 0.1 of 0.3
    coordinator.poll_once()

    assert legs['acct_a'].broker.open_positions()[0]['volume'] == \
        pytest.approx(0.1)
    position = coordinator.book.positions(pair.key)[0]
    assert position.quantity == pytest.approx(1.0)          # one spread
    # The other two clicks are still working, and still separate.
    working = coordinator.book.orders(pair.key)
    assert len(working) == 2


def test_a_partial_fill_pulls_the_resting_remainder_before_hedging(
        engine, pair, legs):
    """MT5 leaves the rest of a partly-filled pending resting. Hedging
    without pulling it would put a SECOND pending on the same level."""
    coordinator = engine
    level = level_of(coordinator, pair)
    for _ in range(3):
        coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.poll_once()
    ticket = coordinator.quoter.snapshot(pair.key)[0]['ticket']

    legs['acct_b'].broker.part_fill_pending(ticket, 0.1)
    coordinator.poll_once()

    pendings = legs['acct_b'].pending_orders()
    assert len(pendings) == 1                      # never two
    assert pendings[0]['ticket'] != ticket         # a fresh one, re-sized
    assert pendings[0]['volume'] == pytest.approx(0.2)
    assert legs['acct_a'].broker.open_positions()[0]['volume'] == \
        pytest.approx(0.1)


def test_a_rejected_hedge_unwinds_the_quoted_leg_and_pulls_the_pair(
        engine, pair, legs):
    """If the crossing account cannot trade, none of the remaining
    synthetics on this pair can complete either."""
    coordinator = engine
    level = level_of(coordinator, pair)
    coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.click(pair.key, SpreadSide.SELL,
                      round(level - pair.increment, 10))
    coordinator.poll_once()
    ticket = coordinator.quoter.snapshot(pair.key)[0]['ticket']
    legs['acct_a'].broker.reject_orders[pair.symbol_a] = \
        '10027 - AutoTrading disabled by client'

    legs['acct_b'].broker.fill_pending(ticket)
    coordinator.poll_once()

    # The quoted leg is CLOSED by ticket, not offset.
    assert legs['acct_b'].broker.open_positions() == []
    assert [e['action'] for e in legs['acct_b'].broker.sent
            if e['action'] in ('market', 'close')] == ['close']
    # And every other working order on this pair is pulled, with the
    # broker's own words on it.
    assert coordinator.book.orders(pair.key) == []
    assert legs['acct_b'].pending_orders() == []
    pulled = coordinator.book.orders(pair.key, working_only=False)
    assert any('10027' in (o.reason or '') for o in pulled)


def test_a_synthetic_does_not_rest_on_a_stale_or_desynced_print(engine, pair,
                                                                legs):
    """Withhold it: the level is still there when the quote refreshes,
    and if it is not then it was never offered."""
    coordinator = engine
    level = level_of(coordinator, pair)
    coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.market[pair.key]['guard_reason'] = 'Leg A stale'

    coordinator.quoter.work(pair, coordinator.market[pair.key])

    assert legs['acct_b'].pending_orders() == []
    assert 'stale' in coordinator.quoter.snapshot(pair.key)[0]['reason']

    # The control: with the feed healthy the same order rests at once.
    coordinator.poll_once()
    assert legs['acct_b'].pending_orders()


def test_a_cancel_that_races_a_fill_is_hedged_not_tidied_away(engine, pair,
                                                              legs):
    """A cancel that did not prevent a fill is a distinct event: the
    position IS there, and it needs its hedge."""
    coordinator = engine
    level = level_of(coordinator, pair)
    order_id = coordinator.click(pair.key, SpreadSide.SELL,
                                 level)['order']['order_id']
    coordinator.poll_once()
    ticket = coordinator.quoter.snapshot(pair.key)[0]['ticket']

    # It fills in the instant between the trader's click and our cancel.
    legs['acct_b'].broker.fill_pending(ticket)
    coordinator.cancel_order(order_id)
    coordinator.poll_once()

    assert len(coordinator.book.positions(pair.key)) == 1
    assert legs['acct_a'].broker.open_positions()[0]['volume'] == \
        pytest.approx(0.1)


def test_the_quoting_leg_defaults_to_the_wider_book_and_can_be_overridden(
        pair, legs):
    from okxtrader.coordinator import _meta_from_report
    pair.meta_a = _meta_from_report(legs['acct_a'].symbol_report('XAUUSD_'))
    pair.meta_b = _meta_from_report(legs['acct_b'].symbol_report('GC1226'))
    assert pair.meta_b['width'] > pair.meta_a['width']
    assert quoting_leg(pair) == 'b'

    pair.quoting_leg = 'a'
    assert quoting_leg(pair) == 'a'


def test_quoting_leg_a_prices_off_leg_b(pair, legs):
    """The mirror of the default, and the arithmetic divides by beta."""
    from okxtrader.spread import compute_spread
    pair.hedge_ratio = 2.0
    md = compute_spread(pair, legs['acct_a'].tick('XAUUSD_'),
                        legs['acct_b'].tick('GC1226'), 2.0)

    price, side = peg_price(pair, md, SpreadSide.BUY, md['spread'], 'a')

    # BUY the spread quoting A: we SELL A, and cross B's ASK.
    assert side.value == 'SELL'
    assert price == pytest.approx((md['leg_b_ask'] - md['spread']) / 2.0)
    assert implied_spread(pair, md, SpreadSide.BUY, 'a',
                          price) == pytest.approx(md['spread'])



def test_a_fill_too_small_to_hedge_says_so_in_our_own_words(engine, pair,
                                                            legs):
    """A fill under one volume step of the crossing leg has no hedge
    that size.

    It used to go to the broker anyway, as a zero-volume order, and come
    back "invalid volume" — a true statement about the wrong thing, at
    the one moment a leg is on and unhedged. The leg still comes off;
    what changes is that the reason names the fill.
    """
    coordinator = engine
    level = level_of(coordinator, pair)
    coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.poll_once()
    ticket = coordinator.quoter.snapshot(pair.key)[0]['ticket']

    # A sliver: leg A cannot trade 0.005 — its step is 0.01.
    legs['acct_b'].broker.part_fill_pending(ticket, 0.005)
    coordinator.poll_once()

    orders = coordinator.book.orders(pair.key, working_only=False)
    reason = ' '.join(o.reason or '' for o in orders)
    assert 'under one volume step of leg A' in reason
    assert 'invalid volume' not in reason.lower()
    # And the sliver came back off: half a spread is a naked leg.
    assert legs['acct_b'].broker.open_positions() == []
    assert legs['acct_a'].broker.open_positions() == []


def test_a_ladder_carries_several_working_orders_at_once(engine, pair, legs):
    """Two sells above the market and two buys below, all resting.

    A price ladder is worked by leaving orders at prices you want rather
    than by watching for one. Each click is its own synthetic, each
    (side, level) its own real pending on the quoting leg, and each is
    cancellable on its own — so pulling the 7.90 sell leaves the 7.85
    working.
    """
    coordinator = engine
    md = coordinator.market[pair.key]
    tick = pair.increment
    sells = [round(md['short_spread'] + n * tick, 10) for n in (5, 10)]
    buys = [round(md['long_spread'] - n * tick, 10) for n in (5, 10)]

    for level in sells:
        coordinator.click(pair.key, SpreadSide.SELL, level)
    for level in buys:
        coordinator.click(pair.key, SpreadSide.BUY, level)
    coordinator.poll_once()

    working = coordinator.book.orders(pair.key)
    assert len(working) == 4
    assert sorted(o.level for o in working) == sorted(sells + buys)

    # Four separate pendings at the broker, one per level and side —
    # not one aggregated order, and not the nearest one only.
    pendings = legs['acct_b'].pending_orders()
    assert len(pendings) == 4
    assert len({p['ticket'] for p in pendings}) == 4
    assert {p['side'] for p in pendings} == {'BUY', 'SELL'}

    # And they are individually cancellable.
    coordinator.cancel_order(
        next(o.order_id for o in working if o.level == sells[1]))
    coordinator.poll_once()

    left = coordinator.book.orders(pair.key)
    assert sorted(o.level for o in left) == sorted([sells[0]] + buys)
    assert len(legs['acct_b'].pending_orders()) == 3


# -- the BLUE side holds as many working orders as the red one ------------

def short_position(coordinator, pair, legs, spreads=2.0):
    """A short, on the board, made the way the ladder makes one."""
    level = level_of(coordinator, pair)
    coordinator.click(pair.key, SpreadSide.SELL, level, spreads)
    coordinator.poll_once()
    ticket = coordinator.quoter.snapshot(pair.key)[0]['ticket']
    legs['acct_b'].broker.fill_pending(ticket)
    coordinator.poll_once()
    positions = coordinator.book.positions(pair.key)
    assert len(positions) == 1
    return positions[0]


def closes(coordinator, pair):
    return [o for o in coordinator.book.orders(pair.key) if o.position_id]


def test_two_closing_clicks_rest_at_two_prices(engine, pair, legs):
    """The desk's report: the red side held every order clicked and the
    blue side held one.

    A buy while short is a CLOSE, and the close path pulled every
    existing closing order on the position before arming its own —
    including ones the trader had placed by hand. So the second buy
    cancelled the first, and scaling out at two prices, which is the
    ordinary way a ladder is used, was impossible.
    """
    coordinator = engine
    position = short_position(coordinator, pair, legs, 2.0)
    md = coordinator.market[pair.key]
    near = round(md['long_spread'] - 5 * pair.increment, 10)
    far = round(md['long_spread'] - 10 * pair.increment, 10)

    coordinator.click(pair.key, SpreadSide.BUY, near, 1.0)
    coordinator.click(pair.key, SpreadSide.BUY, far, 1.0)
    coordinator.poll_once()

    working = closes(coordinator, pair)
    assert sorted(o.level for o in working) == sorted([near, far])
    assert all(o.position_id == position.position_id for o in working)
    assert sum(o.quantity for o in working) == pytest.approx(2.0)


def test_a_click_only_covers_what_is_not_already_covered(engine, pair, legs):
    """Two closes may not add up to more than the position.

    Otherwise a 2-spread short carries 3 spreads of resting closes, and
    whichever fills last opens a long — a close that opens, at the far
    end of the same mistake reduce-before-open exists to prevent.
    """
    coordinator = engine
    short_position(coordinator, pair, legs, 2.0)
    md = coordinator.market[pair.key]
    levels = [round(md['long_spread'] - n * pair.increment, 10)
              for n in (5, 10, 15)]

    for level in levels:
        coordinator.click(pair.key, SpreadSide.BUY, level, 1.0)
    coordinator.poll_once()

    working = closes(coordinator, pair)
    assert sum(o.quantity for o in working) == pytest.approx(2.0)
    # ...and the third click, with nothing left to close, is an ORDINARY
    # opening order rather than a fourth close on a 2-spread position.
    opening = [o for o in coordinator.book.orders(pair.key)
               if not o.position_id]
    assert [o.level for o in opening] == [levels[2]]


def test_automations_target_still_stands_down_for_a_click(engine, pair, legs):
    """The rule that was there for a reason: a click that silently
    rested at AutoRouting's price is a click that did not do what it
    said. Automation's target goes; the trader's own stays."""
    coordinator = engine
    position = short_position(coordinator, pair, legs, 2.0)
    md = coordinator.market[pair.key]
    auto_level = round(md['long_spread'] - 3 * pair.increment, 10)
    mine = round(md['long_spread'] - 5 * pair.increment, 10)
    other = round(md['long_spread'] - 9 * pair.increment, 10)
    coordinator.quoter.arm(pair, position, auto_level, 1.0, auto=True)
    coordinator.click(pair.key, SpreadSide.BUY, mine, 1.0)
    coordinator.poll_once()

    # The automated target went the moment the trader named a price.
    assert auto_level not in [o.level for o in closes(coordinator, pair)]
    # The trader's own does NOT go when they name a second one.
    coordinator.click(pair.key, SpreadSide.BUY, other, 1.0)
    coordinator.poll_once()
    assert sorted(o.level for o in closes(coordinator, pair)) == \
        sorted([mine, other])


def test_OKX_a_sell_at_a_level_already_passed_rests_at_its_own_touch(
        engine, pair, legs):
    """OKX rests the quote POST-ONLY. A SELL at a level the market is
    already through would price leg B under its own bid - a post-only
    OKX cancels the instant it lands. It joins B's own ask instead:
    still maker, and a spread at least as good as the level."""
    coordinator = engine
    md = coordinator.market[pair.key]
    level = level_of(coordinator, pair)              # below: passed
    coordinator.click(pair.key, SpreadSide.SELL, level)
    coordinator.poll_once()
    resting = legs['acct_b'].pending_orders()[0]['price']
    assert resting == pytest.approx(md['leg_b_ask'])
    assert implied_spread(pair, md, SpreadSide.SELL, 'b', resting) >= level
