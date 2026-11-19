"""The session cutoff — on the BROKER's clock.

A box in one time zone and a broker in another is the normal case, and
a cutoff read off the local clock fires hours early or late. On
EXIT_ALWAYS that means flattening in the middle of the session, or not
at all.
"""

from datetime import datetime

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import OvernightMode, SpreadSide, TimeInForce
from okxtrader.session import gtc_caveat, overnight_action

BEFORE = datetime(2026, 8, 27, 16, 54)
AFTER = datetime(2026, 8, 27, 16, 55)


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    # These tests drive the clock directly, so the two clocks are the
    # same one here; the broker-clock behaviour has its own tests below.
    coordinator.session_clock.offset = lambda: 0
    return coordinator


def test_allow_keeps_the_position_and_pays_the_swap():
    """This is a CARRY decision, not a risk rule. Holding a rich basis
    over the swap is often the whole trade."""
    assert overnight_action(OvernightMode.ALLOW, 100.0, AFTER, 16, 55) is None


def test_exit_always_flattens_whatever_the_pnl_is():
    assert overnight_action(OvernightMode.EXIT_ALWAYS, -500.0, AFTER, 16,
                            55) == 'OVERNIGHT_CLOSE'
    # ...but not before the cutoff. The control.
    assert overnight_action(OvernightMode.EXIT_ALWAYS, -500.0, BEFORE, 16,
                            55) is None


def test_exit_if_profit_reads_net_pnl_and_treats_unmeasured_as_not_profit():
    """Marked at the mid it would flatten trades that are not actually in
    profit; marked at nothing it must not flatten at all."""
    assert overnight_action(OvernightMode.EXIT_IF_PROFIT, 12.0, AFTER, 16,
                            55) == 'OVERNIGHT_CLOSE'
    assert overnight_action(OvernightMode.EXIT_IF_PROFIT, -1.0, AFTER, 16,
                            55) is None
    assert overnight_action(OvernightMode.EXIT_IF_PROFIT, None, AFTER, 16,
                            55) is None


def test_the_cutoff_cancels_day_orders_and_leaves_gtc_alone(engine, pair,
                                                            legs):
    coordinator = engine
    pair.time_in_force = TimeInForce.DAY
    day = coordinator.click(pair.key, SpreadSide.BUY, 58.40)['order']
    pair.time_in_force = TimeInForce.GTC
    gtc = coordinator.click(pair.key, SpreadSide.BUY, 58.30)['order']
    coordinator.session_clock.now = lambda: AFTER

    events = coordinator.run_session_cutoff()

    assert events[0]['count'] == 1
    assert coordinator.book.order(day['order_id']).is_working is False
    assert coordinator.book.order(gtc['order_id']).is_working is True


def test_the_cutoff_fires_once_a_day_not_on_every_poll(engine, pair):
    """A rule that re-fires every poll after 16:55 would cancel an order
    the trader deliberately placed at 16:56."""
    coordinator = engine
    coordinator.session_clock.now = lambda: AFTER
    coordinator.click(pair.key, SpreadSide.BUY, 58.40)

    assert coordinator.run_session_cutoff()
    coordinator.click(pair.key, SpreadSide.BUY, 58.40)   # placed at 16:56
    assert coordinator.run_session_cutoff() == []
    assert len(coordinator.book.orders(pair.key)) == 1


def test_an_overnight_close_flattens_by_ticket(engine, pair, legs):
    coordinator = engine
    pair.overnight = OvernightMode.EXIT_ALWAYS
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    coordinator.book.add_position(result.position)
    coordinator.session_clock.now = lambda: AFTER

    events = coordinator.run_session_cutoff()

    assert events[0]['action'] == 'overnight_close'
    assert events[0]['ok']
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []
    # Closed by ticket, never by an offsetting order.
    assert [e['action'] for e in legs['acct_b'].broker.sent] == \
        ['market', 'close']


def test_no_guard_withholds_an_overnight_close(engine, pair, legs):
    """It reads no price level, so the staleness and jump guards have no
    say — a trade must always be closable."""
    coordinator = engine
    pair.overnight = OvernightMode.EXIT_ALWAYS
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    coordinator.book.add_position(result.position)
    coordinator.market[pair.key]['guard_reason'] = 'Leg A stale'
    coordinator.session_clock.now = lambda: AFTER

    events = coordinator.run_session_cutoff()

    assert events[0]['ok']
    assert legs['acct_a'].broker.open_positions() == []


def test_the_cutoff_is_the_brokers_1655_not_this_machines(config, pair,
                                                          legs):
    """The broker is three hours ahead: its 16:55 is 13:55 here, and
    that is when the rule belongs."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    assert coordinator.broker_offset() == 3 * 3600     # measured, not typed

    coordinator.session_clock.now = lambda: datetime(2026, 8, 27, 13, 54)
    assert coordinator.session_clock.due(pair.key) is False

    coordinator.session_clock.now = lambda: datetime(2026, 8, 27, 13, 55)
    assert coordinator.session_clock.due(pair.key) is True
    # ...and the local 16:55, which is 19:55 at the broker, is well past
    # it rather than at it — the same day, already fired.
    coordinator.session_clock.mark(pair.key)
    coordinator.session_clock.now = lambda: datetime(2026, 8, 27, 16, 55)
    assert coordinator.session_clock.due(pair.key) is False


def test_the_cutoff_does_not_fire_at_all_on_an_unmeasured_clock(config, pair,
                                                                legs):
    """Unmeasured is not zero. A session rule on the wrong clock is
    worse than one that waits for the right one — and it says so."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.session_clock.offset = lambda: None
    coordinator.session_clock.now = lambda: AFTER

    assert coordinator.session_clock.due(pair.key) is False
    assert coordinator.session_clock.broker_now() is None
    described = coordinator.session_clock.describe()
    assert described['broker_time'] is None
    assert 'will not fire until it is' in described['note']


def test_the_screen_says_which_clock_the_cutoff_is_on(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()

    block = coordinator.snapshot()['broker_clock']

    assert block['offset_sec'] == 3 * 3600
    assert block['cutoff'] == '16:55'
    assert '+3.0h from this machine' in block['note']
    assert block['per_account'] == {'acct_a': 10800, 'acct_b': 10800}
    assert block['accounts_disagree'] is False


def test_two_brokers_on_different_clocks_is_said_out_loud(config, pair, legs):
    """Two accounts whose clocks differ usually means two brokers — and
    the cutoff can only be on one of them."""
    legs['acct_b'].broker.server_offset_sec = 0
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()

    block = coordinator.snapshot()['broker_clock']
    assert block['accounts_disagree'] is True
    assert set(block['per_account'].values()) == {0, 10800}


def test_the_brokers_clock_is_not_re_measured_on_every_poll(config, pair,
                                                             legs):
    """It does not drift on the scale of a poll, and it is a round trip
    per account."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    calls = []
    real = legs['acct_a'].server_offset
    legs['acct_a'].server_offset = lambda: (calls.append(1), real())[1]

    for _ in range(5):
        coordinator.broker_offset()
    assert len(calls) == 1

    coordinator._offsets['acct_a'] = (coordinator.clock() - 301.0, 10800)
    coordinator.broker_offset()
    assert len(calls) == 2


def test_gtc_says_what_it_can_and_cannot_promise():
    """It is never to be misread as exchange-resident."""
    caveat = gtc_caveat()
    assert 'until this system stops' in caveat
