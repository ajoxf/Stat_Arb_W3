"""What one Qty means on each leg — now that the trader types both.

QTY IS THE LOTS. `clip_lots_a` and `clip_lots_b` are what ONE unit of
the Qty box is on each leg, and both are typed: at 1 and 1 a Qty of 100
is 100 lots of leg A against 100 of leg B; at 1 and 2 it is 100 against
200. Nothing is derived and nothing is matched on the desk's behalf.

The hedge arithmetic that used to compute leg B is gone with it, and so
is the guarantee it bought. `k = L_B x C_B` still prices every
spread-to-money conversion, and a move gives exactly `-dS x k` only
when `L_A x C_A = beta x L_B x C_B`. Typed lots are under no obligation
to satisfy that. The tests that pinned the derivation are therefore
gone too; what is pinned here is that the typed numbers are the ones
that reach the brokers, unchanged except by each broker's own step.
"""

import pytest

from okxtrader.config import PairConfig
from okxtrader.sizing import (clip_plan, leg_ratio, max_qty,
                              minimum_notional, round_step, spread_units)


def a_pair(lots_a=1.0, lots_b=1.0, beta=1.0):
    return PairConfig('K', hedge_ratio=beta,
                      clip_lots_a=lots_a, clip_lots_b=lots_b)


def meta(contract=1000.0, minimum=0.01, step=0.01, maximum=1000.0):
    return {'contract_size': contract, 'volume_min': minimum,
            'volume_step': step, 'volume_max': maximum}


# -- Qty is the lots -------------------------------------------------------

def test_one_qty_is_one_lot_a_side_by_default():
    """A new pair is 1 and 1. The desk's own words: place 1 lot on the
    ladder and it places 1 lot of leg A and 1 lot of leg B."""
    pair = PairConfig('K')
    assert (pair.clip_lots_a, pair.clip_lots_b) == (1.0, 1.0)
    plan = clip_plan(pair, meta(), meta(), 88.63, 96.475, 1.0)
    assert plan['leg_a_lots'] == pytest.approx(1.0)
    assert plan['leg_b_lots'] == pytest.approx(1.0)


def test_qty_multiplies_both_legs():
    """Qty 100 at 1 and 1 is 100 lots a side — 100 x 1,000 bbl on each,
    which is what the desk asked for in so many words."""
    plan = clip_plan(a_pair(), meta(), meta(), 88.63, 96.475, 100.0)
    assert plan['leg_a_lots'] == pytest.approx(100.0)
    assert plan['leg_b_lots'] == pytest.approx(100.0)
    assert plan['leg_a_units'] == pytest.approx(100_000.0)   # bbl
    assert plan['leg_b_units'] == pytest.approx(100_000.0)


def test_an_unequal_ratio_is_taken_exactly_as_typed():
    """1 and 2 is 1 lot of A against 2 of B, at every Qty. Nothing
    second-guesses it against the beta or the contract sizes."""
    plan = clip_plan(a_pair(1.0, 2.0), meta(), meta(), 88.63, 96.475, 10.0)
    assert plan['leg_a_lots'] == pytest.approx(10.0)
    assert plan['leg_b_lots'] == pytest.approx(20.0)


def test_a_blank_leg_reads_as_ONE_never_as_nothing():
    """There is nothing left to derive, so a blank has one honest
    reading. Zero would be a click that sends no order on that leg."""
    pair = PairConfig('K', clip_lots_a=None, clip_lots_b=None)
    assert (pair.clip_lots_a, pair.clip_lots_b) == (1.0, 1.0)
    plan = clip_plan(pair, meta(), meta(), 88.63, 96.475, 5.0)
    assert plan['leg_a_lots'] == pytest.approx(5.0)
    assert plan['leg_b_lots'] == pytest.approx(5.0)


def test_the_ratio_is_read_from_ONE_place():
    """The click and the fill's cross both use it. Two readings of
    'what does this pair trade' is a pair hedged on one route and not
    on the other."""
    assert leg_ratio(a_pair(1.0, 2.0)) == pytest.approx(2.0)
    assert leg_ratio(a_pair(2.0, 1.0)) == pytest.approx(0.5)
    assert leg_ratio(PairConfig('K')) == pytest.approx(1.0)


# -- what each broker will actually take ----------------------------------

def test_each_leg_is_rounded_to_ITS_OWN_step():
    """The two brokers do not have the same step. Leg A to nearest —
    its size is the target; leg B DOWN, because it is what covers leg A
    and short is the recoverable error."""
    plan = clip_plan(a_pair(0.333, 0.333), meta(step=0.01, minimum=0.01),
                     meta(step=0.1, minimum=0.1), 88.63, 96.475, 1.0)
    assert plan['leg_a_lots'] == pytest.approx(0.33)     # nearest
    assert plan['leg_b_lots'] == pytest.approx(0.3)      # down, not 0.4


def test_a_leg_under_its_minimum_is_refused_and_names_the_number():
    plan = clip_plan(a_pair(0.01, 0.01), meta(), meta(minimum=0.1, step=0.1),
                     88.63, 96.475, 1.0)
    # Leg A is fine at 0.01; leg B cannot trade under 0.1, so the
    # CLICK is refused — one leg alone is a naked position.
    assert plan['leg_b_lots'] == 0.0
    assert '0.1-lot minimum' in plan['reason']
    assert 'leg B' in plan['reason']


def test_a_leg_over_the_brokers_MAXIMUM_is_refused_before_either_moves():
    """Discovered AFTER leg A has filled is a naked leg. In the
    stat-arb system an inverted beta asked for 5,167 lots of gold and
    the plan reported it as fine."""
    plan = clip_plan(a_pair(), meta(maximum=50.0), meta(), 88.63, 96.475,
                     100.0)
    assert plan['reason'] and 'maximum' in plan['reason']
    assert '50' in plan['reason'] and 'leg A' in plan['reason']
    # ...and THE QTY THAT WOULD FIT. A refusal that only says "too big"
    # leaves the trader guessing at the number, and the guess is the
    # Qty box — the one control whose meaning they have just been asked
    # to relearn.
    assert 'Qty 50 or less fits' in plan['reason']


def test_the_qty_that_fits_is_worked_out_per_leg():
    """Leg B is the one that caps it here, and the arithmetic is that
    leg's lots-per-Qty, not leg A's."""
    plan = clip_plan(a_pair(1.0, 2.0), meta(), meta(maximum=50.0),
                     88.63, 96.475, 100.0)
    assert 'leg B' in plan['reason']
    assert 'Qty 25 or less fits' in plan['reason']


def test_contract_sizes_that_are_not_known_yet_refuse_rather_than_guess():
    plan = clip_plan(a_pair(), {}, {}, 88.63, 96.475, 1.0)
    assert 'contract sizes are not known' in plan['reason']


# -- the money multiplier --------------------------------------------------

def test_k_is_leg_bs_units_and_follows_the_typed_lots():
    """`k = L_B x C_B` is the ONE multiplier every spread-to-money
    conversion uses, and it is LEG B's."""
    plan = clip_plan(a_pair(1.0, 2.0), meta(), meta(contract=100.0),
                     88.63, 96.475, 10.0)
    assert plan['spread_units'] == pytest.approx(20.0 * 100.0)
    # ...and the per-Qty figure, which is what the exit levels are
    # priced per and does not move when the keypad is touched.
    assert plan['spread_units_per_qty'] == pytest.approx(2.0 * 100.0)


def test_spread_units_is_zero_only_when_there_is_nothing_to_price():
    assert spread_units(0.0, 100.0) == 0.0
    assert spread_units(1.0, 0.0) == 0.0
    assert spread_units(1.5, 100.0) == pytest.approx(150.0)


def test_round_step_snaps_nearest_and_down_where_asked():
    assert round_step(0.055, 0.01) == pytest.approx(0.06)
    assert round_step(0.055, 0.01, down=True) == pytest.approx(0.05)
    # DOWN, under the minimum, is 0.0 — not a size the broker takes.
    assert round_step(0.005, 0.01, minimum=0.01, down=True) == 0.0


def test_the_minimum_notional_a_leg_still_reports_what_it_costs():
    floor = minimum_notional(1000.0, 1000.0, 88.63, 96.475, 1.0, 0.01, 0.01)
    assert floor == pytest.approx(886.3, rel=0.01)


# -- the sentence the ladder prints ---------------------------------------

def test_the_derivation_says_lots_and_money_in_the_same_breath():
    """A number with no unit is not checkable (spec 11)."""
    plan = clip_plan(a_pair(), meta(), meta(), 88.63, 96.475, 100.0)
    assert plan['derivation'] == \
        'Qty 100 = 100 lots A / 100 lots B, $100,000.00 per 1.00 of spread'


# -- the largest Qty this pair can trade ----------------------------------

def test_the_cap_is_the_smaller_of_the_two_brokers():
    """A pair can only trade what BOTH sides will take."""
    pair = a_pair()

    assert max_qty(pair, meta(maximum=10.0), meta(maximum=50.0)) == 10
    assert max_qty(pair, meta(maximum=200.0), meta(maximum=7.0)) == 7


def test_the_cap_is_in_QTY_not_in_lots():
    """Qty is multiplied by the lots-per-Qty on each leg before it
    reaches a broker, so a 10-lot cap at 2 lots per Qty is Qty 5."""
    pair = a_pair(lots_a=2.0, lots_b=0.5)

    assert max_qty(pair, meta(maximum=10.0), meta(maximum=10.0)) == 5


def test_a_broker_that_caps_nothing_caps_nothing():
    """None, not zero. A cap of 0 would grey out every button on the
    keypad on a broker that simply did not report a maximum."""
    assert max_qty(a_pair(), {}, {}) is None
    assert max_qty(a_pair(), {'volume_max': 0.0},
                   {'volume_max': None}) is None


def test_the_cap_agrees_with_the_refusal():
    """Two places must not disagree about the largest Qty that fits:
    the keypad offers it, and `clip_plan` refuses past it."""
    pair = a_pair()
    both = meta(maximum=10.0)
    cap = max_qty(pair, both, both)

    assert clip_plan(pair, both, both, 88.0, 95.0, cap)['reason'] is None
    refusal = clip_plan(pair, both, both, 88.0, 95.0, cap + 1)['reason']
    assert refusal and f'Qty {cap:g} or less fits' in refusal
