"""OKX auto-trader — a focused, server-side mean-reversion executor.

Ported from arrow-statarb ``core/algo.py``, adapted for OKX crypto
(BTC spot vs USDT-perp). Trades the SAME spread the dashboard shows, from
the SAME server-side signal (``core/signal_engine.py``), through the SAME
proven order path used by the manual buttons. Dependency-injected:

  signal_provider()          -> the live signal dict (single source of truth)
  params_provider()          -> dict of strategy params
  execute_fn(direction, qty) -> result dict   (the shared spread execute)
  close_fn(direction, qty)   -> result dict   (the shared spread close)

Signal convention (matches the dashboard):
  spread = k × leg_a − leg_b   (leg_a = SPOT, leg_b = PERP, k = 1)
  z_buy  ≤ −entry → spread cheap → LONG_SPREAD  (buy spot / sell perp)
  z_sell ≥ +entry → spread rich  → SHORT_SPREAD (sell spot / buy perp)

Entries are judged on the EXECUTABLE side (the price that side can actually
trade at); exits on the closing side. The primary exit is the PROFIT TARGET:
net P&L (marked on the executable closing spread, minus the full round-trip
cost) ≥ break-even + tp_capital_pct % of the margin posted. The z-reversion
exit is secondary and break-even-gated so it never books a losing exit.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from core.probability_filter import ProbabilityFilter

logger = logging.getLogger(__name__)


class AlgoTrader:
    def __init__(
        self,
        *,
        signal_provider: Callable[[], Dict],
        params_provider: Callable[[], Dict],
        execute_fn: Callable[..., Dict],
        close_fn: Callable[..., Dict],
        clock: Optional[Callable[[], float]] = None,
    ):
        self._signal = signal_provider
        self._params = params_provider
        self._execute = execute_fn
        self._close = close_fn
        self._clock = clock or time.time

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()

        self._pos: Optional[Dict] = None          # open position, or None
        self._cooldown_until = 0.0
        # After a STOP, block same-direction re-entry until z re-enters the
        # exit band (z-reset gate). None = clear.
        self._stop_block_dir: Optional[str] = None
        # Regime guard: once a UTC day is flagged TRENDING, latch a halt on
        # new entries until the next day.
        self._regime_halt_day: Optional[int] = None
        self._consec_above = 0                     # consecutive ticks z ≥ +entry
        self._consec_below = 0                     # consecutive ticks z ≤ −entry
        self._exit_failures = 0                    # consecutive failed exit attempts
        self._exit_halted = False                  # ceiling hit → stop auto-exit retries
        self._exit_retry_at = 0.0                  # backoff: no exit attempt before this
        self._snap: Dict = {"status": "stopped"}   # last snapshot for /state
        # The last entry signal that was REFUSED — for the "Last Signal
        # Blocked" card.
        self._last_blocked: Optional[Dict] = None
        self.running = False
        self.last_error = ""

    # ── control ────────────────────────────────────────────────────────────
    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop_evt.clear()
            self.running = True
            self.last_error = ""
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="AlgoTrader")
            self._thread.start()
            logger.info("AlgoTrader: started")
            return True

    def stop(self) -> None:
        self._stop_evt.set()
        self.running = False
        logger.info("AlgoTrader: stopped (open position, if any, is left untouched)")

    def restore_position(self, pos: Optional[Dict]) -> bool:
        """Re-adopt an open position after a process restart so the running
        engine manages (and can exit/stop) a trade it didn't itself open."""
        if not pos or not pos.get("direction"):
            return False
        with self._lock:
            if self._pos is not None:
                return False
            direction = pos["direction"]
            self._pos = {
                "direction": direction,
                "qty": float(pos.get("qty", 0) or 0),
                "entry_z": -1.0 if direction == "LONG_SPREAD" else 1.0,
                "entry_spread": pos.get("entry_spread"),
                "entry_fill_spread": pos.get("entry_spread"),
                "entry_leg_a": pos.get("entry_leg_a"),
                "entry_leg_b": pos.get("entry_leg_b"),
                "entry_std": pos.get("entry_std"),
                "entry_time": float(pos.get("ts") or self._clock()),
                "band_source": pos.get("band_source") or "ticks",
                "band_tf": pos.get("band_tf"),
                "peak_pnl": 0.0, "trough_pnl": 0.0,
                "peak_min": 0.0, "trough_min": 0.0,
                "restored": True,
            }
        logger.warning("AlgoTrader: restored open %s position (qty %.6f) from trade log",
                       direction, self._pos["qty"])
        return True

    def clear_position(self, reason: str = "reconcile") -> bool:
        """Force-drop the engine's belief in an open position. Safe — touches
        only in-memory state, places no orders."""
        with self._lock:
            if self._pos is None:
                return False
            direction = self._pos.get("direction")
            self._pos = None
            self._exit_failures = 0
            self._exit_halted = False
            self._exit_retry_at = 0.0
        logger.warning("AlgoTrader: force-cleared engine position (%s) — %s",
                       direction, reason)
        return True

    def restore_cooldown(self, until_ts: float) -> bool:
        try:
            until = float(until_ts)
        except (TypeError, ValueError):
            return False
        if until <= self._clock():
            return False
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, until)
        return True

    def get_state(self) -> Dict:
        with self._lock:
            return {
                **self._snap,
                "running": self.running,
                "in_position": self._pos is not None,
                "position": dict(self._pos) if self._pos else None,
                "cooldown_s": max(0.0, round(self._cooldown_until - self._clock(), 1)),
                "exit_failures": self._exit_failures,
                "exit_halted": self._exit_halted,
                "exit_retry_s": max(0.0, round(self._exit_retry_at - self._clock(), 1)),
                "last_error": self.last_error,
                "last_blocked_signal": dict(self._last_blocked) if self._last_blocked else None,
            }

    # ── loop ───────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._tick()
            except Exception as exc:                       # never let the loop die
                self.last_error = str(exc)
                logger.exception("AlgoTrader: tick error")
            interval = 0.5
            try:
                interval = max(0.05, float(self._params().get("tick_interval", 0.5)))
            except Exception:
                pass
            self._stop_evt.wait(interval)

    # ── sizing & costs (OKX) ─────────────────────────────────────────────────
    def _qty(self, p: Dict, sig: Dict) -> float:
        """Position size in BTC: position_size_usd ÷ spot price."""
        size_usd = float(p.get("position_size_usd", 0) or 0)
        la = sig.get("leg_a")
        if size_usd <= 0 or not la:
            return 0.0
        return size_usd / float(la)

    def _round_trip_cost(self, p: Dict, qty: Optional[float] = None,
                         ref_a: Optional[float] = None,
                         ref_b: Optional[float] = None) -> float:
        """Estimated round-trip TRANSACTION cost in $: exchange fees per leg
        (maker or taker by execution mode, entry + exit) + slippage on all
        four leg turnovers. Defaults to the open position's size/entry
        prices; pass qty/ref prices for a pre-entry (prospective) estimate."""
        pos = self._pos or {}
        qty = float(qty if qty is not None else pos.get("qty", 0) or 0)
        ra = ref_a if ref_a is not None else pos.get("entry_leg_a")
        rb = ref_b if ref_b is not None else pos.get("entry_leg_b")
        if qty <= 0 or not ra or not rb:
            return 0.0
        not_a = qty * float(ra)                     # spot-leg notional
        not_b = qty * float(rb)                     # perp-leg notional
        spot_maker = float(p.get("spot_maker_fee_bps", 8.0) or 0)
        spot_taker = float(p.get("spot_taker_fee_bps", 10.0) or 0)
        fut_maker = float(p.get("futures_maker_fee_bps", 2.0) or 0)
        fut_taker = float(p.get("futures_taker_fee_bps", 5.0) or 0)
        entry_limit = str(p.get("entry_execution_mode", "LIMIT")).upper() == "LIMIT"
        exit_limit = str(p.get("exit_execution_mode", "LIMIT")).upper() == "LIMIT"
        fees = 0.0
        fees += not_a * (spot_maker if entry_limit else spot_taker) / 1e4
        fees += not_b * (fut_maker if entry_limit else fut_taker) / 1e4
        fees += not_a * (spot_maker if exit_limit else spot_taker) / 1e4
        fees += not_b * (fut_maker if exit_limit else fut_taker) / 1e4
        slip_bps = float(p.get("slippage_bps", 0) or 0)
        fees += 2.0 * (not_a + not_b) * slip_bps / 1e4   # 4 leg turnovers
        return fees

    def _margin_usd(self, p: Dict, qty: Optional[float] = None,
                    ref_a: Optional[float] = None,
                    ref_b: Optional[float] = None) -> float:
        """Capital at risk ($): spot notional ÷ spot leverage + perp notional ÷
        perp leverage — the margin actually posted for the pair."""
        pos = self._pos or {}
        qty = float(qty if qty is not None else pos.get("qty", 0) or 0)
        ra = ref_a if ref_a is not None else pos.get("entry_leg_a")
        rb = ref_b if ref_b is not None else pos.get("entry_leg_b")
        if qty <= 0 or not ra or not rb:
            return 0.0
        lev_a = max(1.0, float(p.get("spot_leverage", 1) or 1))
        lev_b = max(1.0, float(p.get("futures_leverage", 1) or 1))
        return qty * float(ra) / lev_a + qty * float(rb) / lev_b

    # ── P&L ──────────────────────────────────────────────────────────────────
    def _live_net_pnl(self, cur_spread: Optional[float], p: Dict,
                      pos: Optional[Dict] = None) -> Optional[float]:
        """Live mark-to-market net P&L ($) of the open position, or None.

        gross = Δspread × qty. The reference is the actual ENTRY FILL spread
        (which already embeds entry slippage); the current side is the
        EXECUTABLE closing spread (sell_spread for a LONG, buy_spread for a
        SHORT), so the exit's bid/ask cost IS already in the figure. Fees are
        the flat round-trip cost. net = 0 IS break-even after all costs.
        LONG profits when the spread rises; SHORT when it falls."""
        pos = pos if pos is not None else self._pos
        if not pos or cur_spread is None:
            return None
        entry = pos.get("entry_fill_spread")
        if entry is None:
            entry = pos.get("entry_spread")
        if entry is None:
            return None
        qty = float(pos.get("qty", 0) or 0)
        change = ((cur_spread - entry) if pos["direction"] == "LONG_SPREAD"
                  else (entry - cur_spread))
        gross = change * qty
        return gross - self._position_fees(p, pos)

    def _position_fees(self, p: Dict, pos: Optional[Dict] = None) -> float:
        """Round-trip fees ($) of the open position — the ONE fee figure
        behind both the live net P&L and the BE/TP/SL spread levels."""
        pos = (pos if pos is not None else self._pos) or {}
        return self._round_trip_cost(p, qty=pos.get("qty"),
                                     ref_a=pos.get("entry_leg_a"),
                                     ref_b=pos.get("entry_leg_b"))

    def _spread_levels(self, p: Dict, profit_target: float,
                       dollar_stop: float, pos: Optional[Dict] = None) -> Optional[Dict]:
        """BE / TP / SL of the open position as SPREAD prices — the levels the
        closing-side spread must reach for each $ exit to fire.

            net = d·(X − E)·qty − fees,   d = +1 LONG, −1 SHORT
        so  BE: net = 0 · TP: net = target · SL: net = −stop."""
        pos = pos if pos is not None else self._pos
        if not pos:
            return None
        E = pos.get("entry_fill_spread")
        if E is None:
            E = pos.get("entry_spread")
        qty = float(pos.get("qty", 0) or 0)
        if E is None or qty <= 0:
            return None
        E = float(E)
        d = 1.0 if pos["direction"] == "LONG_SPREAD" else -1.0
        fees = self._position_fees(p, pos)
        lvl = lambda net_gross: round(E + d * net_gross / qty, 2)
        gate = float(p.get("reversion_gate_usd", 0) or 0)
        return {
            "entry": round(E, 2),
            "break_even": lvl(fees),
            "take_profit": lvl(fees + profit_target) if profit_target > 0 else None,
            "stop": lvl(fees - dollar_stop) if dollar_stop > 0 else None,
            "gate_release": (lvl(fees + gate)
                             if gate > 0 and bool(p.get("reversion_require_profit", True))
                             else None),
            "favorable": "up" if d > 0 else "down",
            "closing_side": "sell" if d > 0 else "buy",
            "fees_usd": round(fees, 2),
        }

    def _reversion_allowed(self, net_pnl: Optional[float], p: Dict,
                           held_sec: float = 0.0, max_hold_sec: float = 0.0) -> bool:
        """Gate the z-reversion exit on P&L — never book a losing profit-take.
        DEADLOCK-PROOF: past 1× max-hold the floor decays to break-even; past
        2× the gate releases entirely. Fail-open when P&L can't be priced."""
        if not bool(p.get("reversion_require_profit", True)):
            return True
        if max_hold_sec > 0 and held_sec >= 2.0 * max_hold_sec:
            return True
        if net_pnl is None:
            return True
        floor = float(p.get("reversion_gate_usd", 0) or 0)
        if max_hold_sec > 0 and held_sec >= max_hold_sec:
            floor = 0.0
        return net_pnl >= floor

    # ── entry gates ──────────────────────────────────────────────────────────
    def _edge_entry_block(self, z: float, std: float, qty: float,
                          sig: Dict, p: Dict) -> Optional[str]:
        """The dead-day gate — "only take a trade that can be profitable after
        ALL costs". Checks the prospective round-trip cost at this size:
          • half-life acceptance band (too fast = noise; too slow = won't
            revert inside the hold);
          • edge filter — expected capture (target_fraction × |z| × σ × qty)
            must be ≥ min_edge_multiple × cost;
          • cost-floor sanity — if cost_floor_mult × cost exceeds the
            plausible FULL reversion, the trade can never win: block it."""
        hl_sec = float(sig.get("half_life_sec", 0) or 0)
        if hl_sec > 0:
            hl_min = float(p.get("half_life_min_sec", 0) or 0)
            hl_max = float(p.get("half_life_max_sec", 0) or 0)
            if hl_min > 0 and hl_sec < hl_min:
                return (f"half-life {hl_sec:.0f}s < min {hl_min:.0f}s — "
                        f"reversion too fast (noise)")
            if hl_max > 0 and hl_sec > hl_max:
                return (f"half-life {hl_sec:.0f}s > max {hl_max:.0f}s — "
                        f"reverts too slowly to hold")
        cost = self._round_trip_cost(p, qty=qty, ref_a=sig.get("leg_a"),
                                     ref_b=sig.get("leg_b"))
        full_move = abs(z) * float(std) * qty
        min_mult = float(p.get("min_edge_multiple", 0) or 0)
        if min_mult > 0 and cost > 0:
            tfrac = float(p.get("profit_target_sigma_frac", 0) or 0) or 0.5
            capture = tfrac * full_move
            if capture < min_mult * cost:
                return (f"edge filter: expected capture ${capture:.2f} < "
                        f"{min_mult:g}× cost ${cost:.2f} — not worth it")
        cfm = float(p.get("cost_floor_mult", 0) or 0)
        if cfm > 0 and cfm * cost > full_move:
            return (f"cost floor ${cfm * cost:.2f} exceeds plausible reversion "
                    f"${full_move:.2f} — trade can never win")
        return None

    def edge_preview(self, sig: Dict) -> Dict:
        """Standing pre-trade economics for the dashboard Filters panel — the
        SAME round-trip cost and edge-multiple the live gate uses, evaluated
        at the ENTRY threshold from the current signal."""
        p = self._params() or {}
        std = float(sig.get("std") or 0)
        la, lb = sig.get("leg_a"), sig.get("leg_b")
        if std <= 0 or not la or not lb:
            return {}
        qty = self._qty(p, sig)
        if qty <= 0:
            return {}
        entry_z = float(p.get("entry_zscore", 2.5) or 2.5)
        cost = self._round_trip_cost(p, qty=qty, ref_a=la, ref_b=lb)
        full_move = entry_z * std * qty
        tfrac = float(p.get("profit_target_sigma_frac", 0) or 0) or 0.5
        capture = tfrac * full_move
        min_mult = float(p.get("min_edge_multiple", 0) or 0)
        edge_mult = (capture / cost) if cost > 0 else None
        notional = abs(float(lb)) * qty
        margin = self._margin_usd(p, qty=qty, ref_a=la, ref_b=lb)
        tp_cap = float(p.get("tp_capital_pct", 0) or 0)

        def _bps(x):
            return round(x / notional * 10000.0, 2) if notional > 0 else None

        return {
            "qty": round(qty, 6),
            "notional_usd": round(notional, 2),
            "margin_usd": round(margin, 2),
            "round_trip_cost_usd": round(cost, 2),
            "round_trip_cost_bps": _bps(cost),
            "expected_capture_usd": round(capture, 2),
            "edge_multiple": (round(edge_mult, 2) if edge_mult is not None else None),
            "min_edge_multiple": min_mult,
            "edge_ok": (min_mult <= 0 or (edge_mult is not None and edge_mult >= min_mult)),
            "profit_target_usd": (round((tp_cap / 100.0) * margin, 2)
                                  if tp_cap > 0 and margin > 0 else None),
            "entry_mode": str(p.get("entry_execution_mode", "LIMIT")),
            "exit_mode": str(p.get("exit_execution_mode", "LIMIT")),
        }

    def _effective_exit_levels(self, p: Dict) -> Tuple[float, float]:
        """Resolve the $ (dollar_stop, profit_target) with scale-invariant
        precedence:
          target: σ-fraction > %-of-margin (BE + tp_capital_pct) > fixed-$,
                  then raised to a cost floor;
          stop:   min(target/RR, %-of-margin) — the TIGHTER binds — with
                  fixed-$ as the fallback.
        The returned target is compared against the LIVE NET P&L, which is
        already net of every cost — so net = 0 IS break-even and a target of
        $T means "close once $T ABOVE break-even"."""
        pos = self._pos or {}
        p_margin = self._margin_usd(p)
        # ── target ──
        target = float(p.get("profit_target_usd", 0) or 0)
        sfrac = float(p.get("profit_target_sigma_frac", 0) or 0)
        std0 = float(pos.get("entry_std", 0) or 0)
        absz = abs(float(pos.get("entry_z", 0) or 0))
        qty = float(pos.get("qty", 0) or 0)
        tp_cap = float(p.get("tp_capital_pct", 0) or 0)
        if sfrac > 0 and std0 > 0 and absz > 0 and qty > 0:
            target = sfrac * absz * std0 * qty
        elif tp_cap > 0 and p_margin > 0:
            target = (tp_cap / 100.0) * p_margin   # BE + tp_capital_pct% of margin
        cost_mult = float(p.get("cost_floor_mult", 0) or 0)
        if cost_mult > 0 and target > 0:
            target = max(target, cost_mult * self._position_fees(p))
        # ── stop ──
        stop = float(p.get("dollar_stop_usd", 0) or 0)
        cands = []
        rr = float(p.get("stop_rr", 0) or 0)
        if rr > 0 and target > 0:
            cands.append(target / rr)
        scap = float(p.get("stop_capital_pct", 0) or 0)
        if scap > 0 and p_margin > 0:
            cands.append((scap / 100.0) * p_margin)
        if cands:
            stop = min(cands)
        return round(stop, 2), round(target, 2)

    # ── tick ─────────────────────────────────────────────────────────────────
    def _tick(self) -> None:
        p = self._params()
        sig = self._signal() or {}
        snap: Dict = {"ts": self._clock(),
                      "leg_a": sig.get("leg_a"), "leg_b": sig.get("leg_b"),
                      "spread": sig.get("spread"), "mean": sig.get("mean"),
                      "std": sig.get("std"), "zscore": sig.get("zscore"),
                      "samples": sig.get("samples"), "half_life": sig.get("half_life"),
                      "regime": sig.get("regime")}

        z = sig.get("zscore")
        std = sig.get("std")
        if z is None or std is None:
            snap["status"] = "waiting for prices"
            self._set_snap(snap)
            return

        # A held position is managed on the bands it ENTERED with, even if the
        # band setting has changed since — so its readiness, not the setting's,
        # gates the exit logic.
        pos_bands = self._position_bands(sig)
        if not sig.get("ready") and pos_bands is None:
            if sig.get("band_source") == "candles":
                snap["status"] = (f"loading candles ({sig.get('candles_have', 0)}/"
                                  f"{sig.get('candles_need', 0)} on "
                                  f"{sig.get('band_timeframe')})")
            else:
                need = sig.get("min_signal_minutes", 0)
                have = (sig.get("history_sec") or 0) / 60.0
                snap["status"] = f"collecting signal ({have:.1f}/{need:.0f} min)"
            self._set_snap(snap)
            return

        # EXECUTABLE sides. With a book, selling the spread is judged on
        # sell_spread (k·bid_A − ask_B) and buying it on buy_spread
        # (k·ask_A − bid_B) — the prices the orders would actually meet.
        z_mid, spread_mid = z, sig.get("spread")
        has_book = any(sig.get(k) is not None for k in ("bid_a", "ask_a", "bid_b", "ask_b"))
        if has_book:
            z_sell, z_buy = sig.get("z_sell"), sig.get("z_buy")
            sp_sell, sp_buy = sig.get("sell_spread"), sig.get("buy_spread")
        else:
            z_sell = z_buy = z
            sp_sell = sp_buy = spread_mid
        snap.update(z_sell=z_sell, z_buy=z_buy, sell_spread=sp_sell, buy_spread=sp_buy)

        entry_z = float(p.get("entry_zscore", sig.get("entry_zscore", 2.5)))
        exit_z = float(p.get("exit_zscore", sig.get("exit_zscore", 0.0)))
        stop_z = float(p.get("stop_zscore", sig.get("stop_zscore", 4.0)))
        max_entry_z = float(p.get("max_entry_zscore", 0) or 0)
        half_life = float(sig.get("half_life", 0.0))
        sample_interval = float(sig.get("sample_interval_sec", 0.5))
        now = self._clock()

        # Trade direction (Settings): which side may OPEN a position.
        #   sell_only — only SHORT the spread; buy_only — only go LONG;
        #   both — either. Gates ENTRIES only; exits always run.
        td = str(p.get("trade_direction", "both") or "both").lower()
        zs_entry = z_sell if td != "buy_only" else None
        zb_entry = z_buy if td != "sell_only" else None
        snap["trade_direction"] = td

        # Confirmation ticks: require N consecutive ticks beyond the threshold.
        confirm = max(1, int(p.get("confirmation_ticks", 1)))
        if zs_entry is not None and zs_entry >= entry_z:
            self._consec_above += 1; self._consec_below = 0
        elif zb_entry is not None and zb_entry <= -entry_z:
            self._consec_below += 1; self._consec_above = 0
        else:
            self._consec_above = self._consec_below = 0
        confirmed_long = self._consec_below >= confirm
        confirmed_short = self._consec_above >= confirm

        if self._pos is None:
            self._tick_flat(p, sig, snap, z, std, entry_z, exit_z, now,
                            z_sell, z_buy, sp_sell, sp_buy, z_mid, spread_mid,
                            td, max_entry_z, confirmed_long, confirmed_short,
                            has_book)
        else:
            self._tick_in_position(p, sig, snap, z, exit_z, stop_z, now,
                                   z_sell, z_buy, sp_sell, sp_buy, z_mid,
                                   spread_mid, half_life, sample_interval,
                                   pos_bands)

        self._set_snap(snap)

    def _position_bands(self, sig: Dict) -> Optional[Tuple[float, float]]:
        """(mean, σ) the OPEN position is managed on when they differ from the
        current setting — the bands it entered with. None = use the signal's."""
        pos = self._pos
        if not pos:
            return None
        src, tf = pos.get("band_source") or "ticks", pos.get("band_tf")
        if src == sig.get("band_source") and (src != "candles"
                                              or tf == sig.get("band_timeframe")):
            return None
        if src == "candles":
            b = (sig.get("bands") or {}).get(tf or "") or {}
            m, sd = b.get("mean"), b.get("std")
        else:
            m, sd = sig.get("tick_mean"), sig.get("tick_std")
        if m is None or not sd or sd <= 1e-12:
            return None
        return float(m), float(sd)

    def _tick_flat(self, p, sig, snap, z, std, entry_z, exit_z, now,
                   z_sell, z_buy, sp_sell, sp_buy, z_mid, spread_mid,
                   td, max_entry_z, confirmed_long, confirmed_short, has_book) -> None:
        mdl = float(p.get("daily_max_loss_usd", 0) or 0)
        day_pnl = float(p.get("day_pnl", 0.0) or 0.0)
        if z_sell is not None and td != "buy_only" and z_sell >= entry_z:
            want_dir = "SHORT_SPREAD"
        elif z_buy is not None and td != "sell_only" and z_buy <= -entry_z:
            want_dir = "LONG_SPREAD"
        else:
            want_dir = "LONG_SPREAD" if z < 0 else "SHORT_SPREAD"
        # The side this setting switched off is past its threshold: say so.
        off_side = None
        if td == "buy_only" and z_sell is not None and z_sell >= entry_z:
            off_side = f"sell spread at z {z_sell:+.2f} — SHORT entries are off (Buy spread only)"
        elif td == "sell_only" and z_buy is not None and z_buy <= -entry_z:
            off_side = f"buy spread at z {z_buy:+.2f} — LONG entries are off (Sell spread only)"
        # From here on, z / spread are the side we would TRADE on.
        z_side = z_sell if want_dir == "SHORT_SPREAD" else z_buy
        z = z_side if z_side is not None else z_mid
        sp_side = sp_sell if want_dir == "SHORT_SPREAD" else sp_buy
        # z-reset: clear a post-stop block once z recovers toward the mean.
        if self._stop_block_dir == "LONG_SPREAD" and z >= -exit_z:
            self._stop_block_dir = None
        elif self._stop_block_dir == "SHORT_SPREAD" and z <= exit_z:
            self._stop_block_dir = None
        streak = int(p.get("loss_streak", 0) or 0)
        pause_at = int(p.get("loss_streak_pause_at", 0) or 0)
        # Regime guard: latch a day-long halt once the spread is flagged
        # TRENDING (auto-rearm next UTC day).
        reg_enabled = bool(p.get("regime_enabled", False))
        reg_state = sig.get("regime")
        reg_slope = float((sig.get("regime_detail") or {}).get("slope", 0.0) or 0.0)
        today = int(self._clock() // 86400)
        if self._regime_halt_day is not None and self._regime_halt_day != today:
            self._regime_halt_day = None
        if (reg_enabled and bool(p.get("regime_halt_on_trending", True))
                and reg_state == "TRENDING"):
            self._regime_halt_day = today
        regime_halted = reg_enabled and self._regime_halt_day == today
        trend_blocks = (reg_enabled and bool(p.get("regime_trend_direction_filter", False))
                        and ((reg_slope > 0 and want_dir == "LONG_SPREAD")
                             or (reg_slope < 0 and want_dir == "SHORT_SPREAD")))
        qty = self._qty(p, sig)
        wanted = abs(z) >= entry_z and (confirmed_long or confirmed_short)
        if mdl > 0 and day_pnl <= -mdl:
            snap["status"] = (f"daily loss limit reached (${day_pnl:.0f} ≤ −${mdl:.0f}) "
                              f"— entries halted")
        elif pause_at > 0 and streak >= pause_at:
            snap["status"] = f"paused: {streak}-loss streak (≥ {pause_at}) — entries halted"
        elif regime_halted:
            snap["status"] = "regime TRENDING — new entries halted for the day"
        elif now < self._cooldown_until:
            snap["status"] = "cooldown"
        elif wanted and trend_blocks:
            snap["status"] = (f"trend filter: {'SHORT' if reg_slope > 0 else 'LONG'}-only "
                              f"(spread {'rising' if reg_slope > 0 else 'falling'})")
        elif wanted and self._stop_block_dir == want_dir:
            snap["status"] = (f"z-reset: blocking {want_dir.replace('_SPREAD', '')} "
                              f"re-entry until z re-enters ±{exit_z:.1f} band (z={z:.2f})")
        elif wanted and max_entry_z > 0 and abs(z) > max_entry_z:
            snap["status"] = (f"blocked: |z|={abs(z):.2f} exceeds entry cap "
                              f"{max_entry_z:.2f} (regime-shift guard)")
        elif wanted and qty <= 0:
            snap["status"] = "blocked: position size is 0 — set Position Size in Settings"
        elif wanted:
            direction = want_dir
            # Loss-streak size reducer: shrink size on a losing run.
            eff_qty = qty
            reduce_at = int(p.get("loss_streak_reduce_at", 0) or 0)
            if reduce_at > 0 and streak >= reduce_at:
                pct = float(p.get("loss_streak_reduce_pct", 0) or 0) / 100.0
                eff_qty = max(qty * 0.1, qty * (1.0 - pct))
            edge_msg = self._edge_entry_block(z, std, eff_qty, sig, p)
            cost = self._round_trip_cost(p, qty=eff_qty, ref_a=sig.get("leg_a"),
                                         ref_b=sig.get("leg_b"))
            pf = ProbabilityFilter(
                min_win_probability=float(p.get("min_win_probability", 0.60)),
                min_expected_value=float(p.get("min_expected_value", 0.0)),
                exit_zscore=float(p.get("exit_zscore", 0.0)),
                stop_zscore=float(p.get("stop_zscore", 4.0)),
                enabled=bool(p.get("enable_probability_filter", True)),
            )
            allow, reason, metrics = pf.check_entry(z, std, eff_qty, cost)
            snap["pf_reason"] = reason
            snap["pf_metrics"] = metrics
            if edge_msg:
                snap["status"] = f"blocked: {edge_msg}"
            elif not allow:
                wp = metrics.get("win_probability")
                extra = ""
                if wp is not None:
                    extra = (f" (P_win={wp*100:.0f}% "
                             f"EV=${metrics.get('expected_value', 0):.0f})")
                snap["status"] = f"blocked: {reason}{extra}"
            else:
                refused = self._enter(
                    direction, eff_qty, z,
                    sp_side if sp_side is not None else (spread_mid or 0.0),
                    mid_spread=spread_mid,
                    z_key=("z_sell" if direction == "SHORT_SPREAD" else "z_buy")
                    if has_book else "zscore")
                snap["status"] = refused or (f"ENTRY {direction} (z={z:.2f}, "
                                             f"qty {eff_qty:.6f})")
        elif abs(z) >= entry_z and not off_side:
            c = max(self._consec_above, self._consec_below)
            confirm = max(1, int(p.get("confirmation_ticks", 1)))
            snap["status"] = f"confirming {c}/{confirm} (z={z:.2f})"
        elif off_side:
            snap["status"] = off_side
        else:
            snap["status"] = "flat — watching"
        # A signal was there but no trade came of it: remember why.
        st_txt = str(snap.get("status") or "")
        if (wanted or off_side) and not st_txt.startswith(("ENTRY", "confirming")):
            if off_side:
                side = "SHORT" if td == "buy_only" else "LONG"
                z_rec = z_sell if side == "SHORT" else z_buy
            else:
                side = "LONG" if want_dir == "LONG_SPREAD" else "SHORT"
                z_rec = z
            self._last_blocked = {
                "would_be_signal": side,
                "zscore": round(float(z_rec), 4) if z_rec is not None else None,
                "timestamp": int(self._clock() * 1000),
                "reason": st_txt,
            }

    def _tick_in_position(self, p, sig, snap, z, exit_z, stop_z, now,
                          z_sell, z_buy, sp_sell, sp_buy, z_mid, spread_mid,
                          half_life, sample_interval,
                          pos_bands: Optional[Tuple[float, float]] = None) -> None:
        # A position is closed on the OPPOSITE side it was opened on: a LONG
        # (bought) spread is closed by SELLING it, a SHORT by BUYING it.
        if self._pos["direction"] == "LONG_SPREAD":
            z_close, sp_close = z_sell, sp_sell
        else:
            z_close, sp_close = z_buy, sp_buy
        if pos_bands is not None:
            # The bands the position ENTERED with, not the current setting's.
            pm, ps = pos_bands
            if sp_close is not None:
                z_close = (float(sp_close) - pm) / ps
            if spread_mid is not None:
                z_mid = (float(spread_mid) - pm) / ps
        z = z_close if z_close is not None else z_mid
        entry_z_sign = self._pos["entry_z"]
        reverted = (z >= exit_z) if entry_z_sign < 0 else (z <= exit_z)
        held_sec = now - self._pos["entry_time"]
        min_hold_sec = float(p.get("min_hold_sec", 0.0) or 0.0)
        max_hold_sec = (float(p.get("time_stop_half_lives", 3.0))
                        * half_life * sample_interval) if half_life > 0 else 0.0
        if self._pos.get("band_source") == "candles":
            # candle mode: max hold = N candles of the timeframe it entered on
            from core.spread_candles import tf_seconds
            mhc = float(p.get("max_hold_candles", 0) or 0)
            max_hold_sec = (mhc * tf_seconds(self._pos.get("band_tf") or "15m")
                            if mhc > 0 else 0.0)
        spread_now = sp_close if sp_close is not None else spread_mid

        # ── live mark-to-market net P&L on the open position ($) ──────────
        net_pnl = self._live_net_pnl(spread_now, p)
        dollar_stop, profit_target = self._effective_exit_levels(p)
        snap["net_pnl"] = round(net_pnl, 2) if net_pnl is not None else None
        snap["dollar_stop"] = -dollar_stop if dollar_stop > 0 else None
        snap["profit_target"] = profit_target if profit_target > 0 else None
        _be_cost = self._position_fees(p)
        snap["break_even"] = round(_be_cost, 2) if _be_cost > 0 else None
        snap["spread_levels"] = self._spread_levels(p, profit_target, dollar_stop)
        snap["tp_gross_target"] = (round(_be_cost + profit_target, 2)
                                   if profit_target > 0 else None)
        _pnl_txt = f"${net_pnl:.2f}" if net_pnl is not None else "n/a"

        # ── position detail for the Signal & Position card ────────────────
        entry_ref = self._pos.get("entry_fill_spread")
        if entry_ref is None:
            entry_ref = self._pos.get("entry_spread")
        snap["entry_spread"] = entry_ref
        snap["held_sec"] = round(held_sec, 1)
        snap["max_hold_sec"] = round(max_hold_sec, 1) if max_hold_sec > 0 else None
        snap["remaining_sec"] = (round(max(0.0, max_hold_sec - held_sec), 1)
                                 if max_hold_sec > 0 else None)
        snap["delta_spread"] = (round(spread_now - entry_ref, 4)
                                if spread_now is not None and entry_ref is not None else None)
        _la = sig.get("leg_a")
        snap["notional"] = (round(self._pos["qty"] * float(_la), 2) if _la else None)

        # Minimum hold: suppress the reversion/target exit until the trade has
        # lived long enough. Risk overrides (dollar stop, z-stop) are NEVER
        # suppressed.
        hold_gated = reverted and min_hold_sec > 0 and held_sec < min_hold_sec

        # Lifecycle extremes: peak (drives the trailing stop) and trough.
        peak = float(self._pos.get("peak_pnl", 0.0) or 0.0)
        trough = float(self._pos.get("trough_pnl", 0.0) or 0.0)
        if net_pnl is not None:
            if net_pnl > peak:
                peak = net_pnl
                self._pos["peak_pnl"] = peak
                self._pos["peak_min"] = round(held_sec / 60.0, 1)
            if net_pnl < trough:
                trough = net_pnl
                self._pos["trough_pnl"] = trough
                self._pos["trough_min"] = round(held_sec / 60.0, 1)
        snap["peak_pnl"] = round(peak, 2) if net_pnl is not None else None
        snap["trough_pnl"] = round(trough, 2) if net_pnl is not None else None

        # Max-hold (the time-stop), upgraded:
        #  • silent when losing — but ONLY when a $ stop is armed;
        #  • z-progress gate — a WINNING trade that has reverted far enough is
        #    let run — but ONLY when a profit target exists to take it out.
        max_hold_due = max_hold_sec > 0 and held_sec >= max_hold_sec
        max_hold_expired = max_hold_due
        if max_hold_due:
            silent = bool(p.get("max_hold_silent_when_losing", False)) and dollar_stop > 0
            if silent and net_pnl is not None and net_pnl <= 0:
                max_hold_due = False
            z_prog_min = float(p.get("max_hold_z_progress_min", 0) or 0)
            if (max_hold_due and z_prog_min > 0 and profit_target > 0
                    and net_pnl is not None and net_pnl > 0):
                entry_abs, exit_abs = abs(self._pos["entry_z"]), abs(exit_z)
                journey = entry_abs - exit_abs
                z_prog = ((entry_abs - abs(z)) / journey) if journey > 1e-9 else 1.0
                if z_prog >= z_prog_min:
                    max_hold_due = False
        snap["max_hold_expired"] = bool(max_hold_expired)

        # Hard time-stop: past hard_time_stop_mult × max-hold, close ANY trade
        # regardless of P&L. 0 = off.
        hard_mult = float(p.get("hard_time_stop_mult", 0) or 0)
        hard_due = (hard_mult > 0 and max_hold_sec > 0
                    and held_sec >= hard_mult * max_hold_sec)

        # z-stop demotion: once a $ stop is armed, in-trade risk is DOLLARS
        # only. FAIL-SAFE: auto-re-enabled whenever NO dollar stop is armed.
        z_stop_armed = bool(p.get("z_stop_exit_enabled", True)) or dollar_stop <= 0

        # Trailing stop: arm once the peak clears the floor, then fire when
        # P&L pulls back trail_pct from the peak.
        trail_pct = float(p.get("trailing_stop_pct", 0) or 0)
        floor_pct = float(p.get("trailing_stop_floor_pct", 0) or 0)
        trailing_fire = trailing_armed = False
        if trail_pct > 0 and net_pnl is not None and peak > 0:
            trailing_armed = (peak >= (floor_pct / 100.0) * profit_target
                              if floor_pct > 0 and profit_target > 0 else True)
            if trailing_armed and net_pnl < peak * (1.0 - trail_pct / 100.0):
                trailing_fire = True
        snap["trailing_armed"] = trailing_armed

        # Exit priority (first match wins) — RISK BEFORE REWARD:
        #   1 dollar stop · 2 profit target · 3 hard time-stop · 4 max-hold
        #   5 trailing stop · 6 z-stop (if armed) · 7 z-reversion (BE-gated)
        if dollar_stop > 0 and net_pnl is not None and net_pnl <= -dollar_stop:
            exit_reason = "dollar_stop"
        elif profit_target > 0 and net_pnl is not None and net_pnl >= profit_target:
            exit_reason = "profit_target"
        elif hard_due:
            exit_reason = "hard_time_stop"
        elif max_hold_due:
            exit_reason = "time_stop"
        elif trailing_fire:
            exit_reason = "trailing_stop"
        elif abs(z) >= stop_z and z_stop_armed:
            exit_reason = "stop"
        elif (reverted and not hold_gated
              and self._reversion_allowed(net_pnl, p, held_sec, max_hold_sec)):
            exit_reason = "target"
        else:
            exit_reason = None

        if exit_reason and self._exit_halted:
            snap["status"] = (f"EXIT HALTED ({exit_reason}, z={z:.2f}) — "
                              f"{self._exit_failures} consecutive failures; "
                              f"close manually")
        elif exit_reason and now < self._exit_retry_at:
            wait = self._exit_retry_at - now
            snap["status"] = (f"exit retry in {wait:.0f}s (backoff after "
                              f"{self._exit_failures} failure(s); {exit_reason}, z={z:.2f})")
        elif exit_reason == "dollar_stop":
            snap["status"] = f"STOP PRICE (net {_pnl_txt} ≤ −${dollar_stop:.2f})"
            self._exit("dollar_stop", z, spread_now)
        elif exit_reason == "profit_target":
            snap["status"] = f"PROFIT TARGET (net {_pnl_txt} ≥ ${profit_target:.2f})"
            self._exit("profit_target", z, spread_now)
        elif exit_reason == "hard_time_stop":
            snap["status"] = (f"HARD TIME-STOP ({held_sec:.0f}s ≥ "
                              f"{hard_mult:g}× max-hold, net {_pnl_txt})")
            self._exit("hard_time_stop", z, spread_now)
        elif exit_reason == "time_stop":
            snap["status"] = f"TIME-STOP ({held_sec:.0f}s ≥ {max_hold_sec:.0f}s, net {_pnl_txt})"
            self._exit("time_stop", z, spread_now)
        elif exit_reason == "trailing_stop":
            snap["status"] = f"TRAILING STOP (net {_pnl_txt}, peak ${peak:.2f})"
            self._exit("trailing_stop", z, spread_now)
        elif exit_reason == "stop":
            snap["status"] = f"STOP (z={z:.2f})"
            self._exit("stop", z, spread_now)
        elif exit_reason == "target":
            snap["status"] = f"EXIT target (z={z:.2f}, net {_pnl_txt})"
            self._exit("target", z, spread_now)
        elif hold_gated:
            snap["status"] = (f"min-hold {held_sec:.0f}s/{min_hold_sec:.0f}s — "
                              f"reverted but holding (z={z:.2f}, net {_pnl_txt})")
        else:
            _extra = " · max-hold EXPIRED (gated)" if max_hold_expired else ""
            snap["status"] = (f"holding {self._pos['direction']} "
                              f"(z={z:.2f}, net {_pnl_txt}){_extra}")

    # ── actions (reuse the shared order path) ────────────────────────────────
    def _enter(self, direction: str, qty: float, z: float, spread: float,
               mid_spread: Optional[float] = None, z_key: str = "zscore") -> Optional[str]:
        """Place the entry. Returns a status string when refused, else None."""
        p = self._params()
        # Stale-signal guard: re-sample the live signal at the moment of entry
        # and refuse if the fill-time z has diverged from the DECISION z.
        max_div = float(p.get("max_entry_z_divergence", 0) or 0)
        if max_div > 0:
            fill_z = (self._signal() or {}).get(z_key)
            if fill_z is None or abs(float(fill_z) - z) > max_div:
                shown = "n/a" if fill_z is None else f"{float(fill_z):.2f}"
                msg = (f"stale signal: decision z={z:.2f} vs fill z={shown} "
                       f"(Δ>{max_div:.2f}) — entry refused")
                self._consec_above = self._consec_below = 0
                self._cooldown_until = self._clock() + float(p.get("cooldown", 300))
                logger.warning("AlgoTrader: %s", msg)
                return msg

        sig_now = self._signal() or {}
        band_src = sig_now.get("band_source") or "ticks"
        band_tf = sig_now.get("band_timeframe") if band_src == "candles" else None
        res = self._execute(direction, qty, source="algo",
                            z=round(z, 4), spread=round(spread, 4)) or {}
        if res.get("success"):
            fill = res.get("fill_spread")
            self._pos = {
                "direction": direction, "qty": qty,
                "entry_z": z, "entry_spread": round(spread, 2),
                "entry_std": sig_now.get("std"),
                "entry_fill_spread": (float(fill) if fill is not None else round(spread, 2)),
                "entry_leg_a": res.get("leg_a_fill"),
                "entry_leg_b": res.get("leg_b_fill"),
                "entry_time": self._clock(),
                "band_source": band_src, "band_tf": band_tf,   # the bands it keeps
                "peak_pnl": 0.0, "trough_pnl": 0.0,
                "peak_min": 0.0, "trough_min": 0.0,
            }
            self._consec_above = self._consec_below = 0
            self._exit_failures = 0
            self._exit_halted = False
            self._exit_retry_at = 0.0
            logger.info("AlgoTrader: ENTER %s qty=%.6f z=%.2f → %s",
                        direction, qty, z, res.get("message"))
            try:
                stop_lv, tgt_lv = self._effective_exit_levels(p)
                logger.info("AlgoTrader: entry geometry — stop $%.2f / target $%.2f "
                            "(fill spread %s, σ=%s, cost $%.2f)",
                            stop_lv, tgt_lv, self._pos.get("entry_fill_spread"),
                            self._pos.get("entry_std"), self._position_fees(p))
            except Exception:
                pass
            return None
        self.last_error = f"entry failed: {res.get('error')}"
        logger.error("AlgoTrader: ENTER failed — %s", res.get("error"))
        return f"entry failed: {res.get('error')}"

    def _exit(self, reason: str, z: float, spread: Optional[float] = None) -> None:
        if not self._pos:
            return
        res = self._close(self._pos["direction"], self._pos["qty"],
                          source="algo", reason=reason, z=round(z, 4),
                          peak_pnl=self._pos.get("peak_pnl"),
                          trough_pnl=self._pos.get("trough_pnl")) or {}
        if res.get("success"):
            closed_dir = self._pos["direction"]
            logger.info("AlgoTrader: EXIT (%s) %s z=%.2f → %s",
                        reason, closed_dir, z, res.get("message"))
            self._pos = None
            self._exit_failures = 0
            self._exit_halted = False
            self._exit_retry_at = 0.0
            p = self._params()
            cooldown = float(p.get("cooldown", 300))
            # A STOP earns a longer cooldown and (optionally) arms the z-reset
            # gate so we don't re-enter the same direction into a runaway move.
            if reason in ("stop", "dollar_stop"):
                cooldown = max(cooldown, float(p.get("stop_cooldown", 0) or 0))
                if bool(p.get("z_reset_after_stop", True)):
                    self._stop_block_dir = closed_dir
            self._cooldown_until = self._clock() + cooldown
        else:
            # Track consecutive failures; after the ceiling, halt auto-exit
            # retries so the human is alerted instead of broker-spamming.
            self._exit_failures += 1
            p = self._params()
            ceiling = int(p.get("max_exit_failures", 5) or 0)
            if ceiling > 0 and self._exit_failures >= ceiling:
                self._exit_halted = True
            base = float(p.get("exit_retry_backoff", 10) or 0)
            delay = 0.0
            if base > 0 and not self._exit_halted:
                cap = float(p.get("exit_retry_backoff_max", 60) or 60)
                delay = min(base * (2 ** (self._exit_failures - 1)), cap)
                self._exit_retry_at = self._clock() + delay
            self.last_error = f"exit failed (x{self._exit_failures}): {res.get('error')}"
            if self._exit_halted:
                plan = "HALTING auto-exit — manual intervention required"
            elif delay > 0:
                plan = f"retry in {delay:.0f}s (backoff; position still open)"
            else:
                plan = "will retry next tick (position still open)"
            logger.error("AlgoTrader: EXIT FAILED — reason=%s attempt=%d err=%s → %s",
                         reason, self._exit_failures, res.get("error"), plan)

    def _set_snap(self, snap: Dict) -> None:
        with self._lock:
            self._snap = snap
