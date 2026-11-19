"""The fair spread: what the carry says the basis should be.

A spot-versus-future spread is not a random number that happens to
oscillate. It is carry: the future must converge to the spot at expiry,
and until then it trades away from it by roughly the cost of holding
the position — financing, storage, and whatever the broker actually
charges, which on an MT5 account is the SWAP.

So the trader supplies two things this system cannot measure:

- **the swap per day**, in SPREAD points, signed the way the spread is
  (`B - beta x A`). It is what carrying one spread for one day is worth
  to them, at their broker, on their account — not a textbook rate, and
  not something derivable from the price feed;
- **the expiry** of the futures leg.

From those, `fair = swap_per_day x days_to_expiry`, converging to zero
at expiry, which is the one point on the curve that is not an opinion.

What this is NOT: a signal. Nothing here places or withholds an order.
It is a number beside the market so the trader can see whether the
spread is rich or cheap to its own carry — which is the question the
whole instrument is about.

Unmeasured is not zero, as everywhere else: with no expiry or no swap
there is no fair value, the function returns None, and the screen shows
an em dash rather than 0.0000, which would read as "the market is
exactly fair".
"""

from datetime import date, datetime


def parse_expiry(value):
    """A date from the UI, or None. Never raises on rubbish."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for pattern in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def days_to_expiry(expiry, now):
    """Whole days from `now` to expiry, on the clock the caller passes.

    The caller passes the BROKER's day: a contract expires on the
    broker's calendar, not on the box's, and on a machine hours away
    those are different dates for part of every day.

    Past expiry returns 0, not a negative: a contract that has expired
    has no carry left, and a negative fair value would be a number the
    market can never trade at.
    """
    expiry = parse_expiry(expiry)
    if expiry is None or now is None:
        return None
    today = now.date() if isinstance(now, datetime) else now
    return max((expiry - today).days, 0)


def fair_spread(swap_per_day, days):
    """`swap_per_day x days`, or None if either is unknown."""
    if swap_per_day is None or days is None:
        return None
    try:
        return float(swap_per_day) * float(days)
    except (TypeError, ValueError):
        return None


def richness(mid_spread, fair):
    """How far the market is from fair, signed: + is RICH.

    Rich means the spread is trading above what its carry justifies —
    the side a trader would sell. Cheap is the other way. Saying which
    is which in words on the screen matters more than the number: the
    sign of a basis is the thing everyone gets backwards once.
    """
    if mid_spread is None or fair is None:
        return None
    return mid_spread - fair


def describe(mid_spread, swap_per_day, expiry, now):
    """Everything the ladder shows about fair value, in one dict."""
    days = days_to_expiry(expiry, now)
    fair = fair_spread(swap_per_day, days)
    gap = richness(mid_spread, fair)
    if fair is None:
        note = ('set the futures leg\'s expiry and swap per day on the '
                'Exchanges page and the fair spread appears here')
    elif days == 0:
        note = 'at or past expiry — fair value is the spot itself'
    else:
        note = (f'{swap_per_day:+g} per day x {days:g} days to '
                f'{parse_expiry(expiry)}')
    return {
        'fair_spread': fair,
        'days_to_expiry': days,
        'expiry': str(parse_expiry(expiry)) if parse_expiry(expiry) else None,
        'swap_per_day': swap_per_day,
        'gap': gap,
        'rich': None if gap is None else gap > 0,
        'note': note,
    }
