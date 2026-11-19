"""Where to get out: break-even, and break-even plus a target.

The trader clicking a spread wants one number back — where do I exit? —
and every figure it is built from is one this system already holds. The
tests here pin the arithmetic and, more importantly, the two rules that
make it safe to read: unmeasured is not zero, and nothing is ever sent
to the broker from it.
"""

import pytest

from okxtrader import takeprofit
from okxtrader.models import SpreadSide


SETTINGS = {'COMMISSION_PER_LOT_A': 2.0, 'COMMISSION_PER_LOT_B': 3.0,
            'TP_TARGET_PCT_OF_MARGIN': 2.0}


def market(short=59.09, long_=59.11):
    return {'short_spread': short, 'long_spread': long_,
            'spread': (short + long_) / 2.0}


def test_break_even_is_the_entry_plus_commission_both_legs_both_ends(pair):
    """The round turn of both legs' bid-ask is already inside the two
    prices being compared — bought at the offer, sold on the bid. What
    is left to recover is commission."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=None, quantity=1.0,
                               spread_units=10.0)

    # 2 x (2.00 x 0.1 + 3.00 x 0.1) = 1.00, over k = 10 -> 0.10 of spread.
    assert body['break_even_buy'] == pytest.approx(59.21)
    assert body['break_even_sell'] == pytest.approx(58.99)


def test_the_target_is_a_percentage_of_the_margin_the_spread_ties_up(pair):
    """Margin is what the trade actually costs to hold, per account and
    per broker — so the target is a return on THAT, not on notional."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=500.0, quantity=1.0,
                               spread_units=10.0)

    # 2% of 500 = 10.00, over k = 10 -> 1.00 of spread, on top of B/E.
    assert body['target_money'] == pytest.approx(10.0)
    assert body['tp_buy'] == pytest.approx(60.21)
    assert body['tp_sell'] == pytest.approx(57.99)


def test_the_two_directions_move_opposite_ways(pair):
    """A long leaves on the bid ABOVE what it paid; a short buys back
    BELOW what it sold. One number here would be right half the time."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=500.0, spread_units=10.0)

    assert body['tp_buy'] > body['break_even_buy'] > market()['long_spread']
    assert body['tp_sell'] < body['break_even_sell'] < market()['short_spread']


def test_without_a_margin_figure_there_is_a_break_even_and_no_target(pair):
    """Unmeasured is not zero. A TP of 0.00 would read as "get out at
    break-even", which is a different instruction — so the target is
    absent and the box says what is missing."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=None, spread_units=10.0)

    assert body['break_even_buy'] is not None
    assert body['tp_buy'] is None and body['tp_sell'] is None
    assert 'margin' in body['note']


def test_a_zero_target_shows_break_even_alone(pair):
    """The control for the case above: a desk that wants break-even and
    nothing else sets the percentage to zero, and that is not the same
    as a target nobody could compute."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    settings = dict(SETTINGS, TP_TARGET_PCT_OF_MARGIN=0.0)

    body = takeprofit.describe(pair, market(), settings,
                               margin_per_spread=500.0, spread_units=10.0)

    assert body['break_even_buy'] is not None
    assert body['tp_buy'] is None
    assert 'Settings' in body['note']


def test_a_position_is_anchored_on_what_it_was_entered_at(pair, config, legs):
    """A take-profit that moves with the market is not a take-profit."""
    from okxtrader.executor import PairExecutor
    from okxtrader.coordinator import _meta_from_report
    pair.meta_a = _meta_from_report(legs['acct_a'].symbol_report(pair.symbol_a))
    pair.meta_b = _meta_from_report(legs['acct_b'].symbol_report(pair.symbol_b))
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    executor = PairExecutor(config, legs, sleep=lambda s: None)
    from okxtrader.spread import compute_spread
    md = compute_spread(pair, legs['acct_a'].tick(pair.symbol_a),
                        legs['acct_b'].tick(pair.symbol_b), pair.hedge_ratio)
    position = executor.market_entry(pair, SpreadSide.BUY, md, 1.0).position

    body = takeprofit.for_position(position, md, pair, SETTINGS,
                                   margin_per_spread=500.0)

    assert body['side'] == 'BUY'
    assert body['break_even'] > position.entry_spread     # commission
    assert body['tp'] > body['break_even']                # the target
    # And it does NOT move when the market does.
    legs['acct_b'].broker.symbols[pair.symbol_b].bid += 5.0
    again = takeprofit.for_position(position, md, pair, SETTINGS,
                                    margin_per_spread=500.0)
    assert again['tp'] == pytest.approx(body['tp'])


def test_the_engine_prices_the_margin_from_both_terminals(config, pair, legs):
    """One spread ties up margin on BOTH accounts, and each terminal
    prices its own — leverage, margin mode and account group are the
    broker's, not something to derive here."""
    from okxtrader.coordinator import Coordinator
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = 2000.0
    coordinator = Coordinator(config, legs)
    coordinator.start()

    per_spread = coordinator.margin_per_spread(pair)

    # 0.1 lots a side: 300 + 200.
    assert per_spread == pytest.approx(500.0)


def test_a_leg_the_terminal_cannot_price_falls_back_to_ITS_OWN_leverage(
        config, pair, legs):
    """Not one leverage for both: each leg's notional over the leverage
    of the account that leg trades on. And the fallback is LABELLED —
    a target computed off a different base looks like a considered
    number."""
    from okxtrader.coordinator import Coordinator
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = None
    coordinator = Coordinator(config, legs)
    coordinator.start()
    coordinator.poll_once()

    detail = coordinator.margin_detail(pair)

    assert detail['source'] == 'leverage'
    # 300 from the terminal, plus 0.1 x 100 x 4351.40 / 100 from leverage.
    assert detail['money'] == pytest.approx(300.0 + 435.14, abs=0.5)
    assert 'leverage' in detail['note']


def test_no_leverage_either_means_NO_target_rather_than_another_base(
        config, pair, legs):
    """The control, and the rule: a target on a base nobody set is
    worse than no target — it looks considered. Break-even, which needs
    no margin at all, still stands."""
    from okxtrader.coordinator import Coordinator
    legs['acct_a'].broker.margin_per_lot = 3000.0
    legs['acct_b'].broker.margin_per_lot = None
    for leg in legs.values():
        leg.broker.leverage = None
    coordinator = Coordinator(config, legs)
    coordinator.start()

    assert coordinator.margin_per_spread(pair) is None

    coordinator.poll_once()
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['exit']['tp_buy'] is None
    assert row['exit']['break_even_buy'] is not None
    assert 'leverage' in row['exit']['note'] or \
        'margin' in row['exit']['note']


# -- the four terms break-even is built from (spec §5.2) -----------------


def test_the_bid_ask_round_trip_is_shown_and_NOT_charged_twice(pair):
    """It is one round turn of both books, and it is already inside the
    two prices being compared: entered at the offer, closed on the bid.
    Adding it to a break-even quoted on the closing side is the bid-ask
    charged twice — the exact fault the cost model was rewritten for.
    """
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    md = market(short=59.09, long_=59.11)

    body = takeprofit.describe(pair, md, SETTINGS, spread_units=10.0)

    assert body['spread_width'] == pytest.approx(0.02)      # shown
    assert body['spread_width_money'] == pytest.approx(0.20)
    # ...and the break-even moved by the COMMISSION only.
    assert body['break_even_buy'] == pytest.approx(59.11 + 0.10)


def test_reaching_break_even_books_exactly_zero(pair):
    """The assertion the whole panel stands on. A position entered at a
    real fill and marked at the CLOSING touch has already paid both
    crossings — they are in the two prices — so the only fee still to
    subtract is commission. Land the closing touch on break-even and
    the net is zero, to the cent."""
    from okxtrader.executor import mark_position
    from okxtrader.models import LegFill, SpreadPosition
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS, spread_units=10.0)
    fill_a = LegFill(account='acct_a', symbol='XAUUSD_', side='SELL',
                     volume=0.1, price=4292.00, contract_size=100.0)
    fill_b = LegFill(account='acct_b', symbol='GC1226', side='BUY',
                     volume=0.1, price=4351.11, contract_size=100.0)
    entered = SpreadPosition(
        pair_key=pair.key, side=SpreadSide.BUY, quantity=1.0,
        leg_a_fill=fill_a, leg_b_fill=fill_b, entry_spread=59.11,
        order_type='MARKET', spread_units=10.0)

    # The market comes to exactly the break-even BID.
    at_be = {'short_spread': body['break_even_buy'],
             'long_spread': body['break_even_buy'] + 0.02,
             'spread': body['break_even_buy'] + 0.01}
    gross, net, closing = mark_position(entered, at_be, SETTINGS)

    assert closing == pytest.approx(body['break_even_buy'])
    assert net == pytest.approx(0.0, abs=1e-9)


def test_the_slippage_allowance_is_a_budget_and_widens_break_even(pair):
    """Different in kind from the other three terms: it is a BUDGET,
    not a measurement. It defaults to 0 — a fabricated cost is charged
    against every trade and the operator cannot tell it was never their
    number — and when set it moves both break-evens apart."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    settings = dict(SETTINGS, SLIPPAGE_ALLOWANCE=2.0)

    body = takeprofit.describe(pair, market(), settings, spread_units=10.0)

    assert body['slippage_allowance'] == pytest.approx(2.0)
    # 1.00 commission + 2.00 allowance over k = 10 -> 0.30 of spread.
    assert body['break_even_buy'] == pytest.approx(59.41)
    assert body['break_even_sell'] == pytest.approx(58.79)
    # The control: the default charges nothing.
    plain = takeprofit.describe(pair, market(), SETTINGS, spread_units=10.0)
    assert plain['break_even_buy'] == pytest.approx(59.21)


def test_swap_over_nights_widens_break_even_and_a_credit_narrows_it(pair):
    """Break-even is only DEFINED given a holding period, because the
    swap is charged per night. And the sign is kept: being PAID to hold
    brings break-even NEARER, which is the case worth finding."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    charged = takeprofit.describe(
        pair, market(), SETTINGS, spread_units=10.0, nights=3,
        carry_buy={'money': -3.0}, carry_sell={'money': -3.0})
    credited = takeprofit.describe(
        pair, market(), SETTINGS, spread_units=10.0, nights=3,
        carry_buy={'money': +3.0}, carry_sell={'money': +3.0})

    # 1.00 commission + 3.00 of swap over k = 10 -> 0.40.
    assert charged['break_even_buy'] == pytest.approx(59.51)
    # 1.00 commission - 3.00 credited -> -0.20: break-even BELOW the ask.
    assert credited['break_even_buy'] == pytest.approx(58.91)


def test_intraday_is_the_default_and_the_swap_term_vanishes(pair):
    """0 nights, no swap, no refusal — the ordinary case must not need
    a carry conversion to quote a break-even at all."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(pair, market(), SETTINGS, spread_units=10.0,
                               nights=0)

    assert body['swap_money'] == 0.0
    assert body['break_even_buy'] == pytest.approx(59.21)


def test_an_unconvertible_swap_over_nights_refuses_rather_than_dropping_it(
        pair):
    """An unconvertible swap is not a zero swap. Over a holding period
    the term is REAL and unknown, so break-even says so instead of
    quoting a number that silently left the financing out."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    body = takeprofit.describe(
        pair, market(), SETTINGS, spread_units=10.0, nights=5,
        carry_buy={'money': None, 'reason': 'GC1226: the broker did not '
                                            'report swap_mode'},
        carry_sell={'money': None, 'reason': 'GC1226: no swap_mode'})

    assert body['break_even_buy'] is None
    assert 'swap_mode' in body['note']


def test_the_break_even_side_is_named(pair):
    """3.1600 looks identical to the ask sitting two rows above it. The
    number is unambiguous once the side is named and dangerously
    ambiguous without it."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    body = takeprofit.describe(pair, market(), SETTINGS, spread_units=10.0)
    assert body['break_even_side_buy'] == 'bid'
    assert body['break_even_side_sell'] == 'ask'


def test_the_measured_round_trip_can_be_overridden_and_the_override_cleared(
        pair):
    """Blank means "use the measured value". An override that cannot be
    deleted outlives the pair it was typed for."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    md = market(short=59.09, long_=59.11)

    typed = takeprofit.describe(pair, md,
                                dict(SETTINGS,
                                     BID_ASK_ROUND_TRIP_OVERRIDE=5.0),
                                spread_units=10.0)
    assert typed['spread_width_money'] == pytest.approx(5.0)
    assert typed['spread_width'] == pytest.approx(0.50)

    cleared = takeprofit.describe(pair, md,
                                  dict(SETTINGS,
                                       BID_ASK_ROUND_TRIP_OVERRIDE=''),
                                  spread_units=10.0)
    assert cleared['spread_width'] == pytest.approx(0.02)    # measured again


def test_the_target_states_its_distance_and_the_range_it_is_measured_against(
        pair):
    """At small size a percentage of margin can be far outside anything
    the pair moves in a session — 1% of a $1,220 margin once came to
    6.65 of spread on a pair that never travelled that far. The
    arithmetic is honest; whether it is REACHABLE is the trader's call,
    and they can only make it with the range on the screen."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1

    far = takeprofit.describe(pair, market(), SETTINGS,
                              margin_per_spread=500.0, spread_units=10.0,
                              session_range=0.30)
    near = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=500.0, spread_units=10.0,
                               session_range=4.00)

    assert far['target_points'] == pytest.approx(1.00)
    assert far['target_reachable'] is False
    assert near['target_reachable'] is True
    # The derivation is stated, in both units and against the range.
    assert '2% of 500.00' in far['note']
    assert '1.0000 of spread' in far['note']
    assert '0.3000 session range' in far['note']


def test_with_no_range_yet_the_target_says_nothing_about_reachability(pair):
    """Unmeasured is not "unreachable"."""
    pair.clip_lots_a = pair.clip_lots_b = 0.1
    body = takeprofit.describe(pair, market(), SETTINGS,
                               margin_per_spread=500.0, spread_units=10.0)
    assert body['target_reachable'] is None
