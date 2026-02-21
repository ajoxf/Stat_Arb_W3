# Order Test Suite — Implementation Skill

A complete reference for building a **live order test suite** against a crypto exchange.
Covers architecture, scenario design, exchange-specific API quirks, backend/frontend
implementation, and everything needed to port the system to a new project or exchange.

---

## Table of Contents

1. [Overview & Purpose](#1-overview--purpose)
2. [Concept: What the Suite Tests](#2-concept-what-the-suite-tests)
3. [Scenario Taxonomy](#3-scenario-taxonomy)
4. [Architecture](#4-architecture)
5. [Backend — Scenario State Machine](#5-backend--scenario-state-machine)
6. [Backend — Async Task Runner](#6-backend--async-task-runner)
7. [Backend — Single-Scenario Runner](#7-backend--single-scenario-runner)
8. [Backend — REST API Endpoints](#8-backend--rest-api-endpoints)
9. [Adapter Layer — OKX-Specific Quirks](#9-adapter-layer--okx-specific-quirks)
10. [Quantity Calculation](#10-quantity-calculation)
11. [Limit-Price Calculation](#11-limit-price-calculation)
12. [Position Lifecycle — Open → Wait → Close](#12-position-lifecycle--open--wait--close)
13. [Partial-Fill Recovery Scenarios](#13-partial-fill-recovery-scenarios)
14. [Frontend — Real-Time UI](#14-frontend--real-time-ui)
15. [Frontend — Per-Row Run Button](#15-frontend--per-row-run-button)
16. [WebSocket Live Updates](#16-websocket-live-updates)
17. [CSV Export](#17-csv-export)
18. [Configuration Parameters](#18-configuration-parameters)
19. [Timing & Rate-Limit Strategy](#19-timing--rate-limit-strategy)
20. [Error Taxonomy & How to Handle Each](#20-error-taxonomy--how-to-handle-each)
21. [Known Bugs Fixed](#21-known-bugs-fixed)
22. [Porting to Another Exchange](#22-porting-to-another-exchange)
23. [Porting to Another Project](#23-porting-to-another-project)
24. [Complete Scenario List Reference](#24-complete-scenario-list-reference)
25. [Checklist: Implementing from Scratch](#25-checklist-implementing-from-scratch)

---

## 1. Overview & Purpose

A live order test suite sends **real orders** to the exchange (demo or live) and verifies
that the full round-trip — place → wait → close — succeeds.  It is not a mock;
it exercises every layer:

- Your adapter's HTTP signing and request construction
- The exchange's parameter validation (side, sz, ccy, tgtCcy, posSide…)
- Position accounting (minSz, ctVal, lot size, precision)
- Order-cancellation paths
- Cross-margin vs cash margin behaviour

**When to run it:**
- After any change to the adapter layer
- When onboarding a new trading pair or instrument type
- After exchange API updates
- Before going live on a new account

**What it does NOT test:**
- Profitability or strategy logic
- Latency under load (orders are spaced out deliberately)
- Slippage (it uses tiny quantities near minimums)

---

## 2. Concept: What the Suite Tests

Each scenario opens one or two legs, waits, then closes them.
Three outcome paths are exercised per order type:

| Variant | What happens | What is validated |
|---------|-------------|-------------------|
| `#1` fill-test | Open → wait → close via closing order | Full fill lifecycle |
| `#2` fill-test | Same as #1 (repetition builds confidence) | Idempotency |
| `#3` cancel / quick-close | Open → 3 s → cancel (LIMIT) or close (MARKET) | Cancellation path |

For LIMIT orders, variant #3 cancels the unfilled pending order.
For MARKET orders, variant #3 closes the already-filled position after 3 seconds
(exercises the fast-exit path — "quick-close").

---

## 3. Scenario Taxonomy

Six order types × two execution modes = 12 scenario families, each with 3 variants:

### Order Types

| ID | Order Type | Legs |
|----|------------|------|
| `BUY_SPOT` | Buy spot asset | 1 leg: SPOT BUY |
| `SELL_SPOT` | Sell (short) spot asset | 1 leg: SPOT SELL |
| `BUY_FUTURES` | Buy (long) perpetual futures | 1 leg: SWAP BUY |
| `SELL_FUTURES` | Sell (short) perpetual futures | 1 leg: SWAP SELL |
| `LONG_SPREAD` | Cash-and-carry: buy spot + sell futures | 2 legs simultaneously |
| `SHORT_SPREAD` | Reverse carry: sell spot + buy futures | 2 legs simultaneously |

### Execution Modes

| Mode | Behaviour | sz interpretation |
|------|-----------|-------------------|
| `LIMIT` | Passive limit order at bid/ask ± offset | Waits up to `limit_timeout` for fill |
| `MARKET` | Immediate market order | Fills instantly; no price param sent |

### Full Scenario List (40 total)

```
LIMIT mode (18):
  1a BUY_SPOT #1          1b BUY_SPOT #2          1c BUY_SPOT #3 (cancel)
  2a SELL_FUTURES #1      2b SELL_FUTURES #2      2c SELL_FUTURES #3 (cancel)
  3a BUY_FUTURES #1       3b BUY_FUTURES #2       3c BUY_FUTURES #3 (cancel)
  4a SELL_SPOT #1         4b SELL_SPOT #2         4c SELL_SPOT #3 (cancel)
  5a LONG_SPREAD #1       5b LONG_SPREAD #2       5c LONG_SPREAD #3 (cancel)
  6a SHORT_SPREAD #1      6b SHORT_SPREAD #2      6c SHORT_SPREAD #3 (cancel)

MARKET mode (18, forced regardless of config):
  m1a MKT BUY_SPOT #1     m1b MKT BUY_SPOT #2     m1c MKT BUY_SPOT #3 (quick-close)
  m2a MKT SELL_FUTURES #1  ... (same pattern for all 6 types)

Partial-fill recovery (4, always MARKET):
  pf-1 LONG_SPREAD:  spot fills, futures fails → market-close spot
  pf-2 LONG_SPREAD:  futures fills, spot fails → market-close futures
  pf-3 SHORT_SPREAD: spot fills, futures fails → market-close spot
  pf-4 SHORT_SPREAD: futures fills, spot fails → market-close futures
```

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (settings page)                                    │
│  ┌──────────────────────┐   WebSocket (socket.io)           │
│  │  Suite table (36 rows│◄──────────────────────────────┐  │
│  │  with Run buttons)   │                               │  │
│  └──────┬───────────────┘                               │  │
│         │ fetch POST                                    │  │
└─────────┼─────────────────────────────────────────────────┘
          │
┌─────────▼─────────────────────────────────────────────────┐
│  Flask + Flask-SocketIO                                    │
│                                                            │
│  POST /api/test-suite/start        → asyncio task          │
│  POST /api/test-suite/run-scenario → asyncio task          │
│  POST /api/test-suite/stop         → sets cancel flag      │
│  GET  /api/test-suite/status       → returns state dict    │
│  GET  /api/test-suite/download-csv → streams CSV           │
│                                                            │
│  run_test_suite()            (coroutine, full 36 scenarios)│
│  run_single_scenario_task()  (coroutine, 1 scenario)       │
│          │                                                 │
│          ▼                                                 │
│  _suite_open_order()   →  adapter.place_order()            │
│  _suite_close_position() → adapter.cancel_order()          │
│                            adapter.place_order() (close)   │
└────────────────────────────────────────────────────────────┘
          │
┌─────────▼──────────────────────────────────────────────────┐
│  Exchange Adapter (OKX)                                    │
│  place_order(symbol, side, order_type, quantity, price,    │
│              pos_side, reduce_only)                        │
│  cancel_order(symbol, order_id)                            │
│  get_order_status(symbol, order_id)                        │
│  get_symbol_info(symbol)                                   │
└────────────────────────────────────────────────────────────┘
```

### Key globals

```python
_test_suite_cancel:  bool        # signals the suite loop to stop after current scenario
_test_suite_running: bool        # True while full suite is running
_single_running:     bool        # True while a single scenario is running
_test_suite_state:   dict        # shared mutable state; emitted over WebSocket on every change
test_positions:      dict        # {pos_id: {...}} — open test positions, keyed by UUID
```

---

## 5. Backend — Scenario State Machine

Each scenario is a dict:

```python
{
    'id':          '1a',           # unique identifier (used by Run button)
    'label':       'BUY_SPOT #1', # human label shown in table
    'order_type':  'BUY_SPOT',    # one of the 6 order types
    'cancel_test': False,          # True → close after 3 s instead of waiting for fill
    'forced_mode': 'MARKET',       # optional; overrides config.entry_execution_mode
    # runtime fields (added by runner):
    'status':      'pending',      # pending | running | pass | fail | cancelled
    'detail':      '',             # error message or fill details
    'mode':        'LIMIT',        # resolved execution mode for this run
}
```

State transitions:

```
pending → running → pass
                  → fail
                  → cancelled   (suite stopped mid-scenario)
```

The full state dict (`_test_suite_state`) is emitted as a WebSocket event after every
transition, so the UI can re-render incrementally.

---

## 6. Backend — Async Task Runner

The full suite runs as a single coroutine (`run_test_suite`) on the Flask app's event loop,
scheduled via `asyncio.run_coroutine_threadsafe(coro, loop)` from the synchronous Flask
route handler.

### Why asyncio (not threading)?

The adapter calls are `async`/`await` — they use `aiohttp` under the hood.
Running them in a background thread would require a nested event loop, which is fragile.
The coroutine approach means all adapter calls share the same event loop safely.

### Core loop skeleton

```python
async def run_test_suite():
    global _test_suite_running, _test_suite_cancel, _test_suite_state

    _test_suite_running = True
    _test_suite_cancel  = False

    order_mode    = config.entry_execution_mode   # global default
    limit_timeout = config.limit_order_timeout_sec

    scenarios = copy.deepcopy(_SUITE_SCENARIOS)
    for s in scenarios:
        s['status'] = 'pending'
        s['detail'] = ''

    _test_suite_state.update({
        'running': True, 'current': 0, 'total': len(scenarios),
        'pass': 0, 'fail': 0, 'scenarios': scenarios,
        'start_time': datetime.now(timezone.utc).isoformat(),
        'order_mode': order_mode,
    })
    socketio.emit('test_suite_update', _test_suite_state)

    # compute quantity once from live spot price
    spot_price   = engine.spot_tick.mid
    symbol_info  = await engine.futures_adapter.get_symbol_info(config.futures_symbol)
    ct_val       = float(symbol_info.get('ct_val', 0.01))
    quantity     = max(100.0 / spot_price, ct_val)   # at least 1 contract worth

    for idx, scenario in enumerate(scenarios):
        if _test_suite_cancel:
            scenario['status'] = 'cancelled'
            scenario['detail'] = 'suite stopped'
            break

        scen_mode   = scenario.get('forced_mode') or order_mode
        inter_pause = 5 if scen_mode == 'MARKET' else 20   # seconds

        scenario['status'] = 'running'
        _test_suite_state['current'] = idx + 1
        socketio.emit('test_suite_update', _test_suite_state)

        # OPEN
        legs, open_err = await _suite_open_order(
            scenario['order_type'], quantity, forced_mode=scen_mode,
        )
        if open_err or not legs:
            scenario['status'] = 'fail'
            scenario['detail'] = f"open failed: {open_err}"
            _test_suite_state['fail'] += 1
            await asyncio.sleep(inter_pause)
            continue

        # register positions
        opened_ids = []
        for (mtype, side, entry_px, result, qty, ps) in legs:
            pos_id = str(uuid.uuid4())[:8]
            test_positions[pos_id] = { ... }
            opened_ids.append(pos_id)

        # WAIT
        if scenario['cancel_test']:
            await asyncio.sleep(3)                          # cancel path
        elif scen_mode == 'LIMIT':
            await asyncio.sleep(limit_timeout)              # wait for fill
        else:
            await asyncio.sleep(4)                          # market confirm

        # CLOSE
        close_ok, close_details = True, []
        for pos_id in opened_ids:
            ok, detail = await _suite_close_position(pos_id)
            close_details.append(detail)
            if not ok: close_ok = False

        scenario['status'] = 'pass' if close_ok else 'fail'
        scenario['detail'] = '  |  '.join(close_details)
        socketio.emit('test_suite_update', _test_suite_state)

        # inter-scenario cooldown (sliced into 1-second chunks for fast cancel response)
        for _ in range(inter_pause):
            if _test_suite_cancel: break
            await asyncio.sleep(1)

    _test_suite_state['running']  = False
    _test_suite_running           = False
    socketio.emit('test_suite_update', _test_suite_state)
```

### Critical: cooldown slicing

```python
# Do NOT use a single await asyncio.sleep(inter_pause)
# It blocks cancel detection for the entire cooldown.
for _ in range(inter_pause):
    if _test_suite_cancel:
        break
    await asyncio.sleep(1)
```

---

## 7. Backend — Single-Scenario Runner

Running one scenario on demand reuses all the same helpers (`_suite_open_order`,
`_suite_close_position`) but skips the outer loop, inter-scenario cooldowns, and
the full-suite counters.

Key differences from the full suite:

| Aspect | Full Suite | Single Run |
|--------|-----------|------------|
| Scenario list | Deep-copies all 36 | Updates one entry in the existing list |
| State reset | Resets all to `pending` | Does NOT reset other rows |
| `_test_suite_state['current']` | Increments per scenario | Not changed |
| `pass` / `fail` counters | Incremented | Not changed |
| Concurrency guard | `_test_suite_running` | `_single_running` |
| Inter-scenario cooldown | Yes | No |

```python
async def run_single_scenario_task(scenario_id: str):
    global _single_running, _test_suite_state, test_positions

    _single_running = True
    _test_suite_state['single_running'] = True

    try:
        scenario_def = next(s for s in _SUITE_SCENARIOS if s['id'] == scenario_id)

        # Ensure scenarios list is populated (first ever run)
        if not _test_suite_state.get('scenarios'):
            _test_suite_state['scenarios'] = [
                {**s, 'status': 'pending', 'detail': '', 'mode': s.get('forced_mode', '')}
                for s in _SUITE_SCENARIOS
            ]

        scen_idx = next(i for i, s in enumerate(_test_suite_state['scenarios'])
                        if s['id'] == scenario_id)
        scenario = _test_suite_state['scenarios'][scen_idx]

        # ... compute quantity, open, wait, close (same as full suite) ...

    finally:
        _single_running = False
        _test_suite_state['single_running'] = False
        socketio.emit('test_suite_update', _test_suite_state)
```

**Concurrency rules:**
- Full suite running → single run is blocked (returns 400)
- Single run active → another single run is blocked (returns 400)
- Full suite checks `_single_running` in reverse (not implemented above but can be added)

---

## 8. Backend — REST API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/test-suite/start` | Launch full 36-scenario suite |
| `POST` | `/api/test-suite/stop` | Set cancel flag (current scenario finishes first) |
| `POST` | `/api/test-suite/run-scenario` | Run one scenario by `scenario_id` |
| `GET`  | `/api/test-suite/status` | Current state dict (pre-populates scenarios if empty) |
| `GET`  | `/api/test-suite/download-csv` | Export last results as CSV |

### `POST /api/test-suite/run-scenario`

Request body:
```json
{ "scenario_id": "m3b" }
```

Response (success):
```json
{ "success": true, "scenario_id": "m3b" }
```

Response (error):
```json
{ "success": false, "error": "Full suite is running" }
```

### `GET /api/test-suite/status`

Returns the full state dict.  If `scenarios` is empty (no suite has run yet),
pre-populates with all 36 scenarios in `pending` state so the UI can render
Run buttons before any test has been triggered:

```python
@app.route('/api/test-suite/status', methods=['GET'])
def get_test_suite_status():
    state = dict(_test_suite_state)
    if not state.get('scenarios'):
        state['scenarios'] = [
            {**s, 'status': 'pending', 'detail': '', 'mode': s.get('forced_mode', '')}
            for s in _SUITE_SCENARIOS
        ]
    return jsonify(state)
```

---

## 9. Adapter Layer — OKX-Specific Quirks

These are hard-won fixes that took multiple test failures to discover.
Document them clearly; similar quirks exist on every exchange.

### 9.1 `tgtCcy` — SPOT Market BUY size denomination

**Problem:** For OKX spot market BUY orders, `sz` is interpreted as **quote currency
(USDT) by default**, not base currency (BTC).  Sending `sz=0.001` means "buy 0.001 USDT
worth of BTC" — a tiny fraction of a cent — not "buy 0.001 BTC".

**Error:** `sCode=51020 — Your order should meet or exceed the minimum order amount`

**Fix:** Add `"tgtCcy": "base_ccy"` to tell OKX that `sz` is denominated in base currency.

```python
# Cash mode SPOT market BUY only.
# Cross-margin (tdMode=cross) rejects tgtCcy with "instrument does not support
# the tgtCcy parameter" — in cross mode sz for market BUY is already base_ccy.
if inst_type == "SPOT" and okx_ord_type == "market" and side.upper() == "BUY" and td_mode == "cash":
    order_data["tgtCcy"] = "base_ccy"
```

**Applies to:** `tdMode=cash` **only**.
**Does NOT apply to:** `tdMode=cross` (cross-margin) — OKX explicitly rejects `tgtCcy`
there and defaults to `base_ccy` anyway.
**Does not apply to:** LIMIT orders (where `sz` is always base_ccy), or SELL.

### 9.2 `ccy` — Cross-Margin SPOT orders

**Problem:** OKX cross-margin SPOT orders require a `ccy` parameter specifying the
margin currency.  Without it, OKX returns `"Parameter ccy can not be empty"`.

**Why only SELL fails first:** SELL uses `sz` in base_ccy (BTC) by default, so the
quantity check passes and OKX reaches `ccy` validation.  BUY without `tgtCcy` fails
earlier at the minimum-amount check.  After fixing `tgtCcy`, BUY would also hit the
`ccy` error.

**Fix:** For any cross-margin SPOT order, add `"ccy"` = quote currency.

```python
if inst_type == "SPOT" and td_mode == "cross":
    symbol_parts = symbol.split("-")       # "BTC-USDT" → ["BTC", "USDT"]
    if len(symbol_parts) >= 2:
        order_data["ccy"] = symbol_parts[1]  # "USDT"
```

**Applies to:** Both BUY and SELL.
**Does not apply to:** `tdMode=cash` (1× spot, no margin).
**Portfolio margin accounts:** May not require `ccy` — test both.

### 9.3 Contract Value (`ctVal`) — Futures Minimum Quantity

**Problem:** Futures quantity is specified in **contracts**, not BTC.
Each contract represents `ctVal` BTC (typically 0.01 BTC for BTC-USDT-SWAP).
Sending `sz=0.001` means 0.001 contracts — less than the 1-contract minimum.

**Error:** `"Quantity too small, need at least 0.01 for 1 contract"` (sCode varies)

**Fix:** Fetch `ctVal` from the exchange's instrument info endpoint and ensure
`quantity ≥ ctVal` (i.e. at least 1 contract):

```python
symbol_info = await engine.futures_adapter.get_symbol_info(config.futures_symbol)
ct_val      = float(symbol_info.get('ct_val', 0.01))   # contracts; default 0.01 BTC
quantity    = max(100.0 / spot_price, ct_val)
# e.g. max(100/98000, 0.01) = max(0.00102, 0.01) = 0.01 BTC = 1 contract
```

**Important:** `get_symbol_info` must be called **once** per run, not per order.
Cache the result.

### 9.4 `posSide` — Long/Short Mode vs Net Mode

**Problem:** OKX supports two position modes:
- **Net mode** (default): one position per instrument; `posSide` must be `"net"` or omitted.
- **Long/short mode**: separate long and short positions; `posSide` must be `"long"` or `"short"`.

Sending the wrong mode causes `"Invalid position side"` errors.

**Fix:** Detect the account's position mode via the account config endpoint
and set `posSide` accordingly.  For entries only (not exits):

```python
if pos_side:                        # explicitly provided (for closing)
    order_data["posSide"] = pos_side
elif not reduce_only:               # opening a new position
    account_config = await self.get_account_config()
    if account_config.get("position_mode") == "long_short_mode":
        order_data["posSide"] = "long" if side == "BUY" else "short"
```

### 9.5 `reduceOnly` — Closing Futures Positions

When closing a futures position, set `"reduceOnly": true` to prevent the order from
opening a new position in the opposite direction if quantity exceeds the current position.
Only applicable to SWAP (perpetual futures), not SPOT.

```python
if reduce_only and inst_type == "SWAP":
    order_data["reduceOnly"] = True
```

### 9.6 Quantity Precision

Each instrument has three precision constraints:
- `minSz` — minimum order size (in base currency for SPOT, contracts for SWAP)
- `lotSz` — order size must be a multiple of this value
- `tickSz` — price must be a multiple of this value

```python
decimals = symbol_info.get("qty_precision", 8)   # derived from lotSz
sz       = round(quantity, decimals)

if sz < min_sz:
    return OrderResult(success=False, error=f"Size {sz} below minimum {min_sz}")

# Avoid scientific notation: format explicitly then strip trailing zeros
sz_str = f"{sz:.{decimals}f}".rstrip("0").rstrip(".")
```

**Critical:** `str(0.00000001)` in Python returns `"1e-08"`, which OKX rejects.
Always use explicit decimal formatting.

---

## 10. Quantity Calculation

The test suite uses a fixed ~$100 notional value, bounded by the minimum 1-contract size:

```python
spot_price = engine.spot_tick.mid          # live mid-price
ct_val     = float(symbol_info['ct_val'])  # e.g. 0.01 BTC per contract

# Ensure at least 1 futures contract worth of BTC
quantity = max(100.0 / spot_price, ct_val)

# Example at BTC = $98,000:
#   100 / 98000 = 0.00102 BTC  (less than 0.01 → below 1 contract)
#   max(0.00102, 0.01) = 0.01 BTC = exactly 1 contract
```

**Why fixed per run (not per scenario):** Fetching `ctVal` from the exchange takes
an HTTP round-trip.  Computing once at the start of the suite is faster and consistent.

**Why ~$100:** Small enough to minimise impact on balances; large enough to clear
the exchange's minimum order value requirements.

---

## 11. Limit-Price Calculation

For LIMIT order scenarios, the price is set passively — deep enough in the book to
avoid accidental fills, but close enough that cancellation works immediately:

```python
def calc_limit_price(side: str, tick) -> float:
    offset_bps = config.limit_order_price_offset_bps / 10000  # e.g. 50 bps = 0.005
    SAFETY     = 0.00005  # 0.5 bps — stops crossing the spread

    if side.upper() == "BUY":
        # Place below current bid (passive; won't fill immediately)
        return round(min(tick.bid * (1 + offset_bps), tick.ask * (1 - SAFETY)), 2)
    else:
        # Place above current ask (passive; won't fill immediately)
        return round(max(tick.ask * (1 - offset_bps), tick.bid * (1 + SAFETY)), 2)
```

A negative `offset_bps` (e.g. −50 bps) puts the BUY **above bid** — closer to the
ask — making fills more likely.  A positive value puts it further from mid.

**Recommended for testing:** Use a modest negative offset (−50 to −100 bps) so limit
orders fill within the timeout window without crossing to the other side.

---

## 12. Position Lifecycle — Open → Wait → Close

### Opening

`_suite_open_order(order_type, quantity, forced_mode)` places the leg(s) for a scenario.
It returns a list of leg tuples: `(market_type, side, entry_px, OrderResult, qty, pos_side)`.

For spread scenarios, it opens both legs and returns partial results even if the second
leg fails — so the caller can clean up the first leg:

```python
elif order_type == "LONG_SPREAD":
    spot_leg, err = await single_leg("SPOT", "BUY")
    if err: return None, f"Spot: {err}"
    legs.append(spot_leg)

    fut_leg, err = await single_leg("FUTURES", "SELL")
    if err: return legs, f"Futures: {err}"   # legs contains spot_leg for cleanup!
    legs.append(fut_leg)
```

The caller must close any successfully opened legs even if a subsequent leg failed.

### Position Registry

Opened legs are stored in `test_positions` — a global dict keyed by 8-char UUID:

```python
test_positions[pos_id] = {
    'id':          pos_id,
    'market_type': 'SPOT',           # or 'FUTURES'
    'side':        'BUY',            # or 'SELL'
    'quantity':    0.01,
    'entry_price': 98000.0,
    'order_id':    'original_oid',   # used to check fill status before closing
    'entry_time':  '2025-...',
    'pos_side':    'long',           # or None for spot
}
```

### Closing

`_suite_close_position(pos_id)` handles three sub-cases:

1. **Order still pending (LIMIT):** Cancel the order.
2. **Order filled:** Place a closing market order (opposite side, same qty).
3. **Order already cancelled:** Nothing to do.

```python
async def _suite_close_position(pos_id: str):
    pos  = test_positions[pos_id]
    oid  = pos.get('order_id')

    if oid:
        status     = await adapter.get_order_status(symbol, oid)
        state      = status.get("state", "")
        filled_qty = status.get("filled_qty", 0)

        if state in ("live", "partially_filled") or filled_qty == 0:
            # Still pending — cancel it
            cancelled = await adapter.cancel_order(symbol, oid)
            del test_positions[pos_id]
            return True, "cancelled (was pending)" if cancelled else "cancel-failed"

        elif state == "filled":
            quantity = filled_qty    # use actual filled qty, not requested

        elif state == "canceled":
            del test_positions[pos_id]
            return True, "already cancelled"

    # Place close order
    close_result = await adapter.place_order(
        symbol=symbol, side=close_side, order_type=exit_mode,
        quantity=quantity, price=close_lp, pos_side=stored_pos_side,
        reduce_only=(market_type == "FUTURES"),
    )
    ...
```

---

## 13. Partial-Fill Recovery Scenarios

Four additional scenarios (ids `pf-1` through `pf-4`) test the **emergency market-close
recovery path** that fires when one spread leg fills and the other fails.  They always
run in MARKET mode regardless of `config.entry_execution_mode`.

### Scenario definition

```python
{'id': 'pf-1',
 'label': 'LONG_SPREAD partial: spot fills, futures fails → market-close spot',
 'order_type': 'LONG_SPREAD', 'cancel_test': False, 'forced_mode': 'MARKET',
 'partial_fail_test': True, 'filled_leg': 'SPOT'},
```

Two extra fields distinguish partial-fail scenarios from normal ones:

| Field | Values | Meaning |
|-------|--------|---------|
| `partial_fail_test` | `True` | Skips the normal open/wait/close path |
| `filled_leg` | `"SPOT"` or `"FUTURES"` | Which leg is placed at MARKET (fills); the other is skipped |

### What each test does (3 steps)

**Step 1 — Open the filled leg at MARKET** (times the call in ms)
- For LONG_SPREAD/`SPOT`: BUY spot at MARKET
- For LONG_SPREAD/`FUTURES`: SELL futures at MARKET
- For SHORT_SPREAD/`SPOT`: SELL spot at MARKET
- For SHORT_SPREAD/`FUTURES`: BUY futures at MARKET

**Step 2 — Wait 1 s, fetch actual filled qty**
- Queries `get_order_status` for the filled leg
- Uses the actual `filled_qty` for the close (handles exchange rounding)

**Step 3 — Market-close the filled leg (recovery)**
- Places the opposite-side MARKET order for the same filled qty
- Times this call in ms
- PASS = close succeeded; FAIL = close failed (orphan risk flagged in detail)

### Detail column format

On PASS:
```
Leg 1 (SPOT BUY) filled 0.010000 BTC @ $68341.00 in 187ms [oid=332828132715…]  |
Leg 2 (FUTURES SELL) SIMULATED FAILURE — not placed  |
Recovery (SPOT SELL MARKET) closed in 203ms [oid=332828156696…]  |  Total: 1412ms
```

On FAIL:
```
Leg 1 (SPOT BUY) filled 0.010000 BTC @ $68341.00 in 187ms [oid=…]  |
Leg 2 (FUTURES SELL) SIMULATED FAILURE — not placed  |
Recovery (SPOT SELL MARKET) FAILED in 95ms: <error> — ORPHAN RISK: filled leg not closed
```

### Helper function: `_suite_partial_fill_test`

```python
async def _suite_partial_fill_test(order_type: str, quantity: float, filled_leg: str) -> tuple[bool, str]:
    """
    Place only the named leg at MARKET (fills), skip the second leg (simulated failure),
    then immediately market-close the filled leg as recovery.

    Returns (success: bool, detail_str: str).
    """
    # Determine sides
    spot_side, futures_side = ("BUY", "SELL") if order_type == "LONG_SPREAD" else ("SELL", "BUY")
    is_spot_filled = (filled_leg == "SPOT")
    open_side      = spot_side if is_spot_filled else futures_side
    pos_side       = None if is_spot_filled else ("long" if futures_side == "BUY" else "short")

    # Step 1: place filled leg at MARKET
    notional = round(quantity * tick.mid, 2) if (is_spot_filled and open_side == "BUY") else None
    open_result = await adapter.place_order(
        symbol=symbol, side=open_side, order_type="MARKET",
        quantity=quantity, pos_side=pos_side, notional_usdt=notional,
    )
    # ... check success, wait 1s, fetch filled_qty ...

    # Step 3: recovery close
    close_result = await adapter.place_order(
        symbol=symbol, side=close_side, order_type="MARKET",
        quantity=filled_qty, pos_side=pos_side,
        reduce_only=(not is_spot_filled),
        notional_usdt=close_notional,  # only for SPOT MARKET BUY close
    )
    return close_result.success, detail
```

### Loop dispatch

The main `run_test_suite` loop checks for `partial_fail_test` **before** the normal
open/wait/close path and dispatches to the helper:

```python
if scenario.get('partial_fail_test'):
    ok, detail = await asyncio.wait_for(
        _suite_partial_fill_test(order_type, quantity, scenario['filled_leg']),
        timeout=60.0,
    )
    scenario['status'] = 'pass' if ok else 'fail'
    scenario['detail'] = detail
    _test_suite_state['pass' if ok else 'fail'] += 1
    socketio.emit('test_suite_update', _test_suite_state)
    # cooldown then continue
    continue
# ... normal open/wait/close path below ...
```

---

## 14. Frontend — Real-Time UI

The test suite page has three zones:

### Zone 1: Controls

```html
<button id="btn-run-suite"     onclick="startTestSuite()">Run Full Test Suite</button>
<button id="btn-stop-suite"    onclick="stopTestSuite()" class="d-none">Stop Suite</button>
<a     id="btn-download-csv"  href="/api/test-suite/download-csv" class="d-none">Download CSV</a>
```

Button visibility is managed by `applySuiteState`:
- `btn-run-suite` hidden while suite is running
- `btn-stop-suite` visible only while suite is running
- `btn-download-csv` visible after completion with results

### Zone 2: Progress Header

```html
<span id="suite-progress-badge">0 / 36</span>
<span id="suite-pass-badge">0 passed</span>
<span id="suite-fail-badge">0 failed</span>
<small id="suite-mode-label"></small>
<div class="progress-bar" id="suite-progress-bar" style="width:0%"></div>
```

### Zone 3: Scenario Table

Rendered dynamically by `renderSuiteTable(state)`.
The table has 7 columns: `#`, `Scenario`, `Mode`, `Type`, `Status`, `Detail`, (Run button).

```javascript
const SUITE_BADGE = {
    pending:   '<span class="badge bg-secondary">pending</span>',
    running:   '<span class="badge bg-warning text-dark"><span class="spinner-border ...">running</span>',
    pass:      '<span class="badge bg-success">pass</span>',
    fail:      '<span class="badge bg-danger">fail</span>',
    cancelled: '<span class="badge bg-secondary">cancelled</span>',
};

const TYPE_BADGE = {
    BUY_SPOT:     '<span class="badge bg-primary">BUY_SPOT</span>',
    SELL_SPOT:    '<span class="badge bg-warning text-dark">SELL_SPOT</span>',
    BUY_FUTURES:  '<span class="badge bg-info text-dark">BUY_FUT</span>',
    SELL_FUTURES: '<span class="badge" style="background:#fd7e14;">SELL_FUT</span>',
    LONG_SPREAD:  '<span class="badge" style="background:#6f42c1;color:#fff;">LONG_SPR</span>',
    SHORT_SPREAD: '<span class="badge" style="background:#d63384;color:#fff;">SHORT_SPR</span>',
};
```

Row highlight:
```javascript
if (s.status === 'running')  tr.classList.add('table-warning');
if (s.status === 'pass')     tr.classList.add('table-success', 'opacity-75');
if (s.status === 'fail')     tr.classList.add('table-danger',  'opacity-75');
```

### Auto-scroll to running row

```javascript
const rows = document.querySelectorAll('#suite-tbody tr');
rows.forEach(r => {
    if (r.classList.contains('table-warning'))
        r.scrollIntoView({ block: 'nearest' });
});
```

---

## 15. Frontend — Per-Row Run Button

Each row has a Run button that triggers a single scenario without running the full suite.

### Button rendering

```javascript
const anyRunning = state.running || state.single_running;
const btnDisabled = anyRunning ? ' disabled' : '';
const btnSpinner  = (s.status === 'running' && state.single_running)
    ? '<span class="spinner-border spinner-border-sm" style="width:.6rem;height:.6rem;"></span>'
    : '';

// In the row template:
`<td>
  <button class="btn btn-sm btn-outline-primary py-0 px-2"
          style="font-size:0.7rem;"
          onclick="runSingleScenario('${s.id}')"${btnDisabled}>
    ${btnSpinner}Run
  </button>
</td>`
```

### JavaScript handler

```javascript
function runSingleScenario(scenarioId) {
    fetch('/api/test-suite/run-scenario', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scenario_id: scenarioId }),
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast(`Running scenario ${scenarioId}…`, 'success');
        } else {
            showToast('Cannot run scenario: ' + data.error, 'danger');
        }
    })
    .catch(e => showToast('Error: ' + e, 'danger'));
}
```

### Auto-populate table on page load

Without this, Run buttons only appear after the first suite run.

```javascript
function fetchSuiteStatus() {
    fetch('/api/test-suite/status')
        .then(r => r.json())
        .then(data => { if (data) applySuiteState(data); })
        .catch(() => {});
}

document.addEventListener('DOMContentLoaded', function() {
    fetchSuiteStatus();   // ← populate table immediately
    if (typeof socket !== 'undefined') {
        socket.on('test_suite_update', data => applySuiteState(data));
    }
});
```

The status endpoint pre-populates pending scenarios so Run buttons are visible
before any test runs:

```python
@app.route('/api/test-suite/status', methods=['GET'])
def get_test_suite_status():
    state = dict(_test_suite_state)
    if not state.get('scenarios'):
        state['scenarios'] = [
            {**s, 'status': 'pending', 'detail': '', 'mode': s.get('forced_mode', '')}
            for s in _SUITE_SCENARIOS
        ]
    return jsonify(state)
```

---

## 16. WebSocket Live Updates

All state changes are pushed to the browser via `socketio.emit('test_suite_update', state)`.
The frontend re-renders the entire table on every event — simple and reliable.

### Event payload (same structure as `_test_suite_state`)

```json
{
  "running": true,
  "single_running": false,
  "current": 5,
  "total": 36,
  "pass": 3,
  "fail": 1,
  "order_mode": "LIMIT",
  "start_time": "2025-01-01T10:00:00Z",
  "scenarios": [
    { "id": "1a", "label": "BUY_SPOT #1", "status": "pass", "detail": "closed pnl=$0.12", ... },
    ...
  ]
}
```

### Backend emit points

Emit after every state change for responsive UI:

1. After marking scenario as `running`
2. After placing order (detail shows order_id)
3. After starting wait phase (detail shows timer)
4. After closing (detail shows close result)
5. After full suite completes

---

## 17. CSV Export

```python
@app.route('/api/test-suite/download-csv', methods=['GET'])
def download_suite_csv():
    import csv, io
    scenarios = _test_suite_state.get('scenarios', [])
    if not scenarios:
        return jsonify({'error': 'No test results available yet'}), 404

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['#', 'Scenario', 'Mode', 'Type', 'Cancel Test', 'Status', 'Detail'])
    for i, s in enumerate(scenarios, 1):
        writer.writerow([
            i,
            s.get('label', ''),
            s.get('mode', ''),
            s.get('order_type', ''),
            s.get('cancel_test', False),
            s.get('status', ''),
            s.get('detail', ''),
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=order_test_results.csv'},
    )
```

The Download CSV button is shown after any run completes:

```javascript
document.getElementById('btn-download-csv').classList.toggle(
    'd-none', anyRunning || !hasResults
);
```

---

## 18. Configuration Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `entry_execution_mode` | `"LIMIT"` or `"MARKET"` | `"LIMIT"` | Default mode for LIMIT scenarios |
| `exit_execution_mode` | `"LIMIT"` or `"MARKET"` | `"MARKET"` | Mode for closing positions |
| `limit_order_timeout_sec` | int | 30 | Seconds to wait for LIMIT fill before closing |
| `limit_order_price_offset_bps` | int | −50 | Offset from mid in basis points (negative = more aggressive) |
| `spot_symbol` | str | `"BTC-USDT"` | Spot instrument |
| `futures_symbol` | str | `"BTC-USDT-SWAP"` | Perpetual futures instrument |

MARKET scenarios always use `forced_mode='MARKET'` regardless of `entry_execution_mode`.

---

## 19. Timing & Rate-Limit Strategy

| Phase | Duration | Notes |
|-------|----------|-------|
| LIMIT open → wait | `limit_timeout` (e.g. 30 s) | Enough time for passive limit to fill |
| MARKET open → confirm | 4 s | Allows exchange to process and respond |
| Cancel/quick-close wait | 3 s | Minimal; just lets the order land |
| LIMIT inter-scenario cooldown | 20 s | Avoids hitting exchange rate limits |
| MARKET inter-scenario cooldown | 5 s | Fills are instant; shorter gap is safe |

**Total time estimate:**

```
18 LIMIT scenarios:    18 × (30 + 4 + 20) = 18 × 54 s  ≈ 16 min
18 MARKET scenarios:   18 × (4 + 4 + 5)   = 18 × 13 s  ≈  4 min
 4 partial-fill tests:  4 × (2 + 5)        =  4 × 7 s   ≈  1 min
Total: ~21 min for full 40-scenario run
```

Adjust `inter_pause` and `limit_timeout` for your exchange's rate limits.

---

## 20. Error Taxonomy & How to Handle Each

| Error | Root Cause | Fix |
|-------|-----------|-----|
| `sCode=51020` — minimum order amount | `sz` interpreted as quote (USDT) for market BUY | Add `tgtCcy=base_ccy` |
| `Parameter ccy can not be empty` | Cross-margin SPOT missing `ccy` | Add `ccy=<quote>` for cross-margin SPOT |
| `Quantity too small, need at least X for 1 contract` | `sz` < `ctVal` (less than 1 contract) | `quantity = max(target/price, ct_val)` |
| `Invalid position side` | Account in long/short mode, missing `posSide` | Detect position mode; set `posSide` |
| `Order does not exist` | Cancelling an already-cancelled order | Check status before cancelling |
| `Insufficient balance` | Account underfunded | Increase demo balance or reduce quantity |
| `Price limit not met` | Limit price too far from market | Tighten `limit_order_price_offset_bps` |

---

## 21. Known Bugs Fixed

These bugs were discovered during live testing of the suite and are documented here
so they are not reintroduced when porting.

### Bug 1 — `NameError: scen_mode referenced before assignment` (silent coroutine crash)

**Symptom:** Suite UI freezes permanently on scenario 1 showing "running".
No errors in the Flask log.  `_test_suite_running` flag stays `True`.

**Root cause:** Inside `run_test_suite`, `logger.info(... scen_mode)` was called
**before** the line that assigns `scen_mode = scenario.get('forced_mode') or order_mode`.
On the very first loop iteration, `scen_mode` is undefined → `NameError` →
uncaught exception kills the coroutine.  Because `socketio.emit` had already run
with status `'running'`, the UI is stuck.

**Fix:** Move all loop-local variable assignments (`order_type`, `cancel_test`,
`scen_mode`, `scenario['mode']`, `inter_pause`) **above** the first
`socketio.emit` and `logger.info` calls in the loop body.

```python
# CORRECT order:
for idx, scenario in enumerate(scenarios):
    order_type  = scenario['order_type']
    cancel_test = scenario['cancel_test']
    scen_mode   = scenario.get('forced_mode') or order_mode  # ← defined first
    scenario['mode'] = scen_mode
    inter_pause = 5 if scen_mode == 'MARKET' else 20

    scenario['status'] = 'running'
    _test_suite_state['current'] = idx + 1
    socketio.emit('test_suite_update', _test_suite_state)
    logger.info("... [%s]", scen_mode)                       # ← safe to log now
```

**Force-reset recovery:** If the suite is stuck, the "Force Reset" button calls
`POST /api/test-suite/reset` which clears the flags without a server restart.

---

### Bug 2 — Partial fill orphan in `_suite_close_position`

**Symptom:** After a cancel-test scenario, a small open position remains on the exchange
(e.g. −0.0001 BTC spot).  The scenario reports PASS ("cancelled (was pending)").

**Root cause:** When `get_order_status` returns `state="partially_filled"` with
`filled_qty > 0`, the code cancelled the remaining open portion but **did not close
the already-filled quantity**.  The filled BTC stayed on the exchange as an orphan.

**Why it happens:** A limit order placed 3 seconds before the cancel window can
partially fill if the market briefly crosses the limit price.  The cancel removes the
open remainder, but the filled portion is gone from the order book.

**Fix:** After cancelling, detect `state == "partially_filled"` with `filled_qty > 0`
and immediately market-close the filled portion:

```python
if state in ("live", "partially_filled") or filled_qty == 0:
    cancelled = await adapter.cancel_order(symbol, original_oid)
    # Close the already-filled qty if a partial fill occurred
    if filled_qty and filled_qty > 0 and state == "partially_filled":
        close_notional = round(filled_qty * tick.mid, 2) if (
            market_type == "SPOT" and close_side == "BUY"
        ) else None
        await adapter.place_order(
            symbol=symbol, side=close_side, order_type="MARKET",
            quantity=filled_qty, pos_side=stored_pos_side,
            reduce_only=(market_type == "FUTURES"),
            notional_usdt=close_notional,
        )
        del test_positions[pos_id]
        return True, f"partial fill: cancelled remaining, market-closed {filled_qty:.6f} BTC"
    del test_positions[pos_id]
    return True, "cancelled (was pending)" if cancelled else "cancel-failed"
```

**Cleanup of pre-existing orphans:** The startup orphan cleanup (`cleanup_orphan_orders`)
only cancels **pending orders**, not open positions.  Tiny orphan spot positions
from previous runs must be closed manually (e.g. via the exchange UI or a direct
market order).

---

### Bug 3 — `fillSz` vs `accFillSz` in `get_order_status` (pf-1/pf-3 recovery under-sells)

**Symptom:** After a `pf-1` or `pf-3` partial-fill test (SPOT BUY recovery), the
exchange shows a residual LONG spot position (e.g. 0.00941 BTC) even though the
scenario reported PASS.  The recovery sold only a fraction of what was bought (e.g.
0.003666 BTC instead of 0.00941 BTC).

**Root cause:** OKX's `GET /api/v5/trade/order` response has two distinct fill-size
fields:

| Field | Meaning |
|-------|---------|
| `fillSz` | **Last** fill tranche size only |
| `accFillSz` | **Accumulated** fill size (total across all tranches) |

The original code:
```python
fill_sz = float(o.get("fillSz", 0) or o.get("accFillSz", 0) or 0)
```
tried `fillSz` first.  For a cross-margin SPOT MARKET BUY that was matched in
multiple tranches (e.g. 0.00574865 BTC + 0.00366627 BTC = 0.00941492 BTC total),
`fillSz` = **0.00366627** (last tranche only) — non-zero, so the fallback to
`accFillSz` never ran.  The recovery SELL was sized at 0.00366627 BTC, leaving the
remaining 0.00574865 BTC as an orphan.

**Fix:** Swap the priority — use `accFillSz` (total) first, fall back to `fillSz`
(single-tranche orders where `accFillSz` may be absent/zero):

```python
# WRONG (fillSz = last tranche only; accFillSz = total):
fill_sz = float(o.get("fillSz", 0) or o.get("accFillSz", 0) or 0)

# CORRECT:
fill_sz = float(o.get("accFillSz", 0) or o.get("fillSz", 0) or 0)
```

**When this matters:** Any order that is matched across multiple liquidity tranches.
MARKET orders on high-liquidity pairs usually fill in one tranche (so both fields
are equal), but cross-margin SPOT MARKET BUY orders with `sz` in USDT can span
multiple tranches on OKX.

**Does not affect:** LIMIT orders (where `fillSz` correctly captures the last
partial fill for status-checking purposes, and the suite only uses `accFillSz` for
final close sizing).  However, swapping the priority is safe for all order types.

---

## 22. Porting to Another Exchange

### Step 1: Implement the adapter interface

Your adapter must implement:

```python
class ExchangeAdapter:
    async def place_order(
        self,
        symbol: str,
        side: str,                 # "BUY" or "SELL"
        order_type: str,           # "MARKET", "LIMIT", "POST_ONLY"
        quantity: float,           # in base currency (BTC)
        price: Optional[float],    # required for LIMIT/POST_ONLY
        pos_side: Optional[str],   # "long", "short", or None
        reduce_only: bool,         # True when closing a futures position
    ) -> OrderResult: ...

    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...

    async def get_order_status(self, symbol: str, order_id: str) -> dict:
        # Returns: {"state": "filled"|"live"|"canceled"|..., "filled_qty": float}
        ...

    async def get_symbol_info(self, symbol: str) -> dict:
        # Returns: {"ct_val": float, "min_sz": float, "lot_sz": float, ...}
        ...
```

### Step 2: Identify your exchange's quirks

Research and test:

1. **For spot market BUY:** Does `sz` default to base or quote currency? If quote, what parameter changes the denomination?
2. **For cross-margin:** Is a margin currency (`ccy`) required?
3. **For futures:** What is the contract multiplier (`ctVal`)? Is `sz` in contracts or BTC?
4. **Position mode:** Net vs long/short? Does `posSide` affect both entries and exits?
5. **Order precision:** What are `minSz`, `lotSz`, `tickSz` for each symbol?
6. **Rate limits:** How many orders/second? How many cancels/second?

### Step 3: Adapt `_suite_open_order`

Replace `config.spot_symbol` and `config.futures_symbol` with your symbols.
Replace `engine.spot_adapter` and `engine.futures_adapter` with your adapter instances.

### Step 4: Adapt quantity calculation

```python
# Fetch your exchange's contract size
info   = await your_adapter.get_symbol_info(futures_symbol)
ct_val = float(info.get('contract_size', info.get('ct_val', 1.0)))
quantity = max(target_usd / spot_price, ct_val)
```

### Step 5: Test LIMIT scenarios first

LIMIT orders don't fill (if offset is large enough), making them safe to run on a live account.
They validate the API call structure without exchanging real value.

### Step 6: Test MARKET scenarios on demo only

MARKET orders fill immediately.  Always use a demo/sandbox account first.

---

## 23. Porting to Another Project

### Minimum files needed

```
adapters/
  your_exchange_adapter.py   ← ExchangeAdapter interface
app.py                       ← _SUITE_SCENARIOS, run_test_suite, _suite_open_order,
                                _suite_close_position, run_single_scenario_task,
                                all API endpoints
templates/
  settings.html              ← HTML table, JS: renderSuiteTable, applySuiteState,
                                startTestSuite, stopTestSuite, runSingleScenario,
                                fetchSuiteStatus, WebSocket listener
```

### Dependencies

```
flask
flask-socketio          ← for WebSocket live updates
asyncio                 ← standard library
uuid                    ← standard library
copy                    ← standard library
```

### Required shared state

```python
engine.spot_adapter     # ExchangeAdapter for spot
engine.futures_adapter  # ExchangeAdapter for futures
engine.spot_tick        # Tick(mid, bid, ask) for spot
engine.futures_tick     # Tick(mid, bid, ask) for futures
config.entry_execution_mode
config.exit_execution_mode
config.limit_order_timeout_sec
config.limit_order_price_offset_bps
config.spot_symbol
config.futures_symbol
loop                    # asyncio event loop reference
socketio                # Flask-SocketIO instance
```

### Minimal config class

```python
@dataclass
class Config:
    entry_execution_mode:        str   = "LIMIT"
    exit_execution_mode:         str   = "MARKET"
    limit_order_timeout_sec:     int   = 30
    limit_order_price_offset_bps: int  = -50
    spot_symbol:                 str   = "BTC-USDT"
    futures_symbol:              str   = "BTC-USDT-SWAP"
```

### Starting the asyncio loop alongside Flask

```python
import asyncio, threading

loop = None

def start_loop(lp):
    asyncio.set_event_loop(lp)
    lp.run_forever()

lp   = asyncio.new_event_loop()
loop = lp
t    = threading.Thread(target=start_loop, args=(lp,), daemon=True)
t.start()

# In a Flask route:
asyncio.run_coroutine_threadsafe(run_test_suite(), loop)
```

---

## 24. Complete Scenario List Reference

```python
_SUITE_SCENARIOS = [
    # ── LIMIT scenarios (id: '1a'–'6c') ──────────────────────────────────
    {'id': '1a', 'label': 'BUY_SPOT #1',              'order_type': 'BUY_SPOT',      'cancel_test': False},
    {'id': '1b', 'label': 'BUY_SPOT #2',              'order_type': 'BUY_SPOT',      'cancel_test': False},
    {'id': '1c', 'label': 'BUY_SPOT #3 (cancel)',     'order_type': 'BUY_SPOT',      'cancel_test': True},
    {'id': '2a', 'label': 'SELL_FUTURES #1',           'order_type': 'SELL_FUTURES',  'cancel_test': False},
    {'id': '2b', 'label': 'SELL_FUTURES #2',           'order_type': 'SELL_FUTURES',  'cancel_test': False},
    {'id': '2c', 'label': 'SELL_FUTURES #3 (cancel)', 'order_type': 'SELL_FUTURES',  'cancel_test': True},
    {'id': '3a', 'label': 'BUY_FUTURES #1',            'order_type': 'BUY_FUTURES',   'cancel_test': False},
    {'id': '3b', 'label': 'BUY_FUTURES #2',            'order_type': 'BUY_FUTURES',   'cancel_test': False},
    {'id': '3c', 'label': 'BUY_FUTURES #3 (cancel)',  'order_type': 'BUY_FUTURES',   'cancel_test': True},
    {'id': '4a', 'label': 'SELL_SPOT #1',              'order_type': 'SELL_SPOT',     'cancel_test': False},
    {'id': '4b', 'label': 'SELL_SPOT #2',              'order_type': 'SELL_SPOT',     'cancel_test': False},
    {'id': '4c', 'label': 'SELL_SPOT #3 (cancel)',    'order_type': 'SELL_SPOT',     'cancel_test': True},
    {'id': '5a', 'label': 'LONG_SPREAD #1',            'order_type': 'LONG_SPREAD',   'cancel_test': False},
    {'id': '5b', 'label': 'LONG_SPREAD #2',            'order_type': 'LONG_SPREAD',   'cancel_test': False},
    {'id': '5c', 'label': 'LONG_SPREAD #3 (cancel)',  'order_type': 'LONG_SPREAD',   'cancel_test': True},
    {'id': '6a', 'label': 'SHORT_SPREAD #1',           'order_type': 'SHORT_SPREAD',  'cancel_test': False},
    {'id': '6b', 'label': 'SHORT_SPREAD #2',           'order_type': 'SHORT_SPREAD',  'cancel_test': False},
    {'id': '6c', 'label': 'SHORT_SPREAD #3 (cancel)', 'order_type': 'SHORT_SPREAD',  'cancel_test': True},
    # ── MARKET scenarios (id: 'm1a'–'m6c', forced_mode overrides config) ─
    {'id': 'm1a', 'label': 'MKT BUY_SPOT #1',               'order_type': 'BUY_SPOT',      'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm1b', 'label': 'MKT BUY_SPOT #2',               'order_type': 'BUY_SPOT',      'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm1c', 'label': 'MKT BUY_SPOT #3 (quick-close)', 'order_type': 'BUY_SPOT',      'cancel_test': True,  'forced_mode': 'MARKET'},
    {'id': 'm2a', 'label': 'MKT SELL_FUTURES #1',            'order_type': 'SELL_FUTURES',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm2b', 'label': 'MKT SELL_FUTURES #2',            'order_type': 'SELL_FUTURES',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm2c', 'label': 'MKT SELL_FUTURES #3 (quick-close)', 'order_type': 'SELL_FUTURES', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm3a', 'label': 'MKT BUY_FUTURES #1',             'order_type': 'BUY_FUTURES',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm3b', 'label': 'MKT BUY_FUTURES #2',             'order_type': 'BUY_FUTURES',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm3c', 'label': 'MKT BUY_FUTURES #3 (quick-close)', 'order_type': 'BUY_FUTURES', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm4a', 'label': 'MKT SELL_SPOT #1',               'order_type': 'SELL_SPOT',     'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm4b', 'label': 'MKT SELL_SPOT #2',               'order_type': 'SELL_SPOT',     'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm4c', 'label': 'MKT SELL_SPOT #3 (quick-close)', 'order_type': 'SELL_SPOT',     'cancel_test': True,  'forced_mode': 'MARKET'},
    {'id': 'm5a', 'label': 'MKT LONG_SPREAD #1',             'order_type': 'LONG_SPREAD',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm5b', 'label': 'MKT LONG_SPREAD #2',             'order_type': 'LONG_SPREAD',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm5c', 'label': 'MKT LONG_SPREAD #3 (quick-close)', 'order_type': 'LONG_SPREAD', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm6a', 'label': 'MKT SHORT_SPREAD #1',            'order_type': 'SHORT_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm6b', 'label': 'MKT SHORT_SPREAD #2',            'order_type': 'SHORT_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm6c', 'label': 'MKT SHORT_SPREAD #3 (quick-close)', 'order_type': 'SHORT_SPREAD', 'cancel_test': True, 'forced_mode': 'MARKET'},
    # ── Partial-fill / leg-failure recovery (4 scenarios, always MARKET) ──
    {'id': 'pf-1', 'label': 'LONG_SPREAD partial: spot fills, futures fails → market-close spot',
     'order_type': 'LONG_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'SPOT'},
    {'id': 'pf-2', 'label': 'LONG_SPREAD partial: futures fills, spot fails → market-close futures',
     'order_type': 'LONG_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'FUTURES'},
    {'id': 'pf-3', 'label': 'SHORT_SPREAD partial: spot fills, futures fails → market-close spot',
     'order_type': 'SHORT_SPREAD', 'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'SPOT'},
    {'id': 'pf-4', 'label': 'SHORT_SPREAD partial: futures fills, spot fails → market-close futures',
     'order_type': 'SHORT_SPREAD', 'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'FUTURES'},
]
```

---

## 25. Checklist: Implementing from Scratch

### Exchange Research
- [ ] What is the minimum order size (SPOT and FUTURES)?
- [ ] Is SPOT market BUY `sz` in base or quote currency? What parameter fixes it?
- [ ] Does cross-margin SPOT require a margin currency (`ccy`) parameter?
- [ ] What is the contract multiplier for FUTURES (`ctVal`)?
- [ ] Does the account use net or long/short position mode?
- [ ] What are the rate limits (orders per second)?

### Adapter
- [ ] Implement `place_order` with all required parameters
- [ ] Implement `cancel_order`
- [ ] Implement `get_order_status` (returns state and filled_qty)
- [ ] Implement `get_symbol_info` (returns ct_val, min_sz, lot_sz)
- [ ] Handle scientific notation in `sz` strings (format explicitly)
- [ ] Handle `reduceOnly` for futures close orders

### Backend
- [ ] Define `_SUITE_SCENARIOS` list (18 LIMIT + 18 MARKET + 4 partial-fail recommended)
- [ ] Implement `_suite_open_order(order_type, quantity, forced_mode)`
- [ ] Implement `_suite_close_position(pos_id)` — cancel or close
  - [ ] **Critical:** when `state="partially_filled"` and `filled_qty > 0`, market-close the filled portion after cancelling the remainder (see Bug 2 in §21)
- [ ] Implement `_suite_partial_fill_test(order_type, quantity, filled_leg)` for recovery path scenarios
- [ ] Implement `run_test_suite()` coroutine with sliced cooldowns
  - [ ] **Critical:** assign `scen_mode` **before** any `socketio.emit` or `logger.info` that references it (see Bug 1 in §21)
  - [ ] Dispatch `partial_fail_test` scenarios to `_suite_partial_fill_test` before the normal open/wait/close path
- [ ] Implement `run_single_scenario_task(scenario_id)` coroutine
- [ ] Add API endpoints: start, stop, run-scenario, status, download-csv
- [ ] Pre-populate `scenarios` in status endpoint if empty
- [ ] Include `single_running` in state dict and emit events

### Frontend
- [ ] 7-column table: `#`, Scenario, Mode, Type, Status, Detail, Run
- [ ] `renderSuiteTable(state)` — rebuild table on every WebSocket event
- [ ] `applySuiteState(state)` — update counters, progress bar, buttons
- [ ] `runSingleScenario(id)` — POST to run-scenario endpoint
- [ ] `fetchSuiteStatus()` — always call `applySuiteState`, not conditionally
- [ ] `DOMContentLoaded` — call `fetchSuiteStatus()` so table loads on page open
- [ ] Run buttons disabled when `state.running || state.single_running`
- [ ] Spinner on the specific running row during single-scenario runs
- [ ] Auto-scroll running row into view

### Testing Sequence
1. Run MARKET single scenario for each order type individually
2. Run LIMIT single scenario for each order type individually
3. Run each partial-fill recovery scenario (pf-1 through pf-4) individually
4. Run full MARKET 18-scenario sweep
5. Run full LIMIT 18-scenario sweep
6. Run complete 40-scenario suite end-to-end

### Post-Run Checklist
- [ ] Verify no orphan positions remain on the exchange after each run
- [ ] If any cancel-test scenario reports "cancelled (was pending)", check that no tiny spot/futures position was left open (partial-fill orphan)
- [ ] If suite freezes on scenario 1 and logs show no error, check for `scen_mode` usage before assignment
