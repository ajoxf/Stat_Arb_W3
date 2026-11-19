"""Lock means LOCKED: the engine holds the window, not just the scroll.

The Lock tick used to stop the BROWSER recentring its scroll while the
two things that actually move a price off its row — the anchor, and the
window widening to follow the touches — carried on underneath it. A
control named Lock that locks a third of the movement is worse than no
control, because the trader believes the ladder is still and aims a
click at a row that is about to become a different price.

Every test here has a CONTROL that turns the lock off and asserts the
opposite, so none of them can pass against a ladder that never moves
for some other reason.
"""

import pytest

from okxtrader.coordinator import Coordinator, ladder_rows, ladder_anchor


class Book:
    """Just enough book for the row window: no working orders."""

    def orders(self, key):
        return []

    def working_at(self, key, level):
        return (None, None)


class Pair:
    key = 'P'
    rows = 30

    def __init__(self, increment=0.05):
        self.increment = increment

    def effective_increment(self):
        return self.increment


def market(spread, width=0.66):
    return {'spread': spread,
            'short_spread': spread - width / 2.0,
            'long_spread': spread + width / 2.0}


def levels(rows):
    return [row['level'] for row in rows]


# -- the anchor -------------------------------------------------------

def coordinator():
    """A Coordinator with only the ladder state these tests touch."""
    engine = Coordinator.__new__(Coordinator)
    engine._ladder_anchor = {}
    engine._ladder_locked = {}
    return engine


def test_an_unlocked_ladder_reanchors_when_the_market_walks():
    """The CONTROL. Without this the lock tests prove nothing."""
    engine = coordinator()
    pair = Pair()
    first = engine._anchor_for('P', pair, market(50.00))
    # Well past max(1, 30 x 0.34) x 0.05 = 0.51.
    moved = engine._anchor_for('P', pair, market(52.00))
    assert first == pytest.approx(50.00)
    assert moved != pytest.approx(first)


def test_a_locked_ladder_keeps_its_anchor_however_far_the_market_goes():
    engine = coordinator()
    pair = Pair()
    first = engine._anchor_for('P', pair, market(50.00))
    engine.lock_ladder('P', True)
    for spread in (50.60, 52.00, 61.00, 40.00):
        held = engine._anchor_for('P', pair, market(spread))
        assert held == pytest.approx(first), f'moved at {spread}'


def test_unlocking_lets_the_window_follow_again():
    engine = coordinator()
    pair = Pair()
    first = engine._anchor_for('P', pair, market(50.00))
    engine.lock_ladder('P', True)
    assert engine._anchor_for('P', pair, market(56.00)) == pytest.approx(first)
    engine.lock_ladder('P', False)
    assert engine._anchor_for('P', pair, market(56.00)) != pytest.approx(first)


def test_centre_rebuilds_a_locked_window_and_leaves_it_locked():
    """Centre means "show me the market", not "and start following it"."""
    engine = coordinator()
    pair = Pair()
    engine._anchor_for('P', pair, market(50.00))
    engine.lock_ladder('P', True)
    engine.recentre_ladder('P')
    rebuilt = engine._anchor_for('P', pair, market(56.00))
    assert rebuilt == pytest.approx(56.00)
    # Still held: the next walk must not move it again.
    assert engine._anchor_for('P', pair, market(62.00)) == pytest.approx(56.00)


# -- the row window ---------------------------------------------------

def test_an_unlocked_window_widens_to_the_touch_and_shifts_the_rows():
    """The CONTROL for the freeze: this is the crawl being reproduced."""
    pair, book = Pair(), Book()
    near = ladder_rows(pair, market(50.00), book, anchor=50.00)
    # The anchor is held, but the touch has walked far above it.
    far = ladder_rows(pair, market(50.00, width=6.0), book, anchor=50.00)
    assert len(far) > len(near), 'the window did not widen at all'
    assert levels(far)[0] != pytest.approx(levels(near)[0])


def test_a_frozen_window_is_the_same_rows_whatever_the_touch_does():
    pair, book = Pair(), Book()
    base = levels(ladder_rows(pair, market(50.00), book, anchor=50.00,
                              frozen=True))
    for width in (0.10, 2.0, 6.0, 14.0):
        now = levels(ladder_rows(pair, market(50.00, width=width), book,
                                 anchor=50.00, frozen=True))
        assert now == base, f'the window moved at width {width}'


def test_a_frozen_window_still_shows_a_resting_order_it_cannot_reach():
    """A resting order the trader cannot see is one they cannot pull.

    Order levels are FIXED once placed, so they widen the window once
    and never again — which is not a crawl.
    """
    class Resting(Book):
        def orders(self, key):
            return [type('O', (), {'level': 58.00})()]

    pair = Pair()
    frozen = levels(ladder_rows(pair, market(50.00), Resting(), anchor=50.00,
                                frozen=True))
    assert max(frozen) >= 58.00
    # And it is still frozen against the TOUCH, which is the mover.
    wide = levels(ladder_rows(pair, market(50.00, width=6.0), Resting(),
                              anchor=50.00, frozen=True))
    assert wide == frozen


def test_the_anchor_helper_itself_still_holds_inside_the_band():
    """`ladder_anchor` is unchanged, and the lock sits above it."""
    assert ladder_anchor(50.0, 50.20, 0.05, 30) == pytest.approx(50.0)
    assert ladder_anchor(50.0, 52.00, 0.05, 30) == pytest.approx(52.0)


def test_every_row_says_whether_it_is_the_anchor():
    """The heavy rule needs the anchor row NAMED, not inferred.

    On a locked ladder the rule sits on the anchor rather than the live
    mid: with the rows frozen and the bands solid, a rule that hops a
    row every few ticks is the only thing left moving.
    """
    pair, book = Pair(), Book()
    rows = ladder_rows(pair, market(50.13), book, anchor=50.00, frozen=True)
    flagged = [row for row in rows if row['is_anchor']]
    assert len(flagged) == 1, 'exactly one row is the anchor'
    assert flagged[0]['level'] == pytest.approx(50.00)


def test_the_anchor_row_does_not_move_while_the_mid_does():
    """The CONTROL that matters: the mid row moves, the anchor does not.

    Without this the test above passes on a ladder where the two are
    always the same row, which is exactly the case being fixed.
    """
    pair, book = Pair(), Book()
    seen_mid, seen_anchor = set(), set()
    for spread in (50.00, 50.12, 50.24, 49.88, 50.31):
        rows = ladder_rows(pair, market(spread), book, anchor=50.00,
                           frozen=True)
        mid = [r['level'] for r in rows if r['is_mid']]
        anchor = [r['level'] for r in rows if r['is_anchor']]
        seen_mid.update(mid)
        seen_anchor.update(anchor)
    assert len(seen_mid) > 1, 'the mid never moved, so this proves nothing'
    assert len(seen_anchor) == 1, 'the anchor row moved'
    assert seen_anchor.pop() == pytest.approx(50.00)
