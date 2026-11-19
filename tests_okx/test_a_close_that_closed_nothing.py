"""'Nothing of ours is there' and 'we closed it' are both zero.

`close_tickets` reported `left: 0` for both, and `left` was the only
thing the callers could see. Two of them believe it about real money:

  - `_closed_fraction` books the position as FULLY CLOSED on it, and
  - `_unwind_what_went_on` reports "nothing naked" on it.

Both readings are right when the tickets really are gone — something
closed them first, and that is ordinary. Both are catastrophic when the
answer came from ONE bad read: a live leg struck off our own books
while the money sits at the broker, with nothing watching it.

So the absence is read TWICE before it is believed, `found` comes back
so a caller can tell the two apart at all, and an absence that survives
both reads is said out loud in the log.
"""

import logging

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import OrderSide, SpreadSide


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def a_position(coordinator, pair):
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    assert coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread'])['ok']
    return coordinator.book.positions(pair.key)[0]


def blink_once(leg):
    """The broker answers an EMPTY book once, then tells the truth.

    One bad read - a terminal mid-refresh, an account momentarily
    between snapshots - and nothing else wrong.
    """
    real = leg.positions
    state = {'blinked': False}

    def positions(symbol=None):
        if not state['blinked']:
            state['blinked'] = True
            return []
        return real(symbol)

    leg.positions = positions
    return state


def always_empty(leg):
    """A leg that genuinely holds none of our tickets, every time."""
    leg.positions = lambda symbol=None: []


# -- the distinction itself ------------------------------------------------

def test_a_single_empty_read_does_not_get_a_live_leg_written_off(engine, pair,
                                                                  legs):
    """THE HOLE. One blink used to be enough to conclude 'already
    closed', and the caller booked the whole position on it."""
    coordinator = engine
    position = a_position(coordinator, pair)
    tickets = position.leg_b.position_tickets
    blink_once(legs['acct_b'])

    answer = coordinator.executor.close_tickets(
        legs['acct_b'], pair.symbol_b, tickets, OrderSide.SELL)

    # The second read found them, so they were CLOSED rather than
    # written off.
    assert answer['ok']
    assert answer['found'] > 0, 'a live leg was declared already gone'
    assert answer['closed'], 'nothing was actually sent to the broker'
    assert legs['acct_b'].broker.open_positions() == []


def test_CONTROL_tickets_that_really_are_gone_are_still_treated_as_gone(
        engine, pair, legs, caplog):
    """The control, and it matters more than the test above.

    A position the trader closed by hand in MT5 must still settle, or a
    fix for a bad read becomes a position nobody can ever take off our
    books. It settles - and it SAYS it settled on an absence rather
    than on a close it performed.
    """
    coordinator = engine
    position = a_position(coordinator, pair)
    tickets = position.leg_b.position_tickets
    always_empty(legs['acct_b'])

    with caplog.at_level(logging.WARNING):
        answer = coordinator.executor.close_tickets(
            legs['acct_b'], pair.symbol_b, tickets, OrderSide.SELL)

    assert answer['ok'] and answer['found'] == 0
    assert answer['closed'] == []
    assert any('two consecutive reads' in r.getMessage()
               for r in caplog.records), \
        'a close that closed nothing passed without a word'


def test_a_leg_that_cannot_be_read_TWICE_is_not_a_close_at_all(engine, pair,
                                                               legs):
    """Unknown is not flat, on the second read as much as the first.
    An unreadable answer must never come back `ok`."""
    coordinator = engine
    position = a_position(coordinator, pair)
    tickets = position.leg_b.position_tickets
    calls = {'n': 0}

    def positions(symbol=None):
        calls['n'] += 1
        return [] if calls['n'] == 1 else None

    legs['acct_b'].positions = positions

    answer = coordinator.executor.close_tickets(
        legs['acct_b'], pair.symbol_b, tickets, OrderSide.SELL)

    assert answer['ok'] is False
    assert answer['found'] is None
    assert 'could not be read again' in answer['error']
    assert legs['acct_b'].broker.open_positions(), 'it was closed anyway'


def test_CONTROL_an_ordinary_close_reads_the_book_once(engine, pair, legs):
    """The control on cost. The second read is for the rare path only —
    charging every close an extra round trip would be a real price to
    pay for a rare case."""
    coordinator = engine
    position = a_position(coordinator, pair)
    reads = {'n': 0}
    real = legs['acct_b'].positions

    def positions(symbol=None):
        reads['n'] += 1
        return real(symbol)

    legs['acct_b'].positions = positions

    coordinator.executor.close_tickets(
        legs['acct_b'], pair.symbol_b, position.leg_b.position_tickets,
        OrderSide.SELL)

    assert reads['n'] == 1


def test_found_tells_a_close_apart_from_an_absence(engine, pair, legs):
    """`left` is zero for both. Without `found` the caller has no way
    to know which of the two it is looking at."""
    coordinator = engine
    position = a_position(coordinator, pair)
    tickets = position.leg_b.position_tickets

    closed = coordinator.executor.close_tickets(
        legs['acct_b'], pair.symbol_b, tickets, OrderSide.SELL)
    # ...and again, now that they really are gone.
    absent = coordinator.executor.close_tickets(
        legs['acct_b'], pair.symbol_b, tickets, OrderSide.SELL)

    assert closed['left'] == absent['left'] == 0.0
    assert closed['found'] > 0 and absent['found'] == 0


# -- what it means for the money ------------------------------------------

@pytest.mark.quote
def test_an_unwind_does_not_report_FLAT_off_one_blink(engine, pair, legs):
    """The worst consumer of the old answer.

    A hedge is rejected, the quoting leg is on, the unwind goes in — and
    a single empty read would have had it report 'nothing naked' while
    the leg stayed at the broker with nobody looking at it. That is the
    naked leg again, arrived at from the other direction.
    """
    coordinator = engine
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] - 0.05, 10)
    pair.order_type = pair.order_type.__class__('LIMIT')
    coordinator.click(pair.key, SpreadSide.BUY, level)
    coordinator.poll_once()
    ticket = legs['acct_b'].pending_orders()[0]['ticket']
    legs['acct_a'].broker.reject_orders['XAUUSD_'] = '10027 AutoTrading off'
    legs['acct_b'].broker.fill_pending(ticket)
    blink_once(legs['acct_b'])

    coordinator.poll_once()

    assert legs['acct_b'].broker.open_positions() == [], \
        'the filled leg was left ON while the unwind reported success'
    assert legs['acct_a'].broker.open_positions() == []
