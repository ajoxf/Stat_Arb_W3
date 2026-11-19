"""An unexplained position that explains itself must stop warning.

The reconciler is right to refuse to close a position carrying our own
comment that the book does not hold: the book is wrong, and closing a
real trade to tidy up a bookkeeping error is the worst outcome there
is. So it lists it for a person.

What it never did was TAKE THE NOTICE BACK DOWN.

And the gap it fires in is not an edge case — it is the ordinary life
of a resting order. The broker fills a pending on ITS clock. We see it
on the next poll and book the pair a moment after that. A reconcile
pass landing in between sees a leg it cannot explain yet. One entirely
normal second therefore left a PERMANENT false alarm against a healthy,
hedged position — with "Close it" as the only button offered, which
would have left the other leg naked.

A warning nobody can clear is a warning everybody learns to ignore,
and the day it means something nobody looks.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide


@pytest.fixture
def engine(config, pair, legs):
    config.settings['RECONCILE_INTERVAL_SEC'] = 0.0001
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def reconcile(coordinator):
    coordinator._last_reconcile = None
    return coordinator.reconcile_if_due()


def rest_and_fill_behind_our_back(coordinator, pair, legs):
    """A pending fills at the broker before we have polled.

    Exactly what happens to every resting order: the broker does not
    wait for us to be looking.
    """
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] - 0.05, 10)
    coordinator.click(pair.key, SpreadSide.BUY, level)
    coordinator.poll_once()
    ticket = legs['acct_b'].pending_orders()[0]['ticket']
    legs['acct_b'].broker.fill_pending(ticket)
    return ticket


@pytest.mark.quote
def test_the_notice_clears_the_moment_the_position_is_in_the_book(
        engine, pair, legs):
    """THE BUG. The warning outlived the thing it was warning about."""
    coordinator = engine
    coordinator.reconciler.book_complete = True   # a desk that is running
    ticket = rest_and_fill_behind_our_back(coordinator, pair, legs)

    reconcile(coordinator)          # lands in the gap: leg unexplained
    assert ('acct_b', str(ticket)) in coordinator.reconciler.unclaimed

    coordinator.poll_once()         # the fill is seen, the pair booked
    report = reconcile(coordinator)

    assert report['unclaimed'] == [], 'a false alarm nobody could clear'
    assert coordinator.reconciler.unclaimed == {}
    # ...and the position is untouched. Nothing was closed to tidy up.
    assert len(coordinator.book.positions(pair.key)) == 1
    assert legs['acct_a'].broker.open_positions()
    assert legs['acct_b'].broker.open_positions()


def test_CONTROL_a_position_nothing_can_explain_stays_listed(engine, pair,
                                                              legs):
    """The control, and the one that matters: this must only ever
    REMOVE a warning for a ticket the book can now account for. A leg
    the book genuinely lost still has to be on the screen, and still
    must never be closed automatically."""
    coordinator = engine
    coordinator.reconciler.book_complete = True
    legs['acct_b'].broker.send_market_order(
        'GC1226', SpreadSide.BUY.leg_sides()[1], 0.10, comment='LADDER9999')

    first = reconcile(coordinator)
    second = reconcile(coordinator)

    assert len(first['unclaimed']) == 1
    assert len(second['unclaimed']) == 1, 'a real orphan was forgotten'
    assert len(legs['acct_b'].broker.open_positions()) == 1, 'auto-closed'


def test_the_report_says_what_THIS_pass_found_not_the_last_one(engine, pair,
                                                               legs):
    """It was built before the scan that fills it, so a new unexplained
    position was reported a whole pass late and a cleared one lingered
    a pass after it was resolved. The screen always described the
    previous twenty seconds."""
    coordinator = engine
    coordinator.reconciler.book_complete = True
    legs['acct_b'].broker.send_market_order(
        'GC1226', SpreadSide.BUY.leg_sides()[1], 0.10, comment='LADDER9999')

    report = reconcile(coordinator)

    assert len(report['unclaimed']) == 1, 'found this pass, reported next'


def test_nothing_is_listed_before_recovery_has_read_the_book(engine, pair,
                                                             legs):
    """An empty book at startup makes every live position look like an
    orphan. Accusing the book of losing something before the book has
    been read is how that warning gets ignored."""
    coordinator = engine
    coordinator.reconciler.book_complete = False
    legs['acct_b'].broker.send_market_order(
        'GC1226', SpreadSide.BUY.leg_sides()[1], 0.10, comment='LADDER9999')

    report = reconcile(coordinator)

    assert report['unclaimed'] == []
    assert len(legs['acct_b'].broker.open_positions()) == 1


@pytest.mark.quote
def test_CLOSE_IT_is_refused_for_a_leg_the_book_holds(engine, pair, legs):
    """The dangerous half. The screen offered exactly one button for
    this position, and pressing it would have closed a live hedged leg
    and left the other one naked."""
    coordinator = engine
    coordinator.reconciler.book_complete = True
    ticket = rest_and_fill_behind_our_back(coordinator, pair, legs)
    reconcile(coordinator)
    coordinator.poll_once()
    # Force the stale notice back, as a screen open since before the
    # fix would still be showing it.
    coordinator.reconciler.unclaimed[('acct_b', str(ticket))] = {
        'account': 'acct_b', 'ticket': ticket, 'symbol': 'GC1226',
        'side': 'sell', 'volume': 0.10, 'price_open': 4351.0}

    answer = coordinator.close_unclaimed('acct_b', ticket)

    assert answer['ok'] is False and answer['refused']
    assert 'IS in the book' in answer['reason']
    assert legs['acct_b'].broker.open_positions(), 'a live leg was closed'
    assert legs['acct_a'].broker.open_positions(), 'the other leg is naked'
    # The stale notice is gone too, so the button stops being offered.
    assert coordinator.reconciler.unclaimed == {}


def test_CONTROL_CLOSE_IT_still_closes_a_genuinely_unexplained_position(
        engine, pair, legs):
    """The control. A trader must still be able to get rid of a
    position nothing accounts for — refusing everything would be a
    worse bug than the one being fixed."""
    coordinator = engine
    coordinator.reconciler.book_complete = True
    result = legs['acct_b'].broker.send_market_order(
        'GC1226', SpreadSide.BUY.leg_sides()[1], 0.10, comment='LADDER9999')
    reconcile(coordinator)

    answer = coordinator.close_unclaimed('acct_b', result.ticket)

    assert answer['ok'], answer
    assert legs['acct_b'].broker.open_positions() == []
    assert coordinator.reconciler.unclaimed == {}
