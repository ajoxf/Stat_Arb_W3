"""Getting a pair on and off again — the part that must be exactly right
before anything is allowed to rest.
"""

import pytest

from okxtrader.executor import PairExecutor, mark_position, slippage
from okxtrader.models import OrderSide, SpreadSide
from okxtrader.spread import compute_spread


def snapshot(pair, legs):
    tick_a = legs['acct_a'].tick(pair.symbol_a)
    tick_b = legs['acct_b'].tick(pair.symbol_b)
    return compute_spread(pair, tick_a, tick_b, pair.hedge_ratio)


def resolved(pair, legs):
    """The metadata the coordinator reads out of MT5 at startup."""
    from okxtrader.coordinator import _meta_from_report
    pair.meta_a = _meta_from_report(
        legs['acct_a'].symbol_report(pair.symbol_a))
    pair.meta_b = _meta_from_report(
        legs['acct_b'].symbol_report(pair.symbol_b))
    pair.clip_lots_a, pair.clip_lots_b = 0.1, 0.1
    return pair


def test_a_market_click_puts_both_legs_on(config, pair, legs):
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)

    result = executor.market_entry(pair, SpreadSide.SELL, md, 1.0,
                                   md['short_spread'])

    assert result.ok, result.reason
    position = result.position
    # SELL the spread = sell B, buy A.
    assert position.leg_a.side is OrderSide.BUY
    assert position.leg_b.side is OrderSide.SELL
    # Entry is anchored on the EXECUTED fills, and those are the two
    # touches a short crosses — so it IS the short spread.
    assert position.entry_spread == pytest.approx(md['short_spread'])
    assert position.entry_slippage == pytest.approx(0.0)
    assert position.click_to_on_ms is not None


def test_the_harder_to_fill_leg_goes_first(config, pair, legs, timeline):
    """Filling the easy leg and then discovering the hard one will not
    fill is how you end up naked. Leg B's minimum is ten times leg A's."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    assert executor.crossing_order(pair) == ('b', 'a')

    executor.market_entry(pair, SpreadSide.BUY, snapshot(pair, legs), 1.0)
    arrived = [e['symbol'] for e in timeline if e['action'] == 'market']
    assert arrived == [pair.symbol_b, pair.symbol_a]


def test_a_rejected_crossing_leg_unwinds_by_ticket_and_never_offsets(
        config, pair, legs):
    """These accounts are HEDGING mode: an opposite market order opens a
    SECOND position and leaves the first live. That has happened on a
    real account — the reconciler found both of them 60 seconds later.
    """
    resolved(pair, legs)
    legs['acct_a'].broker.reject_orders[pair.symbol_a] = \
        '10027 - AutoTrading disabled by client'
    executor = PairExecutor(config, legs, sleep=lambda s: None)

    result = executor.market_entry(pair, SpreadSide.SELL,
                                   snapshot(pair, legs), 1.0)

    assert not result.ok
    # The broker's own words reach the ladder, not "check the log".
    assert '10027' in result.reason
    # Leg B's book is EMPTY, not "flat by offsetting".
    assert legs['acct_b'].broker.open_positions() == []
    actions = [e['action'] for e in legs['acct_b'].broker.sent]
    assert actions == ['market', 'close']       # never a second 'market'
    assert result.naked is None


def test_an_unwind_that_fails_leaves_a_naked_banner_not_a_silent_gap(
        config, pair, legs):
    resolved(pair, legs)
    legs['acct_a'].broker.reject_orders[pair.symbol_a] = 'no margin'
    legs['acct_b'].broker.fail_closes.add(pair.symbol_b)   # unwind fails too
    executor = PairExecutor(config, legs, sleep=lambda s: None)

    result = executor.market_entry(pair, SpreadSide.SELL,
                                   snapshot(pair, legs), 1.0)

    assert not result.ok
    assert result.naked and result.naked['leg'] == 'B'
    assert result.naked['volume'] == pytest.approx(0.1)
    assert legs['acct_b'].broker.open_positions()          # still on


def test_flat_and_back_to_flat_costs_exactly_one_round_turn(
        config, pair, legs):
    """Enter at short_spread, exit at long_spread, nothing moves in
    between: the pair is down EXACTLY the modelled round trip, no more.
    Charging the crossing twice would double it."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)

    result = executor.market_entry(pair, SpreadSide.SELL, md, 1.0)
    position = result.position
    executor.close_position(pair, position, md, reason='test')

    expected = -md['spread_cost'] * position.spread_units * position.quantity
    assert position.realized_pnl == pytest.approx(expected)
    # And both books are actually empty — closed, not offset.
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []


def test_a_position_shows_that_round_turn_as_a_loss_the_instant_it_opens(
        config, pair, legs):
    """Marked where it would actually CLOSE. A mid mark shows a profit
    that cannot be taken."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)
    result = executor.market_entry(pair, SpreadSide.BUY, md, 1.0)

    gross, net, closing = mark_position(result.position, md, config.settings)
    assert closing == md['short_spread']
    assert gross == pytest.approx(
        -md['spread_cost'] * result.position.spread_units)
    assert net == pytest.approx(gross)      # no commission configured


def test_market_protection_refuses_a_fill_through_the_clicked_price(
        config, pair, legs):
    """A market order on a ladder is market-WITH-PROTECTION. Without it
    a click fills at whatever the touch happens to be."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)
    clicked = md['long_spread'] - 10 * pair.increment   # 10 ticks better

    result = executor.market_entry(pair, SpreadSide.BUY, md, 1.0, clicked)

    assert result.refused and not result.ok
    assert 'protection' in result.reason
    assert legs['acct_a'].broker.sent == []      # nothing was sent
    assert legs['acct_b'].broker.sent == []


def test_a_click_within_protection_still_goes(config, pair, legs):
    """The control: the guard must not refuse everything."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)
    clicked = md['long_spread'] - 1 * pair.increment

    result = executor.market_entry(pair, SpreadSide.BUY, md, 1.0, clicked)
    assert result.ok, result.reason


def test_a_click_is_refused_before_anything_moves_when_a_leg_is_missing(
        config, pair, legs):
    resolved(pair, legs)
    executor = PairExecutor(config, {'acct_a': legs['acct_a']},
                            sleep=lambda s: None)
    result = executor.market_entry(pair, SpreadSide.BUY,
                                   snapshot(pair, legs), 1.0)
    assert result.refused
    assert "acct_b" in result.reason
    assert legs['acct_a'].broker.sent == []


def test_slippage_is_positive_when_it_costs_at_both_ends():
    """A short BUYS the spread back, so the sign flips between entry and
    exit. Getting that backwards reports every exit's cost as a gain."""
    # Entry: sold at 55.80 where 55.93 was showing -> it cost 0.13.
    assert slippage(55.93, 55.80, SpreadSide.SELL) == pytest.approx(0.13)
    # Exit of that short: bought back at 56.10 against 55.97 -> also 0.13.
    assert slippage(55.97, 56.10, SpreadSide.SELL,
                    closing=True) == pytest.approx(0.13)
    # Unmeasured is not zero.
    assert slippage(None, 56.10, SpreadSide.SELL) is None


def test_the_exit_is_measured_against_the_price_the_close_was_decided_at(
        config, pair, legs):
    """Slippage exists at the EXIT too, and this is where it was being
    lost: an exit marked at the touch the close was decided at reports
    every close as a perfect fill, whatever the broker gave us.

    Here the market moves against the close between the decision and the
    fill, and the report has to show that as a cost.
    """
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)
    position = executor.market_entry(pair, SpreadSide.BUY, md, 1.0).position

    # A BUY spread is closed by SELLING leg B at its bid. The bid drops
    # ten ticks between the click and the fill.
    symbol = legs['acct_b'].broker.symbols[pair.symbol_b]
    symbol.bid -= 0.10

    executor.close_position(pair, position, md, reason='test')

    assert position.exit_slippage == pytest.approx(0.10)   # positive: a cost
    # ...and the P&L is anchored on the price it actually got, not on
    # the touch it aimed at.
    assert position.exit_spread == pytest.approx(
        md['short_spread'] - 0.10)


def test_an_exit_that_gets_the_price_it_asked_for_measures_zero_not_none(
        config, pair, legs):
    """The control. A measured 0.00 and an unmeasured None mean
    different things, and the report counts them in different columns."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    md = snapshot(pair, legs)
    position = executor.market_entry(pair, SpreadSide.BUY, md, 1.0).position

    executor.close_position(pair, position, md, reason='test')

    assert position.exit_slippage == pytest.approx(0.0)


def test_a_close_the_broker_did_not_price_is_unmeasured_not_zero():
    """Half a spread is not a spread. With one leg's close price
    missing, there is no exit price to report and no slippage to
    measure — and neither is invented."""
    from okxtrader.executor import closed_spread

    priced = {'a': {'closed': [{'volume': 0.1, 'price': 100.0}]},
              'b': {'closed': [{'volume': 0.1, 'price': 105.0}]}}
    assert closed_spread(priced, 1.0) == pytest.approx(5.0)

    priced['a']['closed'][0]['price'] = None
    assert closed_spread(priced, 1.0) is None


def test_a_ticket_closed_in_pieces_is_volume_weighted():
    """A partial close at one price and the rest at another is one exit
    at the weighted average — not at whichever piece went last."""
    from okxtrader.executor import closed_spread

    results = {'a': {'closed': [{'volume': 0.1, 'price': 100.0},
                                {'volume': 0.3, 'price': 104.0}]},
               'b': {'closed': [{'volume': 0.4, 'price': 110.0}]}}
    assert closed_spread(results, 1.0) == pytest.approx(110.0 - 103.0)


# -- a PART close is a volume both brokers can actually trade -------------

def test_a_part_close_never_asks_for_a_volume_the_broker_cannot_trade(
        config, pair, legs, timeline):
    """A share of a position is not a volume.

    Leg lots come in steps — 0.01 on the spot, 0.10 on the future — and
    the pro-rata piece went to the broker raw. 0.05 lots of a future
    whose step is 0.10 is rejected outright, so the SPOT leg closed and
    the future did not: a part-close that leaves the pair imbalanced at
    the broker, with our own book still showing it whole.
    """
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    entered = executor.market_entry(pair, SpreadSide.SELL,
                                    snapshot(pair, legs), 1.0)
    position = entered.position
    assert position.leg_b.volume == pytest.approx(0.1)   # one step exactly
    del timeline[:]

    # Half of it. The future cannot trade 0.05.
    answer = executor.close_position(pair, position,
                                     snapshot(pair, legs), quantity=0.5)

    assert answer['ok'] is False
    assert 'under the 0.1 lots that broker can trade' in answer['reason']
    assert pair.symbol_b in answer['reason']
    # NOTHING was sent. A close that cannot be halved must not half-close
    # it — one leg off and one leg on is worse than neither.
    assert [e for e in timeline if e['action'] == 'close'] == []
    assert position.is_open and position.quantity == pytest.approx(1.0)


def test_the_control_a_part_close_both_legs_can_trade_goes_through(
        config, pair, legs, timeline):
    """Without this the test above would pass on a build that had simply
    stopped part-closing anything."""
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    # Ten spreads: 1.0 lot on each leg, so half of it is 0.5 — a whole
    # number of steps on both.
    position = executor.market_entry(pair, SpreadSide.SELL,
                                     snapshot(pair, legs), 10.0).position
    assert position.leg_b.volume == pytest.approx(1.0)
    del timeline[:]

    answer = executor.close_position(pair, position, snapshot(pair, legs),
                                     quantity=5.0)

    assert answer['ok'] and answer['partial']
    sent = {e['symbol']: e['volume'] for e in timeline
            if e['action'] == 'close'}
    assert sent[pair.symbol_a] == pytest.approx(0.5)
    assert sent[pair.symbol_b] == pytest.approx(0.5)
    assert position.is_open
    assert position.quantity == pytest.approx(5.0)


def test_a_piece_one_leg_must_round_down_is_matched_on_the_other(
        config, pair, legs, timeline):
    """Both legs come off on ONE share, or the piece is not hedged.

    The future rounds 0.63 lots down to 0.60; if the spot still closed
    its own 0.63 the position left behind would be imbalanced by the
    difference, and nothing in the book would say so.
    """
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    position = executor.market_entry(pair, SpreadSide.SELL,
                                     snapshot(pair, legs), 10.0).position
    del timeline[:]

    executor.close_position(pair, position, snapshot(pair, legs),
                            quantity=6.3)

    sent = {e['symbol']: e['volume'] for e in timeline
            if e['action'] == 'close'}
    # 0.60 on both, not 0.63 and 0.60.
    assert sent[pair.symbol_b] == pytest.approx(0.6)
    assert sent[pair.symbol_a] == pytest.approx(0.6)


# --- A leg that went on and could not come off --------------------------
#
#     A trader clicked, ONE LEG EXECUTED, and the screen said the click
#     was refused. Forty-four seconds later the reconciler closed the
#     leg that was on.
#
#     The partial-match branch unwinds both legs and threw the answers
#     away. When an unwind failed, nothing said so: no naked flag, no
#     banner, nothing above WARNING in a log that did not reach disk.
#     The branch beside it - where the second leg is REJECTED - has
#     always reported it.


def _partly_filled(config, pair, legs, monkeypatch):
    """Leg A on, leg B not: the shape of the trader's click.

    Forced through the MATCHED FRACTION, because that is the branch
    that was silent - a leg B that half-fills is not a rejection, so
    the code above this never runs.
    """
    resolved(pair, legs)
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    monkeypatch.setattr(executor, '_matched_fraction', lambda *a, **k: 0.0)
    return executor


def test_a_leg_that_cannot_be_unwound_is_reported_as_naked(
        config, pair, legs, monkeypatch):
    """The whole point. A trader holding an unhedged leg must be told -
    never left to find out when something else tidies it away."""
    executor = _partly_filled(config, pair, legs, monkeypatch)
    monkeypatch.setattr(executor, '_unwind_leg',
                        lambda *a, **k: {'ok': False, 'error': 'refused'})

    result = executor.market_entry(pair, SpreadSide.BUY,
                                   snapshot(pair, legs), 1.0)

    assert result.ok is False
    assert result.naked, 'a leg is on at the broker and nothing said so'
    assert result.naked['leg'] in ('A', 'B')
    assert result.naked['why'] == 'refused'


def test_control_an_unwind_that_works_reports_nothing_naked(
        config, pair, legs, monkeypatch):
    """The control. A refusal that really did undo itself must not
    raise a naked alarm - a banner that cries wolf is one traders
    learn to ignore."""
    executor = _partly_filled(config, pair, legs, monkeypatch)
    monkeypatch.setattr(executor, '_unwind_leg', lambda *a, **k: {'ok': True})

    result = executor.market_entry(pair, SpreadSide.BUY,
                                   snapshot(pair, legs), 1.0)

    assert result.ok is False
    assert result.naked is None


def test_a_leg_that_never_filled_is_not_called_naked(
        config, pair, legs, monkeypatch):
    """Asking to undo a leg that never went on produced 'filled but
    reported no position ticket'. That is not a naked leg, and
    reporting it as one would put a critical banner on the screen for
    a click that placed nothing."""
    executor = _partly_filled(config, pair, legs, monkeypatch)
    asked = []

    def record(pair_, leg, side, fill):
        asked.append(leg)
        return {'ok': False, 'error': 'no position ticket'}

    monkeypatch.setattr(executor, '_unwind_leg', record)
    monkeypatch.setattr(executor, '_send_leg',
                        lambda *a, **k: {'ok': True, 'filled_volume': 0.0,
                                         'price': 100.0, 'ticket': None,
                                         'position_tickets': []})

    result = executor.market_entry(pair, SpreadSide.BUY,
                                   snapshot(pair, legs), 1.0)

    assert result.ok is False
    assert asked == [], 'an unfilled leg was sent for unwinding'
    assert result.naked is None
