"""
Multi-pair statistical arbitrage backtest harness.

Public-data-only, runs against historical OKX 1-minute candles.
Mirrors core/signals.py logic so results extrapolate directly to the
live bot if/when we decide to do the multi-pair refactor.

Flow:
    1. fetch_history.py  → pulls spot + perp 1-min candles for N pairs
    2. signal_replay.py  → replays SignalGenerator math on one pair
    3. basket_simulator.py → runs N pairs, aggregates, weights capital
    4. report.py         → HTML/markdown summary with edge metrics

Decisions about strategy backtested:
    - Same fees as live: VIP 4 Group 1 (spot 3.0/4.5, fut 0.8/2.7 bps)
    - Same slippage assumption (1.5 bps/leg default; tunable)
    - Same SignalGenerator math: spread = futures - spot, z-score on
      rolling lookback, STD filter for profitability ratio
    - Two basket modes:
        (a) "asset_basket"  — N pairs, each spot-vs-perp (mirrors live logic)
        (b) "cross_pair"    — additional spreads like ETH-vs-SOL (ratio-based)
"""

from __future__ import annotations
