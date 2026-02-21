# Risk Management System

## Overview

The risk management system implements multiple layers of protection to prevent excessive losses and ensure safe operation during unattended trading.

## Risk Layers

```
┌─────────────────────────────────────────────────────────────┐
│                    LAYER 1: ENTRY FILTERS                   │
│   Hurst Exponent · STD Filter · Data Ready Check           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    LAYER 2: POSITION GUARDS                 │
│   Cooldowns · Open Order Check · Position Verification     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    LAYER 3: EXECUTION SAFETY                │
│   Duplicate Prevention · Leg Risk Handling · Timeouts      │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    LAYER 4: EXIT PROTECTION                 │
│   Stop-Loss · Position Reconciliation · Orphan Detection   │
└─────────────────────────────────────────────────────────────┘
```

---

## Layer 1: Entry Filters

### Hurst Exponent Filter

**Purpose**: Avoid trading in trending markets where mean reversion is unlikely.

```python
# Signal generation filter
hurst_ok = not self.config.hurst_enabled or self.current_hurst < self.config.hurst_threshold

# If Hurst >= 0.5, market is trending → block entry
if not hurst_ok:
    signal_type = "NONE"  # Block entry
    blocked_reason = f"Hurst filter (H={self.current_hurst:.3f} >= {self.config.hurst_threshold})"
```

**Configuration:**
```python
hurst_enabled: bool = True
hurst_threshold: float = 0.5  # H < 0.5 = mean-reverting
```

### STD Profitability Filter

**Purpose**: Ensure spread volatility is sufficient to cover trading costs.

```python
def _check_std_filter(self) -> Tuple[bool, float]:
    # Calculate round-trip costs
    entry_cost = spot_maker_fee_bps + futures_maker_fee_bps  # e.g., 10 bps
    exit_cost = spot_taker_fee_bps + futures_taker_fee_bps   # e.g., 15 bps
    total_cost_bps = entry_cost + exit_cost  # 25 bps

    costs_price = (total_cost_bps / 10000) * spot_price
    profitability_ratio = current_std / costs_price

    # Must exceed minimum multiple
    passed = profitability_ratio >= self.config.min_std_multiple
    return passed, profitability_ratio
```

**Configuration:**
```python
std_filter_enabled: bool = True
min_std_multiple: float = 1.5  # STD must be 1.5x costs
```

### Data Ready Check

**Purpose**: Require full lookback period before trading.

```python
# In generate_signal()
if len(self.spread_history) < self.lookback:
    return Signal(
        signal_type="NONE",
        regime="COLLECTING",
        hurst_ok=None,      # Unknown
        std_filter_ok=None, # Unknown
    )
```

---

## Layer 2: Position Guards

### Entry Cooldown

**Purpose**: Prevent rapid re-entry after any trade.

```python
# After closing a position
cooldown_sec = self.config.entry_cooldown_seconds  # Default: 60s
self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=cooldown_sec)

# Before opening a position
if self._entry_cooldown_until and datetime.utcnow() < self._entry_cooldown_until:
    remaining = (self._entry_cooldown_until - datetime.utcnow()).total_seconds()
    logger.debug("Entry cooldown active: %.0fs remaining", remaining)
    return  # Block entry
```

### Stop-Loss Cooldown

**Purpose**: Extra protection after emergency exits.

```python
# After stop-loss
if signal.signal_type == "STOP_LOSS":
    self._stop_loss_cooldown_until = datetime.utcnow() + timedelta(seconds=60)

# Before entry
if self._stop_loss_cooldown_until and datetime.utcnow() < self._stop_loss_cooldown_until:
    return  # Block entry
```

### Open Order Check

**Purpose**: Prevent duplicate orders when previous ones are pending.

```python
async def _count_open_orders(self) -> int:
    """Count pending orders on exchange."""
    count = 0

    if self.futures_adapter:
        futures_orders = await self.futures_adapter.get_pending_orders(symbol)
        count += len(futures_orders) if futures_orders else 0

    if self.spot_adapter:
        spot_orders = await self.spot_adapter.get_pending_orders(symbol)
        count += len(spot_orders) if spot_orders else 0

    return count

# Before entry
open_count = await self._count_open_orders()
if open_count > 0:
    logger.warning("Exchange has %d open orders - blocking entry", open_count)
    self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=30)
    return  # Block entry
```

### Position Verification

**Purpose**: Verify no existing exchange position before entry.

```python
async def _check_exchange_position(self) -> Optional[str]:
    """Check for existing positions on exchange."""
    if self.futures_adapter:
        positions = await self.futures_adapter.get_positions(symbol)
        for pos in positions:
            if pos.quantity > 0:
                return f"Futures {pos.side} {pos.quantity:.6f}"
    return None

# Before entry
if self.config.verify_exchange_position and not self.state.paper_trading:
    existing = await self._check_exchange_position()
    if existing:
        logger.warning("Exchange has existing position: %s", existing)
        return  # Block entry
```

---

## Layer 3: Execution Safety

### Duplicate Order Prevention

**Purpose**: Prevent multiple orders from rapid tick processing.

```python
# Tick processing guard
self._processing_tick = False

async def _run_tick_guarded(self):
    if self._processing_tick:
        return  # Skip - already processing
    self._processing_tick = True
    try:
        await self._process_tick_pair()
    finally:
        self._processing_tick = False

# Trade execution guard
self._executing_trade = False

async def _open_position(self, signal):
    if self._executing_trade:
        return  # Skip - trade in progress
    self._executing_trade = True
    try:
        await self._execute_entry_orders(trade, signal)
    finally:
        self._executing_trade = False
```

### Leg Risk Handling

**Purpose**: Recover from partial fills where one leg succeeds but other fails.

```python
async def _handle_leg_risk(self, spread_order):
    """Handle one leg filled, other not."""
    spot_filled = spread_order.spot_leg.status == LegStatus.FILLED
    futures_filled = spread_order.futures_leg.status == LegStatus.FILLED

    if spot_filled and not futures_filled:
        # Try maker recovery for futures
        recovered = await self._attempt_maker_recovery(
            adapter=self.futures_adapter,
            leg=spread_order.futures_leg,
            recovery_timeout_sec=60,
        )
        if not recovered:
            # Market close spot
            await self.spot_adapter.place_order(
                symbol=spread_order.spot_leg.symbol,
                side="SELL" if spread_order.spot_leg.side == "BUY" else "BUY",
                order_type="MARKET",
                quantity=spread_order.spot_leg.filled_qty,
            )

    # Similar for futures filled, spot not...
```

### Order Timeout

**Purpose**: Cancel orders that don't fill within configured time.

```python
# Limit order execution
while not spread_order.is_complete:
    if datetime.utcnow() >= spread_order.timeout_at:
        logger.warning("Limit order timeout")
        await self._handle_timeout(spread_order)
        break
    # ... continue monitoring

async def _handle_timeout(self, spread_order):
    """Cancel unfilled orders on timeout."""
    if spread_order.spot_leg.status == LegStatus.OPEN:
        await self.spot_adapter.cancel_order(
            spread_order.spot_leg.symbol,
            spread_order.spot_leg.order_id,
        )
        spread_order.spot_leg.status = LegStatus.CANCELLED

    # Similar for futures...

    # Handle any partial fills
    if spread_order.has_partial_fill:
        await self._handle_leg_risk(spread_order)
```

---

## Layer 4: Exit Protection

### Stop-Loss

**Purpose**: Emergency exit when spread moves significantly against position.

```python
# In generate_signal() with position
if self.current_position == "LONG":
    if self.current_zscore >= self.config.stop_loss_threshold:  # e.g., >= 4.0
        signal_type = "STOP_LOSS"

if self.current_position == "SHORT":
    if self.current_zscore <= -self.config.stop_loss_threshold:  # e.g., <= -4.0
        signal_type = "STOP_LOSS"
```

**Configuration:**
```python
stop_loss_threshold: float = 4.0  # |Z| >= 4.0 triggers stop-loss
```

### Position Reconciliation

**Purpose**: Detect mismatches between engine state and exchange positions.

```python
async def verify_position_sync(self) -> Dict[str, Any]:
    """Verify engine state matches exchange."""
    result = {
        'mismatch': False,
        'engine_position': self.state.current_position,
        'exchange_positions': [],
    }

    # Get exchange positions
    positions = await self.futures_adapter.get_positions()
    for pos in positions:
        if pos.quantity > 0:
            result['exchange_positions'].append({
                'symbol': pos.symbol,
                'side': pos.side,
                'quantity': pos.quantity,
            })

    engine_has = self.state.current_position != "NONE"
    exchange_has = len(result['exchange_positions']) > 0

    # Detect mismatches
    if engine_has and not exchange_has:
        result['mismatch'] = True
        result['reason'] = "Engine shows position but exchange has none"
    elif not engine_has and exchange_has:
        result['mismatch'] = True
        result['reason'] = "Exchange has position but engine shows FLAT"

    return result

# Called every 60 seconds
async def _periodic_position_check(self):
    now = datetime.utcnow()
    if (now - self._last_position_verify).total_seconds() >= 60:
        await self.verify_position_sync()
        self._last_position_verify = now
```

### Orphan Order Cleanup

**Purpose**: Cancel stale orders from previous sessions on startup.

```python
async def _cleanup_orphan_orders(self):
    """Cancel pending orders from previous sessions."""
    if self.state.paper_trading:
        return

    # Cancel spot orders
    if self.spot_adapter:
        count = await self.spot_adapter.cancel_all_orders(
            symbol=self.config.spot_symbol,
            inst_type="SPOT"
        )

    # Cancel futures orders
    if self.futures_adapter:
        count = await self.futures_adapter.cancel_all_orders(
            symbol=self.config.futures_symbol,
            inst_type="SWAP"
        )

    if count > 0:
        logger.info("Cleaned up %d orphan orders", count)
```

---

## Failure Pattern Detection

### Spot-Only Failure Detection

**Purpose**: Detect systematic issues with spot orders.

```python
def _check_spot_failure_pattern(self):
    """Detect if spot orders are failing repeatedly."""
    if self._spot_order_attempts < 3:
        return  # Need minimum attempts

    spot_fail_rate = self._spot_order_failures / self._spot_order_attempts
    futures_fail_rate = self._futures_order_failures / self._futures_order_attempts

    # Pattern: Spot >50% fail, Futures <20% fail
    if spot_fail_rate > 0.5 and futures_fail_rate < 0.2:
        logger.critical(
            "SPOT-ONLY FAILURE PATTERN: Spot %.0f%%, Futures %.0f%%",
            spot_fail_rate * 100, futures_fail_rate * 100
        )
        self.state.error = f"CRITICAL: Spot failing {spot_fail_rate*100:.0f}%"

        # Log to CSV for analysis
        csv_logger.log_spot_failure_pattern(
            self._spot_order_attempts, self._spot_order_failures,
            self._futures_order_attempts, self._futures_order_failures
        )
```

---

## Leverage Verification

**Purpose**: Ensure exchange leverage matches configuration.

```python
async def _verify_leverage_settings(self) -> bool:
    """Verify leverage on exchange matches config."""
    if not self.futures_adapter:
        return True

    try:
        current = await self.futures_adapter.get_leverage(self.config.futures_symbol)

        if current != self.config.futures_leverage:
            logger.warning(
                "Leverage mismatch: exchange=%dx, config=%dx",
                current, self.config.futures_leverage
            )
            # Attempt correction
            success = await self.futures_adapter.set_leverage(
                self.config.futures_symbol,
                self.config.futures_leverage
            )
            if success:
                logger.info("Leverage corrected to %dx", self.config.futures_leverage)
            else:
                logger.error("Failed to correct leverage")

        return True

    except Exception as e:
        logger.error("Leverage verification error: %s", e)
        return True  # Don't block on error
```

---

## Configuration Summary

| Parameter | Default | Description |
|-----------|---------|-------------|
| `entry_threshold` | 2.0 | Z-score entry level |
| `exit_threshold` | 0.5 | Z-score exit level |
| `stop_loss_threshold` | 4.0 | Emergency exit level |
| `hurst_enabled` | True | Enable regime filter |
| `hurst_threshold` | 0.5 | Max Hurst for entry |
| `std_filter_enabled` | True | Enable volatility filter |
| `min_std_multiple` | 1.5 | STD must be >= costs × 1.5 |
| `entry_cooldown_seconds` | 60 | Min seconds between trades |
| `verify_exchange_position` | True | Check exchange before entry |
| `limit_order_timeout_sec` | 30 | Max wait for LIMIT fill |
| `orphan_recovery_timeout_sec` | 60 | Time to recover orphan leg |

---

## Risk Monitoring Dashboard

The UI displays risk metrics in real-time:

```
┌────────────────────────────────────────┐
│ RISK STATUS                            │
├────────────────────────────────────────┤
│ Regime: MEAN_REVERTING (H=0.42)    ✓  │
│ STD Filter: 1.8x (min: 1.5x)       ✓  │
│ Data Points: 100/100 ready         ✓  │
│ Entry Cooldown: None               ✓  │
│ Position Match: Engine=Exchange    ✓  │
└────────────────────────────────────────┘
```

---

## Emergency Procedures

### Manual Position Close

```python
# API endpoint: POST /api/engine/close-position
async def manual_close_position():
    if engine.open_trade:
        # Create EXIT signal
        signal = Signal(signal_type="EXIT", zscore=engine.signal_generator.current_zscore)
        await engine._close_position(signal)
        return {"success": True, "message": "Position closed"}
    return {"success": False, "error": "No open position"}
```

### Sync Position State

```python
# API endpoint: POST /api/engine/sync-position
async def sync_position():
    # Clear engine state (assumes exchange is source of truth)
    engine.state.current_position = "NONE"
    engine.open_trade = None
    engine.signal_generator.set_position("NONE")
    return {"success": True}
```

### Close Orphaned Exchange Position

```python
# API endpoint: POST /api/close-exchange-position
async def close_exchange_position():
    positions = await engine.futures_adapter.get_positions()
    for pos in positions:
        if pos.quantity > 0:
            await engine.futures_adapter.close_position(pos.symbol)
    return {"success": True}
```
