"""Everything the QUOTER books must reach the database.

`quoter.work` returns its events and the coordinator drops them, so for
as long as nothing on that path wrote through, the database never learned
anything LIMIT mode did:

- a limit entry that filled was recovered as NOTHING after a restart —
  two live legs at the broker with no book entry at all;
- a close that filled on a resting order came back OPEN;
- a partial close came back at its old size.

#15 wired this for `reduce_first` (`on_closed=self.remember`) and not for
the quoter's own fill path.

The reconciler section at the end is a PIN, not a fix: it keys on
tickets, never volumes, so a partly closed position must not read as a
ghost. There is nothing to change there and this says so out loud.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import SpreadSide


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / 'positions.db'))


@pytest.fixture
def engine(config, pair, legs, store, tmp_path):
    coordinator = Coordinator(config, legs, store=store,
                              status_path=str(tmp_path / 'status.json'),
                              sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def stored(store):
    """(id, quantity) for every position the DATABASE calls open."""
    return {row['position_id']: row['quantity']
            for row in store.open_positions()}


def sell_one(coordinator, pair):
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    assert coordinator.click(pair.key, SpreadSide.SELL,
                             md['short_spread']).get('ok')
    coordinator.poll_once()
    return coordinator.book.positions(pair.key)[0]


def rest_a_close(coordinator, pair):
    md = coordinator.market[pair.key]
    return coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread'] - 0.50)


def fill(legs, volume=None, account='acct_b'):
    ticket = list(legs[account].broker.pendings)[0]
    if volume is None:
        return legs[account].broker.fill_pending(ticket)
    return legs[account].broker.part_fill_pending(ticket, volume)


def watched_level(coordinator, pair):
    return [g for g in coordinator.quoter.snapshot(pair.key)
            if g['intent'] == 'CLOSE'][0]['level']


def walk_to_the_level(coordinator, pair, gold_symbols, level):
    """Move leg B until a BUY close reads its level as reached."""
    _spot, future = gold_symbols
    move = (level - coordinator.market[pair.key]['long_spread']) - 0.01
    future.bid += move
    future.ask += move


def half_close(broker):
    real = broker.close_position_ticket

    def stingy(symbol, ticket, volume, entry_side, **kw):
        return real(symbol, ticket, volume / 2.0, entry_side, **kw)

    broker.close_position_ticket = stingy


# -- entries --------------------------------------------------------------

@pytest.mark.quote
def test_a_LIMIT_entry_that_fills_is_written_through(engine, pair, legs,
                                                     store):
    """The worst of the three. A restart recovered NOTHING, so two live
    legs sat at the broker with no book entry — and an empty book makes
    every live position look like an orphan."""
    coordinator = engine
    md = coordinator.market[pair.key]
    coordinator.click(pair.key, SpreadSide.SELL, md['short_spread'] + 0.50)
    coordinator.poll_once()
    fill(legs)
    coordinator.poll_once()

    position = coordinator.book.positions(pair.key)[0]
    assert position.position_id in stored(store), (
        'a filled limit entry is at the broker and not in the database')
    assert stored(store)[position.position_id] == pytest.approx(
        position.quantity)


def test_the_CONTROL_a_MARKET_entry_was_always_written_through(engine, pair,
                                                               legs, store):
    """The control. The market path already persisted, and must still —
    otherwise the test above could pass on a store that saves everything
    by accident."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert position.position_id in stored(store)


# -- closes ---------------------------------------------------------------

def test_a_close_that_fires_at_its_LEVEL_is_written_through(engine, pair,
                                                             legs, store,
                                                             gold_symbols):
    """It came back OPEN: the book had it closed, the broker was flat,
    and a restart put an exposure on the screen that was not there."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert position.position_id in stored(store)
    assert rest_a_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    walk_to_the_level(coordinator, pair, gold_symbols,
                      watched_level(coordinator, pair))
    coordinator.poll_once()

    assert position.is_open is False
    assert position.position_id not in stored(store), (
        'the database still calls a closed position open')


def test_a_PARTLY_filled_close_is_written_through_at_its_NEW_size(engine,
                                                                   pair, legs,
                                                                   store,
                                                                   gold_symbols):
    """Half closed in the book, whole in the database: a restart would
    have put the other half back on the screen."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert rest_a_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    half_close(legs['acct_a'].broker)
    half_close(legs['acct_b'].broker)
    walk_to_the_level(coordinator, pair, gold_symbols,
                      watched_level(coordinator, pair))
    coordinator.poll_once()

    assert position.quantity == pytest.approx(0.5)
    assert stored(store)[position.position_id] == pytest.approx(0.5)


def test_a_close_whose_OTHER_leg_refused_is_written_through_as_OPEN(engine,
                                                                    pair,
                                                                    legs,
                                                                    store,
                                                                    gold_symbols):
    """A close that did not go through leaves the position OPEN, and that
    is exactly the state a restart must come back to rather than guess
    at."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert rest_a_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    legs['acct_a'].broker.fail_closes.add('XAUUSD_')
    walk_to_the_level(coordinator, pair, gold_symbols,
                      watched_level(coordinator, pair))
    coordinator.poll_once()

    assert position.is_open is True
    assert position.position_id in stored(store)


# -- and back again -------------------------------------------------------

def test_a_reduced_position_RECOVERS_at_its_reduced_size(engine, pair, legs,
                                                          store, tmp_path,
                                                          gold_symbols):
    """The round trip that matters: half close, restart, and the book
    must come back agreeing with the broker rather than with what was
    first written."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert rest_a_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    half_close(legs['acct_a'].broker)
    half_close(legs['acct_b'].broker)
    walk_to_the_level(coordinator, pair, gold_symbols,
                      watched_level(coordinator, pair))
    coordinator.poll_once()

    fresh = Coordinator(coordinator.config, legs, store=store,
                        status_path=str(tmp_path / 'status2.json'),
                        sleep=lambda s: None)
    fresh.recover()

    back = fresh.book.position(position.position_id)
    assert back is not None, 'the position did not come back at all'
    assert back.quantity == pytest.approx(0.5)
    assert back.leg_a.volume == pytest.approx(position.leg_a.volume)
    assert back.leg_b.volume == pytest.approx(position.leg_b.volume)


# -- the reconciler, pinned -----------------------------------------------

def test_a_PARTLY_closed_position_is_not_a_GHOST(engine, pair, legs, store,
                                                 gold_symbols):
    """A PIN, not a fix. The reconciler keys on TICKETS and never on
    volumes, and `reduce_by` does not touch tickets — so a position that
    is half closed must raise no ghost and no orphan. If anyone ever
    makes it compare sizes, this is what breaks."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert rest_a_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    half_close(legs['acct_a'].broker)
    half_close(legs['acct_b'].broker)
    walk_to_the_level(coordinator, pair, gold_symbols,
                      watched_level(coordinator, pair))
    coordinator.poll_once()
    assert position.quantity == pytest.approx(0.5)

    coordinator.reconciler.book_complete = True
    for _ in range(coordinator.reconciler.STRIKES + 1):
        report = coordinator.reconciler.run()

    assert report['ghosts'] == []
    assert report['orphans'] == []
    assert position.is_open is True
