"""A resting CLOSE is synthetic, and what happens when it fires.

WHY IT IS SYNTHETIC, in one paragraph, because this is the thing that
cost real money:

A closing order cannot be a broker pending. MT5 honours `position` on
TRADE_ACTION_DEAL and IGNORES it on TRADE_ACTION_PENDING, so a "closing"
limit rests as an ordinary limit — and on a hedging account an ordinary
opposite limit OPENS a second position. Live 2026-09-02, ticket 2092 was
rested to close ticket 2090 and filled as a BUY 0.01 sitting beside the
SELL 0.01 it was meant to close. The engine believed that leg was flat,
closed the other leg on the strength of it, and the reconciler swept
both futures as orphans a minute later:

    15:34:55 CRITICAL reconcile: closed ORPHAN GCZ6.s ticket 2090 — SELL
    15:34:55 CRITICAL reconcile: closed ORPHAN GCZ6.s ticket 2092 — BUY

Attaching a broker take-profit to the leg instead is not the answer
either: one leg closing alone converts the hedge into a naked outright.

So the level is held HERE, and when the market reaches it the position
is closed BY TICKET on both legs at once. What that costs, stated
plainly and tested below: it only fires while this is running, and it
crosses at the touch rather than earning the level.

Every guard has a CONTROL that turns the condition off and asserts the
opposite, so none of them can pass by doing nothing.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import OrderState, SpreadSide
from okxtrader.quoter import closing_trigger_reached


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def sell_one(coordinator, pair):
    """A SELL spread ON, the fast way, so the test is about the exit."""
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, SpreadSide.SELL, md['short_spread'])
    assert answer.get('ok'), answer
    coordinator.poll_once()
    return coordinator.book.positions(pair.key)[0]


def click_to_close(coordinator, pair, offset=0.50):
    """The blue ladder, away from the market: rest a close at my price."""
    md = coordinator.market[pair.key]
    return coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread'] - offset)


def walk_to_the_level(coordinator, pair, gold_symbols, level, through=0.01):
    """Move leg B until the spread a BUY close reads has reached `level`.

    The spread is `B - beta x A`, so leg B moves it one for one.
    """
    _spot, future = gold_symbols
    have = coordinator.market[pair.key]['long_spread']
    move = (level - have) - through
    future.bid += move
    future.ask += move


def volumes(legs):
    """(leg A lots on, leg B lots on) at the two brokers."""
    return (sum(p['volume'] for p in legs['acct_a'].broker.open_positions()),
            sum(p['volume'] for p in legs['acct_b'].broker.open_positions()))


def watches(coordinator, pair):
    return [g for g in coordinator.quoter.snapshot(pair.key)
            if g['intent'] == 'CLOSE']


# -- the fault itself -----------------------------------------------------

def test_a_resting_close_puts_NOTHING_at_the_broker(engine, pair, legs):
    """THE LIVE FAULT. The pending that was supposed to close ticket 2090
    opened ticket 2092 beside it. There must be no such pending."""
    coordinator = engine
    position = sell_one(coordinator, pair)

    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()

    assert not legs['acct_a'].broker.pendings
    assert not legs['acct_b'].broker.pendings, (
        'a closing pending is at the broker, and on a hedging account it '
        'OPENS a second position instead of closing one')
    # It is a level this system watches instead.
    held = watches(coordinator, pair)
    assert len(held) == 1
    assert held[0]['ticket'] is None
    assert held[0]['position_id'] == position.position_id
    # ...and nothing has been closed yet.
    assert volumes(legs) == (0.1, 0.1)
    assert position.is_open is True


@pytest.mark.quote
def test_the_CONTROL_an_ENTRY_still_rests_a_real_pending(engine, pair, legs):
    """The control. Only CLOSING orders lose their pending — an entry
    limit still rests one, because a pending that OPENS is exactly what
    a pending does."""
    coordinator = engine
    md = coordinator.market[pair.key]
    coordinator.click(pair.key, SpreadSide.SELL, md['short_spread'] + 0.50)
    coordinator.poll_once()

    assert len(legs['acct_b'].broker.pendings) == 1


# -- firing at the level --------------------------------------------------

def test_reaching_the_level_closes_BOTH_legs_by_ticket(engine, pair, legs,
                                                        gold_symbols):
    """The close goes as a DEAL carrying the position, which is the one
    form MT5 actually honours — and both legs go together."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    walk_to_the_level(coordinator, pair, gold_symbols, level)
    coordinator.poll_once()

    assert position.is_open is False
    assert volumes(legs) == (0.0, 0.0)
    assert watches(coordinator, pair) == []
    # Closed by TICKET on both accounts, never offset with an opposite
    # order — that is what opened the second position live.
    for account in ('acct_a', 'acct_b'):
        sent = [e for e in legs[account].broker.sent if e['action'] == 'close']
        assert sent, f'{account} was not closed by ticket'


def test_the_CONTROL_it_does_NOT_fire_before_the_level(engine, pair, legs,
                                                        gold_symbols):
    """The control for the test above. One tick short of the level and
    nothing happens — otherwise 'fires at the level' could pass by
    closing at any price at all."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    # Stop one tick ABOVE the level: not reached.
    walk_to_the_level(coordinator, pair, gold_symbols, level, through=-0.01)
    coordinator.poll_once()

    assert position.is_open is True
    assert volumes(legs) == (0.1, 0.1)
    assert len(watches(coordinator, pair)) == 1


def test_the_trigger_reads_the_EXECUTABLE_side_for_its_own_direction():
    """A BUY closes when the spread can be BOUGHT at or under its level;
    a SELL when it can be SOLD at or over it. A price nobody has never
    triggers — None is unknown, not 'reached'."""
    assert closing_trigger_reached('BUY', 58.90,
                                   {'long_spread': 58.90}) is True
    assert closing_trigger_reached('BUY', 58.90,
                                   {'long_spread': 58.91}) is False
    assert closing_trigger_reached('SELL', 58.90,
                                   {'short_spread': 58.90}) is True
    assert closing_trigger_reached('SELL', 58.90,
                                   {'short_spread': 58.89}) is False
    # The WRONG side must never fire it.
    assert closing_trigger_reached('BUY', 58.90,
                                   {'short_spread': 10.0}) is False
    assert closing_trigger_reached('BUY', 58.90, {}) is False
    assert closing_trigger_reached('BUY', 58.90, None) is False


# -- when the close does not go through -----------------------------------

def test_a_close_that_FAILS_keeps_its_level_and_the_brokers_words(
        engine, pair, legs, gold_symbols):
    """The position stays open and so does the level. 'Check the log' is
    not an answer on a live account."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    legs['acct_a'].broker.fail_closes.add('XAUUSD_')
    walk_to_the_level(coordinator, pair, gold_symbols, level)
    coordinator.poll_once()

    assert position.is_open is True
    held = watches(coordinator, pair)
    assert len(held) == 1, 'the level was dropped after a failed close'
    assert 'forced failure' in (held[0]['reason'] or ''), held[0]['reason']


def test_a_stale_print_HOLDS_the_close_and_says_so(engine, pair, legs):
    """Firing a resting order off a print the system itself calls
    untrustworthy closes at a level the market may never have shown.

    This is NOT a guard withholding a close: a close the trader asks for
    is never blocked, and the ladder says the resting one is held so
    they can do exactly that."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    md = dict(coordinator.market[pair.key])
    md['long_spread'] = level - 1.0          # well through it
    md['guard_reason'] = 'the feed is stale'
    coordinator.quoter.work(pair, md)

    assert position.is_open is True
    held = watches(coordinator, pair)
    assert 'stale' in (held[0]['reason'] or '')
    assert 'held' in (held[0]['reason'] or '')


def test_the_CONTROL_the_same_price_fires_it_when_the_print_is_GOOD(
        engine, pair, legs):
    """The control for the test above."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    md = dict(coordinator.market[pair.key])
    md['long_spread'] = level - 1.0
    md.pop('guard_reason', None)
    coordinator.quoter.work(pair, md)

    assert position.is_open is False


# -- a close the broker only partly fills ---------------------------------

def half_close(broker):
    """Make this broker close only HALF of whatever it is asked for."""
    real = broker.close_position_ticket

    def stingy(symbol, ticket, volume, entry_side, **kw):
        return real(symbol, ticket, volume / 2.0, entry_side, **kw)

    broker.close_position_ticket = stingy


def test_a_HALF_filled_close_takes_BOTH_legs_down_together(engine, pair, legs,
                                                            gold_symbols):
    """A market close can come back short. Booking it as whole would
    take lots off our record that are still at the broker — the book
    reading flat while the money is there."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    half_close(legs['acct_a'].broker)
    half_close(legs['acct_b'].broker)
    walk_to_the_level(coordinator, pair, gold_symbols, level)
    coordinator.poll_once()

    lots_a, lots_b = volumes(legs)
    assert lots_a == pytest.approx(0.05)
    assert lots_b == pytest.approx(0.05)
    # ...and the book agrees with the broker rather than claiming flat.
    assert position.is_open is True
    assert position.quantity == pytest.approx(0.5)


def test_the_CONTROL_a_whole_close_still_closes_the_whole_position(
        engine, pair, legs, gold_versions=None, gold_symbols=None):
    """The control. Without it the partial handling could pass by never
    closing anything."""
    coordinator = engine
    position = sell_one(coordinator, pair)

    coordinator.executor.close_position(pair, position,
                                        coordinator.market[pair.key])

    assert position.is_open is False
    assert volumes(legs) == (0.0, 0.0)


def test_a_leg_the_broker_no_longer_lists_counts_as_DONE(engine, pair, legs):
    """A ticket MT5 does not list is already gone, and that leg is done
    however little was closed to get there. Reading it as 'closed
    nothing' would leave the position open for ever."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    for ticket in list(position.leg_b.position_tickets):
        legs['acct_b'].broker.positions.pop(int(ticket), None)

    coordinator.executor.close_position(pair, position,
                                        coordinator.market[pair.key])

    assert position.is_open is False
    assert volumes(legs) == (0.0, 0.0)


# -- a click against a position whose quoting leg has gone ----------------

def test_a_click_never_rests_against_a_ticket_the_broker_has_lost(engine,
                                                                   pair, legs):
    """What is left is one naked leg, and a naked leg cannot wait at a
    price."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    for ticket in list(position.leg_b.position_tickets):
        legs['acct_b'].broker.positions.pop(int(ticket), None)

    answer = click_to_close(coordinator, pair)
    coordinator.poll_once()

    assert answer.get('at_market') is True
    assert position.position_id in (answer.get('closed') or [])
    assert 'NAKED' in answer['reason']
    assert volumes(legs) == (0.0, 0.0)
    assert position.is_open is False


def test_the_CONTROL_a_whole_position_still_RESTS_where_it_is_clicked(
        engine, pair, legs):
    """The control. With both legs where they should be the click must
    rest, not go to market."""
    coordinator = engine
    sell_one(coordinator, pair)

    answer = click_to_close(coordinator, pair)
    coordinator.poll_once()

    assert answer.get('at_market') is not True
    assert answer.get('reducing') is True
    assert len(watches(coordinator, pair)) == 1
    assert volumes(legs) == (0.1, 0.1)


def test_a_leg_that_cannot_be_READ_is_not_treated_as_flat(engine, pair, legs,
                                                           monkeypatch):
    """`positions()` returns None for 'could not be read', which is NOT
    'no position' (spec §7). Reading it as flat would market-close a live
    spread on no evidence at all."""
    coordinator = engine
    sell_one(coordinator, pair)
    monkeypatch.setattr(legs['acct_b'], 'positions', lambda symbol=None: None)

    answer = click_to_close(coordinator, pair)
    coordinator.poll_once()

    assert answer.get('at_market') is not True
    assert volumes(legs) == (0.1, 0.1)


# -- whose close was it ---------------------------------------------------

def test_the_traders_own_close_is_not_recorded_as_a_take_profit(
        engine, pair, legs, gold_symbols):
    """A close the trader clicked is not automation."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    walk_to_the_level(coordinator, pair, gold_symbols, level)
    coordinator.poll_once()

    assert position.close_reason == 'closed by the trader'


def test_the_CONTROL_an_automations_close_still_says_take_profit():
    """The control for the test above: the reason is read off WHO armed
    the order, so a close automation armed is not reported as the
    trader's."""
    from types import SimpleNamespace
    from okxtrader.quoter import Quoter

    auto = SimpleNamespace(orders=[SimpleNamespace(auto_armed=True)])
    hand = SimpleNamespace(orders=[SimpleNamespace(auto_armed=False)])
    assert Quoter._close_reason(None, auto) == 'auto take-profit'
    assert Quoter._close_reason(None, hand) == 'closed by the trader'


def test_the_synthetic_stops_working_once_its_close_is_whole(engine, pair,
                                                             legs,
                                                             gold_symbols):
    """A partial leaves the click WORKING for the rest; a whole close
    must not."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    order = coordinator.book.orders_for_position(position.position_id)[0]
    level = watches(coordinator, pair)[0]['level']

    half_close(legs['acct_a'].broker)
    half_close(legs['acct_b'].broker)
    walk_to_the_level(coordinator, pair, gold_symbols, level)
    coordinator.poll_once()
    assert order.is_working is True, 'the remainder stopped working'

    legs['acct_a'].broker.close_position_ticket = \
        type(legs['acct_a'].broker).close_position_ticket.__get__(
            legs['acct_a'].broker)
    legs['acct_b'].broker.close_position_ticket = \
        type(legs['acct_b'].broker).close_position_ticket.__get__(
            legs['acct_b'].broker)
    coordinator.poll_once()

    assert position.is_open is False
    assert order.state is OrderState.FILLED


# -- a broker that keeps refusing -----------------------------------------

def test_a_close_that_keeps_failing_is_ESCALATED_not_hammered(engine, pair,
                                                               legs,
                                                               gold_symbols):
    """The level is watched every poll, so a broker that refuses would be
    asked three times a second for as long as the market sits there.
    That is hammering, and it buries the one line the trader needs."""
    coordinator = engine
    coordinator.config.settings['CLOSE_ATTEMPTS'] = 3
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    legs['acct_a'].broker.fail_closes.add('XAUUSD_')
    walk_to_the_level(coordinator, pair, gold_symbols, level)
    for _ in range(3):
        coordinator.poll_once()
    tried = len([e for e in legs['acct_a'].broker.sent
                 if e['action'] == 'close'])
    assert watches(coordinator, pair)[0]['escalated'] is True
    assert 'CLOSE IT BY HAND' in watches(coordinator, pair)[0]['reason']

    # ...and it stops asking.
    for _ in range(5):
        coordinator.poll_once()
    assert len([e for e in legs['acct_a'].broker.sent
                if e['action'] == 'close']) == tried
    assert position.is_open is True


def test_the_CONTROL_it_RETRIES_before_it_gives_up(engine, pair, legs,
                                                    gold_symbols):
    """The control. A close that fails once must be tried again —
    otherwise 'escalates' could pass by never retrying at all."""
    coordinator = engine
    coordinator.config.settings['CLOSE_ATTEMPTS'] = 3
    sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    legs['acct_a'].broker.fail_closes.add('XAUUSD_')
    walk_to_the_level(coordinator, pair, gold_symbols, level)
    coordinator.poll_once()
    assert watches(coordinator, pair)[0]['escalated'] is False
    coordinator.poll_once()
    assert len([e for e in legs['acct_a'].broker.sent
                if e['action'] == 'close']) >= 2


def test_the_market_moving_AWAY_clears_the_escalation(engine, pair, legs,
                                                       gold_symbols):
    """A session break or a spread that widened out is not a fault to
    stay escalated over. When the level comes back it gets a clean
    slate — and this time the broker is willing."""
    coordinator = engine
    coordinator.config.settings['CLOSE_ATTEMPTS'] = 2
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    legs['acct_a'].broker.fail_closes.add('XAUUSD_')
    walk_to_the_level(coordinator, pair, gold_symbols, level)
    coordinator.poll_once()
    coordinator.poll_once()
    assert watches(coordinator, pair)[0]['escalated'] is True

    # The market leaves...
    _spot, future = gold_symbols
    future.bid += 5.0
    future.ask += 5.0
    coordinator.poll_once()
    assert watches(coordinator, pair)[0]['escalated'] is False

    # ...and comes back, to a broker that will take it.
    legs['acct_a'].broker.fail_closes.discard('XAUUSD_')
    walk_to_the_level(coordinator, pair, gold_symbols, level)
    coordinator.poll_once()
    assert position.is_open is False


def test_a_broker_that_HANGS_UP_does_not_take_the_pricing_pass_down(
        engine, pair, legs, gold_symbols, monkeypatch):
    """The close is a NETWORK call inside the pricing pass. Unguarded, a
    broker that drops mid-close raises out of `work()` and takes the
    whole poll with it — every pair's quotes, not just this level."""
    coordinator = engine
    position = sell_one(coordinator, pair)
    assert click_to_close(coordinator, pair).get('ok')
    coordinator.poll_once()
    level = watches(coordinator, pair)[0]['level']

    def hang_up(*a, **kw):
        raise ConnectionResetError('the terminal went away')

    monkeypatch.setattr(coordinator.executor, 'close_position', hang_up)
    walk_to_the_level(coordinator, pair, gold_symbols, level)
    coordinator.poll_once()          # must not raise

    # The quotes still refreshed, and the level is still watched.
    assert coordinator.market[pair.key]['long_spread'] is not None
    assert 'went away' in (watches(coordinator, pair)[0]['reason'] or '')
    assert position.is_open is True
