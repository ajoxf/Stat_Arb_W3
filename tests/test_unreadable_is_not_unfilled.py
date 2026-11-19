""""I could not ask" must never be spent as "it has not filled".

The MetaTrader5 API answers None when it could not ask and an EMPTY
TUPLE when it asked and there was nothing. `or ()` collapsed the two,
so silence came back as a confident zero — a filled volume of 0.0 built
out of no information at all. Downstream that reads as "carry on", and
carrying on is the one answer that cannot be right about an order which
may already be on.

That is CLAUDE.md's rule in its most expensive form: None means
UNKNOWN, not "flat"; unmeasured is not zero. Here it is pinned at all
three layers the answer passes through — the broker that reads the
terminal, the leg that carries the answer over IPC, and the quoter that
acts on it — each with a control that supplies a real answer and
asserts the opposite.
"""

import logging

import pytest

from okxtrader import broker as broker_module
from okxtrader.coordinator import Coordinator
from okxtrader.legs import RemoteLeg
from okxtrader.models import SpreadSide


#: The one-leg quote (WORKING_ORDERS = QUOTE); see conftest.
pytestmark = pytest.mark.quote


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def rest_one(coordinator, pair, legs, side=SpreadSide.BUY, offset=-5):
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] + offset * pair.increment, 10)
    coordinator.click(pair.key, side, level)
    coordinator.poll_once()
    pendings = legs['acct_b'].pending_orders()
    assert len(pendings) == 1, pendings
    return level, pendings[0]['ticket']


def group_of(coordinator, pair):
    return [g for g in coordinator.quoter.groups.values()
            if g.pair_key == pair.key][0]


# -- the terminal's own silence -------------------------------------------

class SilentMT5:
    """A terminal that answers NOTHING — the link is down, the symbol is
    gone, the IPC is upset. Every call returns None, which is what the
    real library does."""

    def history_deals_get(self, **kw):
        return None

    def orders_get(self, **kw):
        return None

    def positions_get(self, **kw):
        return None


class EmptyMT5(SilentMT5):
    """A terminal that ANSWERED and had nothing to report. The control:
    the difference between this and `SilentMT5` is the whole point."""

    def history_deals_get(self, **kw):
        return ()

    def orders_get(self, **kw):
        return ()

    def positions_get(self, **kw):
        return ()


def read_state(fake, ticket=5150):
    from types import SimpleNamespace
    original, broker_module.mt5 = broker_module.mt5, fake
    try:
        session = broker_module.BrokerSession(SimpleNamespace(name='a'))
        return session.order_fill_state(ticket)
    finally:
        broker_module.mt5 = original


def test_a_terminal_that_answers_nothing_is_reported_as_UNREADABLE():
    state = read_state(SilentMT5())

    assert state['readable'] is False
    assert state['error'], 'silence was reported without a word about it'
    # ...and the zero is still THERE, deliberately. `cancel_pending`
    # compares this against 0 to decide whether to re-read for a leaked
    # fill, and a None would skip that check — a fix for one blindness
    # opening another.
    assert state['filled_volume'] == 0.0


def test_CONTROL_a_terminal_that_answers_with_nothing_is_READABLE():
    state = read_state(EmptyMT5())

    assert state['readable'] is True
    assert state['error'] is None
    assert state['filled_volume'] == 0.0


def test_the_IPC_fallbacks_say_unreadable_rather_than_unfilled():
    """A leg over a socket has the same problem one layer up: no reply
    is not "no fill". Both answers a RemoteLeg manufactures when the
    runner cannot be reached have to carry it."""
    leg = RemoteLeg('acct_a', '127.0.0.1:1')
    leg._request = lambda payload: None

    state = leg.order_state(9001)
    cancelled = leg.cancel_order(9001)

    assert state['readable'] is False and state['filled_volume'] == 0.0
    assert cancelled['readable'] is False and cancelled['cancelled'] is False
    # `still_open` TRUE on both: the order we could not ask about is
    # assumed to be at the broker, because it probably is.
    assert state['still_open'] and cancelled['still_open']


def test_CONTROL_a_runner_that_answers_is_passed_through_untouched():
    leg = RemoteLeg('acct_a', '127.0.0.1:1')
    answer = {'ok': True, 'readable': True, 'filled_volume': 0.10,
              'price': 4351.0, 'position_tickets': [77], 'still_open': False,
              'error': None}
    leg._request = lambda payload: answer

    assert leg.order_state(9001) is answer


# -- what the quoter does with it -----------------------------------------

def blind(leg):
    """This leg can no longer be asked about its orders."""
    leg.order_state = lambda ticket: {
        'ok': False, 'readable': False, 'filled_volume': 0.0, 'price': None,
        'position_tickets': [], 'still_open': True, 'error': 'IPC failure'}


def test_an_order_that_cannot_be_read_is_NOT_treated_as_unfilled(
        engine, pair, legs, caplog):
    """It may already be on. Nothing is sent and nothing is closed —
    there is no answer to act on — but the ticket is KEPT, the level is
    left alone, and it is said in words on the ladder and at CRITICAL in
    the log, so the next pass reads it again."""
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)
    blind(legs['acct_b'])

    with caplog.at_level(logging.CRITICAL):
        coordinator.poll_once()

    group = group_of(coordinator, pair)
    assert group.ticket == ticket, 'a ticket nobody reads again'
    assert 'cannot tell' in (group.reason or '')
    assert any('CANNOT BE READ' in r.getMessage() for r in caplog.records
               if r.levelno >= logging.CRITICAL)
    # Nothing was invented: no hedge, no close, no position.
    assert legs['acct_a'].broker.open_positions() == []
    assert coordinator.book.positions(pair.key) == []


def test_CONTROL_an_order_that_reads_back_unfilled_is_simply_left_alone(
        engine, pair, legs, caplog):
    """The control. A readable, honestly unfilled order must NOT shout
    and must NOT carry that reason — otherwise the test above passes on
    every quiet pending in the system."""
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)

    with caplog.at_level(logging.CRITICAL):
        coordinator.poll_once()

    group = group_of(coordinator, pair)
    assert group.ticket == ticket
    assert 'cannot tell' not in (group.reason or '')
    assert [r for r in caplog.records
            if r.levelno >= logging.CRITICAL] == []


def test_CONTROL_an_order_that_reads_back_FILLED_is_still_hedged(
        engine, pair, legs):
    """The other control: the branch above withholds the hedge because
    the answer was missing, not because the hedge stopped working."""
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)

    legs['acct_b'].broker.fill_pending(ticket)
    coordinator.poll_once()

    assert len(legs['acct_a'].broker.open_positions()) == 1
    assert len(coordinator.book.positions(pair.key)) == 1


def test_it_shouts_about_one_unreadable_order_at_most_twice_a_minute(
        engine, pair, legs, caplog):
    """Three polls a second. A CRITICAL line at that rate buries the one
    line worth seeing, which is its own kind of blindness."""
    coordinator = engine
    rest_one(coordinator, pair, legs)
    blind(legs['acct_b'])

    with caplog.at_level(logging.CRITICAL):
        for _ in range(20):
            coordinator.poll_once()

    said = [r for r in caplog.records
            if 'CANNOT BE READ' in r.getMessage()]
    assert len(said) == 1


# -- a cancel nobody received -------------------------------------------—

def unreachable_cancel(leg):
    calls = []

    def cancel_order(ticket):
        calls.append(ticket)
        return {'ok': False, 'readable': False, 'cancelled': False,
                'filled_volume': 0.0, 'price': None, 'position_tickets': [],
                'still_open': True, 'error': 'IPC failure'}

    leg.cancel_order = cancel_order
    return calls


def test_a_cancel_that_never_reached_the_broker_KEEPS_the_ticket(
        engine, pair, legs, caplog):
    """The trap inside the fix.

    Pulling a pending because the pair went dark is right. Clearing the
    ticket when the pull was never DELIVERED is how the order is
    forgotten — still live, still fillable, and now with nobody looking
    at it. That is the original fault rebuilt by its own cure.
    """
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)
    tried = unreachable_cancel(legs['acct_b'])
    legs['acct_a'].tick = lambda symbol: None       # the pair goes dark

    with caplog.at_level(logging.CRITICAL):
        coordinator.poll_once()

    group = group_of(coordinator, pair)
    assert group.ticket == ticket, 'a live pending was forgotten'
    assert any('KEEPING the ticket' in r.getMessage()
               for r in caplog.records if r.levelno >= logging.CRITICAL)

    # ...and it is tried AGAIN next pass, which is the point of keeping it.
    coordinator.poll_once()
    assert tried == [ticket, ticket]


def test_CONTROL_a_cancel_the_broker_confirms_clears_the_ticket(
        engine, pair, legs):
    """The control: a ticket the broker says is gone must not be held
    for ever, or the group wedges and nothing rests there again."""
    coordinator = engine
    rest_one(coordinator, pair, legs)
    legs['acct_a'].tick = lambda symbol: None

    coordinator.poll_once()

    assert group_of(coordinator, pair).ticket is None
    assert legs['acct_b'].pending_orders() == []


def test_a_pending_that_filled_as_it_was_pulled_is_hedged_not_lost(
        engine, pair, legs):
    """The race the pull itself creates: the cancel and the fill cross.
    MT5 reports the fill in the cancel's own answer, and it is a FILL —
    hedged, booked, and never smoothed into a clean pull."""
    coordinator = engine
    _level, ticket = rest_one(coordinator, pair, legs)

    original = legs['acct_b'].cancel_order

    def fills_as_it_is_pulled(t):
        legs['acct_b'].broker.fill_pending(t)
        return original(t)

    legs['acct_b'].cancel_order = fills_as_it_is_pulled
    legs['acct_a'].tick = lambda symbol: None

    coordinator.poll_once()

    assert len(legs['acct_a'].broker.open_positions()) == 1, \
        'the fill that raced the cancel was never hedged'
    assert len(coordinator.book.positions(pair.key)) == 1
    assert ticket in [p['ticket'] for p in
                      legs['acct_b'].broker.open_positions()]


def test_an_order_that_comes_BACK_and_goes_dark_again_shouts_again(
        engine, pair, legs, caplog):
    """The rate limit must not become its own silence. An order that is
    readable again has had its clock forgotten, so the NEXT outage is
    reported when it happens rather than half an hour later."""
    coordinator = engine
    rest_one(coordinator, pair, legs)

    with caplog.at_level(logging.CRITICAL):
        blind(legs['acct_b'])
        coordinator.poll_once()
        del legs['acct_b'].order_state          # it answers again
        coordinator.poll_once()
        blind(legs['acct_b'])
        coordinator.poll_once()

    said = [r for r in caplog.records if 'CANNOT BE READ' in r.getMessage()]
    assert len(said) == 2
