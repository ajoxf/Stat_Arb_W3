"""Which hedge ratio belongs to the pair that is actually configured.

Beta is a property of the PAIR, not of the installation. In the
stat-arb system nothing carried it across a pair change: the symbols
were edited, the launcher restarted, and the beta from the last
instrument was still sitting in config.json defining the spread.

Three live incidents there in one day (2026-08-10), all the same shape:

    beta 10       USOIL/UKOIL at 81.76 / 85.07  ->  spread  -732.53
    beta 0.0149   XAGUSD/XAUUSD                 ->  a 5,167-lot hedge
    beta 66.94    left on USOIL/UKOIL           ->  spread -5469.59

Every one of them made the spread — and so every ladder level, every
working order and every mark — describe a series that does not exist,
and the third arrived purely by changing the instruments.

So the value is STAMPED with the pair it was computed for, and the
engine re-derives it at startup whenever the running pair no longer
matches the stamp. What it does NOT do is second-guess a beta that
belongs to the current pair — an operator who tunes beta on their own
pair keeps their number.
"""

BASIS_TYPES = ('SPOT_FUTURE', 'FUTURE_FUTURE')

# How far the spread may travel from zero before it stops being a
# DIFFERENCE between two comparable prices and starts being one price
# scaled by a wrong coefficient. Half the smaller leg is generous —
# a real basis is a percent or two — and it is the threshold the ladder
# marks a pair unusable at.
MAX_SPREAD_FRACTION = 0.5

# When two different instruments are quoted close enough to each other
# that subtracting one from the other is already meaningful. WTI 83 vs
# Brent 86 is 1.04 and belongs here; silver 38 vs gold 4,000 is 105 and
# does not. Deliberately wide — the question is only "is beta 1 going
# to be dominated by one leg?", and at 2x it still is not.
COMPARABLE_LOW, COMPARABLE_HIGH = 0.5, 2.0


def pair_signature(symbol_a, symbol_b):
    """What a beta was computed FOR. Stamped beside the value so a pair
    change is detectable; without it, a stale beta and a deliberately
    tuned one are indistinguishable."""
    return f'{symbol_a or ""}|{symbol_b or ""}'


def suggest(pair_type, price_a, price_b):
    """(beta, why) for this pair, or (None, why not).

    The right answer depends on what the two legs ARE, and getting it
    backwards is harmful in both directions:

    Same underlying (spot vs its own future, or two contract months) —
        beta is 1 and the spread IS the basis. Using the price ratio
        here collapses the spread to roughly zero and deletes the very
        thing the strategy trades: gold spot against its future has a
        ratio of ~1.014, and a beta of 1.014 turns a $59 basis into
        pennies of rounding.

    Two different instruments priced on DIFFERENT scales (silver at 38
        against gold at 4,000) — the price ratio, because beta 1 there
        is not a spread at all, it is gold's own price with a rounding
        error subtracted. The ratio is what brings them onto the same
        scale.

    Two different instruments priced on the SAME scale (WTI and Brent,
        both dollars a barrel, 83 against 86) — beta 1 again, and the
        spread is the DIFFERENTIAL. This case used to fall into the
        price-ratio branch, which is how the operator's oil pair went
        from a +3.30 spread to -0.05 (2026-08-10, "Why is the spread
        Incorrect?"). Nothing was miscomputed — 86.4550 - 1.04 x 83.1750
        really is -0.047 — but the ratio CENTRES the series on zero by
        construction, so it throws away the level that names the trade
        and leaves a number that reads as broken. It buys nothing for
        it: sigma is all but identical between the two (the legs move
        together and beta shifts by 4%), so the edge is the same and
        only the readability differs.
    """
    pair_type = (pair_type or 'SPOT_FUTURE').upper()
    if pair_type in BASIS_TYPES:
        return 1.0, (f'{pair_type}: the two legs are the same '
                     f'underlying, so the spread IS the basis and beta '
                     f'is 1')
    if not price_a or not price_b or price_a <= 0 or price_b <= 0:
        return None, 'both legs need a live price before beta can be derived'
    ratio = price_b / price_a
    if COMPARABLE_LOW <= ratio <= COMPARABLE_HIGH:
        return 1.0, (
            f'{pair_type}, but {price_a:,.4f} and {price_b:,.4f} are '
            f'already on the same scale — beta 1 makes the spread their '
            f'DIFFERENCE ({price_b - price_a:+,.4f}), which is the series '
            f'this pair is actually traded on. The price ratio '
            f'({ratio:,.4f}) would centre it on zero and throw that level '
            f'away for no gain in sigma')
    return round(ratio, 6), (
        f'{pair_type}: {price_b:,.4f} / {price_a:,.4f} — the two legs are '
        f'on different scales, so beta 1 would just be the bigger leg\'s '
        f'own price; the ratio is what brings them onto the same scale')


def spread_for(beta, price_a, price_b):
    """The spread this beta produces: P_B - beta * P_A."""
    return (price_b or 0.0) - float(beta or 1.0) * (price_a or 0.0)


def implausible(beta, price_a, price_b, spread=None):
    """Is the resulting spread not a difference between these prices?

    Returns the offending spread, or None when the beta is usable. One
    threshold, shared by the startup adoption and the ladder badge that
    marks a pair unusable, so the engine cannot adopt a beta it will then
    refuse to trade on.
    """
    smaller = min(abs(price_a or 0.0), abs(price_b or 0.0))
    if not smaller:
        return None
    if spread is None:
        spread = spread_for(beta, price_a, price_b)
    return spread if abs(spread) > MAX_SPREAD_FRACTION * smaller else None


def resolve(configured_beta, stamped_for, pair_type, symbol_a, symbol_b, price_a, price_b):
    """Decide what beta this pair should run with.

    Returns (beta, reason) when it should CHANGE, or (None, reason)
    when the configured value stands. The three cases:

    Stamped for THIS pair — the value belongs here. Left alone, even if
        it is unusual: beta is a strategy parameter and an operator who
        tuned it on their own pair keeps their number.

    Stamped for a DIFFERENT pair — the pair was changed and the beta
        was not. Re-derived. This is the case the operator asked for.

    Not stamped at all — an install predating the stamp, so which pair
        the number was meant for is unknowable. Adopting blindly would
        overwrite a deliberate choice, so the value is kept and merely
        stamped UNLESS it is implausible against the live prices, which
        settles the question on its own.
    """
    signature = pair_signature(symbol_a, symbol_b)
    suggested, why = suggest(pair_type, price_a, price_b)

    if stamped_for == signature:
        return None, f'beta {configured_beta:g} was set for this pair'

    if suggested is None:
        return None, why                    # cannot derive; leave it be

    if not stamped_for:
        broken = implausible(configured_beta, price_a, price_b)
        if broken is None:
            return None, (f'beta {configured_beta:g} is plausible for '
                          f'{symbol_a}/{symbol_b} — kept, and now '
                          f'stamped with the pair it belongs to')
        return suggested, (
            f'beta {configured_beta:g} gives a spread of '
            f'{broken:,.2f} on legs priced {price_a:,.4f} / {price_b:,.4f}, '
            f'which is not a difference between them. {why}')

    return suggested, (
        f'the pair changed to {symbol_a}/{symbol_b} but '
        f'beta {configured_beta:g} was set for '
        f'{stamped_for.replace("|", "/")}. {why}')
