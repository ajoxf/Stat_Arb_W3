"""A click the opposite way REDUCES before it opens.

Reported from the desk: click the red column to sell, then the blue
column to buy back, and the second click opened a SECOND position with
a new ticket instead of closing the first. Both stayed live, both paid
carry, and the ticket count was not what anyone expected.

That is MT5 hedging mode doing exactly what it does: an opposite order
always opens a second position, and the broker will never net for us.
So the netting is done here, by closing tickets OLDEST FIRST.

Two places can create a position, and both go through the same
`book.reduce_first`: a MARKET click closes before it opens, and a
resting LIMIT order closes when it FILLS — because until the price the
trader named actually trades, there is nothing to close against.
"""

import pytest

from okxtrader.book import Book, reduce_first
from okxtrader.models import SpreadSide


class Position:
    """Only what the reduce path reads."""

    def __init__(self, pid, side, quantity, opened_at):
        self.position_id = pid
        self.pair_key = 'P'
        self.side = side
        self.quantity = quantity
        self.opened_at = opened_at
        self.is_open = True


class Pair:
    key = 'P'


class Executor:
    """Records what it was asked to close, and can refuse."""

    def __init__(self, refuse=()):
        self.closed = []
        self.asked = []
        self.refuse = set(refuse)

    def close_position(self, pair, position, md=None, reason=None,
                       quantity=None):
        #: (position_id, spreads asked for) — a part-close is a
        #: quantity, not a whole ticket, and a test that could not see
        #: the quantity could not tell the two apart.
        if position.position_id in self.refuse:
            # Shaped like the real `_settle_close`: an 'ok' and the
            # per-leg answers, which is where the broker's words are.
            return {'ok': False, 'legs': {
                'a': {'ok': False, 'error': '10027 AutoTrading disabled'},
                'b': {'ok': False, 'error': '10027 AutoTrading disabled'}}}
        self.asked.append((position.position_id, quantity))
        self.closed.append(position.position_id)
        take = position.quantity if quantity is None else float(quantity)
        if take >= float(position.quantity or 0.0) - 1e-9:
            position.is_open = False
        else:
            position.quantity = float(position.quantity) - take
        return {'ok': True}


def book_with(*positions):
    book = Book()
    for position in positions:
        book._positions[position.position_id] = position
    return book


BUY, SELL = SpreadSide.BUY, SpreadSide.SELL


# -- which tickets does a click cover? --------------------------------

def test_it_picks_the_OLDEST_opposite_position_first():
    book = book_with(Position('new', SELL, 1.0, 300),
                     Position('old', SELL, 1.0, 100),
                     Position('mid', SELL, 1.0, 200))
    picked = book.positions_to_reduce('P', BUY, 2.0)
    assert [p.position_id for p, _take in picked] == ['old', 'mid']


def test_it_never_touches_a_position_on_the_SAME_side():
    """The control that matters: a second buy while long must stack,
    not eat the position the trader is building."""
    book = book_with(Position('long', BUY, 1.0, 100))
    assert book.positions_to_reduce('P', BUY, 5.0) == []


def test_it_never_over_closes():
    """The click reaches 1; the ticket is 5. It closes ONE of the five
    and leaves four. Closing the whole ticket would flip the net the
    other way, which is the opposite mistake and just as expensive."""
    book = book_with(Position('big', SELL, 5.0, 100))
    assert book.positions_to_reduce('P', BUY, 1.0) == \
        [(book.position('big'), 1.0)]


def test_a_ticket_bigger_than_the_click_is_taken_IN_PART():
    """Live 2026-09-03: short 1207 spreads, BUY 100 clicked, and the
    answer was "CLOSE 2 position(s), then open 93" — two old tickets of
    5 and 2 fitted, the 1,200 behind them did not, and 93 of the
    trader's 100 went the OTHER WAY. A reduce that opens is the exact
    failure "reduce before you open" exists to prevent; it had only
    moved from the side to the size."""
    book = book_with(Position('a', SELL, 5.0, 100),
                     Position('b', SELL, 2.0, 200),
                     Position('big', SELL, 1200.0, 300))
    picked = book.positions_to_reduce('P', BUY, 100.0)

    assert [(p.position_id, take) for p, take in picked] == \
        [('a', 5.0), ('b', 2.0), ('big', 93.0)]
    # ...and nothing is left over to open the other way.
    assert sum(take for _p, take in picked) == 100.0


def test_the_queue_is_still_FIFO_and_still_stops_at_the_click(): 
    """The control: taking part of a ticket must not become taking
    part of every ticket. It stops the moment the click is covered, and
    it never jumps the queue."""
    book = book_with(Position('a', SELL, 1.0, 100),
                     Position('big', SELL, 9.0, 200),
                     Position('c', SELL, 1.0, 300))
    picked = book.positions_to_reduce('P', BUY, 3.0)
    assert [(p.position_id, take) for p, take in picked] == \
        [('a', 1.0), ('big', 2.0)]


def test_a_closed_position_is_not_reduced_again():
    shut = Position('done', SELL, 1.0, 100)
    shut.is_open = False
    book = book_with(shut, Position('live', SELL, 1.0, 200))
    picked = book.positions_to_reduce('P', BUY, 5.0)
    assert [p.position_id for p, _take in picked] == ['live']


def test_a_position_can_be_excluded_from_its_own_reduction():
    """The resting-fill path: the position just opened must not close
    itself."""
    book = book_with(Position('mine', BUY, 1.0, 300),
                     Position('old', SELL, 1.0, 100))
    picked = book.positions_to_reduce('P', SELL, 5.0, exclude='mine')
    assert picked == []


# -- what actually gets closed ----------------------------------------

def test_a_click_that_exactly_covers_leaves_nothing_to_open():
    book = book_with(Position('old', SELL, 1.0, 100))
    ex = Executor()
    closed, left, failure = reduce_first(book, ex, Pair(), BUY, 1.0, None)
    assert closed == ['old']
    assert left == pytest.approx(0.0)
    assert ex.closed == ['old']


def test_a_bigger_click_closes_what_it_covers_and_opens_the_rest():
    book = book_with(Position('old', SELL, 1.0, 100))
    ex = Executor()
    closed, left, failure = reduce_first(book, ex, Pair(), BUY, 3.0, None)
    assert closed == ['old']
    assert left == pytest.approx(2.0), 'the remainder must still open'


def test_a_refused_close_STOPS_the_queue():
    """Stepping over a refusal and opening the remainder on top of a
    position that would not close is how a reduce becomes a BIGGER
    position."""
    book = book_with(Position('a', SELL, 1.0, 100),
                     Position('b', SELL, 1.0, 200))
    ex = Executor(refuse=['a'])
    closed, left, failure = reduce_first(book, ex, Pair(), BUY, 2.0, None)
    assert closed == []
    assert ex.closed == []
    assert left == pytest.approx(2.0)
    # And the caller can TELL a refusal from "nothing to close".
    assert failure is not None
    assert '10027' in failure, failure


def test_nothing_open_means_nothing_closed_and_the_click_opens_whole():
    """The CONTROL. Without it every test above passes on a reduce that
    fires unconditionally."""
    ex = Executor()
    closed, left, failure = reduce_first(book_with(), ex, Pair(), BUY, 2.0, None)
    assert closed == []
    assert left == pytest.approx(2.0)
    assert ex.closed == []
def test_the_market_click_reduces_before_it_opens():
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._click)
    assert '_reduce_first' in source
    # Before market_entry, not after: closing first never holds both
    # sides at once, so a disconnect cannot catch it with double on.
    assert source.index('_reduce_first') < source.index('market_entry')


def test_the_coordinator_and_the_quoter_share_ONE_implementation():
    """Two answers to "which tickets does this click cover" is two
    answers to reconcile the day they disagree, on a live book."""
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._reduce_first)
    assert 'book_module.reduce_first' in source, (
        'the coordinator has its own copy of the reduce loop')


# -- the side arrives as a STRING too ---------------------------------
#
# The bug this section exists for: `side` comes from the command bridge
# as the plain string 'BUY' and from the quoter as a SpreadSide enum.
# The first draft compared with `is not`, which against a string is
# ALWAYS true — so every open position looked opposite and a second buy
# while long CLOSED the trader's own position instead of adding to it.
#
# Every test above passed, because they all passed enums. These do not.

@pytest.mark.parametrize('side', ['BUY', 'buy', ' Buy '])
def test_a_STRING_side_never_reduces_a_position_on_the_same_side(side):
    book = book_with(Position('long', BUY, 1.0, 100))
    assert book.positions_to_reduce('P', side, 5.0) == [], (
        'a same-side click would have closed the trader\'s own position')


@pytest.mark.parametrize('side', ['SELL', 'sell'])
def test_a_STRING_side_still_reduces_the_real_opposite(side):
    """The control. Without it the fix above could just be "never
    reduce anything", which passes the test above and breaks the
    feature."""
    book = book_with(Position('long', BUY, 1.0, 100))
    picked = [p for p, _take in book.positions_to_reduce('P', side, 5.0)]
    assert [p.position_id for p in picked] == ['long']


def test_a_side_nobody_can_read_reduces_NOTHING():
    """Unmeasured is not zero. An unreadable side must not be treated
    as "opposite to everything"."""
    book = book_with(Position('long', BUY, 1.0, 100))
    assert book.positions_to_reduce('P', None, 5.0) == []
    assert book.positions_to_reduce('P', '', 5.0) == []


def test_a_position_whose_side_is_unreadable_is_left_alone():
    odd = Position('odd', BUY, 1.0, 100)
    odd.side = None
    book = book_with(odd)
    assert book.positions_to_reduce('P', 'SELL', 5.0) == []


# -- a failed close must not become a new position --------------------
#
# `reduce_first` used to return ([], quantity) both when there was
# nothing to close AND when the broker refused the close. A caller
# could not tell them apart, so the MARKET path opened a new position
# on top of one that had just failed to close — the trader keeps the
# position they wanted gone AND gets another, and if the close
# half-executed there is a NAKED LEG under both.

def test_nothing_to_close_is_not_reported_as_a_failure():
    """The control. If every empty result were a failure, the MARKET
    path would refuse every ordinary opening click."""
    ex = Executor()
    closed, left, failure = reduce_first(book_with(), ex, Pair(), BUY, 1.0,
                                         None)
    assert closed == [] and failure is None
    assert left == pytest.approx(1.0)


def test_a_refused_close_carries_the_BROKERS_OWN_WORDS():
    book = book_with(Position('a', SELL, 1.0, 100))
    ex = Executor(refuse=['a'])
    _closed, _left, failure = reduce_first(book, ex, Pair(), BUY, 1.0, None)
    assert failure and '10027' in failure
    assert 'a' in failure, 'the failure does not name the position'


def test_a_HALF_closed_position_says_NAKED_LEG_first():
    """One leg closed and the other not is the worst outcome of a
    close, and the sentence the trader has to see first."""
    class HalfClosed:
        closed = []

        def close_position(self, pair, position, md=None, reason=None,
                           quantity=None):
            return {'ok': False, 'legs': {
                'a': {'ok': True},
                'b': {'ok': False, 'error': '10018 market closed'}}}

    book = book_with(Position('a', SELL, 1.0, 100))
    _c, _l, failure = reduce_first(book, HalfClosed(), Pair(), BUY, 1.0, None)
    assert failure.startswith('NAKED LEG'), failure
    assert '10018' in failure


def test_the_market_click_REFUSES_to_open_after_a_failed_close():
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._click)
    assert 'failure' in source
    # The refusal must come BEFORE market_entry, or it is not a refusal.
    assert source.index('if failure is not None') < source.index('market_entry')
    assert "'refused': True" in source


# -- A REDUCING CLICK MUST LEAVE YOU FLAT -----------------------------
#
# The bug the desk found, from the fills themselves:
#
#   12:38:38  B sell open  2006 | A buy  open  2007   first click
#   12:39:24  B buy  close 2006 | A sell close 2007   the reduce fired
#   12:39:24  B buy  open  2008 | A sell open  2009   ...and a NEW one
#
# The old position WAS closed. But the fill had already opened a fresh
# one, so SELL 1 became BUY 1 with two new tickets instead of flat.
#
# Closing after the fact cannot fix that, because by then the broker
# has already opened the position. A resting order that reduces has to
# be a CLOSING order from the moment it is placed: `quoter.arm` builds
# one carrying position=<ticket>, so executing it CLOSES that ticket
# rather than opening a second, and the fill lands in
# `_on_closing_fill`, which closes the other leg by ticket too.

def test_the_fill_path_no_longer_opens_and_then_closes():
    """The mechanism that produced the reported fills is GONE. Two ways
    to reduce one position is a double close."""
    import inspect
    from okxtrader.quoter import Quoter

    assert not hasattr(Quoter, '_reduce_after_fill'), (
        'the open-then-close mechanism is still there')
    # Code only. The comment in `_on_fill` explains at length why it
    # does NOT reduce, and matching prose would fail on the very
    # comment that documents the fix.
    code = '\n'.join(line.split('#')[0]
                     for line in inspect.getsource(Quoter._on_fill).splitlines())
    assert '_reduce_after_fill' not in code
    assert 'reduce_first' not in code
    assert 'close_position' not in code, (
        '_on_fill still reaches the broker to close after a fill')


def test_a_reducing_click_rests_a_CLOSING_order_not_an_opening_one():
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._rest_reducing_orders)
    # arm() is the only thing that sets position_id, which is what
    # makes the group `closing` and sends position=<ticket>.
    assert 'self.quoter.arm(' in source
    assert 'add_order' not in source, (
        'it builds an OPENING order, which is the bug')


def test_both_resting_paths_reduce_before_they_open():
    """A LIMIT click and a MARKET click away from the touch both rest.
    Both must reduce; only one used to."""
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._click)
    assert source.count('_rest_reducing_orders') == 2, (
        'one of the two resting paths still opens unconditionally')
    for chunk in source.split('_rest_reducing_orders')[1:]:
        opening = chunk.find('self.book.add_order')
        assert opening > 0, 'the remainder is never opened'


def test_a_click_that_fully_covers_opens_NOTHING():
    """SELL 1 then BUY 1 must leave the book with no new opening order
    at all — that is the difference between flat and flipped."""
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._click)
    # The early return on a fully-covered click, on both paths.
    assert source.count('quantity <= 1e-9') >= 2
    assert source.count("'reducing': True") == 2


def test_the_trader_s_level_beats_an_armed_target():
    """AutoRouting may already hold a closing order for this position
    at ITS level, and `arm` returns the EXISTING order rather than
    moving it — so a click would silently rest at somebody else's
    price. AUTOMATION'S is pulled; the trader's own closes are not.

    Asserted against a running coordinator in
    tests/test_limit_path.py::test_two_closing_clicks_rest_at_two_prices
    and the two tests beside it. This one only pins that the code
    reaches for the automation-scoped disarm, because the difference
    between that and the one that pulls EVERYTHING is the difference
    between a ladder that can be scaled out of and one that cannot.
    """
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._rest_reducing_orders)
    assert 'auto_armed_for' in source and 'disarm_auto' in source
    assert 'self.book.orders_for_position' not in source
    assert source.index('disarm_auto') < source.index('self.quoter.arm(')


def test_arm_really_marks_the_order_as_CLOSING():
    """The whole fix rests on this: an order carrying a position_id is
    a closing order, and a closing group rests NOTHING at the broker —
    it is a level this system watches and closes by ticket at."""
    import inspect
    from okxtrader.quoter import Quoter, QuoteGroup

    assert 'position_id=position.position_id' in inspect.getsource(Quoter.arm)
    assert 'self.position_id is not None' in inspect.getsource(
        QuoteGroup.closing.fget)
    work = inspect.getsource(Quoter.work)
    assert work.index('group.closing') < work.index('_check_fill')
    # A closing group must never reach the pending machinery.
    assert 'closing' not in inspect.getsource(Quoter._rest_or_repeg)


# -- a trader's close is NOT automation -------------------------------
#
# Seen live: click blue to cover a short, watch the order rest, and
# 300ms later —
#
#   XAUUSD | GCZ6: BUY 1 at 46.8500 — cancelled. AutoRouting was
#   turned off
#
# `work_auto_route` runs every poll, and with AutoRouting OFF it swept
# every order carrying a position_id. A reducing click rests exactly
# such an order, so with AutoRouting off — the desk's own setting — a
# closing click could never survive one poll. The trader believes they
# have an order working to get out, and there is nothing at the broker.

def test_a_closing_order_records_WHO_armed_it():
    from okxtrader.models import SyntheticOrder, OrderType, TimeInForce

    auto = SyntheticOrder('P', 'BUY', 1.0, 1.0, OrderType.LIMIT,
                          TimeInForce.DAY, position_id='POS1',
                          auto_armed=True)
    hand = SyntheticOrder('P', 'BUY', 1.0, 1.0, OrderType.LIMIT,
                          TimeInForce.DAY, position_id='POS1')
    assert auto.auto_armed is True
    assert hand.auto_armed is False, (
        'an unmarked closing order must not read as automation')
    assert hand.to_dict()['auto_armed'] is False


def test_the_switch_pulls_ONLY_what_automation_armed():
    from okxtrader.book import Book
    from okxtrader.models import OrderType, TimeInForce

    class P:
        key = 'P'
        order_type = OrderType.LIMIT
        time_in_force = TimeInForce.DAY

    book = Book()
    auto = book.add_order(P(), 'BUY', 46.85, 1.0, position_id='POS1',
                          auto_armed=True)
    hand = book.add_order(P(), 'BUY', 46.85, 1.0, position_id='POS1')

    swept = [o.order_id for o in book.auto_armed_for('POS1')]
    assert swept == [auto.order_id], (
        "the trader's own close is in the sweep")
    # The CONTROL: both are still closing orders for the position, so
    # the orphan sweep — which must take everything — still sees both.
    both = [o.order_id for o in book.orders_for_position('POS1')]
    assert sorted(both) == sorted([auto.order_id, hand.order_id])


def test_the_reducing_click_arms_as_the_TRADER_not_automation():
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._rest_reducing_orders)
    assert 'auto=False' in source, (
        'the click arms as automation, so the switch will sweep it')


def test_nothing_arms_as_automation_any_more():
    """AutoRouting has been removed: `arm` defaults to the trader, and
    no caller in the engine asks for automation."""
    import inspect
    from okxtrader.quoter import Quoter
    from okxtrader import coordinator as coordinator_module

    assert 'auto=False' in str(inspect.signature(Quoter.arm))
    assert 'auto=True' not in inspect.getsource(coordinator_module)


def test_the_tidy_up_pulls_everything_for_a_gone_position_and_only_leftovers_otherwise():
    """A position that is GONE must have EVERY closing order pulled —
    trader's included — because one left behind fills and opens a naked
    position. For a live position only AutoRouting's leftovers go."""
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator.tidy_closing_orders)
    orphan, leftovers = source.split('AutoRouting has been removed')[0], source
    assert "disarm(order.position_id" in orphan, (
        'the orphan sweep no longer pulls everything')
    assert 'disarm_auto(' in leftovers


# -- what the click actually COVERED, not what it asked for ---------------

class PartialExecutor:
    """A broker that closes less than it was asked for.

    Real: a part-close is rounded to what each leg's volume step can
    trade, and the quoting leg can come down on a piece the other leg
    cannot follow — in which case NOTHING comes off and the answer is
    still `ok`, flagged `imbalanced`.
    """

    def __init__(self, fraction):
        self.fraction = fraction
        self.asked = []

    def close_position(self, pair, position, md=None, reason=None,
                       quantity=None):
        self.asked.append((position.position_id, quantity))
        came_off = float(position.quantity) * self.fraction
        if self.fraction <= 0:
            return {'ok': True, 'partial': True, 'fraction': 0.0,
                    'imbalanced': True, 'legs': {}}
        position.quantity = float(position.quantity) - came_off
        if position.quantity <= 1e-9:
            position.is_open = False
        return {'ok': True, 'partial': True, 'fraction': self.fraction,
                'legs': {}}


def test_a_close_that_took_nothing_does_not_count_as_cover():
    """The dangerous one. `imbalanced` means the quoting leg came down
    and the other could not follow, so NOTHING came off — and it comes
    back `ok`. Counting it as cover let the click open the remainder the
    other way on top of a position that is still on: a reduce that
    opens, which is the failure this whole path exists to prevent."""
    book = book_with(Position('old', SELL, 10.0, 100))
    executor = PartialExecutor(0.0)

    closed, left, failure = reduce_first(book, executor, Pair(), BUY, 4.0,
                                         None)

    assert closed == []
    assert failure is not None                # refused, in its own words
    assert left == pytest.approx(4.0)         # nothing was covered
    assert book.positions('P')[0].quantity == pytest.approx(10.0)


def test_a_close_that_took_half_counts_half():
    """The click covered what came off. Booking the whole `take` would
    leave the difference open and believed closed."""
    book = book_with(Position('old', SELL, 10.0, 100))
    executor = PartialExecutor(0.25)          # 2.5 of the 10 came off

    closed, left, failure = reduce_first(book, executor, Pair(), BUY, 4.0,
                                         None)

    assert failure is None
    assert executor.asked == [('old', 4.0)]   # it did ask for 4
    assert left == pytest.approx(1.5)         # ...and 2.5 of it landed
    assert book.positions('P')[0].quantity == pytest.approx(7.5)


def test_the_control_a_whole_close_still_counts_whole():
    """Without this the two above would pass on a build that had simply
    stopped crediting closes at all."""
    book = book_with(Position('old', SELL, 4.0, 100))
    executor = Executor()

    closed, left, failure = reduce_first(book, executor, Pair(), BUY, 10.0,
                                         None)

    assert failure is None and closed == ['old']
    assert left == pytest.approx(6.0)
