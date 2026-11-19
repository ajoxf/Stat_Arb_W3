"""What survives a restart.

The book lives in memory. Without a database it comes back EMPTY, and
an empty book makes every real position look like an orphan — which the
reconciler then closes, sixty seconds after start, entirely correctly,
on a book that lied to it. These are the tests that stop that.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import SpreadSide


def engine(config, legs, path, **kwargs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(path)), **kwargs)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def test_an_open_position_comes_back_after_a_restart(config, pair, legs,
                                                     tmp_path):
    db = tmp_path / 'trader.db'
    first = engine(config, legs, db)
    first.config.pairs[pair.key].order_type = \
        type(pair.order_type)('MARKET')
    result = first.click(pair.key, SpreadSide.SELL, None)
    assert result['ok'], result.get('reason')
    was = first.book.positions(pair.key)[0]

    # ... the process stops and starts again.
    second = engine(config, legs, db)

    recovered = second.book.positions(pair.key)
    assert len(recovered) == 1
    position = recovered[0]
    # The SAME position: same id, same fills, same tickets. One that came
    # back under a new id would be an orphan to the reconciler and a
    # ghost to the book.
    assert position.position_id == was.position_id
    assert position.entry_spread == was.entry_spread
    assert position.leg_a.position_tickets == was.leg_a.position_tickets
    assert position.leg_b.position_tickets == was.leg_b.position_tickets
    assert position.recovered is True
    assert second.recovery['recovered'] == 1
    assert second.recovery['unclaimed'] == []


def test_the_recovered_position_is_not_then_closed_as_an_orphan(
        config, pair, legs, tmp_path):
    """The whole point. Sixty seconds after a restart, this used to
    close every position the system was holding."""
    db = tmp_path / 'trader.db'
    first = engine(config, legs, db)
    first.config.pairs[pair.key].order_type = type(pair.order_type)('MARKET')
    first.click(pair.key, SpreadSide.SELL, None)

    second = engine(config, legs, db)
    for _ in range(6):                       # well past three strikes
        report = second.reconciler.run()

    assert report['orphans'] == []
    assert report['closed'] == []
    assert legs['acct_a'].broker.open_positions()
    assert legs['acct_b'].broker.open_positions()
    assert second.book.positions(pair.key)


def test_a_closed_position_does_not_come_back(config, pair, legs, tmp_path):
    db = tmp_path / 'trader.db'
    first = engine(config, legs, db)
    first.config.pairs[pair.key].order_type = type(pair.order_type)('MARKET')
    first.click(pair.key, SpreadSide.SELL, None)
    position = first.book.positions(pair.key)[0]
    first.executor.close_position(pair, position, first.market[pair.key])
    first.remember(position)

    second = engine(config, legs, db)

    assert second.book.positions(pair.key) == []
    assert second.recovery['recovered'] == 0
    # ...but it is still in the record, with what it made.
    closed = Store(str(db)).closed_positions()
    assert len(closed) == 1
    assert closed[0]['realized_pnl'] is not None


def test_a_position_whose_close_failed_comes_back_open(config, pair, legs,
                                                        tmp_path):
    """A close that did not go through leaves the position OPEN and
    ACTIVE. Coming back closed would leave the money at the broker with
    the screen reading flat."""
    db = tmp_path / 'trader.db'
    first = engine(config, legs, db)
    first.config.pairs[pair.key].order_type = type(pair.order_type)('MARKET')
    first.click(pair.key, SpreadSide.SELL, None)
    position = first.book.positions(pair.key)[0]
    legs['acct_a'].broker.fail_closes.add(pair.symbol_a)

    outcome = first.executor.close_position(pair, position,
                                            first.market[pair.key])
    first.remember(position)
    assert not outcome['ok']

    second = engine(config, legs, db)
    assert len(second.book.positions(pair.key)) == 1
    assert second.book.positions(pair.key)[0].is_open


def test_an_account_that_cannot_be_read_at_startup_holds_the_fire(
        config, pair, legs, tmp_path):
    """Recovery against an account we could not read is INCOMPLETE, and
    an incomplete book auto-closes nothing."""
    legs['acct_a'].positions = lambda symbol=None: None
    coordinator = engine(config, legs, tmp_path / 'trader.db')

    assert coordinator.recovery['complete'] is False
    assert 'acct_a' in coordinator.recovery['error']
    assert coordinator.reconciler.book_complete is False


def test_a_broken_database_is_said_out_loud_and_nothing_is_closed(
        config, pair, legs, tmp_path):
    db = tmp_path / 'trader.db'
    with open(db, 'wb') as f:
        f.write(b'this is not a database')

    with pytest.raises(Exception):
        # The Store itself refuses to pretend — the caller sees it.
        Store(str(db))

    # And a coordinator with no store at all still refuses to auto-close.
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    assert coordinator.reconciler.book_complete is False
    assert 'no database' in coordinator.recovery['error']


def test_recovery_survives_the_position_being_partly_closed_elsewhere(
        config, pair, legs, tmp_path):
    """Leg A was closed by hand in the terminal while we were down. The
    position comes back, and the GHOST rule — not the orphan rule —
    handles it, after three strikes."""
    db = tmp_path / 'trader.db'
    first = engine(config, legs, db)
    first.config.pairs[pair.key].order_type = type(pair.order_type)('MARKET')
    first.click(pair.key, SpreadSide.SELL, None)
    legs['acct_a'].broker.positions.clear()          # closed by hand

    second = engine(config, legs, db)
    position = second.book.positions(pair.key)[0]
    assert position.is_open                          # recovered, not guessed

    for _ in range(3):
        report = second.reconciler.run()
    assert report['ghosts']
    assert not position.is_open
    assert 'leg A' in position.close_reason
