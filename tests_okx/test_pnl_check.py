"""Our open P&L against MT5's own — compared as of ONE moment.

The Positions monitor shows both totals and the gap, and reddens the
row when they disagree by more than a cent. A disagreement means one of
us is wrong about real money, so it must be shown.

But the two halves were read on different clocks. Our marks come off
the tick the poll just read; `account_info` is cached for ~5 seconds.
The row therefore compared a photo taken now against one taken five
seconds ago, and on a moving market they ALWAYS differed by more than a
cent — a desk reported $28.00 in red with nothing actually wrong. A
light that is always on is a light nobody reads, and the day it means
something nobody looks.

The answer is NOT a bigger tolerance. A dollar figure for "market
noise" would be wrong the moment a pair with a different `k` is added —
oil at 1,000/lot drifts ten times faster than gold at 100/lot for the
same tick — and nobody has a sound basis for picking one. Instead the
comparison is only taken on the passes where BOTH halves were read
together, and held between them.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide


def engine(config, legs, clock):
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=clock)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


class Clock:
    """A clock the test moves by hand, so 'the same pass' is exact."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_the_two_halves_are_read_together_or_not_compared(config, pair, legs):
    """THE FAULT. The account profit is cached ~5s; our marks are live.
    A pass that did not re-read the account must not manufacture a
    comparison out of a stale half."""
    clock = Clock()
    coordinator = engine(config, legs, clock)

    first = coordinator.snapshot()['pnl_check']
    assert first is not None and first['at'] == clock.now

    # Move the market and poll again WITHOUT letting the account cache
    # expire — this is the ordinary case, ~15 polls out of every 16.
    clock.now += 1.0
    legs['acct_a'].broker.quote(pair.symbol_a, 4300.00, 4300.20)
    coordinator.poll_once()
    again = coordinator.snapshot()['pnl_check']

    assert again['at'] == first['at'], (
        'a comparison was taken against an account profit read on an '
        'earlier pass — the two halves are from different moments')
    assert again['ours'] == first['ours']
    assert again['theirs'] == first['theirs']


def test_control_a_pass_that_DOES_re_read_takes_a_fresh_comparison(
        config, pair, legs):
    """The control. Without it, 'hold the old answer' would be
    indistinguishable from never comparing again at all."""
    clock = Clock()
    coordinator = engine(config, legs, clock)
    first = coordinator.snapshot()['pnl_check']

    # Past the account cache TTL, so the next snapshot re-reads both.
    clock.now += float(config.get('ACCOUNT_INFO_CACHE_SEC', 5.0)) + 1.0
    coordinator.poll_once()
    later = coordinator.snapshot()['pnl_check']

    assert later['at'] > first['at'], 'it never re-compared'


def test_a_real_disagreement_still_shows_up(config, pair, legs):
    """The row exists to catch a genuine gap. Holding the answer
    between passes must not hide one."""
    clock = Clock()
    coordinator = engine(config, legs, clock)
    coordinator.config.pairs[pair.key].order_type = \
        type(pair.order_type)('MARKET')
    coordinator.click(pair.key, SpreadSide.SELL, None)

    # A position at the broker that our book knows nothing about —
    # exactly what MT5's account profit would carry and ours would not.
    broker = legs['acct_a'].broker
    broker.positions[999999] = {
        'ticket': 999999, 'symbol': pair.symbol_a, 'side': 'BUY',
        'volume': 1.0, 'price_open': 4000.0, 'magic': 0,
        'comment': 'by hand', 'profit': -250.0}

    clock.now += float(config.get('ACCOUNT_INFO_CACHE_SEC', 5.0)) + 1.0
    coordinator.poll_once()
    check = coordinator.snapshot()['pnl_check']

    assert check['ours'] is not None and check['theirs'] is not None
    assert abs(check['difference']) > 0.01, (
        'a position MT5 counts and we do not produced no disagreement')
    assert check['difference'] == pytest.approx(
        check['ours'] - check['theirs'])


def test_an_account_that_cannot_be_read_makes_it_UNMEASURED(
        config, pair, legs):
    """Unmeasured is not zero. An unreadable account does not make the
    difference nil — it makes it unknown, and the screen says so."""
    clock = Clock()
    coordinator = engine(config, legs, clock)

    legs['acct_a'].broker.account_info = lambda: None
    clock.now += float(config.get('ACCOUNT_INFO_CACHE_SEC', 5.0)) + 1.0
    coordinator.poll_once()
    check = coordinator.snapshot()['pnl_check']

    assert check['theirs'] is None
    assert check['difference'] is None


def test_a_position_that_cannot_be_marked_makes_OUR_side_unknown(
        config, pair, legs):
    """The other end of the same rule. The on-screen sum used to skip
    an unmarkable position, so the total read as authoritative while
    being short one."""
    clock = Clock()
    coordinator = engine(config, legs, clock)
    coordinator.config.pairs[pair.key].order_type = \
        type(pair.order_type)('MARKET')
    coordinator.click(pair.key, SpreadSide.SELL, None)

    # No market data for the pair: nothing can be marked.
    coordinator.market[pair.key] = None
    clock.now += float(config.get('ACCOUNT_INFO_CACHE_SEC', 5.0)) + 1.0
    check = coordinator.snapshot()['pnl_check']

    assert check['ours'] is None, 'an unmarkable position was counted as 0'
    assert check['difference'] is None
