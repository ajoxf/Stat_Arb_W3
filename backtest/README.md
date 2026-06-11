# Multi-pair backtest harness

Public-data-only. Zero impact on the live trading process. Lives entirely under
`backtest/` and reads no production state.

## Purpose

Decide — with **data, not assumptions** — whether refactoring the live single-pair
bot into a multi-pair basket is worth the 2–3 weeks of work and the production
risk it would entail.

## Two-step workflow

### 1. Fetch history (~50 minutes, run once)

```bash
pip install aiohttp numpy pandas pyarrow

python -m backtest.fetch_history --days 180
```

Pulls 6 months of 1-minute OHLC for 8 candidate bases (ETH, SOL, AVAX, LINK,
MATIC, DOT, ATOM, NEAR) × 2 legs each (SPOT + SWAP) = 16 instruments. Plus
BTC if you add `--pairs BTC ETH SOL AVAX LINK MATIC DOT ATOM NEAR`.

Output: `backtest/data/*.parquet` (one per instrument).

OKX public endpoint is rate-limited (~20 req/2s anon); the fetcher self-throttles
to 5 req/sec to leave headroom.

### 2. Run the backtest and generate report (~5 minutes)

```bash
python -m backtest.run_backtest --out backtest/reports
```

Produces:

- `backtest/reports/REPORT.md` — human-readable summary
- `backtest/reports/basket_metrics.json` — machine-readable metrics
- `backtest/reports/asset_basket_trades.parquet` — every trade in the basket
- `backtest/reports/cross_pair_trades.parquet` — every cross-pair trade

## What gets compared

| Strategy | Description |
|---|---|
| **Single-pair baseline** | BTC-USDT spot vs SWAP — what the live bot does today |
| **Asset basket (equal-weight)** | All 8 pairs at the same `$notional_per_leg`, run independently |
| **Asset basket (inverse-vol reweighted)** | Same pairs, but capital allocated inversely to realised P&L vol |
| **Cross-pair basket** | Relative-value spreads (ETH-vs-SOL, ETH-vs-AVAX, DOT-vs-ATOM, ...) on perps using log-prices |

For each: total trades, win rate, gross & net P&L, fees & slippage drag, Sharpe
(annualised on daily returns), max drawdown, stop-loss frequency. Plus pair P&L
correlation matrix so you can see if the diversification is real.

## Fidelity to live bot

The replay mirrors `core/signals.py` 1:1:

- Spread = `futures_price - spot_price`
- Rolling mean & std over `lookback` 1-minute bars (default 3600)
- z-score = `(spread - mean) / std`
- STD filter: enter only if `std / (round_trip_cost_bps / 10000 * spot_price) >= min_std_multiple`
- Hurst optional (default disabled, matching live)
- LONG entry: `z >= entry_threshold` AND filters pass
- SHORT entry: `z <= -entry_threshold` AND filters pass
- Exit when z reverts to `exit_threshold` (computed vs entry-time mean)
- Stop-loss when `|z| >= stop_loss_threshold`
- Fees: VIP 4 Group 1 (3.0/4.5 spot, 0.8/2.7 fut) applied per leg, both sides
- Slippage: 1.5 bps per leg default, on all 4 legs

If the replay disagrees with what the live bot would actually do on the same
history, that's a fidelity bug — file it before believing the report.

## Reading the report

The report ends with a **Decision summary** block that picks the highest-Sharpe
variant and gives a recommendation:

- **"Best = single_pair_baseline"** → skip the multi-pair refactor. Time better
  spent elsewhere (funding-arb repo, pair selection research, or more parameter
  tuning on the current bot).
- **"Best ≠ single_pair_baseline"** → proceed to scope the refactor on a
  separate branch. Net edge after fees & slippage justifies the work.

The recommendation is mechanical. The judgement is yours — look at:

- Is the basket Sharpe meaningfully higher, or just a couple of basis points?
- Are most pairs contributing or is one outlier carrying the basket?
- Is the pair correlation matrix mostly off-diagonal (independent signals = good
  diversification) or mostly diagonal (signals all fire together = no real
  diversification)?
- Does the stop-loss count blow up on the basket?

## Tuning

Override defaults via CLI flags:

```bash
python -m backtest.run_backtest \
  --lookback 1800 \
  --entry-z 3.5 \
  --exit-z 0.5 \
  --notional 1500 \
  --basket-capital 12000
```

Be careful: walk-forward overfit risk is real. Don't tune until you've seen the
out-of-sample results on at least 2 different 3-month windows.

## What this harness does NOT do

- Funding rate income/cost (matters for holds > 8h; current strategy rarely does)
- Exchange-side liquidations (no leverage simulation; assume cash margin)
- Borrow cost (assume unlevered spot)
- Order book impact (slippage is a flat bps estimate, not size-aware)
- Latency simulation (fills assumed at the next 1-min close)

For our decision — "does basket Sharpe meaningfully beat single-pair Sharpe on
this fee tier?" — these omissions don't change the answer.
