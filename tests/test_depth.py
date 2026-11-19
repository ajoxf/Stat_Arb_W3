"""The implied spread book.

A spread has no order book of its own; it has two, and the size at any
spread level is what the WORSE of the two legs can fill at the prices
that level implies. Every test here exists because the alternative — one
leg's size, or the sum of every combination — is a number a trader would
click on and not get.
"""

import pytest

from okxtrader import depth


def flat(sizes, digits=4):
    """A dict of {level: size} as a sorted list of rounded pairs —
    float keys cannot be compared with pytest.approx."""
    return sorted((round(level, digits), round(size, digits))
                  for level, size in sizes.items())


def book(bids, asks):
    rows = [{'type': 'bid', 'price': price, 'volume': volume}
            for price, volume in bids]
    rows += [{'type': 'ask', 'price': price, 'volume': volume}
             for price, volume in asks]
    return rows


def test_the_size_is_the_worse_leg_in_spreads_not_the_better_one_in_lots():
    """Leg B showing 100 lots against leg A's 1 is one lot of spread,
    not a hundred. This is the whole point of the module."""
    a = book(bids=[(4300.0, 1.0)], asks=[(4300.2, 1.0)])
    b = book(bids=[(4360.0, 100.0)], asks=[(4360.4, 100.0)])

    sizes = depth.implied(a, b, beta=1.0, clip_a=1.0, clip_b=1.0,
                          increment=0.01)

    assert flat(sizes['buy']) == [(60.4, 1.0)]
    assert flat(sizes['sell']) == [(59.8, 1.0)]


def test_lots_become_spreads_through_each_legs_own_clip():
    """One spread is `clip_a` lots of A and `clip_b` lots of B. Three
    lots of a leg whose clip is 0.1 is thirty spreads there, and reading
    it as three would understate the book tenfold."""
    a = book(bids=[(100.0, 3.0)], asks=[(100.1, 3.0)])
    b = book(bids=[(120.0, 3.0)], asks=[(120.1, 3.0)])

    sizes = depth.implied(a, b, beta=1.0, clip_a=0.1, clip_b=0.1,
                          increment=0.01)

    assert list(sizes['buy'].values()) == [pytest.approx(30.0)]


def test_liquidity_is_consumed_and_never_counted_twice():
    """Two levels a side is two levels of implied depth — not four
    combinations of the same lots. Summing every pairing would report a
    book several times deeper than either leg actually is."""
    a = book(bids=[(100.0, 1.0)], asks=[(101.0, 1.0), (102.0, 1.0)])
    b = book(bids=[(120.0, 1.0), (119.0, 1.0)], asks=[(121.0, 1.0)])

    sizes = depth.implied(a, b, beta=1.0, clip_a=1.0, clip_b=1.0,
                          increment=1.0)

    assert sum(sizes['sell'].values()) == pytest.approx(2.0)
    # Best against best, then what is left of each: 120-101 and 119-102.
    assert flat(sizes['sell']) == [(17.0, 1.0), (19.0, 1.0)]


def test_a_partly_consumed_level_carries_on_against_the_next_one():
    """One deep level on A fills two of B's: the first takes what it
    needs and the REMAINDER meets the next one."""
    a = book(bids=[(100.0, 1.0)], asks=[(101.0, 5.0)])
    b = book(bids=[(120.0, 2.0), (119.5, 3.0)], asks=[(121.0, 1.0)])

    sizes = depth.implied(a, b, beta=1.0, clip_a=1.0, clip_b=1.0,
                          increment=0.5)

    assert flat(sizes['sell']) == [(18.5, 3.0), (19.0, 2.0)]


def test_beta_prices_the_level_and_does_not_scale_the_size():
    """The level is `B - beta x A`; the size is already in spreads once
    each leg's lots are divided by its own clip. Applying beta again
    would report a size nobody can trade."""
    a = book(bids=[(50.0, 2.0)], asks=[(50.5, 2.0)])
    b = book(bids=[(120.0, 2.0)], asks=[(120.5, 2.0)])

    sizes = depth.implied(a, b, beta=2.0, clip_a=1.0, clip_b=1.0,
                          increment=0.5)

    assert list(sizes['sell']) == [pytest.approx(19.0)]     # 120 - 2 x 50.5
    assert list(sizes['sell'].values()) == [pytest.approx(2.0)]


def test_a_broker_with_no_depth_reports_nothing_rather_than_zero():
    """Most CFD accounts publish no DOM at all. An empty column and a
    column of zeros say different things, and only one of them is
    true."""
    a = book(bids=[(100.0, 1.0)], asks=[(101.0, 1.0)])

    assert depth.implied(a, None, 1.0, 1.0, 1.0, 0.01) == {'buy': {},
                                                           'sell': {}}
    assert depth.at({}, 20.0, 0.01) is None
    assert depth.at(None, 20.0, 0.01) is None


def test_a_level_is_matched_within_half_an_increment():
    """The ladder's rows are rounded to the increment; the implied level
    is arithmetic on two prices. They must still meet."""
    sizes = {20.0: 3.0}
    assert depth.at(sizes, 20.004, 0.01) == 3.0
    assert depth.at(sizes, 20.02, 0.01) is None


def test_the_coordinator_puts_the_sizes_on_the_ladder_rows(config, pair,
                                                            legs):
    """End to end through the engine: both books read, matched, and
    attached to the rows the ladder draws — so the Work column, the
    click and the size all come off one list."""
    from okxtrader.coordinator import Coordinator
    legs['acct_a'].broker.depth_book = book(bids=[(4292.00, 1.0)],
                                            asks=[(4292.20, 1.0)])
    legs['acct_b'].broker.depth_book = book(bids=[(4351.00, 1.0)],
                                            asks=[(4351.40, 1.0)])
    coordinator = Coordinator(config, legs)
    coordinator.start()
    coordinator.poll_once()

    row = coordinator.snapshot()['pairs'][pair.key]

    assert row['depth_published'] is True
    sized = [line for line in row['rows']
             if line['bid_size'] or line['ask_size']]
    assert sized, 'no row carried an implied size'
    # 0.1 lots a side against a 0.1 clip is one spread.
    assert max(line['bid_size'] or 0 for line in row['rows']) == \
        pytest.approx(10.0)


def test_with_no_depth_published_the_ladder_says_so_rather_than_showing_zero(
        config, pair, legs):
    """The control: the same engine, brokers that publish nothing."""
    from okxtrader.coordinator import Coordinator
    coordinator = Coordinator(config, legs)
    coordinator.start()
    coordinator.poll_once()

    row = coordinator.snapshot()['pairs'][pair.key]

    assert row['depth_published'] is False
    assert all(line['bid_size'] is None and line['ask_size'] is None
               for line in row['rows'])
