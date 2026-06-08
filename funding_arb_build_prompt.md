# Build Prompt: Crypto Funding Rate Arbitrage Bot on OKX

> **Paste this entire file as the first message to Claude in a fresh repo session.**
> It is a self-contained brief — Claude will not have seen the prior project.

---

## 0. Mission

Build a production-quality **delta-neutral funding-rate arbitrage** trading bot for OKX in Python. The bot holds **long spot + short perpetual swap** in equal notional across one or more USDT-margined pairs, harvesting the funding payments the perp shorts receive every 8 hours, while remaining indifferent to BTC/ETH/etc. price direction.

The operator is **VIP 4 on OKX** (spot Group 1: 3.0/4.5 bps maker/taker; futures Group 1: 0.8/2.7 bps maker/taker). The bot must respect that fee tier in every cost calculation.

**Build it carefully.** Real money flows through it. The operator runs it on a Windows VM with Python 3.11+. Follow every guardrail in §6.

---

## 1. The Strategy in Detail

### 1.1 The trade

For a target pair (e.g. `ETH-USDT` spot + `ETH-USDT-SWAP` perp):
1. Long `N` USD of spot.
2. Short `N` USD of perp (1 perp contract = `ctVal` of base asset, lookup via `/api/v5/public/instruments`).
3. Net delta = 0. Mark-to-market changes on the spot and perp cancel.
4. At each 8-hour funding settlement (00:00 / 08:00 / 16:00 UTC), the perp short **receives** `funding_rate × notional` when funding is positive (the typical case in bull markets), **pays** that amount when negative.
5. Periodically rotate to the highest-funding pair (or hold the basket if running multi-pair).

### 1.2 Edge math (must be encoded in the cost model)

```
expected_8h_return = funding_rate                       # positive = you earn
fees_per_round_trip = 4 × leg_bps                       # enter+exit, spot+fut
spot_borrow_cost_per_8h = (borrow_apr / 365 / 3) × ratio_borrowed
net_8h = expected_8h_return - fees_amortized_per_8h - spot_borrow_cost_per_8h
```

A trade is taken only when:
- `funding_rate > FUNDING_FLOOR` (configurable, default 0.005% per 8h = 5.5% APR)
- `funding_rate × expected_hold_8h_periods > total_round_trip_cost`
- The pair's open interest and 24h volume exceed `MIN_LIQUIDITY_USD` (default $50M OI, $200M volume)

### 1.3 Strategy variants — implement both, expose as config

- **Static carry:** pick the top-1 funding pair, hold until funding drops below `FUNDING_FLOOR` or basis blows out. Lowest fees, simplest.
- **Rotating carry:** every funding settlement, evaluate the top-N pairs by trailing-7d average funding (volume-weighted); rotate if the new #1 beats current holding by `ROTATION_THRESHOLD` (default 50 bps APR). Higher fees, captures more edge.

Default to static; let operator switch via config.

---

## 2. Architecture (target layout)

```
funding-arb/
├── README.md
├── .env.example                        # template; never commit real .env
├── requirements.txt
├── models.py                           # dataclasses, no logic
├── app.py                              # Flask + SocketIO entry point
├── adapters/
│   ├── __init__.py
│   ├── base.py                         # ExchangeAdapter ABC
│   └── okx_adapter.py                  # OKX V5 REST + WebSocket
├── core/
│   ├── trading_engine.py               # orchestrates strategy + executor
│   ├── strategies/
│   │   ├── base.py                     # Strategy ABC: evaluate() → Signal
│   │   ├── static_carry.py
│   │   └── rotating_carry.py
│   ├── delta_neutral_manager.py        # enter/exit/rebalance one pair
│   ├── funding_poller.py               # async loop: funding rates + history
│   ├── pair_selector.py                # rank by trailing funding × liquidity
│   ├── risk_manager.py                 # kill switches; see §7
│   ├── order_executor.py               # places coordinated 2-leg orders
│   ├── telegram_bot.py                 # operator alerts
│   ├── ai_monitor.py                   # periodic Claude health review
│   └── trade_logger.py                 # CSV + DB
├── database/
│   └── manager.py                      # sqlite tables: positions, fundings,
│                                       #   trades, signals, account_snapshots
├── backtest/
│   ├── fetch_history.py                # 90+ days of funding + spot OHLC
│   ├── replay.py                       # walk-forward simulator
│   └── analyze.py                      # P&L breakdown, Sharpe, max DD
├── templates/                          # Flask dashboard
│   ├── base.html
│   ├── dashboard.html                  # positions, funding curves, P&L
│   ├── settings.html
│   └── backtest.html                   # show backtest results
├── tests/
│   ├── test_pair_selector.py
│   ├── test_delta_neutral_manager.py
│   ├── test_okx_adapter.py             # mocked
│   └── test_orders_live.py             # 40-scenario suite (see §5.4)
└── scripts/
    └── sweep_dust.py                   # standalone dust cleaner
```

---

## 3. OKX V5 API Reference (critical endpoints)

Use OKX V5 only. Document each call's rate limit in code comments.

| Purpose | Endpoint | Rate limit (per UID) |
|---|---|---|
| Place order | `POST /api/v5/trade/order` | 60 req / 2 s |
| Cancel order | `POST /api/v5/trade/cancel-order` | 60 req / 2 s |
| Order status | `GET /api/v5/trade/order` | 60 req / 2 s |
| Pending orders | `GET /api/v5/trade/orders-pending` | 60 req / 2 s |
| Account positions | `GET /api/v5/account/positions` | 10 req / 2 s |
| Account balance | `GET /api/v5/account/balance` | 10 req / 2 s |
| Account config | `GET /api/v5/account/config` | 5 req / 2 s |
| Set leverage | `POST /api/v5/account/set-leverage` | 20 req / 2 s |
| **Current funding rate** | `GET /api/v5/public/funding-rate?instId=…` | 20 req / 2 s |
| **Funding rate history** | `GET /api/v5/public/funding-rate-history?instId=…` | 10 req / 2 s |
| Instrument info (min lot, ctVal, tickSz) | `GET /api/v5/public/instruments?instType=SWAP` | 20 req / 2 s |
| Order book | `GET /api/v5/market/books?instId=…&sz=20` | 40 req / 2 s |
| Ticker | `GET /api/v5/market/ticker?instId=…` | 20 req / 2 s |
| WebSocket private (orders, fills, positions) | `wss://ws.okx.com:8443/ws/v5/private` | — |
| WebSocket public (book, funding) | `wss://ws.okx.com:8443/ws/v5/public` | — |

**Strongly prefer WebSocket for streaming book + private fills** to keep REST budget for funding polls and order placement.

---

## 4. OKX Quirks (these will bite you — bake them in)

### 4.1 Environment header
- **Live**: send no `x-simulated-trading` header.
- **Demo**: send `x-simulated-trading: 1`.
- **Demo keys and live keys are not interchangeable.** Error code `50101 "APIKey does not match current environment"` means they don't match. Read `OKX_DEMO_MODE` from `.env` at startup and refuse to start if the value is ambiguous.

### 4.2 SPOT MARKET BUY oddity
For `tdMode=cross` SPOT MARKET BUY, the `sz` parameter is the **USDT notional**, not the base-asset quantity. Detect this case in the adapter; switch units automatically.

### 4.3 SWAP position side
`pos` (qty) field can be positive or negative depending on side. **Trust `posSide` (`long`/`short`)**, not the sign of `pos`. Convert SWAP qty into base-asset units via `ctVal` (always — never assume 1 contract = 1 BTC).

### 4.4 Fills
- `accFillSz` for SPOT = base asset amount filled.
- `accFillSz` for SWAP = **contracts**, not base asset. Always multiply by `ctVal` for base asset.
- Read `avgPx` for fill price.

### 4.5 Reduce-only behavior
When closing a SWAP, set `reduceOnly: true` to prevent accidental opening of the opposite side if size drifts. Error `51169 "Order would not reduce position size"` means engine and exchange disagree on position — reconcile before retrying.

### 4.6 Rate limit error
Code `50013 "Systems are busy"` is transient. Retry with exponential backoff `[2, 4, 8, 16] s` then surface. **Never retry indefinitely.** Track 5xx + 50013 in a sliding 60s window; if > 20% error rate, halt new orders for 5 minutes.

### 4.7 Clock drift
Code `50102 "Timestamp request expired"` rejects orders. At startup, the adapter must:
1. Sync time via the OKX `/api/v5/public/time` endpoint (no auth needed).
2. Compute offset to local clock.
3. Refuse to place orders if `|offset| > 15 seconds`.
4. Log offset every 5 minutes; alert if it drifts past 5 s during operation.

On Windows, recommend `w32tm /resync /force` in operator's runbook.

### 4.8 IP whitelisting
Trade-permission API keys **require** IP whitelist on OKX. The operator's VM's public IP must be added when the key is created. If you see error code `50110` or similar, surface this as the cause in the operator-facing error message — don't just log a hex code.

### 4.9 Funding rate quirks
- `fundingTime` field is the **next** funding payment timestamp.
- `fundingRate` field is the rate that **will** be applied at `fundingTime`. The most recent settled rate is in `funding-rate-history`.
- Polling `funding-rate` 5 minutes before each settlement catches rate updates from OKX's auction.
- Pairs occasionally hit funding-rate caps (e.g. ±0.75% per 8h). Treat capped funding as a regime signal (extreme positioning), not as a normal trade.

---

## 5. Phased Build Order (do not skip phases)

### Phase 1: Data + Backtest (week 1, no live keys needed)
1. `adapters/okx_adapter.py` — public-data only (no auth required for ticker, book, funding-history, instruments).
2. `backtest/fetch_history.py` — pull 90+ days of:
   - 8-hour funding rates for all USDT-margined perps
   - 1-minute spot + perp OHLC (for basis tracking)
   - Instrument metadata (ctVal, minSz, tickSz)
   - Save as parquet under `data/` (gitignored).
3. `backtest/replay.py` — simulate strategies on the historical data:
   - Honor VIP 4 fees (3.0/4.5 spot, 0.8/2.7 fut).
   - Honor real `minSz` and `ctVal` per pair.
   - Model spot borrow cost (use OKX's published USDT lending rate history, or default to 8% APR).
   - Honor `MIN_LIQUIDITY_USD` filter.
4. `backtest/analyze.py` — output per strategy: total P&L, monthly P&L, Sharpe (8h periods), max drawdown, # rotations, average funding captured, fee drag, borrow cost drag.

**Gate to Phase 2:** static carry must show positive net APR on at least 80% of 30-day rolling windows over the last 6 months. If it doesn't, the operator's fee tier or chosen pairs aren't enough — surface this clearly and stop.

### Phase 2: Paper Engine (week 2)
1. `adapters/okx_adapter.py` — add private-auth REST methods (no order placement yet; reads only).
2. Connect with **demo keys**. Verify `account/balance`, `account/positions` return successfully (this exercises the environment plumbing).
3. `core/funding_poller.py`, `core/pair_selector.py`, `core/strategies/static_carry.py` — same logic as backtest, run against live data.
4. `core/order_executor.py` — generate `Order` objects but log them, do not submit. Verify the order params (size, price, side, ccy field for cross margin, posSide) by inspection.
5. Run paper for **5 trading days minimum**. Compare generated orders to backtest predictions over the same window. Variance < 20% in count and notional → pass.

**Gate to Phase 3:** paper run completes without unhandled exceptions; orders match backtest within 20%; operator manually reviews 5 random order specs and confirms each leg's sz, side, ccy, posSide are correct.

### Phase 3: Single-Pair Live (week 3)
1. Switch to **live OKX keys** in `.env`. Verify with public `/api/v5/account/config` call returns code 0.
2. Implement order placement in `order_executor.py` with full retry + rate-limit budget.
3. Implement the **40-scenario order test suite** (port `mt5_test_suite_reference.py`-style — see §5.4). Run single scenarios before letting the strategy run autonomously.
4. Hardcode the bot to **single-pair, static carry**, **$1k–2k notional per leg**.
5. Run live for 7 days under operator supervision. Monitor:
   - Order fill rates (LIMIT fills > 90%)
   - Realized vs expected funding income (< 10% variance per day)
   - Position drift (delta should stay within ±1% of zero between funding events)
   - All risk kill switches (§7) confirm work via manual trigger tests

**Gate to Phase 4:** 7 days of live single-pair with no manual interventions required; realized P&L within 30% of paper prediction; all kill switches manually tested and confirmed working.

### Phase 4: Multi-Pair Rotation (week 4+)
1. Lift hardcoded pair selection to use `pair_selector.py`.
2. Enable rotating-carry strategy.
3. Scale notional per the operator's risk budget.

### 5.4 Order Test Suite (port verbatim from prior repo)
The operator has an existing 40-scenario automated test suite (`mt5_test_suite_reference.py` in the previous repo). Port it for OKX:
- 18 LIMIT + 18 MARKET scenarios × 6 order types (BUY_SPOT, SELL_SPOT, BUY_FUTURES, SELL_FUTURES, LONG_DELTA_NEUTRAL, SHORT_DELTA_NEUTRAL)
- 4 partial-fill recovery scenarios
- Live WebSocket progress panel
- Per-scenario timeout, pass/fail, detailed P&L breakdown
- See the reference file for the full runner skeleton

---

## 6. Hard Guardrails (non-negotiable)

### 6.1 Credentials — single source of truth
- API keys live **only** in `.env` as `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`, `OKX_DEMO_MODE`.
- **Do not** also store them in the database.
- **Do not** create a Settings UI form that pretends to save them but doesn't (the prior project had this exact bug — dead form, no save handler).
- If you add a Settings UI for keys, it must (a) write to `.env`, (b) rebuild the adapter, (c) re-test the connection, (d) refuse to save if test fails. Otherwise, omit the UI and document `.env` as the only place.

### 6.2 Live vs demo refusal
At startup, if `OKX_DEMO_MODE=false` (live) but a quick auth check returns `50101`, **refuse to start the trading loop**. Log a clear error: *"Stored API key was issued for the demo environment; live mode requires keys created on www.okx.com → API."* Same in reverse.

### 6.3 Dust threshold
Any exchange position with `abs(quantity × price) < $1` USD is treated as rounding residue. Never count it as a real position. Never try to place orders below `minSz` (will reject with `51020` or similar). Surface it in a separate `dust_positions` list with a `closeable: bool` flag based on `qty >= minSz`. **The prior project lost 30 minutes hammering the API trying to close uncloseable dust** — pre-flight every close against `minSz` first.

### 6.4 Min-lot enforcement
Every order placement function fetches the symbol's `minSz` and `lotSz` via cached `get_symbol_info`. Refuse to submit orders below `minSz`. Round size down to `lotSz`. Log if rounding causes a > 1% size difference from the intended notional.

### 6.5 Clock sync gate
At startup and every 5 minutes:
- Fetch `GET /api/v5/public/time` (unauthenticated).
- Compare to `datetime.now(timezone.utc)`.
- If `|drift| > 15s`, refuse to place new orders; existing positions can be closed but not opened.
- Telegram alert if drift > 5 s.

### 6.6 Asyncio loop hygiene
- All adapter calls use a single event loop owned by `trading_engine`.
- Use `asyncio.wait_for(..., timeout=30)` on every external call — never an unbounded await.
- On engine reset or adapter swap, **close old aiohttp sessions** explicitly (`await session.close()`). The prior project leaked `Unclosed client session` warnings every time the operator swapped exchanges.
- Guard all tick callbacks against `None` ticks — engine reset transiently nulls them.

### 6.7 No silent failures
Every caught exception logs at WARNING or ERROR level with `logger.exception(...)` for the traceback. **Never `except: pass`.** If you must suppress, the comment beside it must explain exactly why.

### 6.8 Idempotent order placement
Use OKX's `clOrdId` (client order ID) field on every placement, generated as `f"{strategy}-{pair}-{epoch_ms}-{uuid8}"`. On retry-after-timeout, query by `clOrdId` first to detect whether the original placement succeeded. **The prior project occasionally double-placed during retries.**

### 6.9 Persistent state for crash recovery
Before any state-changing operation (open/close leg, rotate), persist the intent to the DB with a `status: 'pending'` row. On startup, scan for pending rows and reconcile against the exchange. Never assume the bot's view of "what should be open" matches reality after a crash — query the exchange first.

---

## 7. Risk Kill Switches (all must exist; all alert Telegram)

| Switch | Trigger | Action |
|---|---|---|
| **Funding flip** | Active position's pair shows funding < `EXIT_FUNDING` (default -0.005% / -5.5% APR) for 2 consecutive settlements | Close the position, pause rotation for 4 h |
| **Basis blowout** | `\|perp_mid - spot_mid\| / spot_mid > BASIS_KILL_PCT` (default 2%) | Close immediately, alert critical |
| **Drawdown** | Account equity drops > `MAX_DRAWDOWN_PCT` from recent high (default 5%) | Halt new entries, hold existing, alert |
| **Borrow rate spike** | USDT borrow APR > `MAX_BORROW_APR` (default 25%) | Halt new entries, exit if currently in margin-borrowed position |
| **Stale data** | No tick or funding update for 60 s | Halt new entries, alert |
| **Order error rate** | > 20% order placements fail in trailing 60 s window | Halt new entries for 5 min, alert |
| **Position drift** | Delta exposure exceeds ±1% of notional after rebalance | Force-rebalance via market; if fails 2× alert critical |
| **Operator kill** | Telegram command `/halt` or dashboard button | Halt new entries instantly; ongoing closes allowed |

The risk manager runs as its own asyncio task at 1 Hz. Each switch logs to a dedicated `risk_events` table.

---

## 8. Observability

### 8.1 Dashboard (Flask + SocketIO)
- Top bar: connection status, account UID, equity, available, current pair, current delta (should be ~0)
- Funding curve: 7-day rolling funding rate per held pair
- P&L: today, 7d, 30d (gross funding income, fees, borrow cost, net)
- Positions table: each leg with size, mark, unrealized
- Risk panel: current state of every kill switch
- Recent trades / rotations log

### 8.2 Telegram
- Entry/exit confirmations with funding rate captured
- All kill switch triggers (severity: warn / critical)
- Daily P&L summary at 00:05 UTC
- Operator commands: `/status`, `/halt`, `/resume`, `/close <pair>`, `/pnl`

### 8.3 AI Monitor (port from prior repo)
- `core/ai_monitor.py` — port verbatim. Adjust prompt to look for:
  - Stale funding polls
  - Position drift > 0.5% sustained
  - Repeated 50013 / 50102 / 51169 codes
  - Daily realized funding < 30% of expected (suggests fills slipping)
- Same self-disable behaviour without `ANTHROPIC_API_KEY`.
- Same 2 h repeat-alert suppression.

### 8.4 Structured logs
- `logs/trading_YYYYMMDD.log` — rotating daily.
- `logs/trades_YYYYMMDD.csv` — every order with timestamp, pair, side, size, target px, fill px, fee, clOrdId.
- `logs/funding_YYYYMMDD.csv` — every settlement with pair, rate, position_size, funding_paid_to_us.

---

## 9. Configuration Surface

Keep `models.py` minimal — one `BotConfig` dataclass. Sensible defaults:

```python
@dataclass
class BotConfig:
    # Strategy
    strategy: str = "static_carry"             # or "rotating_carry"
    pairs_whitelist: list[str] = field(default_factory=lambda: ["ETH-USDT", "SOL-USDT", "BTC-USDT"])
    pairs_blacklist: list[str] = field(default_factory=list)
    funding_floor_8h: float = 0.005 / 100      # 0.005% = 5.5% APR
    exit_funding_8h: float = -0.005 / 100      # -5.5% APR
    rotation_threshold_apr: float = 0.005      # 0.5% APR

    # Sizing
    notional_per_leg_usd: float = 1000.0
    max_notional_per_leg_usd: float = 50000.0
    futures_leverage: int = 10
    spot_use_margin: bool = True               # 10x via cross margin
    min_liquidity_usd: float = 50_000_000      # OI floor

    # Risk
    basis_kill_pct: float = 0.02
    max_drawdown_pct: float = 0.05
    max_borrow_apr: float = 0.25
    stale_data_seconds: int = 60

    # Fees (VIP 4 — verify against current OKX statement quarterly)
    spot_maker_bps: float = 3.0
    spot_taker_bps: float = 4.5
    fut_maker_bps: float = 0.8
    fut_taker_bps: float = 2.7
    slippage_bps_per_leg: float = 1.0

    # Execution
    entry_mode: str = "LIMIT"                  # or "MARKET"
    exit_mode: str = "LIMIT"
    limit_offset_bps: float = 1.0
    limit_timeout_sec: int = 30

    # Modes
    paper_trading: bool = True                 # start in paper, flip after Phase 3
    algo_enabled: bool = False                 # operator must enable explicitly
```

---

## 10. Anti-Patterns (do not do these)

1. **Don't build a UI form for credentials that doesn't actually save them.** Either wire it end-to-end or document `.env` only.
2. **Don't store credentials in both `.env` and the database.** Pick one (use `.env`). Anything else is a footgun.
3. **Don't retry rate-limit errors indefinitely.** Bounded exponential backoff, then surface.
4. **Don't trust pos-side from quantity sign.** Read `posSide` field.
5. **Don't assume 1 SWAP contract = 1 base asset.** Always multiply by `ctVal`.
6. **Don't issue closing orders for dust below `minSz`.** They'll fail loudly; pre-flight against `minSz`.
7. **Don't mix demo and live keys in the same `.env`.** OKX issues separate keys; they are not interchangeable.
8. **Don't enable Algo in live before:** (a) backtest passes (b) paper runs 5 days clean (c) single-pair live runs 7 days clean (d) all kill switches manually tested.
9. **Don't add features past the build plan.** Phase 4 is multi-pair. Anything beyond — cross-exchange arb, options overlay, custom hedges — is a separate project.
10. **Don't ignore the AI monitor's `critical` verdicts.** They are alerts not suggestions. Plumb them to a notification you actually see (Telegram with sound on, not email).

---

## 11. First-Session Deliverables

When the operator pastes this prompt and says "begin", produce in order:

1. **`README.md`** with the strategy summary and the phased build plan above, plus a "Status: Phase 0 (planning)" line at the top.
2. **`.env.example`** with all required keys, comments explaining each.
3. **`requirements.txt`** pinning: `python-okx>=2.2`, `aiohttp`, `flask`, `flask-socketio`, `pandas`, `pyarrow`, `python-dotenv`, `anthropic`, `python-telegram-bot`, `apscheduler`.
4. **`models.py`** with `BotConfig` per §9.
5. **`adapters/base.py`** + **`adapters/okx_adapter.py`** — public-data methods only (`get_funding_rate`, `get_funding_history`, `get_instrument_info`, `get_ticker`, `get_orderbook`, `get_server_time`).
6. **`backtest/fetch_history.py`** — runnable script that pulls 90 days for top 20 pairs by volume.

Then **stop** and ask the operator to run the fetch and review one pair's funding history before continuing to backtest replay. The operator wants to see real numbers, not assumed ones, before any more code.

---

## 12. Working Style

- Two-sentence end-of-turn summaries.
- No multi-paragraph code comments. Identifiers self-document; comment only WHY, not WHAT.
- No defensive coding for cases that can't happen (e.g. don't check for `None` on values that are guaranteed populated).
- Don't create planning documents unless asked.
- Commit and push every working step; never leave the working tree dirty across phase boundaries.
- Branch: `claude/funding-arb-MAIN` (rename to your preference).

---

## 13. Reference Materials

The operator has these from the prior project — port verbatim where noted:

- `mt5_test_suite_reference.py` — 40-scenario test runner scaffold. Port to OKX, adapting the broker layer.
- `core/ai_monitor.py` — Claude-powered health monitor. Strategy-agnostic; drop in.
- `core/telegram_bot.py` — Telegram notifier. Drop in, retune messages.
- `adapters/okx_adapter.py` — exists but for spot+perp stat-arb. Use as reference for OKX V5 quirks; rewrite cleanly for funding-arb needs.

---

**End of brief. Acknowledge by summarising your understanding in 5 bullets, then begin with §11 deliverable #1.**
