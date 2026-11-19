"""The ladder's price window must hold still.

It was rebuilt around the live mid on every poll — three times a
second. One increment of movement re-centred the whole window, so the
row under the trader's cursor became a DIFFERENT PRICE without the
cursor moving, and a click landed on whatever had slid under it.
"""

from okxtrader.coordinator import ladder_anchor, ladder_rows
from okxtrader.config import PairConfig


def pair(increment=0.05, rows=30):
    return PairConfig.from_dict('X|Y', {
        'leg_a': {'account': 'a', 'symbol': 'X'},
        'leg_b': {'account': 'b', 'symbol': 'Y'},
        'hedge_ratio': 1.0, 'increment': increment, 'rows': rows})


class NoBook:
    def orders(self, key):
        return []

    def working_at(self, key, level):
        return (None, None)


def md_at(spread):
    return {'spread': spread, 'short_spread': spread - 0.05,
            'long_spread': spread + 0.05}


def test_the_window_does_not_move_when_the_market_ticks():
    """The whole point: one increment of drift must not renumber every
    row on the screen."""
    p, book = pair(), NoBook()

    anchor = ladder_anchor(None, 52.00, 0.05, 30)
    first = [r['level'] for r in
             ladder_rows(p, md_at(52.00), book, anchor=anchor)]

    # The market moves several increments — a normal second.
    for spread in (52.05, 52.10, 51.95, 52.15, 51.90):
        anchor = ladder_anchor(anchor, spread, 0.05, 30)
        levels = [r['level'] for r in
                  ladder_rows(p, md_at(spread), book, anchor=anchor)]
        assert levels == first, (
            f'the window moved at spread {spread}: row 0 was {first[0]} '
            f'and is now {levels[0]}')


def test_the_window_follows_a_market_that_really_leaves():
    """Holding still is not the same as going blind: a market that has
    walked out of the band takes the window with it."""
    anchor = ladder_anchor(None, 52.00, 0.05, 30)
    # Well past rows * drift (30 * 0.34 = 10.2 increments = 0.51).
    moved = ladder_anchor(anchor, 53.20, 0.05, 30)
    assert moved != anchor
    assert abs(moved - 53.20) <= 0.05


def test_a_row_keeps_its_price_across_a_tick():
    """The guarantee a click depends on: the level at a given position
    is the same level after the market moves."""
    p, book = pair(), NoBook()
    anchor = ladder_anchor(None, 52.00, 0.05, 30)
    before = ladder_rows(p, md_at(52.00), book, anchor=anchor)
    anchor = ladder_anchor(anchor, 52.10, 0.05, 30)
    after = ladder_rows(p, md_at(52.10), book, anchor=anchor)

    for index in (0, 5, len(before) // 2, len(before) - 1):
        assert before[index]['level'] == after[index]['level'], (
            f'row {index} was {before[index]["level"]} and is now '
            f'{after[index]["level"]} — a click there would be repriced')


def test_the_control_puts_the_window_back():
    """Centre drops the anchor, and the next build is around the
    market."""
    anchor = ladder_anchor(None, 52.00, 0.05, 30)
    anchor = ladder_anchor(anchor, 52.20, 0.05, 30)
    assert anchor == 52.00                      # held still, as designed
    # What recentre_ladder does: forget it, and rebuild.
    assert ladder_anchor(None, 52.20, 0.05, 30) == 52.20
