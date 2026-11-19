"""The Algo's entry filters: is this stretch WORTH trading?

Ported from the stat-arb system's algo (its edge filter, its OU
probability filter, its regime check and its half-life band), with the
arithmetic moved onto this system's spread (`B - beta x A`), its candles
and its one money multiplier `k`. Pure: it is handed numbers and returns
numbers and reasons. It never reads a price or a broker, and a test
keeps it that way.

All money is for the Algo's own size: `k` is `spread_units` (money per
1.00 of spread, per one spread) and `qty` is the Algo qty in spreads.

- **Round-trip cost** — what one trade costs, all in: the bid-ask of
  the spread crossed once each way (entering on one touch, leaving on
  the other is one full width), commission on both legs both ways, and
  the slippage budget.
- **Edge** — the stat-arb rule "only take a trade I know can be
  profitable after ALL costs": the expected capture, `capture_frac x |z|
  x sigma`, in money, must be at least `multiple x` the round-trip cost.
  Capture is a FRACTION of the full reversion (0.5 by default) because a
  trade rarely gets all of it.
- **Probability / EV** — the Ornstein-Uhlenbeck gambler's ruin: from
  `|z|`, the chance the spread reverts to the exit before it runs to the
  stop, `P = 1 - erfi(|z|/sqrt2) / erfi(z_stop/sqrt2)`; and the expected
  value after costs. Below break-even z, below the minimum P, or a
  negative EV is no entry.
- **Regime** — a TRENDING spread is not mean-reverting, and a band on a
  trend is a band the price walks through. Kaufman's efficiency ratio
  (net move over path) high AND few crossings of the mean = TRENDING.
- **Half-life** — AR(1) on the candles: how long a stretch takes to
  halve. Too fast is noise; too slow will not revert inside a hold.

**Unmeasured is not zero.** Anything that cannot be computed is None,
and a filter that cannot be evaluated BLOCKS an entry, saying why — an
edge nobody could price is not an edge.
"""

import math


def round_trip_cost(width, k, qty, commission=None, slippage=None):
    """{crossing, commission, slippage, total} in money, or total None.

    `width` is the spread's own bid-ask (long_spread - short_spread);
    `commission` is the round turn for `qty` spreads, both legs;
    `slippage` the budget for `qty` spreads. None is unknown: the total
    is then unknown too, never the sum of what happened to be known.
    """
    body = {'crossing': None, 'commission': commission,
            'slippage': slippage, 'total': None}
    if width is None or not k or not qty:
        return body
    body['crossing'] = float(width) * float(k) * float(qty)
    if commission is None:
        return body
    body['total'] = (body['crossing'] + float(commission)
                     + float(slippage or 0.0))
    return body


def edge(z, sigma, k, qty, cost, capture_frac=0.5, multiple=1.5):
    """Expected capture against the round-trip cost.

    {capture, cost, ratio, required, ok}. `ok` is None when it cannot be
    priced — which the caller treats as a block.
    """
    body = {'capture': None, 'cost': cost, 'ratio': None,
            'required': multiple, 'ok': None}
    if z is None or not sigma or not k or not qty:
        return body
    capture = (float(capture_frac) * abs(float(z)) * float(sigma)
               * float(k) * float(qty))
    body['capture'] = capture
    if cost is None:
        return body
    if cost <= 0:
        body['ok'] = True
        return body
    body['ratio'] = capture / float(cost)
    body['ok'] = body['ratio'] >= float(multiple)
    return body


def erfi(x):
    """The imaginary error function, erfi(x) = -i erf(ix).

    A series below 4, the asymptotic form above — the same fallback the
    stat-arb system uses when scipy is not there.
    """
    x = abs(float(x))
    if x == 0:
        return 0.0
    if x > 4.0:
        return math.exp(min(x * x, 700.0)) / (math.sqrt(math.pi) * x)
    result, term, n = x, x, 0
    x2 = x * x
    while n < 200:
        n += 1
        term *= x2 / n
        step = term / (2 * n + 1)
        result += step
        if abs(step) < 1e-14 * abs(result):
            break
    return 2.0 / math.sqrt(math.pi) * result


def win_probability(z, stop_z):
    """P(the spread reverts to 0 before it runs to `stop_z`), from |z|."""
    z = abs(float(z))
    stop_z = abs(float(stop_z))
    if z >= stop_z:
        return 0.0
    denominator = erfi(stop_z / math.sqrt(2.0))
    if denominator <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - erfi(z / math.sqrt(2.0)) / denominator))


def probability(z, sigma, k, qty, cost, stop_z=4.0, exit_z=0.0,
                min_win=0.60, min_ev=0.0):
    """{breakeven_z, win, ev, ok, reason} — the OU / EV gate.

    `ok` None when it cannot be evaluated (no sigma or no cost), which
    blocks.
    """
    body = {'breakeven_z': None, 'win': None, 'ev': None, 'ok': None,
            'reason': None, 'min_win': min_win, 'min_ev': min_ev}
    if z is None or not sigma or not k or not qty or cost is None:
        body['reason'] = 'not priced yet'
        return body
    scale = float(sigma) * float(k) * float(qty)        # money per 1 z
    z = abs(float(z))
    body['breakeven_z'] = float(cost) / scale if scale > 0 else None
    win = win_probability(z, stop_z)
    body['win'] = win
    if win is None or body['breakeven_z'] is None:
        body['reason'] = 'not priced yet'
        return body
    profit = max(0.0, (z - float(exit_z)) * scale)
    loss = max(0.0, (float(stop_z) - z) * scale)
    body['ev'] = win * profit - (1.0 - win) * loss - float(cost)
    if z < body['breakeven_z']:
        body['ok'] = False
        body['reason'] = (f'|z| {z:.2f} is under break-even z '
                          f'{body["breakeven_z"]:.2f}')
    elif win < float(min_win):
        body['ok'] = False
        body['reason'] = (f'win chance {win:.0%} under '
                          f'{float(min_win):.0%}')
    elif body['ev'] < float(min_ev):
        body['ok'] = False
        body['reason'] = f'expected value {body["ev"]:,.2f} after costs'
    else:
        body['ok'] = True
    return body


def half_life(closes):
    """Mean-reversion half-life in CANDLES, by AR(1); None if the series
    is not reverting (phi outside 0..1) or too short to say."""
    values = [float(v) for v in closes or () if v is not None]
    if len(values) < 10:
        return None
    mean = sum(values) / len(values)
    y = [v - mean for v in values]
    lagged, ahead = y[:-1], y[1:]
    denominator = sum(a * a for a in lagged)
    if denominator <= 0:
        return None
    phi = sum(a * b for a, b in zip(ahead, lagged)) / denominator
    if phi <= 0 or phi >= 1:
        return None
    return math.log(2.0) / -math.log(phi)


def regime(closes, er_max=0.6, min_crossings=4):
    """{state, efficiency_ratio, crossings, slope} over `closes`.

    TRENDING when the efficiency ratio is at least `er_max` AND the
    series crossed its own mean `min_crossings` times or fewer; RANGE
    otherwise; COLLECTING while there are too few closes to say.
    """
    values = [float(v) for v in closes or () if v is not None]
    body = {'state': 'COLLECTING', 'efficiency_ratio': None,
            'crossings': None, 'slope': None}
    if len(values) < 10:
        return body
    path = sum(abs(b - a) for a, b in zip(values, values[1:]))
    net = values[-1] - values[0]
    er = abs(net) / path if path > 1e-12 else 0.0
    mean = sum(values) / len(values)
    signs = [(v > mean) - (v < mean) for v in values]
    crossings = sum(1 for a, b in zip(signs, signs[1:]) if a * b < 0)
    body.update(efficiency_ratio=er, crossings=crossings, slope=net,
                state=('TRENDING' if er >= float(er_max)
                       and crossings <= int(min_crossings) else 'RANGE'))
    return body


def atr(closes, period=14):
    """The average true range of the SPREAD, close to close, Wilder's
    smoothing - in spread points, or None with too few candles.

    A spread candle keeps its close only: the two legs make their highs
    and lows at different moments, so a spread high/low built from them
    is a price that never traded. The true range of a close-only series
    is the size of each move from one close to the next, and that is
    what is averaged here: period + 1 closes give the first value.
    """
    period = int(period)
    values = [float(c) for c in closes or () if c is not None]
    if period < 2 or len(values) < period + 1:
        return None
    moves = [abs(b - a) for a, b in zip(values, values[1:])]
    value = sum(moves[:period]) / period
    for move in moves[period:]:
        value = (value * (period - 1) + move) / period
    return value
