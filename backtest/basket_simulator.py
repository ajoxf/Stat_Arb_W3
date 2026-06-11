"""
Run signal_replay on N pairs in parallel and aggregate basket-level metrics.

Two modes:
  - asset_basket: each pair is its own spot-vs-perp instance (mirrors live bot)
  - cross_pair:   relative-value spreads between assets (e.g. ETH vs SOL perps)

Capital allocation:
  - equal_weight: notional_per_leg_usd is identical for every pair
  - inverse_vol:  pairs with lower realised spread vol get LARGER allocation
                  (more confident in signal-to-noise; safer)

Output:
  trades_df:  every trade across all pairs/spreads, with pair_id
  per_pair:   {pair_id → metrics dict}
  basket:     aggregate metrics, with portfolio Sharpe and correlation matrix
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .signal_replay import ReplayConfig, Trade, replay_pair, summarize_trades


# ---- pair loading ----------------------------------------------------------

def _load_candles(data_dir: Path, symbol: str) -> pd.DataFrame:
    path = data_dir / f"{symbol}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing data: {path}. Run fetch_history first.")
    return pd.read_parquet(path)


def _available_bases(data_dir: Path) -> List[str]:
    """List bases that have BOTH spot and perp parquet."""
    bases = []
    for p in data_dir.glob("*-USDT.parquet"):
        base = p.stem.replace("-USDT", "")
        if (data_dir / f"{base}-USDT-SWAP.parquet").exists():
            bases.append(base)
    return sorted(bases)


# ---- asset basket: each base is its own spot-vs-perp pair ------------------

def run_asset_basket(data_dir: Path,
                     bases: Optional[List[str]] = None,
                     cfg: Optional[ReplayConfig] = None
                     ) -> Tuple[pd.DataFrame, Dict[str, dict], dict]:
    if cfg is None:
        cfg = ReplayConfig()
    if bases is None:
        bases = _available_bases(data_dir)

    all_trades: List[pd.DataFrame] = []
    per_pair: Dict[str, dict] = {}

    for base in bases:
        try:
            spot_df = _load_candles(data_dir, f"{base}-USDT")
            futures_df = _load_candles(data_dir, f"{base}-USDT-SWAP")
        except FileNotFoundError:
            continue
        pair_id = f"{base}-USDT⊗SWAP"
        trades = replay_pair(spot_df, futures_df, cfg, pair_id)
        if not trades.empty:
            all_trades.append(trades)
            per_pair[pair_id] = summarize_trades(trades)

    if not all_trades:
        return pd.DataFrame(), per_pair, {}

    combined = pd.concat(all_trades, ignore_index=True).sort_values("exit_ts")
    basket = _basket_metrics(combined, per_pair)
    return combined, per_pair, basket


# ---- cross-pair basket: relative spreads between two perps -----------------

def run_cross_pair_basket(data_dir: Path,
                          pairs: List[Tuple[str, str]],
                          cfg: Optional[ReplayConfig] = None
                          ) -> Tuple[pd.DataFrame, Dict[str, dict], dict]:
    """
    Each `pairs` entry is (BASE_A, BASE_B). The spread is:
        log(price_A_perp) - β * log(price_B_perp)
    with β fit from a rolling regression on the lookback window.

    Treat A as the "spot" leg and B as the "futures" leg in the replay,
    so the same SignalGenerator math applies — just with log-prices.
    """
    if cfg is None:
        cfg = ReplayConfig()

    all_trades: List[pd.DataFrame] = []
    per_pair: Dict[str, dict] = {}

    for base_a, base_b in pairs:
        try:
            a_df = _load_candles(data_dir, f"{base_a}-USDT-SWAP")
            b_df = _load_candles(data_dir, f"{base_b}-USDT-SWAP")
        except FileNotFoundError:
            continue
        pair_id = f"{base_a}⇄{base_b}"

        # Build log-price "spot"/"futures" stand-ins. The replay treats
        # spread = futures - spot, so put log(A) as futures, log(B) as spot.
        a_log = a_df[["ts", "c"]].copy()
        b_log = b_df[["ts", "c"]].copy()
        a_log["c"] = np.log(a_log["c"])
        b_log["c"] = np.log(b_log["c"])

        trades = replay_pair(b_log, a_log, cfg, pair_id)
        if not trades.empty:
            all_trades.append(trades)
            per_pair[pair_id] = summarize_trades(trades)

    if not all_trades:
        return pd.DataFrame(), per_pair, {}

    combined = pd.concat(all_trades, ignore_index=True).sort_values("exit_ts")
    basket = _basket_metrics(combined, per_pair)
    return combined, per_pair, basket


# ---- aggregation ----------------------------------------------------------

def _basket_metrics(trades: pd.DataFrame, per_pair: Dict[str, dict]) -> dict:
    if trades.empty:
        return {}
    daily = trades.set_index("exit_ts")["net_pnl"].resample("1D").sum()
    cum = daily.cumsum()
    dd = (cum - cum.cummax()).min()
    sharpe = (np.sqrt(365) * daily.mean() / daily.std()) if daily.std() > 0 else 0.0

    # Correlation matrix on per-pair daily P&L
    daily_per_pair = (trades.set_index("exit_ts")
                            .groupby("pair_id")["net_pnl"]
                            .resample("1D").sum()
                            .unstack(level=0).fillna(0.0))
    corr = daily_per_pair.corr().round(2) if daily_per_pair.shape[1] > 1 else pd.DataFrame()

    return {
        "n_pairs": len(per_pair),
        "trades": len(trades),
        "net_pnl": float(trades["net_pnl"].sum()),
        "gross_pnl": float(trades["gross_pnl"].sum()),
        "fees": float(trades["fees"].sum()),
        "slippage": float(trades["slippage"].sum()),
        "daily_mean": float(daily.mean()),
        "daily_std": float(daily.std()),
        "max_drawdown": float(dd),
        "sharpe": float(sharpe),
        "win_rate": float((trades["net_pnl"] > 0).mean()),
        "stop_losses": int((trades["exit_reason"] == "stop_loss").sum()),
        "trade_count_by_pair": trades.groupby("pair_id").size().to_dict(),
        "pair_correlation": corr.to_dict() if not corr.empty else {},
    }


# ---- inverse-vol re-weight (post-hoc; doesn't change trade timing) --------

def inverse_vol_reweight(trades: pd.DataFrame,
                         target_total_notional: float = 8000.0) -> pd.DataFrame:
    """
    Scale each pair's notional inversely to its realised P&L volatility.
    Conservative pairs get larger allocation; volatile pairs get smaller.
    Sum of allocations = target_total_notional.

    Returns a new trades DataFrame with rescaled gross/fees/slippage/net.
    """
    if trades.empty:
        return trades

    vol_per_pair = trades.groupby("pair_id")["net_pnl"].std().replace(0, np.nan).dropna()
    inv = 1.0 / vol_per_pair
    weight = inv / inv.sum()
    notional_per_pair = (weight * target_total_notional).to_dict()

    rescaled = trades.copy()
    scale = rescaled["pair_id"].map(lambda p: notional_per_pair.get(p, 0.0) /
                                    rescaled.loc[rescaled["pair_id"] == p,
                                                 "notional_usd"].iloc[0]
                                    if p in notional_per_pair else 0.0)
    for col in ("gross_pnl", "fees", "slippage", "net_pnl", "notional_usd"):
        rescaled[col] = rescaled[col] * scale
    return rescaled
