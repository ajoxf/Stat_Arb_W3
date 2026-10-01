"""Spread candles and TradingView-style bands (Bollinger Bands, basis = EMA).

Ported from arrow-statarb ``core/candles.py``, adapted for OKX crypto:
the market trades 24/7 and OKX's own candles sit on the UTC grid, so the
candle grid is anchored at UTC midnight (no exchange session open), and
1 H / 4 H are fetched natively instead of being rebuilt from 15-min bars.

Mirrors what a TradingView spread chart shows for ``k × A − B``:

  * Candles are built from each leg's LAST-TRADED price.
  * The CURRENT (still forming) candle is part of the series — the bands
    move while the candle is open.
  * basis = ta.ema(close, N): alpha = 2 / (N + 1), seeded with the SMA of
    the first N closes (Pine's own definition).
  * σ     = ta.stdev(close, N): population standard deviation of the last
    N closes (Pine's default, biased = true).

Candles survive restarts (saved to disk) and history is back-filled from
OKX, so the bands are ready at start-up instead of after a warm-up. Each
leg's close is stored separately, so a change of hedge ratio k re-prices
the series instead of invalidating it.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: timeframe key → seconds. The keys are what settings and the UI use.
TIMEFRAMES: Dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
TF_LABELS: Dict[str, str] = {"5m": "5 min", "15m": "15 min", "1h": "1 hour", "4h": "4 hours"}

#: How many candles each timeframe keeps. EMA needs a few × N candles to
#: settle to the chart's value; 600 is ample for N ≤ 200.
MAX_BARS = 600

#: Candles requested from OKX per timeframe on a back-fill.
BACKFILL_BARS = 300


def tf_seconds(tf: str) -> int:
    return TIMEFRAMES.get(str(tf).lower(), 900)


def normalise_tf(tf) -> str:
    t = str(tf or "15m").lower().replace(" ", "")
    aliases = {"5": "5m", "5min": "5m", "15": "15m", "15min": "15m", "60": "1h",
               "60m": "1h", "1hour": "1h", "240": "4h", "240m": "4h", "4hour": "4h"}
    t = aliases.get(t, t)
    return t if t in TIMEFRAMES else "15m"


# ── the two TradingView functions, exactly ───────────────────────────────────
def pine_ema(values: Sequence[float], length: int) -> Optional[float]:
    """ta.ema(src, length) on the LAST element: SMA-seeded, then
    ema = alpha·x + (1 − alpha)·ema[1] with alpha = 2 / (length + 1)."""
    n = int(length)
    if n < 1 or len(values) < n:
        return None
    alpha = 2.0 / (n + 1.0)
    ema = sum(values[:n]) / n
    for x in values[n:]:
        ema = alpha * x + (1.0 - alpha) * ema
    return ema


def pine_stdev(values: Sequence[float], length: int) -> Optional[float]:
    """ta.stdev(src, length) (biased = true) on the last ``length`` values."""
    n = int(length)
    if n < 1 or len(values) < n:
        return None
    w = values[-n:]
    avg = sum(w) / n
    var = sum((x - avg) ** 2 for x in w) / n
    return math.sqrt(max(var, 0.0))


# ── candle grid (UTC — matches OKX's own candles) ────────────────────────────
def bucket_start(ts: float, tf_sec: int) -> float:
    """Start (epoch s) of the candle holding ``ts`` on the UTC grid."""
    return math.floor(ts / tf_sec) * tf_sec


def _ohlc_row(r) -> Tuple[float, float, float, float, float]:
    """(ts, open, high, low, close) from a (ts, close) or (ts, o, h, l, c) row."""
    if len(r) >= 5:
        return float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
    c = float(r[1])
    return float(r[0]), c, c, c, c


def align_legs(a: List[Tuple], b: List[Tuple], tf_sec: int,
               k: float = 1.0) -> List[Tuple[float, float, float, float, float, float]]:
    """Merge two legs' candles into the SPREAD's candles on one grid:
    [(bucket, close_a, close_b, open, high, low)] — open/high/low of k·A − B.

    Rows are (ts, close) or (ts, open, high, low, close). A leg missing a
    source candle carries its last close forward (flat), as a spread chart
    does. The spread's o/h/l per source candle are the legs' o/h/l combined
    the way TradingView builds a spread symbol (k·A_x − B_x for each of
    o/h/l/c), widened to contain open and close, then rolled up into
    ``tf_sec`` buckets (first open, highest high, lowest low, last close)."""
    ra = {t: (o, h, l, c) for t, o, h, l, c in (_ohlc_row(r) for r in a) if c > 0}
    rb = {t: (o, h, l, c) for t, o, h, l, c in (_ohlc_row(r) for r in b) if c > 0}
    last_a = last_b = None
    out: Dict[float, List[float]] = {}
    order: List[float] = []
    for t in sorted(set(ra) | set(rb)):
        pa = ra.get(t) or ((last_a[3],) * 4 if last_a else None)
        pb = rb.get(t) or ((last_b[3],) * 4 if last_b else None)
        if pa is not None:
            last_a = pa
        if pb is not None:
            last_b = pb
        if pa is None or pb is None:
            continue
        sp = [k * pa[i] - pb[i] for i in range(4)]
        so, sc = sp[0], sp[3]
        hi, lo = max(sp), min(sp)
        bkt = bucket_start(t, tf_sec)
        cur = out.get(bkt)
        if cur is None:
            out[bkt] = [pa[3], pb[3], so, hi, lo]
            order.append(bkt)
        else:
            cur[0], cur[1] = pa[3], pb[3]
            cur[3], cur[4] = max(cur[3], hi), min(cur[4], lo)
    return [(t, *out[t]) for t in order]


#: history_provider(tf, bars) -> {"a": [(ts, o, h, l, c)...], "b": [...]} or None
HistoryFn = Callable[[str, int], Optional[Dict[str, List[Tuple]]]]


class SpreadCandles:
    """Candles of both legs on every timeframe, their bands, persistence and
    back-fill. Thread-safe; ``update`` is called on every price sample."""

    def __init__(self, persist_path: Optional[Path] = None,
                 history_provider: Optional[HistoryFn] = None,
                 key_provider: Optional[Callable[[], str]] = None,
                 clock: Callable[[], float] = time.time,
                 k_provider: Optional[Callable[[], float]] = None):
        self._path = Path(persist_path) if persist_path else None
        self._history = history_provider
        self._key_fn = key_provider or (lambda: "")
        self._clock = clock
        self._k = k_provider or (lambda: 1.0)
        self._lock = threading.RLock()
        # tf → {bucket_start: [close_a, close_b, open, high, low, k]} — the
        # spread's open/high/low for hedge ratio k; the close is always
        # k·close_a − close_b so a change of k re-prices the series.
        self._bars: Dict[str, Dict[float, List[float]]] = {tf: {} for tf in TIMEFRAMES}
        self._key: Optional[str] = None
        self._dirty = False
        self._last_save = 0.0
        # per-timeframe back-fill status for the UI
        self.status: Dict[str, Dict] = {tf: {"state": "pending", "detail": "", "at": 0.0}
                                        for tf in TIMEFRAMES}
        self._fill_thread: Optional[threading.Thread] = None
        self._ema_cache: Dict[Tuple, Tuple[float, float]] = {}
        self._load()

    # ── identity: a different pair is a different series ─────────────────────
    def _check_key(self) -> None:
        try:
            key = str(self._key_fn() or "")
        except Exception:
            return
        if not key:
            return
        if self._key is None:
            self._key = key
        elif key != self._key:
            logger.info("Candles: pair changed (%s → %s) — candles restart and "
                        "history is re-fetched", self._key, key)
            self._key = key
            self._bars = {tf: {} for tf in TIMEFRAMES}
            self._ema_cache.clear()
            self.status = {tf: {"state": "pending", "detail": "", "at": 0.0}
                           for tf in TIMEFRAMES}
            self._dirty = True

    # ── live ticks ────────────────────────────────────────────────────────────
    def update(self, ts: float, ltp_a: Optional[float], ltp_b: Optional[float],
               k: Optional[float] = None) -> None:
        """Fold one pair of last-traded prices into the current candle of every
        timeframe: the latest price is its close, and the spread's high / low
        widen to every price seen while the candle is open."""
        if not ltp_a or not ltp_b or ltp_a <= 0 or ltp_b <= 0:
            return
        k = float(k if k is not None else (self._k() or 1.0))
        a, b = float(ltp_a), float(ltp_b)
        sp = k * a - b
        with self._lock:
            self._check_key()
            for tf, sec in TIMEFRAMES.items():
                bars = self._bars[tf]
                bkt = bucket_start(ts, sec)
                bar = bars.get(bkt)
                if bar is not None and len(bar) >= 6 and bar[5] == k:
                    bars[bkt] = [a, b, bar[2], max(bar[3], sp), min(bar[4], sp), k]
                elif bar is not None:              # a close-only bar: it opened at its close
                    o = k * bar[0] - bar[1]
                    bars[bkt] = [a, b, o, max(o, sp), min(o, sp), k]
                else:
                    bars[bkt] = [a, b, sp, sp, sp, k]
                if len(bars) > MAX_BARS:
                    for t in sorted(bars)[:len(bars) - MAX_BARS]:
                        del bars[t]
            self._dirty = True
        self._maybe_save()

    # ── bands ─────────────────────────────────────────────────────────────────
    def closes(self, tf: str, k: float = 1.0) -> List[Tuple[float, float]]:
        """[(bucket_start, spread_close)] oldest first, current candle last."""
        tf = normalise_tf(tf)
        with self._lock:
            rows = sorted(self._bars[tf].items())
        return [(t, k * bar[0] - bar[1]) for t, bar in rows]

    def ohlc(self, tf: str, k: float = 1.0) -> List[Tuple[float, float, float, float, float]]:
        """[(bucket_start, open, high, low, close)] of the spread, oldest first."""
        tf = normalise_tf(tf)
        with self._lock:
            rows = sorted(self._bars[tf].items())
        out = []
        for t, bar in rows:
            c = k * bar[0] - bar[1]
            if len(bar) >= 6 and abs(bar[5] - k) < 1e-12:
                o = bar[2]
                out.append((t, o, max(bar[3], o, c), min(bar[4], o, c), c))
            else:
                out.append((t, c, c, c, c))
        return out

    def bands(self, tf: str, length: int, k: float = 1.0) -> Dict:
        """EMA basis and σ of the spread on ``tf`` with the current candle
        included — the numbers a TradingView BB(length, basis EMA) shows now."""
        tf = normalise_tf(tf)
        n = max(2, int(length))
        series = self.closes(tf, k)
        closes = [c for _, c in series]
        out = {"timeframe": tf, "length": n, "count": len(closes), "ready": False,
               "mean": None, "std": None,
               "last_candle": series[-1][0] if series else None,
               "status": dict(self.status.get(tf, {}))}
        if len(closes) < n:
            return out
        # EMA of everything BEFORE the current candle is fixed until that candle
        # closes — cache it, then roll the current close in (cheap per tick).
        prev = closes[:-1]
        ck = (tf, n, round(k, 10), len(prev), series[-2][0] if len(series) > 1 else None,
              prev[-1] if prev else None)
        if len(prev) >= n:
            hit = self._ema_cache.get(ck)
            if hit is None:
                ema_prev = pine_ema(prev, n)
                if len(self._ema_cache) > 64:
                    self._ema_cache.clear()
                self._ema_cache[ck] = (ema_prev, 0.0)
            else:
                ema_prev = hit[0]
            alpha = 2.0 / (n + 1.0)
            mean = alpha * closes[-1] + (1.0 - alpha) * ema_prev
        else:
            mean = pine_ema(closes, n)           # exactly N candles: the SMA seed
        std = pine_stdev(closes, n)
        out.update(mean=mean, std=std, ready=bool(std and std > 1e-12))
        return out

    def series(self, tf: str, length: int, k: float = 1.0, last: int = 200) -> Dict:
        """Chart data: OHLC with the EMA / σ at each candle (current included)."""
        tf = normalise_tf(tf)
        n = max(2, int(length))
        rows = self.ohlc(tf, k)
        closes = [r[4] for r in rows]
        alpha = 2.0 / (n + 1.0)
        pts, ema = [], None
        for i, (t, o, h, l, c) in enumerate(rows):
            pt = {"t": t, "open": o, "high": h, "low": l, "close": c,
                  "mean": None, "std": None}
            if i + 1 >= n:
                ema = sum(closes[:n]) / n if ema is None else alpha * c + (1.0 - alpha) * ema
                pt.update(mean=ema, std=pine_stdev(closes[:i + 1], n))
            pts.append(pt)
        return {"timeframe": tf, "length": n, "points": pts[-last:]}

    # ── back-fill ─────────────────────────────────────────────────────────────
    def backfill_async(self, force: bool = False) -> bool:
        """Fetch history for every timeframe in the background (no-op while
        one runs). Timeframes filled recently are skipped unless forced."""
        if self._history is None:
            return False
        if self._fill_thread is not None and self._fill_thread.is_alive():
            return False
        self._fill_thread = threading.Thread(target=self._backfill, args=(force,),
                                             daemon=True, name="CandleBackfill")
        self._fill_thread.start()
        return True

    def _backfill(self, force: bool) -> None:
        now = self._clock()
        with self._lock:
            self._check_key()
        for tf, sec in TIMEFRAMES.items():
            st = self.status.get(tf, {})
            if not force and st.get("state") == "ok" and now - float(st.get("at", 0)) < 3600:
                continue
            self.status[tf] = {"state": "loading", "detail": "", "at": now}
            try:
                got = self._history(tf, BACKFILL_BARS)
            except Exception as exc:              # noqa: BLE001 — reported, not raised
                got, err = None, str(exc)
            else:
                ok = bool(got and got.get("a") and got.get("b"))
                err = "" if ok else "the exchange returned no history for a leg"
                got = got if ok else None
            if not got:
                self.status[tf] = {"state": "failed", "detail": err or "no history",
                                   "at": now}
                logger.warning("Candles: %s history unavailable — %s",
                               tf, self.status[tf]["detail"])
                continue
            k_now = float(self._k() or 1.0)
            merged = align_legs(got["a"], got["b"], sec, k_now)
            with self._lock:
                bars = self._bars[tf]
                current = bucket_start(now, sec)
                for t, ca, cb, so, sh, sl in merged:
                    if t == current and t in bars:
                        continue                  # the live candle keeps live prices
                    bars[t] = [ca, cb, so, sh, sl, k_now]
                for t in sorted(bars)[:max(0, len(bars) - MAX_BARS)]:
                    del bars[t]
                self._ema_cache.clear()
                self._dirty = True
            self.status[tf] = {"state": "ok",
                               "detail": f"{len(merged)} candles from OKX",
                               "at": now}
            logger.info("Candles: %s back-filled — %d candles", tf, len(merged))
        self._maybe_save(force=True)

    # ── persistence ───────────────────────────────────────────────────────────
    def _maybe_save(self, force: bool = False) -> None:
        if not self._path or not self._dirty:
            return
        now = self._clock()
        if not force and now - self._last_save < 30.0:
            return
        with self._lock:
            data = {"key": self._key,
                    "saved_at": now,
                    "bars": {tf: [[t, *bar] for t, bar in sorted(bars.items())]
                             for tf, bars in self._bars.items()}}
            self._dirty = False
            self._last_save = now
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._path)
        except Exception as exc:                      # noqa: BLE001
            logger.warning("Candles: could not save %s: %s", self._path, exc)

    def save(self) -> None:
        self._dirty = True
        self._maybe_save(force=True)

    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except Exception as exc:                      # noqa: BLE001
            logger.warning("Candles: could not read %s: %s", self._path, exc)
            return
        try:
            key_now = str(self._key_fn() or "")
        except Exception:
            key_now = ""
        if key_now and data.get("key") and data["key"] != key_now:
            logger.info("Candles: saved candles are for another pair — not loaded")
            return
        self._key = data.get("key") or key_now or None
        for tf, rows in (data.get("bars") or {}).items():
            if tf in self._bars:
                self._bars[tf] = {float(r[0]): [float(x) for x in r[1:]]
                                  for r in rows if len(r) >= 3}
        n = {tf: len(b) for tf, b in self._bars.items()}
        logger.info("Candles: restored from disk — %s", n)
        for tf, cnt in n.items():
            if cnt:
                self.status[tf] = {"state": "restored",
                                   "detail": f"{cnt} candles from disk",
                                   "at": float(data.get("saved_at") or 0)}

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {tf: len(b) for tf, b in self._bars.items()}
