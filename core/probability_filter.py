"""OU-based probability filter for trade entry decisions (ported from
arrow-statarb, adapted to USD / crypto).

Blocks low-quality entries, yielding fewer but more profitable trades.

Win probability uses the Ornstein-Uhlenbeck gambler's-ruin formula
(OU scale function): given the spread is at z₀ σ, what is the probability
that it reverts to the exit level before blowing out to the stop-loss level?

    P_win = 1 − erfi(|z₀|/√2) / erfi(z_stop/√2)

Typical entries (z₀ = 2–3.5, z_stop = 4) yield P_win > 80 %, so the 60 %
threshold blocks only trades already dangerously close to the stop.
"""

from __future__ import annotations

import math
from typing import Dict, Tuple

try:
    from scipy.special import erfi as _scipy_erfi

    def _erfi(x: float) -> float:
        return float(_scipy_erfi(x))

except ImportError:
    def _erfi(x: float) -> float:
        """Fallback erfi via asymptotic + series without scipy."""
        x = abs(x)
        if x == 0:
            return 0.0
        if x > 4.0:
            return math.exp(min(x * x, 700)) / (math.sqrt(math.pi) * x)
        result, term, k = x, x, 0
        x2 = x * x
        while k < 60:
            k += 1
            term *= x2 / k
            contrib = term / (2 * k + 1)
            result += contrib
            if abs(contrib) < 1e-12 * abs(result):
                break
        return (2.0 / math.sqrt(math.pi)) * result


class ProbabilityFilter:
    """Three quality checks before entry:

    1. Break-even z — |z| must exceed the minimum spread move needed to
       cover the round-trip cost ($).
    2. OU gambler's-ruin win probability — P(revert to exit_z before
       hitting stop_z) ≥ min_win_probability.
    3. Expected value — EV ($) ≥ min_expected_value after weighting the
       profit vs the stop-loss payoff.
    """

    def __init__(
        self,
        *,
        min_win_probability: float = 0.60,
        min_expected_value: float = 0.0,
        exit_zscore: float = 0.0,
        stop_zscore: float = 4.0,
        enabled: bool = True,
    ):
        self.min_win_probability = min_win_probability
        self.min_expected_value = min_expected_value
        self.exit_zscore = abs(exit_zscore)
        self.stop_zscore = abs(stop_zscore)
        self.enabled = enabled

    def check_entry(self, z_score: float, std: float, qty: float,
                    round_trip_cost_usd: float) -> Tuple[bool, str, Dict]:
        """Decide whether an ENTRY signal should be allowed.

        ``std`` is the spread σ in $/unit; ``qty`` the position size in units
        (BTC), so σ × qty is the $ value of one z of reversion.
        Returns (allow, reason, metrics)."""
        if not self.enabled:
            return True, "filter_disabled", {}
        if std <= 0 or qty <= 0:
            return False, "insufficient_data", {}

        metrics = self._compute_metrics(z_score, std, qty, round_trip_cost_usd)

        if abs(z_score) < metrics["breakeven_z"]:
            return False, "below_breakeven", metrics
        if metrics["win_probability"] < self.min_win_probability:
            return False, "low_win_probability", metrics
        if metrics["expected_value"] < self.min_expected_value:
            return False, "negative_ev", metrics
        return True, "all_checks_passed", metrics

    # ----------------------------------------------------------------- private

    def _win_probability(self, z: float) -> float:
        """P(hit exit_zscore before stop_zscore | normalized OU process at |z|)."""
        z_abs = abs(z)
        if z_abs >= self.stop_zscore:
            return 0.0
        if z_abs <= self.exit_zscore:
            return 1.0
        try:
            sqrt2 = math.sqrt(2)
            erfi_z = _erfi(z_abs / sqrt2)
            erfi_stop = _erfi(self.stop_zscore / sqrt2)
            if erfi_stop <= 0:
                return 1.0
            return max(0.0, 1.0 - erfi_z / erfi_stop)
        except Exception:
            span = self.stop_zscore - self.exit_zscore
            return max(0.0, (self.stop_zscore - z_abs) / span)

    def _compute_metrics(self, z: float, std: float, qty: float,
                         rt_cost: float) -> Dict:
        scale = std * qty                       # $ per 1z of spread movement

        breakeven_z = rt_cost / scale if scale > 0 else float("inf")

        p_win = self._win_probability(z)
        p_stop = 1.0 - p_win

        profit_if_win = max(0.0, (abs(z) - self.exit_zscore) * scale)
        loss_if_stop = max(0.0, (self.stop_zscore - abs(z)) * scale)
        ev = p_win * profit_if_win - p_stop * loss_if_stop - rt_cost

        return {
            "z_score": z,
            "std": std,
            "breakeven_z": round(breakeven_z, 4),
            "win_probability": round(p_win, 4),
            "expected_value": round(ev, 2),
            "round_trip_cost": round(rt_cost, 2),
            "profit_if_win": round(profit_if_win, 2),
            "loss_if_stop": round(loss_if_stop, 2),
        }
