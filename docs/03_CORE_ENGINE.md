# Core Trading Engine

## Overview

The Trading Engine (`core/trading_engine.py`, ~900 lines) is the central orchestrator that coordinates price feeds, signal generation, and order execution. It manages the main trading loop and all position state.

## File Location
```
core/trading_engine.py
```

## Dependencies
```python
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable, Dict, Any, List
from dataclasses import dataclass

from models import (
    TradingConfig, Trade, MarketTick, Signal, Position,
    OrderResult, CRYPTO_ASSETS, get_symbols_for_asset
)
from core.signals import SignalGenerator
from core.order_executor import OrderExecutor
from core.trade_logger import get_trade_logger
from adapters.base import ExchangeAdapter
from adapters.okx_websocket import OKXWebSocketManager
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       TRADING ENGINE                            │
│                                                                 │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │                    ENGINE STATE                          │  │
│   │  is_running, algo_enabled, paper_trading                 │  │
│   │  current_position, last_tick_time, open_trade            │  │
│   └─────────────────────────────────────────────────────────┘  │
│                              │                                  │
│   ┌──────────────────────────┼──────────────────────────────┐  │
│   │                          ▼                               │  │
│   │  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐    │  │
│   │  │   SIGNAL    │   │    ORDER    │   │  WEBSOCKET  │    │  │
│   │  │  GENERATOR  │   │   EXECUTOR  │   │   MANAGER   │    │  │
│   │  │             │   │             │   │             │    │  │
│   │  │ Z-score     │   │ Spread      │   │ Real-time   │    │  │
│   │  │ Hurst       │   │ Orders      │   │ Ticks       │    │  │
│   │  │ STD Filter  │   │ Leg Risk    │   │ ~100ms      │    │  │
│   │  └─────────────┘   └─────────────┘   └─────────────┘    │  │
│   │                          │                               │  │
│   └──────────────────────────┼──────────────────────────────┘  │
│                              ▼                                  │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │              EXCHANGE ADAPTERS (REST)                    │  │
│   │         spot_adapter        futures_adapter              │  │
│   └─────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## EngineState Dataclass

Tracks the current state of the trading engine.

```python
@dataclass
class EngineState:
    """Current engine state."""
    is_running: bool = False           # Engine loop is active
    algo_enabled: bool = False         # Automated trading enabled
    paper_trading: bool = True         # Simulation mode
    current_position: str = "NONE"     # NONE, LONG, SHORT
    last_tick_time: Optional[datetime] = None
    last_signal: Optional[Signal] = None
    current_trade: Optional[Trade] = None
    error: str = ""                    # Last error message
```

---

## TradingEngine Class

### Constructor

```python
def __init__(self, config: TradingConfig):
    self.config = config
    self.signal_generator = SignalGenerator(config)
    self.state = EngineState(paper_trading=config.paper_trading)

    # Exchange adapters (REST)
    self.spot_adapter: Optional[ExchangeAdapter] = None
    self.futures_adapter: Optional[ExchangeAdapter] = None

    # Order executor for spread trades
    self.order_executor: Optional[OrderExecutor] = None

    # WebSocket manager (optional)
    self.ws_manager: Optional[OKXWebSocketManager] = None
    self._use_websocket: bool = False

    # Current market data
    self.spot_tick: Optional[MarketTick] = None
    self.futures_tick: Optional[MarketTick] = None

    # Current open trade
    self.open_trade: Optional[Trade] = None

    # Callbacks for UI updates
    self.on_tick: Optional[Callable[[MarketTick, MarketTick], None]] = None
    self.on_signal: Optional[Callable[[Signal], None]] = None
    self.on_trade: Optional[Callable[[Trade], None]] = None
    self.on_status: Optional[Callable[[Dict[str, Any]], None]] = None
    self.on_error: Optional[Callable[[str], None]] = None

    # Control flags
    self._running = False
    self._task: Optional[asyncio.Task] = None

    # Cooldowns
    self._stop_loss_cooldown_sec = 60
    self._stop_loss_cooldown_until: Optional[datetime] = None
    self._entry_cooldown_until: Optional[datetime] = None

    # Execution guards
    self._executing_trade = False      # Prevents duplicate order placement
    self._processing_tick = False      # Prevents concurrent tick processing

    # Position verification
    self._last_position_verify: Optional[datetime] = None
    self._position_verify_interval = 60  # seconds
    self._position_mismatch: Optional[Dict[str, Any]] = None

    # Order tracking for pattern detection
    self._spot_order_attempts = 0
    self._spot_order_failures = 0
    self._futures_order_attempts = 0
    self._futures_order_failures = 0

    # Tick interval
    self.tick_interval = 0.5  # 500ms for REST polling
```

---

## Core Methods

### start()

Initializes and starts the trading engine.

```python
async def start(self) -> None:
    """Start the trading engine."""
    if self._running:
        logger.warning("Engine already running")
        return

    self._running = True
    self.state.is_running = True
    self.state.error = ""

    # Log startup configuration
    self._log_startup_summary()

    # 1. Clean up orphan orders from previous sessions
    await self._cleanup_orphan_orders()

    # 2. Apply leverage settings to exchange
    if self._pending_leverage_setup:
        await self._apply_leverage_settings()

    # 3. Start WebSocket if configured
    if self._use_websocket and self.ws_manager:
        success = await self.ws_manager.start(
            self.config.spot_symbol,
            self.config.futures_symbol
        )
        if not success:
            self._use_websocket = False  # Fall back to REST

    # 4. Start REST polling loop if not using WebSocket
    if not self._use_websocket:
        self._task = asyncio.create_task(self._main_loop())
```

### stop()

Gracefully stops the engine.

```python
async def stop(self) -> None:
    """Stop the trading engine."""
    self._running = False
    self.state.is_running = False

    # Stop WebSocket
    if self.ws_manager:
        await self.ws_manager.stop()

    # Cancel polling task
    if self._task:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
```

### set_adapters()

Configures exchange adapters for order execution.

```python
def set_adapters(self, spot: Optional[ExchangeAdapter],
                 futures: Optional[ExchangeAdapter]) -> None:
    """Set exchange adapters (REST mode)."""
    self.spot_adapter = spot
    self.futures_adapter = futures
    self._use_websocket = False

    # Initialize order executor with both adapters
    if spot and futures:
        self.order_executor = OrderExecutor(self.config, spot, futures)
        self._pending_leverage_setup = True
```

### set_websocket_manager()

Enables real-time WebSocket streaming (~100ms latency).

```python
def set_websocket_manager(self, ws_manager: OKXWebSocketManager) -> None:
    """Set WebSocket manager for real-time streaming."""
    self.ws_manager = ws_manager
    self._use_websocket = True

    # Register tick callback
    ws_manager.add_tick_callback(self._on_websocket_tick)
```

---

## Tick Processing

### REST Polling Mode

```python
async def _main_loop(self) -> None:
    """Main trading loop (REST polling)."""
    while self._running:
        try:
            await self._tick()
            await asyncio.sleep(self.tick_interval)  # 500ms
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Error in main loop: %s", e)
            await asyncio.sleep(1)  # Wait before retry

async def _tick(self) -> None:
    """Process one tick (REST polling mode)."""
    # Fetch prices from exchanges
    spot_tick = await self._get_spot_tick()
    futures_tick = await self._get_futures_tick()

    if not spot_tick or not futures_tick:
        return  # Skip if data unavailable

    self.spot_tick = spot_tick
    self.futures_tick = futures_tick

    await self._process_tick_pair()
```

### WebSocket Mode

```python
def _on_websocket_tick(self, symbol: str, tick: MarketTick) -> None:
    """Handle incoming WebSocket tick."""
    # Update appropriate tick
    if symbol == self.config.spot_symbol:
        self.spot_tick = tick
    elif symbol == self.config.futures_symbol:
        self.futures_tick = tick

    # Process if we have both AND not already processing
    # Guard prevents concurrent execution on rapid ticks
    if self.spot_tick and self.futures_tick and not self._processing_tick:
        asyncio.create_task(self._run_tick_guarded())

async def _run_tick_guarded(self) -> None:
    """Process tick with concurrency guard."""
    if self._processing_tick:
        return  # Skip - already processing
    self._processing_tick = True
    try:
        await self._process_tick_pair()
    finally:
        self._processing_tick = False
```

### Common Processing

```python
async def _process_tick_pair(self) -> None:
    """Process a pair of spot/futures ticks."""
    self.state.last_tick_time = datetime.utcnow()

    # Periodic position verification (non-paper mode)
    if not self.state.paper_trading:
        await self._periodic_position_check()

    # Update signal generator position context
    self.signal_generator.set_position(self.state.current_position)

    # Add tick to signal generator
    self.signal_generator.add_tick(self.spot_tick, self.futures_tick)

    # Notify tick callback (for UI)
    if self.on_tick:
        self.on_tick(self.spot_tick, self.futures_tick)

    # Generate trading signal
    signal = self.signal_generator.generate_signal()
    self.state.last_signal = signal

    # Notify signal callback (for UI)
    if self.on_signal:
        self.on_signal(signal)

    # Execute trading logic if enabled
    if self.state.algo_enabled and signal.signal_type != "NONE":
        await self._process_signal(signal)
```

---

## Position Management

### Opening a Position

```python
async def _open_position(self, signal: Signal) -> None:
    """Open a new position."""
    # Guard 1: Already in position
    if self.state.current_position != "NONE":
        logger.warning("Already in position")
        return

    # Guard 2: Trade execution in progress
    if self._executing_trade:
        logger.debug("Trade execution in progress")
        return

    # Guard 3: Stop-loss cooldown
    if self._stop_loss_cooldown_until and datetime.utcnow() < self._stop_loss_cooldown_until:
        return

    # Guard 4: General entry cooldown
    if self._entry_cooldown_until and datetime.utcnow() < self._entry_cooldown_until:
        return

    # Guard 5: Existing position on exchange (live mode)
    if self.config.verify_exchange_position and not self.state.paper_trading:
        existing = await self._check_exchange_position()
        if existing:
            logger.warning("Exchange has existing position: %s", existing)
            return

    # Guard 6: Open orders already pending
    if not self.state.paper_trading:
        order_count = await self._count_open_orders()
        if order_count > 0:
            logger.warning("Exchange has %d open orders", order_count)
            self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=30)
            return

    # Calculate position size
    quantity = self.config.position_size_usd / self.spot_tick.mid

    # Create trade record
    trade = Trade(
        asset=self.config.asset,
        position_type=signal.signal_type,
        entry_time=datetime.utcnow(),
        entry_spot_price=self.spot_tick.mid,
        entry_futures_price=self.futures_tick.mid,
        entry_spread=signal.spread,
        entry_zscore=signal.zscore,
        quantity=quantity,
        notional_usd=self.config.position_size_usd,
        is_open=True,
        is_paper=self.state.paper_trading,
    )

    # Execute orders (live mode)
    if not self.state.paper_trading:
        self._executing_trade = True
        try:
            success = await self._execute_entry_orders(trade, signal)
            if not success:
                # Apply cooldown to prevent rapid retry
                self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=60)
                return
        finally:
            self._executing_trade = False

    # Update state
    self.open_trade = trade
    self.state.current_position = signal.signal_type
    self.signal_generator.set_position(signal.signal_type)

    # Notify callback
    if self.on_trade:
        self.on_trade(trade)
```

### Closing a Position

```python
async def _close_position(self, signal: Signal) -> None:
    """Close current position."""
    if self.state.current_position == "NONE" or not self.open_trade:
        return

    trade = self.open_trade

    # Calculate P&L
    if trade.position_type == "LONG":
        # Long: bought spot, sold futures
        spread_change = signal.spread - trade.entry_spread
    else:
        # Short: sold spot, bought futures
        spread_change = trade.entry_spread - signal.spread

    pnl = spread_change * trade.quantity
    pnl_percent = (pnl / trade.notional_usd) * 100

    # Update trade record
    trade.exit_time = datetime.utcnow()
    trade.exit_spot_price = self.spot_tick.mid
    trade.exit_futures_price = self.futures_tick.mid
    trade.exit_spread = signal.spread
    trade.exit_zscore = signal.zscore
    trade.exit_reason = signal.signal_type
    trade.pnl_usd = pnl
    trade.pnl_percent = pnl_percent
    trade.is_open = False

    # Execute exit orders (live mode)
    if not self.state.paper_trading:
        self._executing_trade = True
        try:
            await self._execute_exit_orders(trade, signal)
        finally:
            self._executing_trade = False

    # Reset state
    self.state.current_position = "NONE"
    self.signal_generator.set_position("NONE")
    self.open_trade = None

    # Apply cooldowns
    if signal.signal_type == "STOP_LOSS":
        self._stop_loss_cooldown_until = datetime.utcnow() + timedelta(seconds=60)

    cooldown_sec = self.config.entry_cooldown_seconds
    if cooldown_sec > 0:
        self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=cooldown_sec)

    # Notify callback
    if self.on_trade:
        self.on_trade(trade)
```

---

## Order Execution

### Entry Orders

```python
async def _execute_entry_orders(self, trade: Trade, signal: Signal) -> bool:
    """Execute entry orders using order executor."""
    if not self.order_executor:
        logger.error("Order executor not configured")
        return False

    # Verify leverage before trading
    await self._verify_leverage_settings()

    # Track attempt
    self._spot_order_attempts += 1
    self._futures_order_attempts += 1

    try:
        spread_order = await self.order_executor.execute_entry(
            position_type=signal.signal_type,
            spot_tick=self.spot_tick,
            futures_tick=self.futures_tick,
            quantity=trade.quantity,
        )

        if spread_order and spread_order.is_complete:
            # Update trade with actual fill prices
            trade.spot_order_id = spread_order.spot_leg.order_id
            trade.futures_order_id = spread_order.futures_leg.order_id
            trade.entry_spot_price = spread_order.spot_leg.filled_price
            trade.entry_futures_price = spread_order.futures_leg.filled_price

            # Log to CSV
            csv_logger = get_trade_logger()
            csv_logger.log_trade(
                event_type="ENTRY",
                position_type=signal.signal_type,
                quantity=trade.quantity,
                spot_price=trade.entry_spot_price,
                futures_price=trade.entry_futures_price,
                spot_order_id=trade.spot_order_id,
                futures_order_id=trade.futures_order_id,
                spot_status="FILLED",
                futures_status="FILLED",
            )
            return True
        else:
            # Track failures for pattern detection
            if spread_order:
                if spread_order.spot_leg.status in (LegStatus.FAILED, LegStatus.CANCELLED):
                    self._spot_order_failures += 1
                if spread_order.futures_leg.status in (LegStatus.FAILED, LegStatus.CANCELLED):
                    self._futures_order_failures += 1

            self._check_spot_failure_pattern()
            return False

    except Exception as e:
        logger.exception("Error executing entry orders: %s", e)
        self._spot_order_failures += 1
        self._futures_order_failures += 1
        return False
```

### Exit Orders

```python
async def _execute_exit_orders(self, trade: Trade, signal: Signal) -> bool:
    """Execute exit orders using order executor."""
    if not self.order_executor:
        return False

    try:
        spread_order = await self.order_executor.execute_exit(
            position_type=trade.position_type,
            spot_tick=self.spot_tick,
            futures_tick=self.futures_tick,
            quantity=trade.quantity,
        )

        if spread_order and spread_order.is_complete:
            trade.exit_spot_price = spread_order.spot_leg.filled_price
            trade.exit_futures_price = spread_order.futures_leg.filled_price
            return True
        else:
            # Leg risk is handled by order executor
            return True

    except Exception as e:
        logger.exception("Error executing exit orders: %s", e)
        return False
```

---

## Safety Mechanisms

### Position Verification

```python
async def verify_position_sync(self) -> Dict[str, Any]:
    """Verify engine state matches exchange positions."""
    result = {
        'checked': True,
        'mismatch': False,
        'engine_position': self.state.current_position,
        'exchange_positions': [],
    }

    try:
        # Get actual positions from exchange
        if self.futures_adapter:
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
            result['mismatch_reason'] = "Engine shows position but exchange has none"
        elif not engine_has and exchange_has:
            result['mismatch'] = True
            result['mismatch_reason'] = "Exchange has position but engine shows FLAT"

        self._position_mismatch = result if result['mismatch'] else None
        return result

    except Exception as e:
        result['error'] = str(e)
        return result

async def _periodic_position_check(self) -> None:
    """Run position verification every 60 seconds."""
    now = datetime.utcnow()
    if self._last_position_verify is None:
        await self.verify_position_sync()
    elif (now - self._last_position_verify).total_seconds() >= 60:
        await self.verify_position_sync()
    self._last_position_verify = now
```

### Orphan Order Cleanup

```python
async def _cleanup_orphan_orders(self) -> None:
    """Cancel pending orders from previous sessions."""
    if self.state.paper_trading:
        return

    try:
        cancelled = 0

        # Clean spot orders
        if self.spot_adapter and hasattr(self.spot_adapter, 'cancel_all_orders'):
            count = await self.spot_adapter.cancel_all_orders(
                symbol=self.config.spot_symbol,
                inst_type="SPOT"
            )
            cancelled += count

        # Clean futures orders
        if self.futures_adapter and hasattr(self.futures_adapter, 'cancel_all_orders'):
            count = await self.futures_adapter.cancel_all_orders(
                symbol=self.config.futures_symbol,
                inst_type="SWAP"
            )
            cancelled += count

        if cancelled > 0:
            logger.info("Cleaned up %d orphan orders", cancelled)

    except Exception as e:
        logger.error("Error cleaning up orphans: %s", e)
```

### Leverage Verification

```python
async def _verify_leverage_settings(self) -> bool:
    """Verify exchange leverage matches configuration."""
    if not self.futures_adapter:
        return True

    try:
        if hasattr(self.futures_adapter, 'get_leverage'):
            current = await self.futures_adapter.get_leverage(self.config.futures_symbol)
            if current != self.config.futures_leverage:
                logger.warning("Leverage mismatch: exchange=%dx, config=%dx",
                             current, self.config.futures_leverage)
                # Attempt correction
                await self.futures_adapter.set_leverage(
                    self.config.futures_symbol,
                    self.config.futures_leverage
                )
        return True
    except Exception as e:
        logger.error("Error verifying leverage: %s", e)
        return True  # Don't block on error
```

### Spot Failure Pattern Detection

```python
def _check_spot_failure_pattern(self) -> None:
    """Detect systematic spot order failures."""
    if self._spot_order_attempts < 3:
        return

    spot_fail_rate = self._spot_order_failures / self._spot_order_attempts
    futures_fail_rate = self._futures_order_failures / self._futures_order_attempts

    # Pattern: Spot failing >50% while futures <20%
    if spot_fail_rate > 0.5 and futures_fail_rate < 0.2:
        logger.critical(
            "SPOT-ONLY FAILURE PATTERN: Spot %.0f%% fail, Futures %.0f%% fail",
            spot_fail_rate * 100, futures_fail_rate * 100
        )
        self.state.error = f"CRITICAL: Spot failing {spot_fail_rate*100:.0f}%"

        # Log to CSV
        csv_logger = get_trade_logger()
        csv_logger.log_spot_failure_pattern(
            self._spot_order_attempts, self._spot_order_failures,
            self._futures_order_attempts, self._futures_order_failures
        )
```

---

## Paper Trading

### Simulated Ticks

```python
def _simulate_tick(self, symbol: str, is_spot: bool) -> MarketTick:
    """Generate simulated market tick for paper trading."""
    import random

    base_prices = {
        'BTC': 65000.0, 'ETH': 3500.0, 'SOL': 150.0,
        'XRP': 0.55, 'DOGE': 0.12, 'AVAX': 35.0, 'LINK': 15.0,
    }

    base = base_prices.get(self.config.asset, 100.0)
    noise = random.gauss(0, base * 0.0001)
    price = base + noise

    # Futures trade at premium/discount
    if not is_spot:
        basis = random.uniform(-0.001, 0.003)  # -0.1% to +0.3%
        price *= (1 + basis)

    # Simulate bid-ask spread
    spread_bps = random.uniform(1, 5)
    half_spread = (spread_bps / 10000) * price / 2

    return MarketTick(
        symbol=symbol,
        bid=price - half_spread,
        ask=price + half_spread,
        last=price,
        volume_24h=random.uniform(1_000_000, 10_000_000),
        timestamp=datetime.utcnow(),
    )
```

---

## Status Reporting

```python
def get_status(self) -> Dict[str, Any]:
    """Get current engine status for API/UI."""
    signal_state = self.signal_generator.get_state()

    # Calculate cooldown remaining
    sl_cooldown = 0
    if self._stop_loss_cooldown_until:
        remaining = (self._stop_loss_cooldown_until - datetime.utcnow()).total_seconds()
        sl_cooldown = max(0, round(remaining))

    return {
        'is_running': self.state.is_running,
        'algo_enabled': self.state.algo_enabled,
        'paper_trading': self.state.paper_trading,
        'asset': self.config.asset,
        'position': self.state.current_position,
        'last_tick_time': self.state.last_tick_time.isoformat() if self.state.last_tick_time else None,
        'error': self.state.error,
        'spot_connected': self.spot_adapter is not None,
        'futures_connected': self.futures_adapter is not None,
        'signal': signal_state,
        'spot_tick': self.spot_tick.to_dict() if self.spot_tick else None,
        'futures_tick': self.futures_tick.to_dict() if self.futures_tick else None,
        'open_trade': self.open_trade.to_dict() if self.open_trade else None,
        'sl_cooldown_remaining': sl_cooldown,
        'sl_cooldown_sec': self._stop_loss_cooldown_sec,
        'executing_trade': self._executing_trade,
        'position_mismatch': self._position_mismatch,
    }
```

---

## Callbacks

The engine exposes callbacks for UI integration:

| Callback | Arguments | Purpose |
|----------|-----------|---------|
| `on_tick` | `(spot_tick, futures_tick)` | Price updates |
| `on_signal` | `(signal)` | Trading signals |
| `on_trade` | `(trade)` | Trade events (entry/exit) |
| `on_status` | `(status_dict)` | Status changes |
| `on_error` | `(error_msg)` | Error notifications |

**Usage in app.py:**
```python
engine.on_tick = on_tick_callback
engine.on_signal = on_signal_callback
engine.on_trade = on_trade_callback
engine.on_error = on_error_callback
```

---

## Configuration Update

```python
def update_config(self, config: TradingConfig) -> None:
    """Update trading configuration dynamically."""
    self.config = config
    self.signal_generator.update_config(config)
    self.state.paper_trading = config.paper_trading
    self.state.algo_enabled = config.algo_enabled

    if self.order_executor:
        self.order_executor.update_config(config)

    # Apply leverage if possible
    if self.futures_adapter and not config.paper_trading:
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(self._apply_leverage_settings())
        except RuntimeError:
            pass  # No event loop - apply later
```

---

## Reset

```python
def reset(self) -> None:
    """Reset engine state (preserves running status)."""
    # Preserve running state
    was_running = self.state.is_running
    algo_was_enabled = self.state.algo_enabled

    # Reset components
    self.signal_generator.reset()
    self.state = EngineState(paper_trading=self.config.paper_trading)

    # Restore running state
    self.state.is_running = was_running
    self.state.algo_enabled = algo_was_enabled

    # Clear trade state
    self.open_trade = None
    self.spot_tick = None
    self.futures_tick = None
    self._stop_loss_cooldown_until = None
    self._executing_trade = False
```

---

## Integration Example

```python
# Initialize
config = TradingConfig()
engine = TradingEngine(config)

# Set up exchange adapters
spot_adapter = OKXAdapter(exchange, is_futures=False)
futures_adapter = OKXAdapter(exchange, is_futures=True)
engine.set_adapters(spot_adapter, futures_adapter)

# Optional: Enable WebSocket
ws_manager = OKXWebSocketManager(exchange)
engine.set_websocket_manager(ws_manager)

# Set up callbacks
def on_trade(trade):
    db.save_trade(trade)
    socketio.emit('trade', trade.to_dict())

engine.on_trade = on_trade

# Start in async context
await engine.start()

# Toggle trading
engine.toggle_algo(True)

# Stop when done
await engine.stop()
```

---

## Concurrency Model

The engine uses asyncio with careful concurrency control:

1. **Single Processing Guard**: `_processing_tick` prevents multiple ticks from being processed simultaneously

2. **Execution Lock**: `_executing_trade` prevents duplicate orders while one is being placed

3. **Cooldowns**: Time-based guards prevent rapid re-entry after trades or failures

4. **Position Verification**: Periodic checks ensure engine state matches exchange

This model ensures thread-safety and prevents race conditions in high-frequency environments.
