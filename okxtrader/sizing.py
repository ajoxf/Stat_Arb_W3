"""How many lots each leg trades, and what one unit of Qty is worth.

## THE TRADER TYPES BOTH LEGS

`clip_lots_a` and `clip_lots_b` are what ONE unit of the Qty box means
on each leg, and both are typed. Qty 100 at 1 and 1 is 100 lots of leg
A against 100 lots of leg B; at 1 and 2 it is 100 against 200. Nothing
is derived, and nothing is matched on the desk's behalf.

That is a deliberate reversal. Leg B used to be computed — from the
hedge arithmetic, or lot for lot, or by equal notional — and every one
of those could round a leg to zero, disagree with what the trader
believed they were trading, or refuse to size at all. A number typed
into a box can do none of those things.

## WHAT THAT COSTS, SAID PLAINLY

`k = L_B * C_B` is the dollars-per-1.00-of-spread multiplier, and it is
LEG B's units. Every conversion from spread to money uses this one `k`:
the ladder's per-tick value, the working order's notional, the
position's P&L, the fees.

The spread is `S = P_B - beta * P_A`, and a move gives exactly `-dS * k`
only when `L_A * C_A = beta * L_B * C_B`. Typed lots are under no
obligation to satisfy that, so on a pair where they do not, part of the
position is an outright in the underlying rather than a spread. That is
the trader's choice to make — they typed both numbers — and the P&L
this system reports is still the spread's, computed from `k`.

`L_B = L_A * beta` would be wrong for that identity, and was wrong in
the stat-arb engine this is ported from: at beta 2 the matching hedge
is HALF leg A's lots, and the naive rule traded double. It is recorded
here because the arithmetic still governs what `k` means, even though
nothing computes a hedge from it any more.
"""

import math


#: Where a quantity stops being a number and starts being noise.
#: Clicks are typed in hundredths at the smallest, so nine decimal
#: places is far below anything a trader can mean and far above the
#: binary error that accumulates through a subtraction.
QUANTITY_DIGITS = 9


def tidy(quantity):
    """A quantity with the binary dust swept back off it.

    `0.15 - 0.1` is `0.04999999999999999` in float, and a click of 0.15
    covering a 0.1 position put exactly that on the Working Orders
    panel — seventeen digits of it, next to a clean `0.1`, which reads
    like the size was changed on the way to the broker. It was not: the
    two rows summed to the 0.15 that was clicked. But a trader cannot
    be asked to add up seventeen-digit floats to satisfy themselves
    that their own click went in whole.

    So every subtraction that walks a click down a stack of positions
    goes through here. It changes no quantity anybody could type.
    """
    try:
        return round(float(quantity), QUANTITY_DIGITS)
    except (TypeError, ValueError):
        return quantity


def round_step(volume, step, minimum=0.0, down=False):
    """Snap to a tradable volume — NEAREST by default.

    `down=True` where overshooting is genuinely unsafe: leg B, which
    is crossed to cover leg A. Short is the recoverable error.
    """
    if step and step > 0:
        scaled = volume / step
        volume = (math.floor(scaled + 1e-9) if down
                  else math.floor(scaled + 0.5 + 1e-9)) * step
        volume = round(volume, 8)
    return volume if volume >= minimum - 1e-9 else 0.0


def leg_ratio(pair):
    """Leg B lots per ONE lot of leg A, as the trader typed them.

    The one place the ratio is read, so the click, the fill's cross and
    every panel agree about what this pair trades.
    """
    lots_a = float(getattr(pair, 'clip_lots_a', None) or 1.0)
    lots_b = float(getattr(pair, 'clip_lots_b', None) or 1.0)
    return (lots_b / lots_a) if lots_a else 0.0


def spread_units(leg_b_lots, contract_b):
    """`k` — dollars per 1.00 of spread movement. Leg B's units."""
    if not leg_b_lots or not contract_b:
        return 0.0
    return leg_b_lots * contract_b


def notional(lots, contract_size, price):
    if not lots or not contract_size or not price:
        return 0.0
    return lots * contract_size * price


def minimum_notional(contract_a, contract_b, price_a, price_b, beta,
                     min_a=0.0, min_b=0.0):
    """The smallest per-leg notional this PAIR can trade at all.

    The binding constraint is whichever leg needs more money, and it is
    usually leg B. Returning the number lets the ladder say "this pair
    needs at least $43,515 a leg" BEFORE the operator tries to trade
    under it, instead of "the hedge rounds to zero" after.
    """
    beta = float(beta or 1.0)
    if not contract_a or not contract_b or not price_a or not price_b:
        return None
    needs = [min_a * contract_a * price_a] if min_a else []
    if min_b:
        # Leg B's minimum expressed as the leg A notional producing it:
        # L_A = min_b * beta * C_B / C_A, so that notional is
        # L_A * C_A * P_A, and C_A cancels.
        needs.append(min_b * beta * contract_b * price_a)
    return max(needs) if needs else None


def max_qty(pair, meta_a, meta_b):
    """The largest whole Qty this pair can trade, or None if unbounded.

    The same arithmetic the refusal uses, in a form the SCREEN can read
    — so the keypad stops offering a size that is a guaranteed refusal.
    A ladder whose broker caps leg A at 10 lots was showing 50 and 100
    buttons, and a trader who pressed one got "Qty 100 x 1 wants 100
    lots on leg A" back. Better not to offer it.
    """
    caps = []
    for meta, unit in ((meta_a or {}, getattr(pair, 'clip_lots_a', None)),
                       (meta_b or {}, getattr(pair, 'clip_lots_b', None))):
        maximum = meta.get('volume_max') or 0.0
        per_qty = float(unit or 1.0)
        if maximum and per_qty:
            caps.append(math.floor((maximum / per_qty) + 1e-9))
    if not caps:
        return None
    return max(0, min(caps))


def clip_plan(pair, meta_a, meta_b, price_a, price_b, spreads=1.0):
    """Resolve one click into leg lots, or say why it cannot be.

    `spreads` is the Qty box, and QTY IS THE LOTS: at 1 and 1 a Qty of
    100 is 100 lots of leg A against 100 of leg B; at 1 and 2 it is 100
    against 200. Both numbers are the trader's, so nothing here derives
    a hedge or matches the legs on their behalf — it multiplies, rounds
    to what each broker can trade, and checks the result against both
    brokers' own limits.

    Returns the execution instruction and the display block together,
    because the ladder must show the derivation beside the quantity —
    "1 Qty = 1 A / 1 B, $1,000 per 1.00 of spread" (spec §11: a number
    with no unit is not checkable).

    `reason` is set when the click must be REFUSED. It is checked
    before either leg moves (spec §4: pre-check BOTH legs before
    resting EITHER).
    """
    beta = float(pair.hedge_ratio or 1.0)
    contract_a = float(meta_a.get('contract_size') or 0.0)
    contract_b = float(meta_b.get('contract_size') or 0.0)
    step_a = meta_a.get('volume_step') or 0.0
    step_b = meta_b.get('volume_step') or 0.0
    min_a = meta_a.get('volume_min') or 0.0
    min_b = meta_b.get('volume_min') or 0.0
    max_a = meta_a.get('volume_max') or 0.0
    max_b = meta_b.get('volume_max') or 0.0

    reason = None
    if not contract_a or not contract_b:
        return {'reason': 'contract sizes are not known yet — read them '
                          'from MT5 before sizing anything',
                'leg_a_lots': 0.0, 'leg_b_lots': 0.0, 'spread_units': 0.0}

    # BOTH TYPED, and a blank reads as 1. There is nothing left to
    # derive: the trader named what one unit of Qty is on each leg.
    unit_a = float(pair.clip_lots_a or 1.0)
    unit_b = float(pair.clip_lots_b or 1.0)

    spreads = float(spreads or 0.0)
    lots_a = round_step(unit_a * spreads, step_a, min_a)
    lots_b = round_step(unit_b * spreads, step_b, min_b, down=True)

    floor = minimum_notional(contract_a, contract_b, price_a, price_b,
                             beta, min_a, min_b)
    def fits(maximum, per_qty):
        """The largest whole Qty that stays under a broker's cap.

        A refusal that only says "too big" leaves the trader guessing
        at the number, and the guess is the Qty box — the one control
        whose meaning they have just been asked to relearn. So the
        message carries the answer.
        """
        if not maximum or not per_qty:
            return None
        return math.floor((maximum / per_qty) + 1e-9)

    if lots_a <= 0:
        reason = (f"Qty {spreads:g} x {unit_a:g} is {unit_a * spreads:g} lots "
                  f"on leg A, under its {min_a:g}-lot minimum")
    elif lots_b <= 0:
        reason = (f"Qty {spreads:g} x {unit_b:g} is {unit_b * spreads:g} lots "
                  f"on leg B, under its {min_b:g}-lot minimum"
                  + (f' — this pair needs at least ${floor:,.0f} a leg'
                     if floor else ''))
    elif max_a and lots_a > max_a + 1e-9:
        room = fits(max_a, unit_a)
        reason = (f"Qty {spreads:g} x {unit_a:g} wants {lots_a:g} lots on "
                  f"leg A, over this broker's {max_a:g}-lot maximum"
                  + (f' — Qty {room:g} or less fits' if room else ''))
    elif max_b and lots_b > max_b + 1e-9:
        # Never discovered AFTER leg A has filled. In the stat-arb
        # system an inverted beta sized leg B at 5,167 lots of gold —
        # $2.25bn — and the plan reported it as fine; MT5 would have
        # rejected it with 10014 after leg A was already on.
        room = fits(max_b, unit_b)
        reason = (f"Qty {spreads:g} x {unit_b:g} wants {lots_b:g} lots on "
                  f"leg B, over this broker's {max_b:g}-lot maximum"
                  + (f' — Qty {room:g} or less fits' if room else ''))

    k = spread_units(lots_b, contract_b)
    return {
        'reason': reason,
        'spreads': spreads,
        'leg_a_lots': lots_a, 'leg_b_lots': lots_b,
        'leg_a_contract': contract_a, 'leg_b_contract': contract_b,
        'leg_a_units': lots_a * contract_a, 'leg_b_units': lots_b * contract_b,
        'leg_a_notional_usd': notional(lots_a, contract_a, price_a),
        'leg_b_notional_usd': notional(lots_b, contract_b, price_b),
        'hedge_ratio': beta,
        #: `k` — the ONE multiplier. Everything money-valued reads this.
        'spread_units': k,
        #: ...and what ONE unit of Qty is worth, which is the figure
        #: the exit levels are priced per and the one that does not
        #: move when the keypad is touched.
        'spread_units_per_qty': spread_units(unit_b, contract_b),
        'min_notional_usd': floor,
        'unit_lots_a': unit_a, 'unit_lots_b': unit_b,
        # The sentence the ladder prints beside the quantity box.
        'derivation': (f"Qty {spreads:g} = {lots_a:g} lots A / {lots_b:g} "
                       f"lots B, ${k:,.2f} per 1.00 of spread"),
    }
