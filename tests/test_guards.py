"""Two independent ways a level can be a lie, and the controls that
prove each guard is the thing doing the withholding.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.executor import PairExecutor
from okxtrader.models import SpreadSide
from okxtrader.spread import SpreadJumpTracker, compute_spread


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def engine(config, pair, legs):
    clock = FakeClock()
    coordinator = Coordinator(config, legs, monotonic=clock,
                              sleep=lambda s: None)
    # The self-healing re-subscribe is OFF here: these tests keep a leg
    # deliberately frozen, and an engine that fixes it mid-test would be
    # testing the nursing rather than the guard.
    config.settings['AUTO_REFRESH_STALE_SEC'] = 0
    coordinator.resolve_symbols()
    pair.clip_lots_a, pair.clip_lots_b = 0.1, 0.1
    return coordinator, clock


def test_a_frozen_leg_withholds_the_order_and_the_other_leg_keeps_ticking(
        engine, config, pair, legs):
    """A pair is only as good as its WORSE leg. A combined "108
    quotes/min" reads healthy while one leg is frozen."""
    coordinator, clock = engine
    executor = PairExecutor(config, legs, sleep=lambda s: None)

    coordinator.poll_once()
    for tick in range(10):
        clock.advance(2.0)
        # Leg B ticks the spread hard; leg A has stopped.
        legs['acct_b'].broker.quote('GC1226', 4351.0 + tick, 4351.4 + tick)
        coordinator.poll_once()

    md = coordinator.market[pair.key]
    assert md['stale_reason'] and 'Leg A' in md['stale_reason']
    # The badge carries the NUMBER: "stale" alone says nothing about
    # whether the feed died or the market is quiet, and those want
    # different answers.
    assert md['feed_badge'].startswith('stale ')
    assert 's' in md['feed_badge']

    refusal = executor.precheck(pair, SpreadSide.BUY, md, 1.0)
    assert refusal and 'stale' in refusal
    assert legs['acct_a'].broker.sent == []


def test_the_moment_the_frozen_leg_quotes_again_the_order_goes(
        engine, config, pair, legs):
    """The control. Without it the test above could pass for an
    unrelated reason — the level is still there when the quote
    refreshes, and if it is not then it was never offered."""
    coordinator, clock = engine
    executor = PairExecutor(config, legs, sleep=lambda s: None)

    coordinator.poll_once()
    clock.advance(30.0)
    legs['acct_b'].broker.quote('GC1226', 4355.0, 4355.4)
    coordinator.poll_once()
    assert coordinator.market[pair.key]['stale_reason']

    legs['acct_a'].broker.quote('XAUUSD_', 4293.0, 4293.2)
    coordinator.poll_once()
    md = coordinator.market[pair.key]

    assert md['stale_reason'] is None
    assert executor.precheck(pair, SpreadSide.BUY, md, 1.0) is None


def test_a_desync_between_two_ticking_legs_is_caught_by_the_jump_guard(pair):
    """The feed reads perfectly healthy — both legs are ticking, one is
    a moment behind. That printed a level 8 sigma away that was gone
    within seconds, and cost a trade $20.40."""
    clock = FakeClock()
    tracker = SpreadJumpTracker(clock)
    quiet = compute_spread(pair, {'bid': 100.0, 'ask': 100.2, 'time': 1},
                           {'bid': 110.0, 'ask': 110.4, 'time': 1})
    assert tracker.observe(pair.key, quiet, sigma=0.1, max_sigmas=5.0,
                           settle_sec=2.0) is None

    jumped = compute_spread(pair, {'bid': 100.0, 'ask': 100.2, 'time': 2},
                            {'bid': 112.3, 'ask': 112.7, 'time': 2})
    reason = tracker.observe(pair.key, jumped, sigma=0.1, max_sigmas=5.0,
                             settle_sec=2.0)
    assert reason and 'lagging' in reason

    # A disturbance jumps twice — out and back — so the level stays
    # unusable until the series has been QUIET for the settle period,
    # not for one quote.
    back = compute_spread(pair, {'bid': 100.0, 'ask': 100.2, 'time': 3},
                          {'bid': 110.0, 'ask': 110.4, 'time': 3})
    clock.advance(1.0)
    # The way back is itself a jump, and it re-arms the window: one
    # quote of quiet is not the end of a disturbance.
    assert tracker.observe(pair.key, back, 0.1, 5.0, 2.0) is not None
    clock.advance(1.5)
    assert tracker.observe(pair.key, back, 0.1, 5.0, 2.0) is not None

    # Quiet for the full settle period, and only then is the level
    # usable again.
    clock.advance(1.0)
    assert tracker.observe(pair.key, back, 0.1, 5.0, 2.0) is None


def test_the_jump_guard_lets_a_real_move_through(pair):
    """The control, and the direction the guard must err in: it can
    withhold an order, so it is deliberately wide."""
    tracker = SpreadJumpTracker(FakeClock())
    first = compute_spread(pair, {'bid': 100.0, 'ask': 100.2, 'time': 1},
                           {'bid': 110.0, 'ask': 110.4, 'time': 1})
    tracker.observe(pair.key, first, 0.1, 5.0, 2.0)
    moved = compute_spread(pair, {'bid': 100.0, 'ask': 100.2, 'time': 2},
                           {'bid': 110.3, 'ask': 110.7, 'time': 2})
    assert tracker.observe(pair.key, moved, 0.1, 5.0, 2.0) is None


def test_the_jump_guard_holds_no_opinion_before_it_has_a_sigma(pair):
    """Unknown is neither fresh nor stale. A cold start must not read as
    a jump, and must not read as safe either — it holds no opinion while
    still tracking the series."""
    tracker = SpreadJumpTracker(FakeClock())
    md = compute_spread(pair, {'bid': 100.0, 'ask': 100.2, 'time': 1},
                        {'bid': 110.0, 'ask': 110.4, 'time': 1})
    assert tracker.observe(pair.key, md, sigma=None, max_sigmas=5.0,
                           settle_sec=2.0) is None


def test_a_guard_never_prevents_a_close(engine, config, pair, legs):
    """A trade must always be closable. The close path consults no guard
    at all — this pins that it stays that way."""
    coordinator, clock = engine
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    coordinator.poll_once()
    result = executor.market_entry(pair, SpreadSide.SELL,
                                   coordinator.market[pair.key], 1.0)
    assert result.ok

    clock.advance(60.0)                       # both legs now stale
    legs['acct_b'].broker.quote('GC1226', 4351.0, 4351.4)
    coordinator.poll_once()
    md = coordinator.market[pair.key]
    assert md['guard_reason']

    closed = executor.close_position(pair, result.position, md)
    assert closed['ok']
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []




def test_the_feed_can_be_re_subscribed_and_the_age_starts_again(engine, legs,
                                                                 pair):
    """"The price is moving in MT5 and this says stale" needs an answer
    the trader can act on. Taking both symbols out of Market Watch and
    putting them back is what restarts a feed the terminal has gone
    quiet on — and the staleness clock goes with it, because the age it
    was carrying was measured against a subscription that no longer
    exists."""
    coordinator, clock = engine
    coordinator.poll_once()
    for _ in range(10):
        clock.advance(3.0)                      # both legs frozen
        coordinator.poll_once()
    assert coordinator.market[pair.key]['stale_reason']

    answer = coordinator.refresh_feed(pair.key)

    assert answer['ok'], answer
    assert pair.symbol_a in legs['acct_a'].broker.resubscribed
    assert pair.symbol_b in legs['acct_b'].broker.resubscribed
    assert '/' in answer['reason']              # the prices that came back

    coordinator.poll_once()
    md = coordinator.market[pair.key]
    # The clock starts again: the first pass after a re-subscribe is a
    # first sighting, not a twenty-minute-old quote.
    assert md['stale_reason'] is None
    assert md['leg_a_quote_age_sec'] in (None, 0.0)


def test_a_refresh_that_answers_with_nothing_says_so(engine, pair):
    """The control: it reports what came back, so the screen can say
    whether it worked rather than claim it did."""
    coordinator, _ = engine
    for leg in coordinator.legs.values():
        leg.resubscribe = lambda symbol: None

    answer = coordinator.refresh_feed(pair.key)

    assert answer['ok'] is False
    assert 'is OKX reachable' in answer['reason']






def test_a_stale_pair_re_subscribes_itself(engine, legs, pair):
    """Some terminals drop a subscription silently: the ladder ticks for
    a few seconds after a refresh and then goes quiet again, and
    pressing Feed brings it back every time. A machine that needs the
    same button pressed every twenty seconds should press it itself."""
    coordinator, clock = engine
    coordinator.config.settings['AUTO_REFRESH_STALE_SEC'] = 20.0
    coordinator.poll_once()

    for _ in range(12):                      # frozen, well past the limit
        clock.advance(3.0)
        coordinator.poll_once()

    assert legs['acct_a'].broker.resubscribed, 'nobody pressed Feed'
    assert legs['acct_b'].broker.resubscribed


def test_it_can_be_turned_off_and_then_nothing_touches_the_feed(engine, legs):
    """The control: a desk that wants the button pressed by a person
    sets the interval to zero, and the engine leaves the subscription
    exactly as it found it."""
    coordinator, clock = engine
    coordinator.config.settings['AUTO_REFRESH_STALE_SEC'] = 0
    coordinator.poll_once()

    for _ in range(12):
        clock.advance(3.0)
        coordinator.poll_once()

    assert not getattr(legs['acct_a'].broker, 'resubscribed', [])
