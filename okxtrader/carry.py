"""What it costs to CARRY one spread to expiry — priced from the swap
the broker actually charges.

The fair spread answers a question nothing else on the screen answers:
at expiry the future converges to the spot, so what should this basis
be today, on financing alone, for the nights remaining?

    carry       = sum over both legs of ( swap_per_lot_per_night
                                          x lots x nights )
    fair_spread = - carry / k

`k` is `spread_units` — the one multiplier every spread-to-money
conversion in this system uses. Dividing by it makes the reading
SIZE-FREE: the fair spread does not move when the clip does, which is
what lets it sit beside a ladder quoted in spread points.

Three things about this module are load-bearing, and each of them is a
live loss in the system this is ported from:

- **Swap units are the whole difficulty.** MT5 reports `swap_long` /
  `swap_short` in whatever `swap_mode` says, and the same `-4.5` is 4.5
  points on one symbol, 4.5 units of account currency on another and
  4.5 percent a year on a third. Each mode is converted EXPLICITLY, and
  a mode this cannot convert returns None with the reason.
  **An unconvertible swap is not a zero swap.**
- **The sign is the trade.** A pair is long one leg and short the
  other, so the two swaps frequently pull opposite ways, and being PAID
  to wait is the case worth finding. Which side each leg is charged on
  follows the direction HELD, so there are four rates, not two:
  `swap_{a,b}_{long,short}_per_lot`. Reading `swap_long` on both legs
  prices a trade nobody places.
- **One unpriced leg makes the whole estimate None.** Half a carry
  estimate is not a smaller estimate. A fair value that quietly dropped
  a leg's financing reads as an edge that is not there.

And the cross-check, which is the cheapest check on the screen: the
same basis is priced a second time from an annual rate, by different
arithmetic on different inputs. When the two disagree in SIGN — or by
more than ~3x — one of the inputs is wrong, and the reading is
REPLACED by the warning rather than printed beneath it. In the system
this is ported from, a swap typed `+58.00` where `-58.00` belonged
produced "you are paid to hold this at any spread", which is a licence
to print money from one missing minus sign.

Nothing here places an order, withholds one, or feeds break-even, the
take-profit or AutoRouting. It is a REFERENCE reading: the trader looks
at it and decides.
"""

import math

# MT5 SYMBOL_SWAP_MODE_* values, named here rather than imported: this
# module must work off-Windows, where the MetaTrader5 package does not
# exist, and `broker.py` is the only module allowed to import it.
SWAP_DISABLED = 0
SWAP_POINTS = 1
SWAP_CURRENCY_SYMBOL = 2
SWAP_CURRENCY_MARGIN = 3
SWAP_CURRENCY_DEPOSIT = 4
SWAP_INTEREST_CURRENT = 5
SWAP_INTEREST_OPEN = 6
SWAP_REOPEN_CURRENT = 7
SWAP_REOPEN_BID = 8

#: Modes whose value is already money per lot per night.
MONEY_MODES = (SWAP_CURRENCY_SYMBOL, SWAP_CURRENCY_MARGIN,
               SWAP_CURRENCY_DEPOSIT)

MODE_NAMES = {
    SWAP_DISABLED: 'no swap charged',
    SWAP_POINTS: 'points per night',
    SWAP_CURRENCY_SYMBOL: 'symbol currency per lot per night',
    SWAP_CURRENCY_MARGIN: 'margin currency per lot per night',
    SWAP_CURRENCY_DEPOSIT: 'deposit currency per lot per night',
    SWAP_INTEREST_CURRENT: 'annual percent (current price)',
    SWAP_INTEREST_OPEN: 'annual percent (open price)',
    SWAP_REOPEN_CURRENT: 'position reopened at close price',
    SWAP_REOPEN_BID: 'position reopened at bid',
}

#: Which legs a basis pair is on for each direction of the spread.
#: BUY the spread is buy B, sell A — so leg B is charged its LONG swap
#: and leg A its SHORT one.
SIDES = {'BUY': {'a': 'short', 'b': 'long'},
         'SELL': {'a': 'long', 'b': 'short'}}

#: How far the swap-implied basis may sit from the rate-implied one
#: before the swap input is the likeliest explanation.
SANITY_MULT = 3.0

YEAR_DAYS = 365.25


def swap_per_lot_night(swap, mode, contract_size=None, price=None,
                       tick_size=None, tick_value=None):
    """(money per lot per night, note) for one leg, or (None, why not).

    The sign is preserved exactly as the broker reports it: a positive
    swap is credited and a negative one charged.
    """
    if swap is None:
        return None, 'the broker did not report a swap for this symbol'
    if mode is None:
        return None, ('the broker did not report swap_mode, so the number '
                      'cannot be read as money, points or percent')
    try:
        swap = float(swap)
    except (TypeError, ValueError):
        return None, f'swap {swap!r} is not a number'
    if mode == SWAP_DISABLED or swap == 0:
        return 0.0, 'no swap charged on this symbol'
    if mode in MONEY_MODES:
        return swap, f'{swap:+.2f} per lot per night, as quoted'
    if mode == SWAP_POINTS:
        # A point is worth tick_value per tick_size of price. Falling
        # back to the contract size assumes a point IS one unit of
        # price — true on most CFDs, not on all of them — so the note
        # says which route was taken.
        if tick_size and tick_value:
            per_point = float(tick_value) / float(tick_size)
            return swap * per_point, (
                f'{swap:+.1f} points x {per_point:,.2f} per point '
                f'(tick value {tick_value} / tick size {tick_size})')
        if contract_size:
            return swap * float(contract_size), (
                f'{swap:+.1f} points x {contract_size:g} per lot — no tick '
                f'value reported, so a point is taken as one unit of price')
        return None, 'swap is in points but nothing prices a point'
    if mode in (SWAP_INTEREST_CURRENT, SWAP_INTEREST_OPEN):
        if not (contract_size and price):
            return None, ('swap is an annual percent and needs the contract '
                          'size and a live price to convert')
        notional = float(contract_size) * float(price)
        return notional * swap / 100.0 / 360.0, (
            f'{swap:+.3f}% a year on {notional:,.0f} of notional, over a '
            f'360-day year')
    return None, (f'swap mode {mode} ({MODE_NAMES.get(mode, "unrecognised")}) '
                  f'is not one this can convert')


def leg_rate(meta, side, override=None):
    """One leg's money per lot per night, on the side it is HELD.

    `override` is the operator's own figure for that side. It wins
    outright, and **0 is a real statement** — only None means "use the
    broker's". A field loop that skips blanks can never CLEAR an
    override, and one that cannot be cleared outlives the pair it was
    typed for.
    """
    meta = meta or {}
    if override is not None:
        return float(override), 'typed by the operator, overriding MT5'
    key = 'swap_long' if side == 'long' else 'swap_short'
    return swap_per_lot_night(
        meta.get(key), meta.get('swap_mode'),
        contract_size=meta.get('contract_size'),
        price=meta.get('bid') or meta.get('ask'),
        tick_size=meta.get('tick_size'), tick_value=meta.get('tick_value'))


def overrides_for(overrides, role, side):
    """The typed rate for one leg and side, or None for "use MT5's"."""
    value = (overrides or {}).get(f'swap_{role}_{side}_per_lot')
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def carry_money(meta_a, meta_b, direction, lots_a, lots_b, nights,
                overrides=None):
    """What holding ONE spread in `direction` costs over `nights`.

    Returns a dict with `money` (signed: positive means the pair is
    PAID to wait), the per-leg workings, and `reason` set — with
    `money` None — whenever either leg could not be priced.
    """
    sides = SIDES.get(str(direction).upper()) or SIDES['BUY']
    out = {'direction': str(direction).upper(), 'nights': nights,
           'money': None, 'per_leg': [], 'reason': None}
    if nights is None:
        out['reason'] = ('no expiry on the futures leg — there is no date '
                         'for this trade to be decided on')
        return out
    total = 0.0
    unpriced = []
    for role, meta, lots in (('a', meta_a, lots_a), ('b', meta_b, lots_b)):
        side = sides[role]
        rate, note = leg_rate(meta, side,
                              overrides_for(overrides, role, side))
        entry = {'role': role, 'side': 'L' if side == 'long' else 'S',
                 'symbol': (meta or {}).get('symbol'), 'lots': lots,
                 'per_lot_night': rate, 'note': note, 'money': None}
        if rate is None or lots is None:
            unpriced.append(f"{entry['symbol'] or 'leg ' + role.upper()}: "
                            f"{note if rate is None else 'no clip size yet'}")
        else:
            entry['money'] = rate * float(lots) * float(nights)
            total += entry['money']
        out['per_leg'].append(entry)
    if unpriced:
        # Half a carry estimate is not a smaller estimate.
        out['reason'] = '; '.join(unpriced)
        return out
    out['money'] = total
    return out


def fair_from_carry(money, spread_units):
    """`-carry / k` — the basis the financing alone justifies."""
    if money is None or not spread_units:
        return None
    return -float(money) / float(spread_units)


def rate_implied(price_a, beta, rate_pct, days):
    """The same basis priced from an ANNUAL rate, for the cross-check.

    Compounding leg A to expiry and subtracting it gives what the
    future should stand above the spot: `beta x A x (e^(r t) - 1)`.
    Deliberately different arithmetic on deliberately different inputs
    — that is what makes the comparison worth anything.
    """
    if price_a is None or rate_pct in (None, '') or days is None:
        return None
    try:
        years = float(days) / YEAR_DAYS
        return (float(beta or 1.0) * float(price_a)
                * (math.exp(float(rate_pct) / 100.0 * years) - 1.0))
    except (TypeError, ValueError, OverflowError):
        return None


def sanity(carry_spread, rate_spread):
    """Do the broker's swap and an annual rate tell the same story?

    Returns a message when they do not, and None when they agree or
    when there is nothing to compare against.
    """
    if carry_spread is None or rate_spread is None:
        return None
    if abs(rate_spread) < 1e-9:
        return None
    if carry_spread * rate_spread < 0:
        return (f'The swap says this basis is worth {carry_spread:+,.4f}, an '
                f'annual carry rate says {rate_spread:+,.4f} — OPPOSITE '
                f'signs. A leg you are LONG is normally charged, so its swap '
                f'is negative.')
    ratio = abs(carry_spread) / abs(rate_spread)
    if ratio > SANITY_MULT or ratio < 1.0 / SANITY_MULT:
        return (f'The swap says this basis is worth {carry_spread:+,.4f}, an '
                f'annual carry rate says {rate_spread:+,.4f} — '
                f'{max(ratio, 1 / ratio):,.1f}x apart. They price the same '
                f'thing, so check the units.')
    return None


def _credited_long(per_leg):
    """The first leg being held LONG whose swap is a CREDIT, or None."""
    for leg in per_leg or ():
        rate = leg.get('per_lot_night')
        if leg.get('side') == 'L' and rate is not None and rate > 0:
            return leg
    return None


def credited_long_leg(per_leg):
    """Flag a long leg showing a credit — no second estimate needed.

    Holding a long position is financed, so the broker charges you: a
    long leg's swap is negative on essentially every instrument this
    trades. A CREDIT there is the signature of a magnitude typed
    without its minus sign. It is possible to be paid on a long leg, so
    this REPORTS rather than refuses — but it must say so, because the
    alternative is a fabricated edge that looks like the best trade on
    the screen.
    """
    leg = _credited_long(per_leg)
    if not leg:
        return None
    return (f"{leg.get('symbol') or 'leg ' + str(leg.get('role', '')).upper()}"
            f" is the leg you would be LONG and its swap is a CREDIT "
            f"({leg['per_lot_night']:+.2f} a night). A long leg is normally "
            f"charged — check the sign.")


def credit_fix(per_leg):
    """The exact correction for a credited long leg, or None.

    Naming the field and the value lets the panel offer the correction
    as ONE CLICK. Still an explicit action, never applied behind the
    operator: a sign the engine flipped by itself is a sign nobody
    would ever notice was wrong.
    """
    leg = _credited_long(per_leg)
    if not leg or not leg.get('role') or not leg.get('side'):
        return None
    side = 'long' if leg['side'] == 'L' else 'short'
    return {'field': f"swap_{leg['role']}_{side}_per_lot",
            'value': -abs(float(leg['per_lot_night'])),
            'symbol': leg.get('symbol')}


def describe(meta_a, meta_b, beta, lots_a, lots_b, spread_units, days,
             market=None, overrides=None, expects_expiry=True,
             rate_pct=None):
    """The fair spread for BOTH directions, and the gap on each.

    There is no mid here and there is not meant to be. Buying the
    spread pays `long_spread` and selling it receives `short_spread`,
    so each direction is compared against the price it would actually
    trade at (spec section 2). A gap measured off a midpoint is a
    comparison against a price nobody fills at.
    """
    market = market or {}
    body = {'days_to_expiry': days, 'expects_expiry': bool(expects_expiry),
            'spread_units': spread_units,
            'fair_buy': None, 'fair_sell': None,
            'gap_buy': None, 'gap_sell': None,
            'carry_buy': None, 'carry_sell': None,
            'per_leg': [], 'rate_implied': None,
            'warning': None, 'fix': None, 'note': None, 'source': 'swap'}

    if not expects_expiry:
        body['note'] = ('two different instruments — no carry ties them '
                        'together, so there is no fair spread to quote')
        return body
    if days is None:
        body['note'] = ("set the futures leg's expiry on the Exchanges page "
                        "and the fair spread appears here")
        return body
    if not spread_units:
        body['note'] = ('no sizing yet — the carry cannot become a spread '
                        'without k')
        return body

    buy = carry_money(meta_a, meta_b, 'BUY', lots_a, lots_b, days, overrides)
    sell = carry_money(meta_a, meta_b, 'SELL', lots_a, lots_b, days, overrides)
    body['per_leg'] = buy['per_leg']
    if buy['money'] is None:
        body['note'] = buy['reason']
        return body

    body['carry_buy'] = buy['money']
    body['carry_sell'] = sell['money']
    body['fair_buy'] = fair_from_carry(buy['money'], spread_units)
    body['fair_sell'] = fair_from_carry(sell['money'], spread_units)

    # The cross-check, and the two warnings that need no second
    # estimate. A conclusion drawn from an input that can be proven
    # wrong should not render at all — so the reading is REPLACED.
    body['rate_implied'] = rate_implied(
        (meta_a or {}).get('bid') or (meta_a or {}).get('ask'),
        beta, rate_pct, days)
    body['warning'] = (sanity(body['fair_buy'], body['rate_implied'])
                       or credited_long_leg(buy['per_leg'])
                       or credited_long_leg(sell['per_leg']))
    body['fix'] = credit_fix(buy['per_leg']) or credit_fix(sell['per_leg'])
    if body['warning']:
        body['disputed'] = {'fair_buy': body['fair_buy'],
                            'fair_sell': body['fair_sell']}
        body['fair_buy'] = body['fair_sell'] = None
        return body

    long_spread, short_spread = market.get('long_spread'), \
        market.get('short_spread')
    if long_spread is not None and body['fair_buy'] is not None:
        body['gap_buy'] = long_spread - body['fair_buy']
    if short_spread is not None and body['fair_sell'] is not None:
        body['gap_sell'] = short_spread - body['fair_sell']
    body['note'] = (f'{days:g} night{"" if days == 1 else "s"} of the '
                    f"broker's own swap, over k")
    return body
