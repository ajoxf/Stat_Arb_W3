"""How long was one leg alone? On the BROKER's clock, not on ours.

After the 2026-09-16 incident the report said the hedge took one
second. It had: one second from the moment this process NOTICED the
fill. The fill itself was twelve minutes and fifty-one seconds earlier,
and the number that would have shown that did not exist.

A report timed from when we looked cannot see a window created by
looking late. So the fill time now comes from the broker's own stamp on
the deal — and because MT5 stamps deals with the SERVER's wall clock,
the measured offset has to come off it first, or the subtraction is
between two time zones and reads in hours.

Unmeasured stays None and renders as a dash. A naked window shown as
0ms is the report telling the desk that nothing happened.
"""

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


def rest_one(coordinator, pair, legs, side=SpreadSide.BUY, offset=-5):
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] + offset * pair.increment, 10)
    coordinator.click(pair.key, side, level)
    coordinator.poll_once()
    return legs['acct_b'].pending_orders()[0]['ticket']


def test_a_fill_nobody_noticed_for_thirteen_minutes_reports_thirteen_minutes(
        engine, pair, legs):
    """THE NUMBER THAT WAS MISSING ON 2026-09-16.

    The pending filled and was not looked at. `click_to_on_ms` cannot
    see that - its clock starts at the noticing - so it still reads
    about a second, and on its own it made a catastrophe look clean.
    """
    coordinator = engine
    ticket = rest_one(coordinator, pair, legs)
    legs['acct_b'].broker.fill_pending(ticket)
    legs['acct_b'].broker.back_date(ticket, 771)        # 12m51s ago

    coordinator.poll_once()

    position = coordinator.book.positions(pair.key)[0]
    assert position.naked_ms == pytest.approx(771_000, rel=0.01)
    # ...and the old number is still the old number, deliberately: it
    # answers a different question (how fast we crossed once we knew).
    assert position.click_to_on_ms < 1000


def test_CONTROL_a_fill_hedged_at_once_reports_a_small_window(engine, pair,
                                                              legs):
    """The control. Without it, a measurement that is simply always
    large would pass the test above."""
    coordinator = engine
    ticket = rest_one(coordinator, pair, legs)
    legs['acct_b'].broker.fill_pending(ticket)

    coordinator.poll_once()

    position = coordinator.book.positions(pair.key)[0]
    assert position.naked_ms is not None
    assert position.naked_ms < 5_000


@pytest.mark.parametrize('offset_hours', [5, -5])
def test_the_brokers_time_zone_is_taken_off_before_subtracting(
        engine, pair, legs, offset_hours):
    """The trap the whole measurement dies on.

    MT5 stamps a deal with the server's WALL CLOCK as an epoch, so
    subtracting it from our clock raw is a subtraction between two time
    zones: hours, not milliseconds.

    BOTH DIRECTIONS, and that is the point. A broker AHEAD of us gives a
    negative answer that the clamp quietly turns into a comfortable
    zero - so the ahead case is pinned by `> 0`, which no clamp can
    fake, and the behind case by the size, which nothing can hide.
    """
    coordinator = engine
    legs['acct_b'].broker.server_offset_sec = offset_hours * 3600
    ticket = rest_one(coordinator, pair, legs)
    legs['acct_b'].broker.fill_pending(ticket)

    coordinator.poll_once()

    naked = coordinator.book.positions(pair.key)[0].naked_ms
    assert naked is not None
    assert 0 < naked < 5_000, \
        f"the broker's time zone leaked into the naked window: {naked}ms"


def test_a_window_that_cannot_be_measured_is_a_dash_not_a_zero(engine, pair,
                                                               legs):
    """CLAUDE.md, in the place it matters most: unmeasured is not zero.

    A broker that will not say when it filled must leave the column
    blank. Zero there reads as 'hedged instantly', which is the one
    conclusion nobody may draw from missing information.
    """
    coordinator = engine
    ticket = rest_one(coordinator, pair, legs)
    legs['acct_b'].broker.fill_pending(ticket)
    original = legs['acct_b'].order_state
    legs['acct_b'].order_state = lambda t: dict(original(t), filled_at=None,
                                                server_offset_sec=None)

    coordinator.poll_once()

    assert coordinator.book.positions(pair.key)[0].naked_ms is None


def test_it_survives_a_restart_and_is_still_there(engine, pair, legs,
                                                  tmp_path):
    """A number that vanishes at the next restart cannot be audited the
    next morning, which is when anyone asks."""
    from okxtrader.database import Store
    from okxtrader.models import SpreadPosition

    store = Store(str(tmp_path / 'okxtrader.db'))
    coordinator = engine
    ticket = rest_one(coordinator, pair, legs)
    legs['acct_b'].broker.fill_pending(ticket)
    legs['acct_b'].broker.back_date(ticket, 771)
    coordinator.poll_once()
    position = coordinator.book.positions(pair.key)[0]
    store.save_position(position)

    back = [SpreadPosition.from_dict(row) for row in store.open_positions()]

    assert len(back) == 1
    assert back[0].naked_ms == pytest.approx(position.naked_ms)


def test_a_database_written_before_this_column_existed_still_opens(tmp_path):
    """The migration, which is the part that breaks a live desk.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
    exists, so an older database would have taken 'no such column:
    naked_ms' on the first write after an update - with a position on.
    """
    import sqlite3
    from okxtrader.database import Store

    path = str(tmp_path / 'old.db')
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE positions (
               position_id TEXT PRIMARY KEY, pair_key TEXT NOT NULL,
               side TEXT NOT NULL, quantity REAL NOT NULL,
               entry_spread REAL, exit_spread REAL, spread_units REAL,
               order_type TEXT, opened_at REAL, closed_at REAL,
               close_reason TEXT, realized_pnl REAL, entry_slippage REAL,
               exit_slippage REAL, click_to_on_ms REAL,
               leg_a TEXT, leg_b TEXT);""")
    connection.commit()
    connection.close()

    store = Store(path)          # the upgrade happens here

    with sqlite3.connect(path) as check:
        columns = {row[1] for row in
                   check.execute('PRAGMA table_info(positions)')}
    assert 'naked_ms' in columns
    assert store.open_positions() == []


def test_CONTROL_the_migration_does_not_run_twice_or_lose_a_row(tmp_path):
    """The control: opening the same database again must be a no-op,
    not a second ALTER and not a rebuilt table."""
    from okxtrader.database import Store

    path = str(tmp_path / 'again.db')
    Store(path)
    store = Store(path)
    store.event('opened', 'XAUUSD_|GC1226')
    Store(path)

    assert len(Store(path).events()) == 1
