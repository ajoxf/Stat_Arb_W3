"""A resting close takes the PIECE the click reached, not the ticket.

THE INCIDENT. A trader held a 0.10 spread from the previous day and
clicked the ladder the opposite way for 0.01, in LIMIT, to take a
little off. `positions_to_reduce` picked the oldest ticket and sized
the piece at 0.01, `arm` recorded 0.01 on the synthetic — and when the
market reached the level, the quoter closed the position with no
quantity at all, which means ALL of it. The whole 0.10 went, at a
price chosen for a 0.01 trade, and the 0.01 they meant to close was
still open afterwards.

It reached both modes, because both rest through the quoter: LIMIT at
any level, and MARKET clicked away from the touch. A MARKET click AT
the touch was always right — that path goes through `reduce_first`,
which has passed its `take` since the day it was written — which is
why every existing test passed.

The controls matter as much as the tests here: a fix that closed only
ever a part would break CLOSE ALL, AutoRouting's target and a click
made deliberately at full size, and each of those is somebody's exit.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide, OrderType


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def open_short(coordinator, pair, spreads):
    """The older, bigger ticket — put on at market."""
    pair.order_type = OrderType.MARKET
    pair.default_quantity = spreads
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, SpreadSide.SELL, md['short_spread'])
    assert answer.get('ok'), answer
    return coordinator.book.positions(pair.key)[0]


def below_the_market(coordinator, pair, by=5.0):
    """A level a BUY cannot cross now, so the click rests."""
    return coordinator.market[pair.key]['long_spread'] - by


def reach(coordinator, pair, gold_symbols, level):
    """Walk leg B down until the spread can be BOUGHT at or under `level`."""
    _spot, future = gold_symbols
    move = (level - coordinator.market[pair.key]['long_spread']) - 0.01
    future.bid += move
    future.ask += move
    coordinator.poll_once()
    coordinator.poll_once()


def still_open(coordinator, pair, position):
    live = [p for p in coordinator.book.positions(pair.key)
            if p.position_id == position.position_id]
    return live[0].quantity if live else 0.0


# -- the bug, in both modes ------------------------------------------------

@pytest.mark.parametrize('mode', [OrderType.LIMIT, OrderType.MARKET])
def test_a_small_click_takes_a_small_piece_off_a_big_ticket(
        engine, pair, legs, gold_symbols, mode):
    """1 spread clicked over a 10-spread ticket leaves NINE.

    Run in both modes on purpose. In MARKET the level is away from the
    touch, which is the case that rests rather than crossing — and rests
    through exactly the same code as LIMIT.
    """
    coordinator = engine
    position = open_short(coordinator, pair, 10.0)

    pair.order_type = mode
    pair.default_quantity = 1.0
    coordinator.poll_once()
    level = below_the_market(coordinator, pair)
    answer = coordinator.click(pair.key, SpreadSide.BUY, level, quantity=1.0)
    assert answer.get('ok'), answer

    resting = coordinator.book.orders_for_position(position.position_id)
    assert [o.quantity for o in resting] == [1.0], \
        'the arm size was already right before this fix; it is the FIRING ' \
        'that threw it away'

    reach(coordinator, pair, gold_symbols, level)

    assert still_open(coordinator, pair, position) == 9.0, \
        'the whole ticket came off for a click that asked for a tenth of it'


def test_the_piece_comes_off_BOTH_legs_so_the_rest_stays_hedged(
        engine, pair, legs, gold_symbols):
    """A part-close that took one leg only would leave a naked leg —
    the worst outcome on the board, and worse than closing too much."""
    coordinator = engine
    position = open_short(coordinator, pair, 10.0)
    before_a = sum(p['volume'] for p in legs['acct_a'].broker.open_positions())
    before_b = sum(p['volume'] for p in legs['acct_b'].broker.open_positions())

    pair.order_type = OrderType.LIMIT
    coordinator.poll_once()
    level = below_the_market(coordinator, pair)
    assert coordinator.click(pair.key, SpreadSide.BUY, level,
                             quantity=1.0)['ok']
    reach(coordinator, pair, gold_symbols, level)

    after_a = sum(p['volume'] for p in legs['acct_a'].broker.open_positions())
    after_b = sum(p['volume'] for p in legs['acct_b'].broker.open_positions())
    off_a, off_b = before_a - after_a, before_b - after_b
    assert off_a > 0 and off_b > 0, 'a leg was left untouched'
    assert off_a == pytest.approx(off_b, rel=0.01), \
        f'leg A gave up {off_a} and leg B {off_b} — that is not a spread'


def test_scaling_out_at_two_levels_takes_two_separate_pieces(
        engine, pair, legs, gold_symbols):
    """Two clicks at two prices are two exits, each for its own size.
    The first to be reached must not take the other one's piece too."""
    coordinator = engine
    position = open_short(coordinator, pair, 10.0)

    pair.order_type = OrderType.LIMIT
    coordinator.poll_once()
    near = below_the_market(coordinator, pair, by=5.0)
    far = below_the_market(coordinator, pair, by=9.0)
    assert coordinator.click(pair.key, SpreadSide.BUY, near, quantity=1.0)['ok']
    assert coordinator.click(pair.key, SpreadSide.BUY, far, quantity=2.0)['ok']

    reach(coordinator, pair, gold_symbols, near)
    assert still_open(coordinator, pair, position) == 9.0, \
        'the nearer level took more than the 1 it was armed for'

    reach(coordinator, pair, gold_symbols, far)
    assert still_open(coordinator, pair, position) == 7.0, \
        'the further level did not take its own 2'


# -- the controls: what must still close in FULL ---------------------------

def test_CONTROL_a_full_size_click_still_closes_the_whole_ticket(
        engine, pair, legs, gold_symbols):
    """The control. A click made deliberately at the ticket's own size
    is a flatten, and must stay one."""
    coordinator = engine
    position = open_short(coordinator, pair, 10.0)

    pair.order_type = OrderType.LIMIT
    coordinator.poll_once()
    level = below_the_market(coordinator, pair)
    assert coordinator.click(pair.key, SpreadSide.BUY, level,
                             quantity=10.0)['ok']
    reach(coordinator, pair, gold_symbols, level)

    assert still_open(coordinator, pair, position) == 0.0
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []


def test_CONTROL_autoroutings_own_target_still_closes_it_all(
        engine, pair, legs, gold_symbols):
    """The control for the other caller. AutoRouting arms with no
    quantity, meaning the whole position, and that is its contract."""
    coordinator = engine
    position = open_short(coordinator, pair, 10.0)
    level = below_the_market(coordinator, pair)
    order = coordinator.quoter.arm(pair, position, level, auto=True)
    assert order is not None and order.quantity == 10.0

    reach(coordinator, pair, gold_symbols, level)
    assert still_open(coordinator, pair, position) == 0.0


def test_CONTROL_a_market_click_at_the_touch_is_unchanged(engine, pair,
                                                          legs):
    """The control for the path that was never broken: 10 short, buy 2
    at the touch, 8 left."""
    coordinator = engine
    open_short(coordinator, pair, 10.0)

    pair.default_quantity = 2.0
    coordinator.poll_once()
    md = coordinator.market[pair.key]
    assert coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread'])['ok']
    net, _avg = coordinator.book.net_position(pair.key)
    assert net == -8.0


# -- a piece the broker cannot trade ---------------------------------------

def test_a_piece_under_the_brokers_step_is_refused_ON_THE_CLICK(
        engine, pair, legs):
    """The trap a careless fix would set.

    A share of a ticket is not automatically a tradable volume. Armed
    anyway, the exit can never fire and the trader watches a price that
    will never get them out — quieter than closing too much, and just
    as bad. So it is refused at the click, in the broker's own words.
    """
    coordinator = engine
    open_short(coordinator, pair, 10.0)

    pair.order_type = OrderType.LIMIT
    coordinator.poll_once()
    level = below_the_market(coordinator, pair)
    answer = coordinator.click(pair.key, SpreadSide.BUY, level, quantity=0.5)

    assert answer.get('ok') is False and answer.get('refused')
    reason = answer.get('reason') or ''
    assert 'lots' in reason, reason
    assert 'Close more of it, or all of it' in reason, reason
    # ...and NOTHING was armed against a level that could never fire.
    assert not coordinator.book.orders(pair.key)


def test_CONTROL_a_piece_the_broker_CAN_trade_is_armed_normally(
        engine, pair, legs):
    """The control. The refusal above must be about the broker's volume
    step and nothing else — a piece that clears it still rests."""
    coordinator = engine
    open_short(coordinator, pair, 10.0)

    pair.order_type = OrderType.LIMIT
    coordinator.poll_once()
    level = below_the_market(coordinator, pair)
    answer = coordinator.click(pair.key, SpreadSide.BUY, level, quantity=1.0)

    assert answer.get('ok') and answer.get('reducing')
    assert [o.quantity for o in coordinator.book.orders(pair.key)] == [1.0]


def test_CONTROL_a_click_BIGGER_than_the_ticket_closes_it_all_then_opens(
        engine, pair, legs, gold_symbols):
    """The control at the far end. A BUY 12 over a SELL 10 must close
    the whole 10 — `part` has to fall back to None when the group
    covers the ticket — and then open the 2 that is left over, once
    the close has actually gone through."""
    coordinator = engine
    position = open_short(coordinator, pair, 10.0)

    pair.order_type = OrderType.LIMIT
    coordinator.poll_once()
    level = below_the_market(coordinator, pair)
    answer = coordinator.click(pair.key, SpreadSide.BUY, level, quantity=12.0)
    assert answer.get('ok') and answer.get('reducing')
    assert answer.get('remainder') == 2.0

    reach(coordinator, pair, gold_symbols, level)

    assert still_open(coordinator, pair, position) == 0.0, \
        'the whole ticket must go, not a share of it'


def test_a_ticket_that_SHRANK_since_the_level_was_armed_closes_what_is_left(
        engine, pair, legs, gold_symbols):
    """Armed for 5, but something else took the ticket down to 3 in the
    meantime. The level must close the 3 that is there — never ask the
    broker for 5 of a 3, and never leave 3 on because the arithmetic
    expected more."""
    coordinator = engine
    position = open_short(coordinator, pair, 10.0)

    pair.order_type = OrderType.LIMIT
    coordinator.poll_once()
    level = below_the_market(coordinator, pair)
    assert coordinator.click(pair.key, SpreadSide.BUY, level,
                             quantity=5.0)['ok']

    # Something else takes it down to 3 before the level is reached.
    coordinator.executor.close_position(
        pair, position, coordinator.market.get(pair.key),
        reason='taken down elsewhere', quantity=7.0, disarm=False)
    assert still_open(coordinator, pair, position) == 3.0

    reach(coordinator, pair, gold_symbols, level)
    assert still_open(coordinator, pair, position) == 0.0, \
        'the level left the remaining 3 on'


def test_ten_slices_off_one_ticket_end_genuinely_flat(engine, pair, legs):
    """Dust, at the tenth close rather than the second.

    `reduce_by` multiplies the quantity down by what came off, and that
    multiplication runs again on its own result every time another
    piece goes. Untidied it left 6.999999999999999 after two slices —
    on the panel, in the size of the next closing order, and in the
    `share` the one after that is computed from. The number to worry
    about is not the ugly one on the screen, it is the residue at the
    end that no click can reach.

    So: ten slices off a ten, every intermediate size exact, and flat
    at the end by every reading that matters — our book, the open-book
    list, the net, and both brokers.
    """
    coordinator = engine
    position = open_short(coordinator, pair, 10.0)

    seen = []
    for _ in range(10):
        coordinator.executor.close_position(
            pair, position, coordinator.market.get(pair.key),
            reason='slice', quantity=1.0, disarm=False)
        seen.append(position.quantity)

    assert seen[:9] == [9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0], seen
    assert position.is_open is False
    assert coordinator.book.positions(pair.key) == []
    assert coordinator.book.net_position(pair.key)[0] in (None, 0.0)
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []
