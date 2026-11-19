"""Bollinger bands on the SPREAD: candles, an EMA middle, and sigma.

The Algo's whole statistical input, and nothing else. It is arithmetic
on numbers it is handed — it never reads a price, a broker or a clock
of its own, so the same code answers for a backfill from MT5's bars, a
restart from the database and a live tick, and a test can drive all
three.

The convention is TradingView's Bollinger with an EMA basis, because
that is the chart a trader checks it against:

- **candles** of `timeframe_sec`, each closing on the last spread seen
  in its bucket. The candle still FORMING counts: its close is the live
  mid spread, so the middle line and the band move with the market the
  way they do on a chart;
- **middle** = Pine's `ta.ema(close, N)`: seeded with the simple average
  of the first N closes, then `alpha = 2 / (N + 1)` over every close
  after it. The more history behind it, the closer it sits to the
  chart's own value, which is why more than N candles are kept;
- **sigma** = Pine's `ta.stdev(close, N)`: the POPULATION standard
  deviation of the last N closes, forming candle included.

**Unmeasured is not zero.** Fewer than N closes, or a flat series, is
not ready: `stats()` says how many it has and how many it needs, and
returns no mean and no sigma rather than a band drawn from nothing.
"""

import math

#: How many closed candles are KEPT, as a multiple of N. An EMA seeded
#: on N closes has not forgotten its seed until a few N later; keeping
#: five N is what makes the middle line agree with a chart's.
KEEP_MULTIPLE = 5

#: Below this, sigma is a flat line and a z-score is a division by it.
MIN_SIGMA = 1e-12


def bucket_of(at, timeframe_sec, anchor=0.0):
    """The start of the candle `at` (UTC seconds) falls in.

    `anchor` is the broker's offset from UTC, so a 15-minute candle
    starts where the broker's own chart starts it — on a GMT+2 broker,
    10:00 broker time — rather than wherever UTC happens to put it.
    """
    tf = float(timeframe_sec)
    return math.floor((float(at) + anchor) / tf) * tf - anchor


class SpreadCandles:
    """Closed candles of one pair's spread, plus the one forming now."""

    def __init__(self, timeframe_sec, length, anchor=0.0):
        self.timeframe_sec = float(timeframe_sec)
        self.length = int(length)
        self.anchor = float(anchor or 0.0)
        #: bucket start -> close, for CLOSED candles only.
        self.closed = {}
        self.forming = None          # (bucket, close) or None

    # -- feeding it ---------------------------------------------------------

    def seed(self, rows):
        """Closed candles from history: [(bucket, close), ...].

        History never overwrites a candle this process watched close
        itself — what we saw is the better record of our own spread.
        The forming candle's bucket is skipped: it is not closed yet.
        """
        added = 0
        for bucket, close in rows or ():
            if close is None:
                continue
            bucket = float(bucket)
            if self.forming and bucket >= self.forming[0]:
                continue
            if bucket not in self.closed:
                self.closed[bucket] = float(close)
                added += 1
        self._trim()
        return added

    def observe(self, at, spread):
        """One live mid spread at `at` (UTC seconds).

        Returns the candle that CLOSED on this observation, as
        (bucket, close), or None — the caller saves closed candles so a
        restart does not start from nothing.
        """
        if spread is None:
            return None
        bucket = bucket_of(at, self.timeframe_sec, self.anchor)
        finished = None
        if self.forming is not None and bucket > self.forming[0]:
            finished = self.forming
            self.closed[finished[0]] = finished[1]
            self._trim()
        if self.forming is None or bucket >= self.forming[0]:
            self.forming = (bucket, float(spread))
        return finished

    def _trim(self):
        keep = max(self.length * KEEP_MULTIPLE, self.length + 1)
        if len(self.closed) > keep:
            for bucket in sorted(self.closed)[:len(self.closed) - keep]:
                del self.closed[bucket]

    # -- reading it ---------------------------------------------------------

    def closes(self):
        """Every close in time order, the forming candle last."""
        values = [self.closed[b] for b in sorted(self.closed)]
        if self.forming is not None:
            values.append(self.forming[1])
        return values

    def stats(self):
        """{'ready', 'count', 'needed', 'mean', 'sigma'}.

        Not ready is said with the count, so the screen can show
        "collecting 7/20 candles" instead of a band it cannot draw.
        """
        values = self.closes()
        body = {'ready': False, 'count': len(values), 'needed': self.length,
                'mean': None, 'sigma': None}
        if len(values) < self.length or self.length < 1:
            return body
        mean = ema(values, self.length)
        sigma = population_stdev(values[-self.length:])
        if mean is None or sigma is None or sigma < MIN_SIGMA:
            body['note'] = 'the spread has not moved — no band to measure'
            return body
        body.update(ready=True, mean=mean, sigma=sigma)
        return body


def ema(values, length):
    """Pine's `ta.ema`: an SMA of the first `length`, then recursive."""
    if length < 1 or len(values) < length:
        return None
    alpha = 2.0 / (length + 1.0)
    value = sum(values[:length]) / float(length)
    for close in values[length:]:
        value = alpha * close + (1.0 - alpha) * value
    return value


def population_stdev(values):
    """Pine's `ta.stdev`: divided by N, not N - 1."""
    if not values:
        return None
    mean = sum(values) / float(len(values))
    return math.sqrt(sum((v - mean) ** 2 for v in values) / float(len(values)))


def spread_bars(bars_a, bars_b, beta, timeframe_sec, offset_a, offset_b,
                anchor=0.0):
    """MT5 bars for the two legs, as closed SPREAD candles.

    `bars_*` are what `broker.rates` returns: {'time', 'close', 'spread',
    'point'}, with `time` on the BROKER's wall clock. Each leg is moved
    to UTC by its own broker's offset — two brokers are two clocks —
    and the two are matched on the candle they fall in. A candle only
    one leg traded in is DROPPED, not filled in: a spread needs both.

    MT5's bars are priced on the BID. The spread this system trades is
    built from the mid, so each close is moved half the bar's own
    recorded spread up. That field is the broker's figure for the bar
    and is approximate, but it is applied to both legs the same way.

    Returns [] when either leg's clock is unmeasured: a bar whose hour
    cannot be placed cannot be matched, and a guess would pair the
    wrong two prices.
    """
    if offset_a is None or offset_b is None:
        return []
    beta = float(beta or 1.0)

    def by_bucket(bars, offset):
        out = {}
        for bar in bars or ():
            close = bar.get('close')
            if close is None or bar.get('time') is None:
                continue
            half = (float(bar.get('spread') or 0.0)
                    * float(bar.get('point') or 0.0) / 2.0)
            utc = float(bar['time']) - float(offset)
            out[bucket_of(utc, timeframe_sec, anchor)] = float(close) + half
        return out

    a = by_bucket(bars_a, offset_a)
    b = by_bucket(bars_b, offset_b)
    return [(bucket, b[bucket] - beta * a[bucket])
            for bucket in sorted(set(a) & set(b))]


def spread_ohlc(bars_a, bars_b, beta, sub_sec, candle_sec, offset_a,
                offset_b, anchor=0.0):
    """MT5 bars for the two legs, as SPREAD candles with an open, a high,
    a low and a close.

    A spread's high is not leg B's high less beta times leg A's: the two
    highs are seldom the same minute. So the candle is built from FINER
    bars, each matched across the two legs exactly as `spread_bars`
    matches them - a 15-minute candle from fifteen 1-minute spreads -
    and its open, high, low and close are those spreads'. The candle
    still forming is kept: it is the right-hand end of a chart.

    [] when either leg's clock is unmeasured (see `spread_bars`).
    """
    fine = spread_bars(bars_a, bars_b, beta, sub_sec, offset_a, offset_b,
                       anchor=anchor)
    candles = []
    for bucket, close in fine:
        start = bucket_of(bucket, candle_sec, anchor)
        if candles and candles[-1]['t'] == start:
            candle = candles[-1]
            candle['h'] = max(candle['h'], close)
            candle['l'] = min(candle['l'], close)
            candle['c'] = close
        else:
            candles.append({'t': start, 'o': close, 'h': close, 'l': close,
                            'c': close})
    return candles


def round_offset(offset, step=900.0):
    """A broker's offset to the nearest quarter hour.

    It is measured off the newest tick, so it carries the second or two
    since that tick. Every real broker sits on a quarter hour (even
    GMT+5:45 does), and a candle boundary needs the exact one.
    """
    if offset is None:
        return None
    return round(float(offset) / step) * step
