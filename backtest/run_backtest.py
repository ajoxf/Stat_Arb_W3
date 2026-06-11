"""
CLI driver: replay the live SignalGenerator against fetched history,
produce per-pair + basket reports for asset-basket AND cross-pair modes,
and write a Markdown summary the operator can review before deciding
whether to refactor the production code.

Usage:
    # Fetch data first (one-off, ~50 min):
    python -m backtest.fetch_history --days 180

    # Run backtest + generate report:
    python -m backtest.run_backtest --out backtest/reports

The report includes:
  - Per-pair: trades, win rate, net P&L, Sharpe, max drawdown, stop-loss count
  - Asset-basket aggregate (equal-weight and inverse-vol)
  - Cross-pair aggregate
  - Pair correlation matrix
  - Direct comparison vs single-pair baseline (BTC, which the live bot trades)
  - Decision summary: does basket edge justify the refactor?
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Dict

import pandas as pd

from .basket_simulator import (
    inverse_vol_reweight, run_asset_basket, run_cross_pair_basket,
    _available_bases,
)
from .signal_replay import ReplayConfig, replay_pair, summarize_trades

logger = logging.getLogger(__name__)


# Default cross-pair combos for the cross-pair mode.
DEFAULT_CROSS_PAIRS = [
    ("ETH", "SOL"), ("ETH", "AVAX"), ("SOL", "AVAX"),
    ("ETH", "LINK"), ("SOL", "LINK"), ("DOT", "ATOM"),
]


def _fmt_money(x: float) -> str:
    return f"${x:+,.2f}"


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _per_pair_table(per_pair: Dict[str, dict]) -> str:
    if not per_pair:
        return "_no trades_"
    rows = ["| Pair | Trades | Win % | Net $ | Sharpe | MaxDD $ | Stops |",
            "|---|---|---|---|---|---|---|"]
    for pid, m in sorted(per_pair.items(), key=lambda kv: -kv[1]["net_pnl"]):
        rows.append(
            f"| {pid} | {m['trades']} | {_fmt_pct(m['win_rate'])} | "
            f"{_fmt_money(m['net_pnl'])} | {m['sharpe']:.2f} | "
            f"{_fmt_money(m['max_drawdown'])} | {m['stop_losses']} |"
        )
    return "\n".join(rows)


def _basket_summary(label: str, basket: dict) -> str:
    if not basket:
        return f"### {label}\n_no trades_\n"
    return f"""### {label}

- **Pairs traded:** {basket['n_pairs']}
- **Total trades:** {basket['trades']}
- **Win rate:** {_fmt_pct(basket['win_rate'])}
- **Gross P&L:** {_fmt_money(basket['gross_pnl'])}
- **Fees:** {_fmt_money(-basket['fees'])}
- **Slippage:** {_fmt_money(-basket['slippage'])}
- **Net P&L:** {_fmt_money(basket['net_pnl'])}
- **Daily mean:** {_fmt_money(basket['daily_mean'])}  ·  **Daily σ:** {_fmt_money(basket['daily_std'])}
- **Sharpe (annualised):** {basket['sharpe']:.2f}
- **Max drawdown:** {_fmt_money(basket['max_drawdown'])}
- **Stop-losses triggered:** {basket['stop_losses']}
"""


def _correlation_block(basket: dict) -> str:
    corr = basket.get("pair_correlation", {})
    if not corr:
        return ""
    df = pd.DataFrame(corr).round(2)
    return "### Pair P&L correlation\n\n```\n" + df.to_string() + "\n```\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("backtest/data"))
    parser.add_argument("--out", type=Path, default=Path("backtest/reports"))
    parser.add_argument("--notional", type=float, default=1000.0,
                        help="Notional per leg in USD (default 1000)")
    parser.add_argument("--basket-capital", type=float, default=8000.0,
                        help="Target total capital for inverse-vol re-weight")
    parser.add_argument("--lookback", type=int, default=3600)
    parser.add_argument("--entry-z", type=float, default=4.5)
    parser.add_argument("--exit-z",  type=float, default=0.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    args.out.mkdir(parents=True, exist_ok=True)

    cfg = ReplayConfig(
        lookback=args.lookback,
        entry_threshold=args.entry_z,
        exit_threshold=args.exit_z,
        notional_per_leg_usd=args.notional,
    )

    available = _available_bases(args.data)
    logger.info("Available bases: %s", ", ".join(available) or "(none)")
    if not available:
        logger.error("No data in %s. Run `python -m backtest.fetch_history` first.", args.data)
        return

    # 1. Asset basket: each pair is spot vs its own SWAP
    logger.info("Running asset_basket on %d pairs…", len(available))
    asset_trades, asset_per_pair, asset_basket = run_asset_basket(args.data, available, cfg)

    # 1a. Inverse-vol reweight
    asset_trades_invol = inverse_vol_reweight(asset_trades, args.basket_capital)
    asset_basket_invol = {}
    if not asset_trades_invol.empty:
        from .basket_simulator import _basket_metrics
        asset_basket_invol = _basket_metrics(asset_trades_invol, {
            pid: summarize_trades(asset_trades_invol[asset_trades_invol["pair_id"] == pid])
            for pid in asset_trades_invol["pair_id"].unique()
        })

    # 2. Cross-pair basket
    logger.info("Running cross_pair on %d combos…", len(DEFAULT_CROSS_PAIRS))
    cross_trades, cross_per_pair, cross_basket = run_cross_pair_basket(
        args.data, DEFAULT_CROSS_PAIRS, cfg)

    # 3. Single-pair baseline (BTC if present, else first available)
    baseline_base = "BTC" if "BTC" in available else available[0]
    logger.info("Single-pair baseline: %s", baseline_base)
    try:
        b_spot = pd.read_parquet(args.data / f"{baseline_base}-USDT.parquet")
        b_fut  = pd.read_parquet(args.data / f"{baseline_base}-USDT-SWAP.parquet")
        baseline_trades = replay_pair(b_spot, b_fut, cfg, f"{baseline_base}-USDT⊗SWAP")
        baseline_summary = summarize_trades(baseline_trades)
    except FileNotFoundError:
        baseline_summary = {}

    # 4. Persist artifacts
    if not asset_trades.empty:
        asset_trades.to_parquet(args.out / "asset_basket_trades.parquet")
    if not cross_trades.empty:
        cross_trades.to_parquet(args.out / "cross_pair_trades.parquet")
    with open(args.out / "basket_metrics.json", "w") as f:
        json.dump({
            "asset_basket": asset_basket,
            "asset_basket_invol": asset_basket_invol,
            "cross_pair": cross_basket,
            "baseline_single_pair": baseline_summary,
            "config": cfg.__dict__,
        }, f, indent=2, default=str)

    # 5. Markdown report
    md = [
        "# Multi-pair backtest report",
        "",
        f"_Config: lookback={cfg.lookback}, entry|z|={cfg.entry_threshold}, "
        f"exit|z|={cfg.exit_threshold}, notional/leg=${cfg.notional_per_leg_usd:,.0f}_",
        f"_Fees: spot maker {cfg.spot_maker_bps}/taker {cfg.spot_taker_bps}, "
        f"fut maker {cfg.fut_maker_bps}/taker {cfg.fut_taker_bps} bps "
        f"(VIP 4 Group 1)_",
        "",
        "## Single-pair baseline",
        f"**{baseline_base}-USDT spot vs SWAP** — what your live bot currently runs.",
        "",
    ]
    if baseline_summary:
        md.append(_basket_summary(f"{baseline_base} single-pair", {
            "n_pairs": 1, **baseline_summary,
            "gross_pnl": baseline_summary["gross_pnl"],
            "fees": baseline_summary["fees"],
            "slippage": baseline_summary["slippage"],
            "daily_mean": 0.0, "daily_std": 0.0,
            "pair_correlation": {},
        }))
    else:
        md.append("_(no baseline data)_\n")

    md += [
        "## Asset basket — N pairs of spot-vs-perp",
        _basket_summary("Equal-weight (each pair = $%.0f notional)" % cfg.notional_per_leg_usd,
                        asset_basket),
        _basket_summary("Inverse-vol reweighted (total $%.0f deployed)" % args.basket_capital,
                        asset_basket_invol),
        "### Per-pair breakdown (equal-weight)",
        _per_pair_table(asset_per_pair),
        "",
        _correlation_block(asset_basket),
        "## Cross-pair basket — relative-value perp spreads",
        _basket_summary("Cross-pair (equal-weight)", cross_basket),
        "### Per-spread breakdown",
        _per_pair_table(cross_per_pair),
        "",
        _correlation_block(cross_basket),
        "## Decision summary",
        _decision_block(baseline_summary, asset_basket, asset_basket_invol, cross_basket),
    ]

    report_path = args.out / "REPORT.md"
    report_path.write_text("\n".join(md))
    print("\n=== Report written: %s ===" % report_path)
    print("Open it in any Markdown viewer or paste into Claude for analysis.")


def _decision_block(baseline: dict, asset_eq: dict, asset_invol: dict, cross: dict) -> str:
    def get(d, k):
        return d.get(k, 0.0) if d else 0.0
    base_net = get(baseline, "net_pnl")
    base_sr  = get(baseline, "sharpe")
    best_label = "single_pair_baseline"
    best_net   = base_net
    best_sr    = base_sr
    for label, m in [("asset_basket_equal", asset_eq),
                     ("asset_basket_invol", asset_invol),
                     ("cross_pair", cross)]:
        if get(m, "sharpe") > best_sr:
            best_label, best_net, best_sr = label, get(m, "net_pnl"), get(m, "sharpe")

    verdict = (
        f"**Best risk-adjusted: `{best_label}`** "
        f"(Sharpe {best_sr:.2f}, net {_fmt_money(best_net)} over the test window)"
    )
    if best_label == "single_pair_baseline":
        verdict += "\n\n→ **Recommendation: skip the multi-pair refactor.** "
        verdict += "Basket variants did not improve Sharpe vs current single-pair logic on this fee tier. "
        verdict += "Time is better spent on the funding-arb repo or pair-selection research."
    else:
        verdict += "\n\n→ **Recommendation: proceed with multi-pair refactor on a separate branch.** "
        verdict += "Net edge after VIP 4 fees & slippage justifies the rework. "
        verdict += "Next step: scope the refactor in detail before touching production code."
    return verdict


if __name__ == "__main__":
    main()
