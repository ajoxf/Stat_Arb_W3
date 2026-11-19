"""A reduce must cover the WHOLE click, or it is not a reduce.

Live 2026-09-03, short 1207 spreads on the oil differential, BUY 100
clicked on the ladder:

    resting at 7.69 to CLOSE 2 position(s), then open 93

Two old tickets of 5 and 2 fitted inside the click; the 1,200 behind
them did not. `positions_to_reduce` took whole tickets only and STOPPED
at the first one too big, so 93 of the trader's 100 went the other way
— opening longs against their own short instead of covering it.

That is the failure "reduce before you open" exists to prevent. It had
not come back; it had moved from the SIDE to the SIZE, where the same
rule was not being applied.

The fix is to take the oldest ticket in PART. Everything a part-close
needs was already there — `close_position` sends pro-rata volumes,
`_closed_fraction` measures what actually came off, `reduce_by` leaves
the position open for the rest — and nothing was using it.
"""

import pytest

from okxtrader.book import Book, reduce_first
from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide

BUY, SELL = SpreadSide.BUY, SpreadSide.SELL


class Position:
    def __init__(self, position_id, side, quantity, opened_at):
        self.position_id = position_id
        self.pair_key = 'P'
        self.side = side
        self.quantity = quantity
        self.opened_at = opened_at
        self.is_open = True


class Pair:
    key = 'P'


class Executor:
    def __init__(self):
        self.asked = []

    def close_position(self, pair, position, md=None, reason=None,
                       quantity=None):
        self.asked.append((position.position_id, quantity))
        take = position.quantity if quantity is None else float(quantity)
        if take >= float(position.quantity) - 1e-9:
            position.is_open = False
        else:
            position.quantity -= take
        return {'ok': True}


def book_with(*positions):
    book = Book()
    for position in positions:
        book._positions[position.position_id] = position
    return book


# -- the shape of the live failure ----------------------------------------

def test_the_live_case_covers_the_whole_click_and_opens_NOTHING():
    book = book_with(Position('a', SELL, 5.0, 100),
                     Position('b', SELL, 2.0, 200),
                     Position('big', SELL, 1200.0, 300))
    executor = Executor()

    closed, left, failure = reduce_first(book, executor, Pair(), BUY,
                                         100.0, None)

    assert failure is None
    assert left == 0.0, 'a reduce that leaves a remainder OPENS it'
    assert executor.asked == [('a', 5.0), ('b', 2.0), ('big', 93.0)]
    assert closed == ['a', 'b', 'big']
    # The big ticket is still on, 93 smaller. Closing it whole would
    # have flipped the trader long by 1,100.
    assert book.position('big').is_open is True
    assert book.position('big').quantity == pytest.approx(1107.0)


def test_a_click_bigger_than_EVERYTHING_still_opens_the_rest():
    """The control: a part-close must not swallow a genuine remainder.
    Short 7 and a BUY 100 covers the 7 and opens 93 — and THAT is a
    real opening, not a reduce that gave up."""
    book = book_with(Position('a', SELL, 5.0, 100),
                     Position('b', SELL, 2.0, 200))
    closed, left, failure = reduce_first(book, Executor(), Pair(), BUY,
                                         100.0, None)
    assert failure is None
    assert closed == ['a', 'b'] and left == pytest.approx(93.0)


def test_nothing_opposite_leaves_the_click_untouched():
    """The other control: a click with nothing to cover opens in full,
    and the part-close path must not invent something to close."""
    book = book_with(Position('long', BUY, 5.0, 100))
    closed, left, failure = reduce_first(book, Executor(), Pair(), BUY,
                                         100.0, None)
    assert (closed, left, failure) == ([], 100.0, None)


def test_the_part_is_never_MORE_than_the_click():
    """Over-closing is the mistake at the opposite end and just as
    expensive: it flips the net the other way."""
    book = book_with(Position('big', SELL, 500.0, 100))
    executor = Executor()
    reduce_first(book, executor, Pair(), BUY, 1.0, None)
    assert executor.asked == [('big', 1.0)]
    assert book.position('big').quantity == pytest.approx(499.0)


# -- and the RESTING path, which is what the ladder click took ------------

@pytest.fixture
def engine(config, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def a_short_of(coordinator, pair, spreads):
    """Get short by `spreads` the fast way, so the test is about the
    reduce."""
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, SpreadSide.SELL, md['short_spread'],
                               spreads)
    assert answer.get('ok'), answer
    pair.order_type = pair.order_type.__class__('LIMIT')
    return coordinator.book.positions(pair.key)[-1]


def test_a_resting_reduce_rests_for_the_WHOLE_click(engine, pair):
    """The path the ladder click actually took. One closing order, for
    the size clicked, against the ticket it covers — and no remainder,
    so nothing opens the other way when it fills."""
    coordinator = engine
    position = a_short_of(coordinator, pair, 20.0)

    answer = coordinator.click(pair.key, SpreadSide.BUY, 40.0, 5.0)

    assert answer['ok'] and answer['reducing'] is True
    assert answer['remainder'] is None, answer['reason']
    assert 'then open' not in answer['reason']
    resting = coordinator.book.orders_for_position(position.position_id)
    assert len(resting) == 1
    assert resting[0].quantity == pytest.approx(5.0)
    assert resting[0].side is SpreadSide.BUY


def test_a_resting_reduce_bigger_than_the_book_still_says_what_it_opens(
        engine, pair):
    """The control: a genuine remainder is still reported, and still
    rides on the close rather than resting beside it."""
    coordinator = engine
    a_short_of(coordinator, pair, 2.0)

    answer = coordinator.click(pair.key, SpreadSide.BUY, 40.0, 5.0)

    assert answer['remainder'] == pytest.approx(3.0)
    assert 'then open 3' in answer['reason']


# -- the desk's screen, 2026-09-03, after the fix --------------------------

def test_a_click_that_ADDS_to_the_net_still_clears_the_opposite_ticket(
        engine, pair):
    """Short 1707 across one BUY of 93 and the sells behind it; SELL 100
    clicked. The answer was "CLOSE 1 position(s), then open 7", and it
    is right — though it does not look it at first.

    The 93 long and 93 of the click cancel: closing the ticket and
    opening 93 more shorts reach the same net, and closing takes the
    margin and the financing off both sides instead of stacking a third
    position on a hedging account. The net moves by exactly the 100 the
    trader asked for.

    What is NOT obvious, and is now on the screen: the other 7 are held
    back until that close goes through. One click is one instruction,
    in order.
    """
    coordinator = engine
    a_short_of(coordinator, pair, 200.0)
    # The 93-lot LONG the desk was carrying, made last so the short
    # above cannot reduce it while the book is being set up.
    long_93 = a_short_of(coordinator, pair, 93.0)
    long_93.side = SpreadSide.BUY

    net, _avg = coordinator.book.net_position(pair.key)
    assert net == pytest.approx(-107.0)

    answer = coordinator.click(pair.key, SpreadSide.SELL, 8.11, 100.0)

    assert answer['ok'] and answer['reducing'] is True
    # ONE closing order, for the whole 93 — not "as much as fits".
    resting = coordinator.book.orders_for_position(long_93.position_id)
    assert len(resting) == 1 and resting[0].quantity == pytest.approx(93.0)
    # ...and the remainder is held on it, not rested beside it.
    assert answer['remainder'] == pytest.approx(7.0)
    assert len(coordinator.book.orders(pair.key)) == 1, (
        'the remainder was rested as its own order — that gives it a '
        'different engine from the close and opens before it')


def test_the_remainder_is_PUBLISHED_so_the_screen_can_show_it(engine, pair):
    """It was intent the engine held and nothing reported. A trader who
    clicked 100 saw 93 and had no way to learn the other 7 existed."""
    coordinator = engine
    position = a_short_of(coordinator, pair, 2.0)
    position.side = SpreadSide.BUY

    coordinator.click(pair.key, SpreadSide.SELL, 8.11, 5.0)

    quote = [q for q in coordinator.quoter.snapshot(pair.key)
             if q['intent'] == 'CLOSE'][0]
    assert quote['open_after'] == pytest.approx(3.0)


def test_turning_CLOSE_FIRST_off_makes_a_click_purely_an_OPEN(engine, pair):
    """The control, and the way out for a desk that wants a click to
    mean only what it says. Off, nothing is closed and the whole click
    opens — on a hedging account that stacks an opposite ticket, which
    is the trade-off and why it is on by default."""
    coordinator = engine
    position = a_short_of(coordinator, pair, 93.0)
    position.side = SpreadSide.BUY
    coordinator.config.settings['CLOSE_FIRST'] = False

    answer = coordinator.click(pair.key, SpreadSide.SELL, 8.11, 100.0)

    assert answer['ok'] and not answer.get('reducing')
    assert coordinator.book.orders_for_position(position.position_id) == []
    assert coordinator.book.orders(pair.key)[0].quantity == \
        pytest.approx(100.0)
