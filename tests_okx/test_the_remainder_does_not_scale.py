"""A reducing click bigger than the position it covers.

BUY 12 over a SELL 10 is ONE instruction: cover the 10, then open 2.
The 2 is held here (`QuoteGroup.open_after`) and opens only once the
close has actually gone through — resting it at the same moment gave it
a different engine from the close, and live 2026-09-03 that left a
trader +2 AND still -10.

The question this file answers is whether the 2 should SHRINK when the
close only goes through in part. It should not, and that is the point
being pinned: the click named an absolute size, not a proportion of
somebody else's fill. A trader who asked to end up long 2 wants to end
up long 2, whether the cover took one pass or four.

What the review did find is the other half: the paths that DROP the
remainder. Dropping it is right — something else covered the position,
so the rest is no longer the trade that was asked for — but one of the
two paths said so and the other did it in silence.
"""

import logging

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def short_ten(coordinator, pair):
    """A SELL 10 on the book, at the market."""
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    assert coordinator.click(pair.key, SpreadSide.SELL, md['short_spread'],
                             quantity=10)['ok']
    pair.order_type = pair.order_type.__class__('LIMIT')
    return coordinator.book.positions(pair.key)[0]


def group_of(coordinator, pair):
    return [g for g in coordinator.quoter.groups.values()
            if g.pair_key == pair.key][0]


def buy_twelve_away(coordinator, pair):
    """BUY 12 at a level the market has not reached: covers 10, holds 2."""
    md = coordinator.market[pair.key]
    level = round(md['long_spread'] - 5.0, 10)
    answer = coordinator.click(pair.key, SpreadSide.BUY, level, quantity=12)
    assert answer['ok'] and answer.get('reducing'), answer
    assert answer['remainder'] == pytest.approx(2.0)
    return level


def test_the_remainder_is_the_size_the_trader_typed(engine, pair, legs):
    coordinator = engine
    short_ten(coordinator, pair)
    buy_twelve_away(coordinator, pair)

    assert group_of(coordinator, pair).open_after == pytest.approx(2.0)


def reach(coordinator, pair, gold_symbols, level):
    _spot, future = gold_symbols
    move = (coordinator.market[pair.key]['long_spread'] - level) + 0.01
    future.bid -= move
    future.ask -= move
    coordinator.poll_once()


def test_a_PART_close_does_not_shrink_the_remainder(engine, pair, legs,
                                                     gold_symbols,
                                                     monkeypatch):
    """THE QUESTION ITSELF, on the branch that answers it.

    The broker takes off only part of the 10. The level stays armed for
    the rest and the synthetics settle for what actually went — and the
    2 the click asked for is still 2, because it was never a proportion
    of anything.

    `close_position` is stubbed to report the partial directly: the
    fraction is what drives this branch, and manufacturing one through
    a fake broker would be testing the fake.
    """
    coordinator = engine
    position = short_ten(coordinator, pair)
    level = buy_twelve_away(coordinator, pair)

    def half_of_it(pair_, position_, md=None, **kw):
        return {'ok': True, 'partial': True, 'fraction': 0.4,
                'remaining': 6.0, 'closed': {}}

    monkeypatch.setattr(coordinator.executor, 'close_position', half_of_it)
    reach(coordinator, pair, gold_symbols, level)

    group = group_of(coordinator, pair)
    assert group.open_after == pytest.approx(2.0), \
        'the remainder was scaled to what the broker happened to close'
    assert group.position_id == position.position_id, \
        'the level stopped watching the rest of the position'


def test_a_close_that_does_not_go_through_keeps_the_remainder_too(
        engine, pair, legs, gold_symbols):
    """The other way a close falls short: the broker refuses a leg. The
    position stays open, the level stays armed, and the 2 waits."""
    coordinator = engine
    short_ten(coordinator, pair)
    level = buy_twelve_away(coordinator, pair)
    legs['acct_b'].broker.fail_closes.add('GC1226')

    reach(coordinator, pair, gold_symbols, level)

    assert group_of(coordinator, pair).open_after == pytest.approx(2.0)
    assert coordinator.book.positions(pair.key)[0].is_open


def test_CONTROL_the_remainder_opens_at_its_typed_size_when_the_close_lands(
        engine, pair, legs, gold_symbols):
    """The control. A remainder that never opened at all would pass the
    test above for the wrong reason."""
    coordinator = engine
    short_ten(coordinator, pair)
    level = buy_twelve_away(coordinator, pair)

    reach(coordinator, pair, gold_symbols, level)

    working = coordinator.book.orders(pair.key)
    assert len(working) == 1
    assert working[0].side is SpreadSide.BUY
    assert working[0].quantity == pytest.approx(2.0)
    assert working[0].level == pytest.approx(level)


def test_a_remainder_dropped_because_the_position_went_is_SAID(engine, pair,
                                                               legs, caplog):
    """The gap the review actually found.

    The trader closes the position by hand. Dropping the 2 with it is
    right — the click said 'cover this and open the rest', something
    else covered it, and opening a naked 2 by itself minutes later is
    the last thing anyone wants from a ladder. But it happened in
    silence: a trader who clicked 12 over a 10 saw the 2 simply never
    appear, with nothing anywhere to say why.
    """
    coordinator = engine
    position = short_ten(coordinator, pair)
    buy_twelve_away(coordinator, pair)

    with caplog.at_level(logging.INFO):
        coordinator.quoter.disarm(position.position_id,
                                  'its position is gone')

    assert any('would have opened' in r.getMessage()
               for r in caplog.records), \
        'the remainder was dropped without a word'
    assert not [g for g in coordinator.quoter.groups.values()
                if g.pair_key == pair.key]


def test_CONTROL_disarming_a_plain_close_says_nothing_about_a_remainder(
        engine, pair, legs, caplog):
    """The control: the line above must belong to the remainder, not be
    printed for every disarm in the system."""
    coordinator = engine
    position = short_ten(coordinator, pair)
    md = coordinator.market[pair.key]
    coordinator.close_at_limit(pair.key, round(md['long_spread'] - 5.0, 10))

    with caplog.at_level(logging.INFO):
        coordinator.quoter.disarm(position.position_id,
                                  'its position is gone')

    assert not [r for r in caplog.records
                if 'would have opened' in r.getMessage()]
