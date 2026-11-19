"""MT5's own profit carries no commission. Ours did.

The monitor's reconciliation row puts our open P&L beside MT5's and
reddens on a difference. Ours was the NET figure - commission for both
ends of both legs already taken off - and MT5's `profit` is the
floating P&L on the two prices and nothing else.

So the difference carried a whole round trip's commission as a
permanent, structural gap that no market could ever close: a light
that is always on, which is a light nobody reads, and the day it means
something nobody looks.

It was invisible only because these accounts are billed at $0.00 a
lot. A number that is right only while a setting is zero is not right,
and that setting changes the day the desk moves broker.

Nothing the trader is SHOWN changes. `mark_fees` still charges both
ends, which is what a trade's P&L means and what keeps an open
position's figure continuous with the realised one when it closes.
Only the comparison changed - and the row now says which number it
used.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide


class Clock:
    """A clock the test moves by hand.

    The comparison is only taken on a pass where both halves were read
    together, so a clock that drifts between the poll and the snapshot
    means there is no comparison to assert about (see test_pnl_check).
    """

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def engine_with(config, legs, commission=0.0):
    config.settings['COMMISSION_PER_LOT_A'] = commission
    config.settings['COMMISSION_PER_LOT_B'] = commission
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=Clock())
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def one_position(coordinator, pair):
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, SpreadSide.BUY, md['long_spread'])
    assert answer.get('ok'), answer
    coordinator.poll_once()


def check_of(coordinator):
    return coordinator.snapshot()['pnl_check']


def test_commission_is_not_reported_as_a_disagreement_with_MT5(config, pair,
                                                               legs):
    """THE BUG. A commissioned account had the round trip in the
    difference for ever."""
    coordinator = engine_with(config, legs, commission=3.50)
    one_position(coordinator, pair)

    check = check_of(coordinator)

    assert check is not None and check['difference'] is not None
    # The fake broker's own `profit` is 0.0 on a fresh position and our
    # GROSS mark is the round turn of the two books - so the difference
    # is that crossing and NOT a penny of commission.
    assert check['basis'] == 'gross'
    assert check['ours_net'] is not None
    commission = check['ours'] - check['ours_net']
    assert commission > 0, 'the fixture charged no commission at all'
    assert abs(check['difference']) < abs(check['difference'] - commission), \
        'the commission is still inside the difference'


def test_CONTROL_the_net_total_the_trader_reads_is_unchanged(config, pair,
                                                              legs):
    """The control this fix needs most: the P&L on every other panel
    must be the SAME number it was. A comparison fixed by quietly
    changing what the trader is shown would be a worse bug than the
    one it replaced."""
    coordinator = engine_with(config, legs, commission=3.50)
    one_position(coordinator, pair)

    row = coordinator.snapshot()['pairs'][pair.key]['positions'][0]
    check = check_of(coordinator)

    # net_pnl is still gross minus BOTH ends of BOTH legs...
    assert row['net_pnl'] < row['gross_pnl']
    # ...and that is the figure carried beside the comparison.
    assert check['ours_net'] == pytest.approx(row['net_pnl'])
    assert check['ours'] == pytest.approx(row['gross_pnl'])


def test_CONTROL_a_real_disagreement_still_shows_up(config, pair, legs):
    """The row exists to catch a genuine gap. A fix that simply stopped
    the row disagreeing would have deleted the check."""
    coordinator = engine_with(config, legs, commission=0.0)
    one_position(coordinator, pair)

    check = check_of(coordinator)

    # Our mark is the round turn of both books against MT5's 0.00 on a
    # position it has not moved yet. That IS a real difference and it
    # must survive.
    assert check['difference'] is not None
    assert abs(check['difference']) > 0.01


def test_an_unmarkable_position_makes_the_gross_total_unknown_too(config,
                                                                   pair, legs):
    """Unmeasured is not zero, on the new total as much as the old. A
    gross figure that quietly skipped what it could not mark would be
    authoritative-looking and short one position."""
    coordinator = engine_with(config, legs)
    one_position(coordinator, pair)
    coordinator.book.positions(pair.key)[0].entry_spread = None

    check = check_of(coordinator)

    assert check['ours'] is None
    assert check['difference'] is None


def test_CONTROL_a_markable_position_produces_a_number(config, pair, legs):
    coordinator = engine_with(config, legs)
    one_position(coordinator, pair)

    check = check_of(coordinator)

    assert check['ours'] is not None
    assert check['difference'] is not None
