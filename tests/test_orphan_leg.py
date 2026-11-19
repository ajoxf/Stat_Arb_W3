"""The orphan leg: a click that put one leg on and reported a refusal.

Reported by two desks on 2026-09-09. Leg A executed, leg B was never
sent, and the position turned up in the reconciler as an unclaimed
ticket carrying our own comment - which nothing is allowed to close, so
there was no way off it from the screen.

ONE FAULT, IN THREE LAYERS:

  * `BrokerSession.send_market_order` compared the retcode against
    `TRADE_RETCODE_DONE` alone. MT5 answers 10010 DONE_PARTIAL when it
    fills PART of a market order, and a position is open for the part
    that filled. That was reported upwards as a flat failure, with no
    ticket and no volume.
  * `LocalLeg.order` zeroed `filled_volume` and `position_tickets`
    whenever the broker said no, so even a ticket that HAD come back
    was thrown away.
  * `PairExecutor.market_entry` read a failed FIRST leg as "nothing is
    on. This is a refusal, not a naked position", returned, and never
    sent the second leg or unwound the first.

Each layer is tested here with its control, because a guard that fires
either way is not a guard.
"""

import types

import pytest

from okxtrader.executor import PairExecutor
from okxtrader.legs import LocalLeg
from okxtrader.models import OrderSide, SpreadSide
from okxtrader.spread import compute_spread


def snapshot(pair, legs):
    return compute_spread(pair, legs['acct_a'].tick(pair.symbol_a),
                          legs['acct_b'].tick(pair.symbol_b),
                          pair.hedge_ratio)


def resolved(pair, legs):
    from okxtrader.coordinator import _meta_from_report
    pair.meta_a = _meta_from_report(
        legs['acct_a'].symbol_report(pair.symbol_a))
    pair.meta_b = _meta_from_report(
        legs['acct_b'].symbol_report(pair.symbol_b))
    pair.clip_lots_a, pair.clip_lots_b = 0.1, 0.1
    return pair


# -- layer 1: the broker's own retcodes ---------------------------------



















# -- layer 2: the leg reports what is AT THE BROKER ---------------------

def test_a_refused_order_still_reports_the_position_it_left(legs, pair):
    """`ok` is the broker's verdict on the REQUEST. The volume and the
    tickets answer a different question - is anything of ours on? - and
    zeroing them on a refusal is what left the unwind with nothing to
    close."""
    broker = legs['acct_a'].broker
    broker.refuse_but_fill[pair.symbol_a] = (0.04, '10010 - partial')
    leg = LocalLeg(broker)

    answer = leg.order(pair.symbol_a, 'BUY', 0.1)

    assert answer['ok'] is False, "the broker's refusal must still stand"
    assert answer['filled_volume'] == pytest.approx(0.04)
    assert answer['position_tickets'], 'nothing to unwind by ticket'
    assert broker.open_positions(pair.symbol_a), 'the fake models nothing'


def test_control_a_clean_refusal_reports_nothing_on(legs, pair):
    """A refusal that left nothing behind must NOT look like a naked
    leg, or every rejected click raises the loudest alarm on the
    screen."""
    broker = legs['acct_a'].broker
    broker.reject_orders[pair.symbol_a] = '10027 - AutoTrading disabled'
    leg = LocalLeg(broker)

    answer = leg.order(pair.symbol_a, 'BUY', 0.1)

    assert answer['ok'] is False
    assert answer['filled_volume'] == 0.0
    assert answer['position_tickets'] == []


# -- layer 3: the executor, which is the safety net ---------------------

def first_leg_of(pair, legs, config):
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    first, _second = executor.crossing_order(pair)
    return executor, first


def test_a_first_leg_that_is_refused_AND_ON_is_never_called_a_refusal(
        config, pair, legs):
    """THE REPORTED FAULT. The click said 'refused', leg B was never
    sent, and the leg that went on was found by the reconciler."""
    resolved(pair, legs)
    executor, first = first_leg_of(pair, legs, config)
    symbol = pair.symbol_a if first == 'a' else pair.symbol_b
    account = 'acct_a' if first == 'a' else 'acct_b'
    legs[account].broker.refuse_but_fill[symbol] = (0.04, '10010 - partial')

    result = executor.market_entry(pair, SpreadSide.SELL,
                                   snapshot(pair, legs), 1.0)

    assert not result.ok
    assert not result.refused, (
        'a click that put a position on was reported as having moved '
        'nothing — the exact fault')
    # It came off again, so nothing is naked and nothing is left for the
    # reconciler to find.
    assert result.naked is None, result.naked
    assert legs[account].broker.open_positions(symbol) == []


def test_control_a_first_leg_refused_with_nothing_on_IS_a_refusal(
        config, pair, legs):
    """The control. Without it the test above passes on a build that
    calls every failure naked and unwinds positions that do not
    exist."""
    resolved(pair, legs)
    executor, first = first_leg_of(pair, legs, config)
    symbol = pair.symbol_a if first == 'a' else pair.symbol_b
    account = 'acct_a' if first == 'a' else 'acct_b'
    legs[account].broker.reject_orders[symbol] = '10027 - AutoTrading disabled'

    result = executor.market_entry(pair, SpreadSide.SELL,
                                   snapshot(pair, legs), 1.0)

    assert not result.ok
    assert result.refused, 'nothing moved, so nothing to unwind'
    assert result.naked is None
    assert '10027' in result.reason


def test_a_refused_first_leg_that_will_not_come_off_is_reported_NAKED(
        config, pair, legs):
    """The worst case, and the one the trader is owed a banner for: it
    went on under a refusal AND the unwind was refused too."""
    resolved(pair, legs)
    executor, first = first_leg_of(pair, legs, config)
    symbol = pair.symbol_a if first == 'a' else pair.symbol_b
    account = 'acct_a' if first == 'a' else 'acct_b'
    legs[account].broker.refuse_but_fill[symbol] = (0.04, '10010 - partial')
    legs[account].broker.fail_closes.add(symbol)

    result = executor.market_entry(pair, SpreadSide.SELL,
                                   snapshot(pair, legs), 1.0)

    assert not result.ok and not result.refused
    assert result.naked, 'a leg is on at the broker and nothing said so'
    assert result.naked['leg'] == first.upper()
    assert result.naked['volume'] == pytest.approx(0.04)
    assert result.naked['tickets'], 'no ticket to close it by hand with'
    assert legs[account].broker.open_positions(symbol), \
        'the fake models nothing'


def test_the_crossing_leg_does_not_retry_on_top_of_a_fill(
        config, pair, legs, timeline):
    """The deadline retries a crossing leg that FAILED. A failure that
    left a position on must stop it dead: the next attempt would put a
    second order on top of the piece already there, and the deadline
    allows a great many of them."""
    resolved(pair, legs)
    executor, first = first_leg_of(pair, legs, config)
    second = 'b' if first == 'a' else 'a'
    symbol = pair.symbol_a if second == 'a' else pair.symbol_b
    account = 'acct_a' if second == 'a' else 'acct_b'
    legs[account].broker.refuse_but_fill[symbol] = (0.02, '10010 - partial')

    executor.market_entry(pair, SpreadSide.SELL, snapshot(pair, legs), 1.0)

    sent = [e for e in legs[account].broker.sent
            if e['action'] == 'market' and e['symbol'] == symbol]
    assert len(sent) == 1, (
        f'it sent {len(sent)} orders on a leg that was already partly on')


def test_control_a_crossing_leg_with_nothing_on_still_retries(
        config, pair, legs):
    """Without this the stop above would be indistinguishable from a
    deadline that never retries at all — and the retry is what covers a
    broker that briefly will not answer."""
    resolved(pair, legs)
    executor, first = first_leg_of(pair, legs, config)
    second = 'b' if first == 'a' else 'a'
    symbol = pair.symbol_a if second == 'a' else pair.symbol_b
    account = 'acct_a' if second == 'a' else 'acct_b'
    legs[account].broker.reject_orders[symbol] = 'no answer'
    # A clock that creeps, so the deadline is reached by attempts and
    # not by one jump past it.
    now = [0.0]

    def creeping():
        now[0] += 0.005
        return now[0]

    executor.clock = creeping

    executor.market_entry(pair, SpreadSide.SELL, snapshot(pair, legs), 1.0)

    sent = [e for e in legs[account].broker.sent
            if e['action'] == 'market' and e['symbol'] == symbol]
    assert len(sent) > 1, 'the deadline never retried anything'


def test_both_legs_come_off_when_the_second_is_rejected_after_dealing(
        config, pair, legs):
    """A rejected crossing leg can have dealt part of itself first.
    Unwinding only the leg we believe is on leaves that piece at the
    broker — the orphan, one leg over."""
    resolved(pair, legs)
    executor, first = first_leg_of(pair, legs, config)
    second = 'b' if first == 'a' else 'a'
    symbol = pair.symbol_a if second == 'a' else pair.symbol_b
    account = 'acct_a' if second == 'a' else 'acct_b'
    legs[account].broker.refuse_but_fill[symbol] = (0.02, '10010 - partial')

    result = executor.market_entry(pair, SpreadSide.SELL,
                                   snapshot(pair, legs), 1.0)

    assert not result.ok
    assert result.naked is None, result.naked
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []
