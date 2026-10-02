"""Server-side signal engine — the SINGLE source of truth for the spread/z.

Both the auto-trader (``core/algo_trader.py``) and the web dashboard read this
one engine, so the z-score they show/act on is always identical (the alignment
requirement). It samples live leg prices into a TIME-based rolling window
(``window_minutes``, default 120) every ``sample_interval_sec``, and computes
mean/std/z plus a mean-reversion half-life over that window.

Convention (ported from arrow-statarb):
    spread = k × leg_a − leg_b ; z = (spread − mean) / std
    Leg A = SPOT (BTC-USDT) · Leg B = PERP (BTC-USDT-SWAP) · k = 1

The window is built from the MID of each leg's book (last trade when the feed
carries no book). Two EXECUTABLE prices are published beside it:

    sell_spread = k × bid_a − ask_b    (what selling the spread gets: sell A, buy B)
    buy_spread  = k × ask_a − bid_b    (what buying the spread costs:  buy A, sell B)

each with its own z against the same mean/std, so an entry or exit is judged on
the price it would actually trade at, not the mid.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class SignalEngine:
    """Samples the spread into a time window and serves the live signal."""

    def __init__(
        self,
        params_provider: Callable[[], Dict],
        book_provider: Callable[[], Optional[Dict]],
        persist_path: Optional[Path] = None,
        series_key_provider: Optional[Callable[[], str]] = None,
        candles=None,
    ):
        # Optional SpreadCandles: TradingView-style candle bands (EMA basis).
        # Fed with every sample; used for mean/σ when ``band_source`` is
        # "candles" (and always published per timeframe, so a position keeps
        # the bands it entered with when the setting changes).
        self.candles = candles
        # Top-of-book source: {"leg_a": {bid, ask, ltp, ts}, "leg_b": {…}}.
        self._book_provider = book_provider
        self._book: Optional[Dict] = None        # latest book, for get_signal
        self._sample_status = ""                 # why the last sample was skipped
        self._k_last: Optional[float] = None     # hedge ratio the window was built with
        self._stats_cache: Optional[Tuple[float, float, float]] = None
        self._params = params_provider
        # Optional disk persistence of the rolling window so a quick restart
        # resumes the warm-up instead of re-collecting it.
        self._persist_path = Path(persist_path) if persist_path else None
        self._series_key_provider = series_key_provider
        self._last_persist = 0.0

        # (timestamp, leg_a, leg_b, spread)
        self._samples: Deque[Tuple[float, float, float, float]] = deque()
        # z of each sample AS OF the moment it was taken, aligned to the TAIL of
        # _samples (restored samples have none). The chart draws these so past
        # points hold still instead of being re-scored against today's mean.
        self._z_hist: Deque[float] = deque()
        self._lock = threading.RLock()

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self.running = False
        self._last_error = ""

        # ── z-score excursion counters (session-cumulative; manual reset) ────
        self._exc: Dict = self._fresh_exc()
        self._exc_events: Deque[Dict] = deque(maxlen=500)
        self._z_prev: Optional[float] = None
        # per-side arming so boundary chatter near a band isn't double-counted
        self._arm_2u = self._arm_3u = self._arm_2d = self._arm_3d = True
        self._active_up = self._active_dn = False
        # Regime detection anchor, frozen once per UTC day after warm-up.
        self._anchor: Optional[float] = None
        self._anchor_day: Optional[int] = None

    @staticmethod
    def _fresh_exc() -> Dict:
        return {"touch_2_up": 0, "touch_2_down": 0, "touch_3_up": 0,
                "touch_3_down": 0, "reversions": 0, "max_z": 0.0, "min_z": 0.0,
                "since": time.time()}

    # ── params ───────────────────────────────────────────────────────────────
    def _p(self) -> Dict:
        p = {
            "window_minutes": 120.0,
            "sample_interval_sec": 0.5,
            "min_signal_minutes": 90.0,
            "entry_zscore": 2.5,
            "exit_zscore": 0.0,
            "stop_zscore": 4.0,
            # Recompute the rolling mean/std only this often (cache between),
            # so the bands stay STABLE instead of shifting every tick.
            # 0 = recompute every tick (bands drift).
            "stats_update_interval_sec": 0.0,
            # spread = hedge_ratio × leg_a − leg_b. 1.0 for spot-vs-perp.
            "hedge_ratio": 1.0,
            # Skip a sample when either leg's last tick is older than this (s).
            "max_quote_age_sec": 60.0,
        }
        try:
            p.update({k: float(v) for k, v in (self._params() or {}).items()
                      if k in p and v is not None})
        except Exception:
            pass
        return p

    def _band_params(self) -> Tuple[str, str, int]:
        """(band_source, timeframe, length) — 'ticks' unless candles are set up."""
        from core.spread_candles import normalise_tf
        try:
            raw = self._params() or {}
        except Exception:
            raw = {}
        src = str(raw.get("band_source", "ticks") or "ticks").lower()
        if src != "candles" or self.candles is None:
            src = "ticks"
        try:
            n = max(2, int(float(raw.get("band_length", 20) or 20)))
        except (TypeError, ValueError):
            n = 20
        return src, normalise_tf(raw.get("band_timeframe", "15m")), n

    # ── control ──────────────────────────────────────────────────────────────
    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._restore()
            self._stop_evt.clear()
            self.running = True
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="SignalEngine")
            self._thread.start()
            logger.info("SignalEngine: started")
            return True

    def stop(self) -> None:
        self._stop_evt.set()
        self.running = False
        self._persist()
        logger.info("SignalEngine: stopped")

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._z_hist.clear()
            self._stats_cache = None
            self._reset_exc_state()
            self._clear_persisted()

    def _reset_exc_state(self) -> None:
        self._z_prev = None
        self._arm_2u = self._arm_3u = self._arm_2d = self._arm_3d = True
        self._active_up = self._active_dn = False

    def reset_excursions(self) -> None:
        with self._lock:
            self._exc = self._fresh_exc()
            self._exc_events.clear()
            self._reset_exc_state()
        logger.info("SignalEngine: excursion counters reset")

    def get_excursions(self, max_events: int = 100) -> Dict:
        with self._lock:
            e = dict(self._exc)
            events = list(self._exc_events)
        e["touch_2_total"] = e["touch_2_up"] + e["touch_2_down"]
        e["touch_3_total"] = e["touch_3_up"] + e["touch_3_down"]
        e["since_sec"] = round(max(0.0, time.time() - e.get("since", time.time())), 0)
        e["max_z"] = round(e["max_z"], 2)
        e["min_z"] = round(e["min_z"], 2)
        e["event_count"] = len(events)
        e["events"] = list(reversed(events))[:max_events]
        return e

    def _tally_z(self, z: float, ts: Optional[float] = None) -> None:
        """Count ±2σ/±3σ touches (outward crossing, hysteresis re-arm at |z|<1)
        and reversions (an open ≥2σ excursion crossing back through the mean)."""
        ts = time.time() if ts is None else ts
        e = self._exc
        if z > e["max_z"]:
            e["max_z"] = z
        if z < e["min_z"]:
            e["min_z"] = z

        if z >= 2.0 and self._arm_2u:
            e["touch_2_up"] += 1; self._arm_2u = False; self._active_up = True
            self._log_event("touch_2_up", z, ts)
        if z >= 3.0 and self._arm_3u:
            e["touch_3_up"] += 1; self._arm_3u = False
            self._log_event("touch_3_up", z, ts)
        if z < 1.0:
            self._arm_2u = self._arm_3u = True

        if z <= -2.0 and self._arm_2d:
            e["touch_2_down"] += 1; self._arm_2d = False; self._active_dn = True
            self._log_event("touch_2_down", z, ts)
        if z <= -3.0 and self._arm_3d:
            e["touch_3_down"] += 1; self._arm_3d = False
            self._log_event("touch_3_down", z, ts)
        if z > -1.0:
            self._arm_2d = self._arm_3d = True

        if self._z_prev is not None:
            crossed_zero = (self._z_prev > 0 >= z) or (self._z_prev < 0 <= z)
            if crossed_zero and (self._active_up or self._active_dn):
                e["reversions"] += 1
                self._active_up = self._active_dn = False
                self._log_event("reversion", z, ts)
        self._z_prev = z

    def _log_event(self, etype: str, z: float, ts: float) -> None:
        self._exc_events.append({"ts": ts,
                                 "time": time.strftime("%H:%M:%S", time.gmtime(ts)),
                                 "type": etype, "z": round(z, 2)})

    # ── sampling loop ────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            interval = 0.5
            try:
                interval = max(0.05, self._p()["sample_interval_sec"])
                self.sample_once()
                self._maybe_persist()
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("SignalEngine: sample error")
            self._stop_evt.wait(interval)

    @staticmethod
    def _mid(leg: Optional[Dict]) -> Optional[float]:
        """Mid of a leg's book, else its last trade, else None."""
        if not leg:
            return None
        bid, ask = leg.get("bid"), leg.get("ask")
        if bid and ask and bid > 0 and ask > 0:
            return (float(bid) + float(ask)) / 2.0
        ltp = leg.get("ltp")
        return float(ltp) if ltp and ltp > 0 else None

    def _read_prices(self, now: float) -> Tuple[Optional[float], Optional[float]]:
        """(leg_a, leg_b) to sample — book mids — or (None, None) with
        ``_sample_status`` saying why nothing was sampled."""
        book = None
        try:
            book = self._book_provider()
        except Exception:
            book = None
        if book and book.get("leg_a") and book.get("leg_b"):
            a, b = book["leg_a"], book["leg_b"]
            max_age = float(self._p().get("max_quote_age_sec", 0) or 0)
            if max_age > 0:
                ages = [now - float(x.get("ts") or 0) for x in (a, b) if x.get("ts")]
                if ages and max(ages) > max_age:
                    self._book = book
                    self._sample_status = f"stale quotes (last tick {max(ages):.0f}s ago)"
                    return None, None
            self._book = book
            la, lb = self._mid(a), self._mid(b)
            if la is not None and lb is not None:
                self._sample_status = ""
                return la, lb
        self._book = book if book else None
        self._sample_status = "waiting for prices"
        return None, None

    def _check_hedge_ratio(self, k: float) -> None:
        """A window mixes spreads from ONE hedge ratio only."""
        if self._k_last is not None and abs(k - self._k_last) > 1e-12 and self._samples:
            logger.info("SignalEngine: hedge ratio changed %s → %s — window reset",
                        self._k_last, k)
            self._samples.clear()
            self._z_hist.clear()
            self._stats_cache = None
            self._reset_exc_state()
        self._k_last = k

    def sample_once(self, now: Optional[float] = None) -> None:
        """Take one spread sample from the live feed and trim the window."""
        now = time.time() if now is None else now
        la, lb = self._read_prices(now)
        if la is None or lb is None or la <= 0 or lb <= 0:
            return
        k = self._p().get("hedge_ratio", 1.0) or 1.0
        spread = k * float(la) - float(lb)
        window_sec = self._p()["window_minutes"] * 60.0
        band = self._candle_band(k)
        with self._lock:
            self._check_hedge_ratio(k)
            self._samples.append((now, float(la), float(lb), spread))
            self._trim(now, window_sec)
            stats = self._update_excursions_locked(now)
            self._z_hist.append(self._point_z(spread, band, stats))
        self._feed_candles(now, la, lb)

    def _feed_candles(self, now: float, la: float, lb: float) -> None:
        """Candles are built from each leg's LAST TRADE, as a TradingView
        spread chart is; the book mid is only the fallback without an LTP."""
        if self.candles is None:
            return
        book = self._book or {}
        ta = (book.get("leg_a") or {}).get("ltp") or la
        tb = (book.get("leg_b") or {}).get("ltp") or lb
        try:
            self.candles.update(now, float(ta), float(tb),
                                k=float(self._p().get("hedge_ratio", 1.0) or 1.0))
        except Exception as exc:                      # noqa: BLE001 — never stop sampling
            logger.debug("SignalEngine: candle update failed: %s", exc)

    def push(self, leg_a: float, leg_b: float, ts: Optional[float] = None) -> None:
        """Inject a sample directly (used by tests)."""
        ts = time.time() if ts is None else ts
        k = self._p().get("hedge_ratio", 1.0) or 1.0
        spread = k * float(leg_a) - float(leg_b)
        band = self._candle_band(k)
        with self._lock:
            self._check_hedge_ratio(k)
            self._samples.append((ts, float(leg_a), float(leg_b), spread))
            self._trim(ts, self._p()["window_minutes"] * 60.0)
            stats = self._update_excursions_locked(ts)
            self._z_hist.append(self._point_z(spread, band, stats))

    def _candle_band(self, k: float) -> Optional[Tuple[float, float]]:
        """(mean, std) of the candle bands when they are the band source
        ((0, 0) until they are ready), else None for the tick window."""
        src, tf, n_len = self._band_params()
        if src != "candles" or self.candles is None:
            return None
        try:
            b = self.candles.bands(tf, n_len, k)
        except Exception:
            return 0.0, 0.0
        return (float(b["mean"]), float(b["std"])) if b.get("ready") else (0.0, 0.0)

    @staticmethod
    def _point_z(spread: float, band: Optional[Tuple[float, float]],
                 stats: Optional[Tuple[float, float]]) -> float:
        mean, std = band if band is not None else (stats or (0.0, 0.0))
        return (spread - mean) / std if std > 1e-12 else 0.0

    def _update_excursions_locked(self, ts: Optional[float] = None
                                  ) -> Optional[Tuple[float, float]]:
        """Tally the newest sample's z; returns the tick window's (mean, std)."""
        if len(self._samples) < 2:
            return None
        spreads = [s[3] for s in self._samples]
        mean, std = self.compute_stats(spreads)
        if std <= 1e-12:
            return None
        self._tally_z((spreads[-1] - mean) / std, ts)
        return mean, std

    def _trim(self, now: float, window_sec: float) -> None:
        while self._samples and (now - self._samples[0][0]) > window_sec:
            self._samples.popleft()
        while len(self._z_hist) > len(self._samples):
            self._z_hist.popleft()

    # ── window persistence (resume warm-up across a quick restart) ───────────
    def _series_key(self) -> str:
        try:
            return str(self._series_key_provider()) if self._series_key_provider else ""
        except Exception:
            return ""

    def _persist_cfg(self) -> Tuple[bool, float, float]:
        p = self._params() or {}
        enabled = bool(p.get("persist_window", True))
        gap = float(p.get("resume_max_gap_min", 10.0) or 0.0) * 60.0
        every = float(p.get("persist_interval_sec", 30.0) or 30.0)
        return enabled, gap, every

    def _maybe_persist(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        _, _, every = self._persist_cfg()
        if now - self._last_persist >= max(5.0, every):
            self._persist(now)

    def _persist(self, now: Optional[float] = None) -> None:
        if not self._persist_path:
            return
        enabled, _, _ = self._persist_cfg()
        if not enabled:
            return
        key = self._series_key()
        if not key:
            return
        now = time.time() if now is None else now
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return
        payload = {"series_key": key,
                   "hedge_ratio": self._p().get("hedge_ratio", 1.0) or 1.0,
                   "window_minutes": self._p()["window_minutes"],
                   "saved_at": now,
                   "samples": [list(s) for s in samples]}
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(payload, f)
            tmp.replace(self._persist_path)
            self._last_persist = now
        except Exception as exc:
            logger.warning("SignalEngine: could not persist window — %s", exc)

    def _clear_persisted(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _restore(self) -> None:
        """Repopulate the window from disk IFF it is safe to resume (same leg
        series + hedge ratio, samples within the window, gap under the cap)."""
        if not self._persist_path or not self._persist_path.exists():
            return
        enabled, max_gap, _ = self._persist_cfg()
        if not enabled:
            return
        try:
            with open(self._persist_path) as f:
                data = json.load(f) or {}
        except Exception as exc:
            logger.warning("SignalEngine: could not read persisted window — %s", exc)
            return
        cur_key = self._series_key()
        if not cur_key or data.get("series_key") != cur_key:
            logger.info("SignalEngine: window not restored — leg series changed")
            return
        k_now = self._p().get("hedge_ratio", 1.0) or 1.0
        if abs(float(data.get("hedge_ratio", k_now) or k_now) - k_now) > 1e-12:
            logger.info("SignalEngine: window not restored — hedge ratio changed")
            return
        now = time.time()
        window_sec = self._p()["window_minutes"] * 60.0
        kept = [tuple(s) for s in (data.get("samples") or [])
                if isinstance(s, (list, tuple)) and len(s) == 4
                and (now - s[0]) <= window_sec]
        if not kept:
            return
        kept.sort(key=lambda s: s[0])
        gap = now - kept[-1][0]
        if max_gap > 0 and gap > max_gap:
            logger.info("SignalEngine: window not restored — last sample %.1f min old",
                        gap / 60.0)
            return
        with self._lock:
            self._samples = deque(kept)
            self._z_hist.clear()
            self._stats_cache = None
            self._k_last = k_now
            self._reset_exc_state()
        logger.info("SignalEngine: restored %d samples spanning %.1f min",
                    len(kept), (kept[-1][0] - kept[0][0]) / 60.0)

    # ── stats ────────────────────────────────────────────────────────────────
    @staticmethod
    def compute_stats(spreads: List[float]) -> Tuple[float, float]:
        """(mean, std) with sample std (ddof=1); std floored away from zero."""
        if len(spreads) < 2:
            return (float(spreads[0]) if spreads else 0.0, 0.0)
        arr = np.asarray(spreads, dtype=float)
        mean = float(np.mean(arr))
        std = float(np.std(arr, ddof=1))
        if std < 1e-10:
            std = 0.0
        return mean, std

    @staticmethod
    def half_life(spreads: List[float]) -> float:
        """Mean-reversion half-life in SAMPLES via AR(1). 0.0 if not reverting."""
        if len(spreads) < 30:
            return 0.0
        data = np.asarray(spreads, dtype=float)
        y = data - np.mean(data)
        y_lag, y_t = y[:-1], y[1:]
        den = float(np.dot(y_lag, y_lag))
        if den == 0:
            return 0.0
        phi = float(np.dot(y_t, y_lag)) / den
        if phi <= 0 or phi >= 1:
            return 0.0
        return max(0.0, float(np.log(2) / (-np.log(phi))))

    def regime(self, spreads: Optional[List[float]] = None,
               ts_now: Optional[float] = None) -> Dict:
        """Classify the spread's regime over the trailing window:
          • efficiency ratio (Kaufman) = |net move| ÷ path length → 1 = trending;
          • zero-crossings of (S − anchor): few = trending, many = reverting;
          • variance ratio VR(k): >1 trending, <1 reverting, ≈1 random walk.
        state ∈ {TRENDING, RANGE, UNKNOWN}; the anchor freezes once per UTC day."""
        p = self._params() or {}
        if spreads is None:
            with self._lock:
                spreads = [s[3] for s in self._samples]
        n_win = int(p.get("regime_window_samples", 120) or 120)
        blank = {"state": "UNKNOWN", "efficiency_ratio": None, "zero_crossings": None,
                 "variance_ratio": None, "slope": 0.0, "anchor": self._anchor}
        if len(spreads) < max(20, n_win // 4):
            return blank
        arr = np.asarray(spreads[-n_win:], dtype=float)
        now = time.time() if ts_now is None else ts_now
        day = int(now // 86400)                              # UTC day index
        if self._anchor is None or self._anchor_day != day:
            self._anchor = float(np.mean(arr)); self._anchor_day = day
        anchor = self._anchor
        path = float(np.sum(np.abs(np.diff(arr))))
        er = abs(float(arr[-1] - arr[0])) / path if path > 1e-12 else 0.0
        signs = np.sign(arr - anchor)
        zc = int(np.sum(signs[1:] * signs[:-1] < 0))
        k = max(2, int(p.get("regime_vr_lag", 5) or 5))
        d1 = np.diff(arr)
        vk = arr[k:] - arr[:-k]
        v1 = float(np.var(d1, ddof=1)) if len(d1) > 1 else 0.0
        vr = (float(np.var(vk, ddof=1)) / (k * v1)) if (v1 > 1e-12 and len(vk) > 1) else 1.0
        er_max = float(p.get("regime_efficiency_ratio_max", 0.6) or 0.6)
        zc_min = int(p.get("regime_min_zero_crossings", 4) or 4)
        trending = (er >= er_max) and (zc <= zc_min)
        return {"state": "TRENDING" if trending else "RANGE",
                "efficiency_ratio": round(er, 3), "zero_crossings": zc,
                "variance_ratio": round(vr, 3), "slope": float(arr[-1] - arr[0]),
                "anchor": round(anchor, 4)}

    @staticmethod
    def _coverage(ts: List[float], interval: float) -> Tuple[float, float]:
        """(history_sec, measured_interval_sec) — history counts only time the
        engine was actually SAMPLING (gaps add nothing)."""
        if len(ts) < 2:
            return 0.0, float(interval)
        d = np.diff(np.asarray(ts, dtype=float))
        tol = max(5.0, 10.0 * float(interval))
        live = d[(d > 0) & (d <= tol)]
        history = float(np.sum(live))
        measured = float(np.median(live)) if live.size else float(interval)
        return history, measured

    def _window_stats(self, spreads: List[float], ready: bool, now: float,
                      interval: float) -> Tuple[float, float]:
        """Mean/std shared by the signal AND the chart, cached for
        ``stats_update_interval_sec`` so the bands hold still — but only once
        the window is ready."""
        cache = self._stats_cache
        if ready and interval > 0 and cache is not None and (now - cache[2]) < interval:
            return cache[0], cache[1]
        mean, std = self.compute_stats(spreads)
        self._stats_cache = (mean, std, now) if ready else None
        return mean, std

    def get_signal(self) -> Dict:
        """Return the live signal snapshot — the ONE z the algo + dashboard use."""
        p = self._p()
        with self._lock:
            samples = list(self._samples)
        book = self._book

        out: Dict = {
            "running": self.running,
            "samples": len(samples),
            "window_minutes": p["window_minutes"],
            "sample_interval_sec": p["sample_interval_sec"],
            "min_signal_minutes": p["min_signal_minutes"],
            "entry_zscore": p["entry_zscore"],
            "exit_zscore": p["exit_zscore"],
            "stop_zscore": p["stop_zscore"],
            "leg_a": None, "leg_b": None, "spread": None,
            "mean": None, "std": None, "zscore": None,
            "sell_spread": None, "buy_spread": None, "z_sell": None, "z_buy": None,
            "bid_a": None, "ask_a": None, "bid_b": None, "ask_b": None,
            "book": False,
            "half_life": 0.0, "half_life_sec": 0.0,
            "history_sec": 0.0, "min_history_sec": p["min_signal_minutes"] * 60.0,
            "window_sec": p["window_minutes"] * 60.0,
            "measured_interval_sec": p["sample_interval_sec"],
            "quote_rate_per_min": 0,
            "degenerate": False,
            "sample_status": self._sample_status,
            "ready": False, "last_error": self._last_error,
        }
        _src, _tf, _n = self._band_params()
        out.update(band_source=_src, band_timeframe=_tf, band_length=_n)
        if not samples:
            return out

        ts = [x[0] for x in samples]
        tsN, la, lb, spread = samples[-1]
        now = time.time()
        span_min = (tsN - ts[0]) / 60.0
        history_sec, measured = self._coverage(ts, p["sample_interval_sec"])
        spreads = [x[3] for x in samples]
        enough = history_sec >= p["min_signal_minutes"] * 60.0
        mean, std = self._window_stats(spreads, enough, now,
                                       float(p.get("stats_update_interval_sec", 0) or 0))
        # The tick window's own mean/σ are always published (a position opened
        # in tick mode keeps them if the band source is switched to candles).
        out["tick_mean"], out["tick_std"] = round(mean, 4), round(std, 6)
        src, tf, n_len = self._band_params()
        k_now = p.get("hedge_ratio", 1.0) or 1.0
        bands_all: Dict[str, Dict] = {}
        if self.candles is not None:
            from core.spread_candles import TIMEFRAMES
            for _tf2 in TIMEFRAMES:
                try:
                    b = self.candles.bands(_tf2, n_len, k_now)
                except Exception:
                    b = {"ready": False, "mean": None, "std": None, "count": 0}
                bands_all[_tf2] = {"mean": b.get("mean"), "std": b.get("std"),
                                   "ready": bool(b.get("ready")),
                                   "count": b.get("count", 0),
                                   "status": b.get("status")}
        out["bands"] = bands_all
        if src == "candles":
            b = bands_all.get(tf) or {}
            if b.get("ready"):
                mean, std = float(b["mean"]), float(b["std"])
            else:
                std = 0.0                             # not tradeable until N candles exist
            # Back-filled candles are ready at once, but the live warm-up still
            # gates trading: N candles AND min_signal_minutes of collected ticks.
            enough = bool(b.get("ready")) and enough
            out["candles_have"], out["candles_need"] = b.get("count", 0), n_len
        hl = self.half_life(spreads)
        usable = std > 1e-12
        z = (spread - mean) / std if usable else 0.0
        cutoff = tsN - 60.0
        rate = sum(1 for t in ts if t >= cutoff)

        out.update(
            leg_a=round(la, 4), leg_b=round(lb, 4), spread=round(spread, 4),
            mean=round(mean, 4), std=round(std, 6), zscore=round(z, 4),
            half_life=round(hl, 4), half_life_sec=round(hl * measured, 2),
            span_minutes=round(span_min, 3),
            history_sec=round(history_sec, 1),
            measured_interval_sec=round(measured, 3),
            quote_rate_per_min=rate,
            degenerate=(len(samples) >= 2 and not usable),
            ready=(enough and usable),
        )

        # Executable spreads from the book (None where a side is missing).
        if book and book.get("leg_a") and book.get("leg_b"):
            k = p.get("hedge_ratio", 1.0) or 1.0
            A, B = book["leg_a"], book["leg_b"]
            ba, aa, bb, ab = A.get("bid"), A.get("ask"), B.get("bid"), B.get("ask")
            out.update(bid_a=ba, ask_a=aa, bid_b=bb, ask_b=ab,
                       book=all(v is not None for v in (ba, aa, bb, ab)))
            if ba is not None and ab is not None:
                sell = k * float(ba) - float(ab)
                out["sell_spread"] = round(sell, 4)
                out["z_sell"] = round((sell - mean) / std, 4) if usable else None
            if aa is not None and bb is not None:
                buy = k * float(aa) - float(bb)
                out["buy_spread"] = round(buy, 4)
                out["z_buy"] = round((buy - mean) / std, 4) if usable else None

        reg = self.regime(spreads=spreads, ts_now=tsN)
        out["regime"] = reg["state"]
        out["regime_detail"] = reg
        return out

    def get_series(self, max_points: int = 200, last_sec: Optional[float] = None) -> Dict:
        """Recent spread + per-sample z series for the dashboard charts,
        downsampled to about ``max_points``. Each point's z is the one recorded
        when it was sampled (a restored sample without one is scored against
        the current mean/std), so drawn history holds still."""
        with self._lock:
            samples = list(self._samples)
            zs = list(self._z_hist)
        if not samples:
            return {"points": [], "spread_min": None, "spread_max": None,
                    "entry_zscore": self._p()["entry_zscore"],
                    "stop_zscore": self._p()["stop_zscore"]}

        spreads = [s[3] for s in samples]
        cache = self._stats_cache
        if cache is not None:
            mean, std = cache[0], cache[1]
        else:
            mean, std = self.compute_stats(spreads)
        src, tf, n_len = self._band_params()
        if src == "candles" and self.candles is not None:
            b = self.candles.bands(tf, n_len, self._p().get("hedge_ratio", 1.0) or 1.0)
            mean, std = (b["mean"], b["std"]) if b.get("ready") else (mean, 0.0)
        sd = std if std > 1e-12 else 0.0
        off = len(samples) - len(zs)              # leading samples with no recorded z

        first = 0
        if last_sec and last_sec > 0:
            cut = samples[-1][0] - float(last_sec)
            first = next((i for i, x in enumerate(samples) if x[0] >= cut),
                         len(samples) - 1)

        # Downsample on FIXED time buckets (the last sample of each), so a
        # point already drawn never changes as the window slides. The bucket
        # width only doubles as history grows, so the line re-flows a handful
        # of times during warm-up and then holds.
        n, m = len(samples) - first, max(2, int(max_points))
        if n <= m:
            idx = list(range(first, len(samples)))
        else:
            span = samples[-1][0] - samples[first][0]
            w = max(0.05, float(self._p()["sample_interval_sec"]))
            while span / w > m - 2:
                w *= 2.0
            idx = []
            for i in range(first, len(samples)):
                bkt = int(samples[i][0] // w)
                if idx and int(samples[idx[-1]][0] // w) == bkt:
                    idx[-1] = i
                else:
                    idx.append(i)
        points = []
        for i in idx:
            sp = samples[i][3]
            if i >= off:
                z = zs[i - off]
            else:
                z = (sp - mean) / sd if sd else 0.0
            points.append({"t": round(samples[i][0], 1), "spread": round(sp, 4),
                           "z": round(z, 4)})

        return {
            "points": points,
            "spread_min": round(min(spreads), 4),
            "spread_max": round(max(spreads), 4),
            "mean": round(mean, 4), "std": round(std, 6),
            "entry_zscore": self._p()["entry_zscore"],
            "stop_zscore": self._p()["stop_zscore"],
            "sample_interval_sec": self._p()["sample_interval_sec"],
        }
