"""The spread the ladder displays, built from two ticks.

    spread = P_B - beta * P_A

Leg B minus the hedge ratio times leg A, and nothing else. The hedge
ratio is the same number that sizes the hedge, so the spread is exactly
the P&L of the pair per unit — which is why beta is structural and
cannot change under an open position.

Ported from the stat-arb system's `marketdata.py`, with the strategy
machinery (z-scores, fair value) left behind. What is kept is every
rule that came out of a live loss: the mid comes from the BOOK, a level
is read on the EXECUTABLE side for its own direction, and a position
reads the opposite executable side to close.
"""

import time as time_mod

from .models import SpreadSide


def compute_spread(pair, tick_a, tick_b, hedge_ratio=1.0, clock=time_mod.time):
    """Build the snapshot for one pair from its two legs' ticks.

    Ticks are plain dicts (`bid`, `ask`, `last`, `time`) as they come
    off the wire from a leg runner, so the coordinator never depends on
    MT5 objects.
    """
    beta = float(hedge_ratio or 1.0)
    bid_a, ask_a = float(tick_a['bid']), float(tick_a['ask'])
    bid_b, ask_b = float(tick_b['bid']), float(tick_b['ask'])

    # Identity of the two quotes this snapshot was built from. Two polls
    # that read the same pair of ticks are ONE observation of the
    # spread, however many times we looked — the staleness and jump
    # guards below both count quote EVENTS, not poll iterations. Prices
    # join the tick times because some brokers stamp ticks to the second.
    quote_id_a = f"{tick_a.get('time', '')}:{bid_a}/{ask_a}"
    quote_id_b = f"{tick_b.get('time', '')}:{bid_b}/{ask_b}"

    # The MID of the BOOK, always — never `tick.last`. A trade print
    # above the ask puts the "mid" above the long spread, i.e. above the
    # best price anyone can buy the spread at, and `short <= mid <= long`
    # stops being true exactly when it is being relied on.
    mid_a = (bid_a + ask_a) / 2.0
    mid_b = (bid_b + ask_b) / 2.0

    mid_spread = mid_b - beta * mid_a
    # SHORT the spread = sell B, buy A -> hit B's bid, lift A's ask.
    short_spread = bid_b - beta * ask_a
    # LONG the spread  = buy B, sell A -> lift B's ask, hit A's bid.
    long_spread = ask_b - beta * bid_a

    return {
        'pair_key': pair.key,
        'timestamp': clock(),
        'quote_id': f"{quote_id_a}|{quote_id_b}",
        'quote_id_a': quote_id_a,
        'quote_id_b': quote_id_b,
        'leg_a_bid': bid_a, 'leg_a_ask': ask_a, 'leg_a_mid': mid_a,
        'leg_b_bid': bid_b, 'leg_b_ask': ask_b, 'leg_b_mid': mid_b,
        # The broker's own stamps, and whether each symbol was in
        # Market Watch when it was read. A frozen leg is either not
        # subscribed or subscribed and receiving nothing.
        'leg_a_tick_time': tick_a.get('time'),
        'leg_b_tick_time': tick_b.get('time'),
        'leg_a_visible': tick_a.get('visible'),
        'leg_b_visible': tick_b.get('visible'),
        'leg_a_width': ask_a - bid_a,
        'leg_b_width': ask_b - bid_b,
        'hedge_ratio': beta,
        'spread': mid_spread,
        # The same number by the name the Algo reads. Without it the
        # Algo's live candles were never fed: its band was MT5's history
        # from the moment it was switched on, going stale all session.
        'mid_spread': mid_spread,
        'short_spread': short_spread,
        'long_spread': long_spread,
        # Exactly one round turn of both legs' bid-ask, in spread units:
        #   long - short = (ask_b - bid_b) + beta * (ask_a - bid_a)
        # This is the SAME quantity costs.crossing_cost charges in
        # dollars. Two views of one cost, never two costs (spec §2).
        'spread_cost': long_spread - short_spread,
        # Spelled out so the number on the ladder can be checked against
        # the two prices beside it (spec §11).
        'formula': f"spread = B - {beta:g} x A",
    }


def executable_spread(md, side, closing=False):
    """The spread THIS action can actually be done at.

    A price someone NAMED is compared against what the market OFFERS. A
    short position SELLS the spread to get in and BUYS it back to get
    out, so the same position reads a different touch at each end.
    Reading the favourable side at both ends is worse than using the
    mid: every trade then looks like it cleared its costs.

        SELL the spread   entering -> short_spread   exiting -> long_spread
        BUY  the spread   entering -> long_spread    exiting -> short_spread
    """
    if not md:
        return None
    selling = SpreadSide(getattr(side, 'value', side)) is SpreadSide.SELL
    if closing:
        selling = not selling
    value = md.get('short_spread' if selling else 'long_spread')
    return md.get('spread') if value is None else value


def closing_prices(md, side):
    """(leg A price, leg B price) this position would be CLOSED at.

    `executable_spread` answers the same question for the SPREAD; this
    answers it per LEG, because P&L is marked leg by leg. The two agree
    by construction — `B - beta * A` of the pair below IS the closing
    executable spread — and a test pins that.

        SELL the spread   long A, short B
                          -> close by SELLING A (hit the BID) and
                             BUYING B (lift the ASK)
        BUY  the spread   the mirror: A's ASK, B's BID
    """
    if not md:
        return None, None
    selling = SpreadSide(getattr(side, 'value', side)) is SpreadSide.SELL
    key_a, key_b = (('leg_a_bid', 'leg_b_ask') if selling
                    else ('leg_a_ask', 'leg_b_bid'))
    a, b = md.get(key_a), md.get(key_b)
    return (md.get('leg_a_mid') if a is None else a,
            md.get('leg_b_mid') if b is None else b)


class QuoteAgeTracker:
    """How long since each leg's quote last CHANGED, measured locally.

    Measured against `time.monotonic()` and the quote's own identity,
    NOT against the broker's timestamp: `tick.time - time.time()`
    conflates the broker's clock offset with how old the tick is, and a
    clock offset would poison a guard that gates real orders.

    Ages are None until a leg has been seen change twice. An unknown age
    is not a fresh one, but it must not read as stale on the first poll
    either — callers treat None as "no opinion".
    """

    def __init__(self, clock=time_mod.monotonic):
        self.clock = clock
        self._seen = {}

    def observe(self, key, md):
        """Stamp `leg_a_quote_age_sec` / `leg_b_quote_age_sec` and return them."""
        now = self.clock()
        ages = {}
        for leg in ('a', 'b'):
            qid = md.get(f'quote_id_{leg}')
            prev = self._seen.get((key, leg))
            if prev is None or prev[0] != qid:
                self._seen[(key, leg)] = (qid, now)
                age = 0.0 if prev is not None else None
            else:
                age = now - prev[1]
            md[f'leg_{leg}_quote_age_sec'] = age
            ages[leg] = age
        return ages['a'], ages['b']

    def forget(self, key):
        for leg in ('a', 'b'):
            self._seen.pop((key, leg), None)


def stale_quote(md, max_age_sec):
    """A one-line reason this snapshot must not price an order, or None.

    A pair is only as good as its WORSE leg: the spread is a difference,
    so one lagging quote makes the whole number fictitious even while
    the other leg ticks perfectly — a combined "108 quotes/min" reads
    healthy while one leg is frozen. `max_age_sec` of 0 turns it off.
    """
    if not md or not max_age_sec or max_age_sec <= 0:
        return None
    worst, name = None, None
    for label, field in (('Leg A', 'leg_a_quote_age_sec'),
                         ('Leg B', 'leg_b_quote_age_sec')):
        age = md.get(field)
        if age is not None and age > max_age_sec \
                and (worst is None or age > worst):
            worst, name = age, label
    if worst is None:
        return None
    return (f"{name}'s quote has not moved for {worst:.1f}s "
            f"(limit {max_age_sec:g}s) — the spread is stale")


class SpreadJumpTracker:
    """Catches the OTHER way a level can be a lie.

    `QuoteAgeTracker` finds a leg that has STOPPED. It cannot find a leg
    that lags during a fast move, because both legs are ticking hard —
    one is simply a moment behind the other, and the difference between
    them prints a spread neither book is offering. In the stat-arb
    system that cost one trade $20.40: a target fired on a print 8 sigma
    away that was gone within seconds, while the feed correctly read
    "oldest leg 0.0s".

    The scale is the spread's own sigma — of the LEVEL, not of the
    tick-to-tick change, so the threshold is generously wide and errs
    firmly towards letting a real move through. That is the right
    direction for a guard that can withhold an order.

    A jump makes the level unusable until the series has been quiet for
    `settle_sec`: a disturbance jumps twice, out and back, and one quote
    of quiet is not the end of it.
    """

    def __init__(self, clock=time_mod.monotonic):
        self.clock = clock
        self._last = {}       # key -> (quote_id, spread)
        self._until = {}      # key -> (expiry, jump, sigmas)

    def observe(self, key, md, sigma, max_sigmas, settle_sec):
        spread = (md or {}).get('spread')
        quote_id = (md or {}).get('quote_id')
        if spread is None:
            return None

        previous = self._last.get(key)
        moved = previous is not None and previous[0] != quote_id
        if previous is None or moved:
            self._last[key] = (quote_id, spread)

        # No sigma yet (cold start) or the guard is off: still TRACK the
        # series, so the first quote after warm-up has something to be
        # measured against, but hold no opinion.
        if not sigma or sigma <= 0 or not max_sigmas or max_sigmas <= 0:
            self._until.pop(key, None)
            return None

        now = self.clock()
        if moved:
            jump = abs(spread - previous[1])
            sigmas = jump / sigma
            md['spread_jump_sigmas'] = sigmas
            if sigmas > max_sigmas:
                self._until[key] = (now + max(settle_sec or 0.0, 0.0),
                                    jump, sigmas)

        pending = self._until.get(key)
        if pending is None:
            return None
        expiry, jump, sigmas = pending
        if now >= expiry:
            self._until.pop(key, None)
            return None
        return (f"the spread jumped {jump:.4g} ({sigmas:.1f} sigma) between "
                f"two quotes — one leg is lagging the other, so this level "
                f"is not one the market is offering")

    def forget(self, key):
        self._last.pop(key, None)
        self._until.pop(key, None)


class LevelSigma:
    """A rolling sigma of the spread LEVEL, for the jump guard only.

    Deliberately not a strategy input: nothing here signals, and this
    number's whole job is to give `SpreadJumpTracker` a unit. Counts
    quote EVENTS, so a fast poll on a slow feed does not inflate the
    sample.
    """

    def __init__(self, window=600):
        self.window = int(window)
        self._values = []
        self._last_quote_id = None

    def observe(self, md):
        quote_id = (md or {}).get('quote_id')
        spread = (md or {}).get('spread')
        if spread is None or quote_id == self._last_quote_id:
            return self.sigma
        self._last_quote_id = quote_id
        self._values.append(float(spread))
        if len(self._values) > self.window:
            del self._values[:len(self._values) - self.window]
        return self.sigma

    @property
    def samples(self):
        return len(self._values)

    @property
    def sigma(self):
        """None until there is enough to measure — unmeasured is not zero."""
        n = len(self._values)
        if n < 30:
            return None
        mean = sum(self._values) / n
        variance = sum((v - mean) ** 2 for v in self._values) / (n - 1)
        return variance ** 0.5
