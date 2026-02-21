# Order Management

## Overview

The Order Executor (`core/order_executor.py`, ~700 lines) handles the execution of spread trades - coordinating simultaneous orders on spot and futures markets while managing leg risk.

## File Location
```
core/order_executor.py
```

## Dependencies
```python
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass
from enum import Enum

from models import TradingConfig, OrderResult, MarketTick
from adapters.base import ExchangeAdapter
```

---

## Core Concepts

### Spread Trade
A spread trade consists of two simultaneous orders (legs):
- **Spot leg**: Buy or sell on the spot market
- **Futures leg**: Opposite side on the futures market

### Leg Risk
When one leg fills but the other doesn't, you have an unbalanced position. This is called "leg risk" and must be handled immediately.

### Execution Modes
| Mode | Pros | Cons | Use Case |
|------|------|------|----------|
| **MARKET** | Guaranteed fill, fast | Higher fees, slippage | Exits, urgent trades |
| **LIMIT** | Lower fees, no slippage | May not fill | Entries, less urgent |

---

## Data Structures

### ExecutionMode Enum

```python
class ExecutionMode(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
```

### LegStatus Enum

```python
class LegStatus(Enum):
    PENDING = "PENDING"     # Order not yet placed
    OPEN = "OPEN"          # Order placed, awaiting fill
    PARTIAL = "PARTIAL"    # Partially filled
    FILLED = "FILLED"      # Completely filled
    CANCELLED = "CANCELLED" # Cancelled (POST_ONLY rejection or manual)
    FAILED = "FAILED"      # Failed to place
```

### LegOrder Dataclass

```python
@dataclass
class LegOrder:
    """Represents one leg of a spread trade."""
    symbol: str                          # e.g., "BTC-USDT"
    side: str                           # "BUY" or "SELL"
    quantity: float                     # Amount to trade
    target_price: float = 0.0           # For LIMIT orders
    order_id: str = ""                  # Exchange order ID
    status: LegStatus = LegStatus.PENDING
    filled_qty: float = 0.0             # Amount filled
    filled_price: float = 0.0           # Average fill price
    last_update: Optional[datetime] = None
    pos_side: Optional[str] = None      # OKX: "long" or "short"
```

### SpreadOrder Dataclass

```python
@dataclass
class SpreadOrder:
    """Represents a complete spread trade (both legs)."""
    spot_leg: LegOrder
    futures_leg: LegOrder
    created_at: datetime = None
    timeout_at: datetime = None
    is_entry: bool = True              # True for entry, False for exit
    position_type: str = ""            # "LONG" or "SHORT"

    @property
    def is_complete(self) -> bool:
        """Both legs filled."""
        return (self.spot_leg.status == LegStatus.FILLED and
                self.futures_leg.status == LegStatus.FILLED)

    @property
    def is_failed(self) -> bool:
        """Either leg failed or cancelled."""
        failed = (LegStatus.FAILED, LegStatus.CANCELLED)
        return (self.spot_leg.status in failed or
                self.futures_leg.status in failed)

    @property
    def has_partial_fill(self) -> bool:
        """One leg filled, other not (leg risk)."""
        filled = (LegStatus.FILLED, LegStatus.PARTIAL)
        spot_filled = self.spot_leg.status in filled
        futures_filled = self.futures_leg.status in filled
        return spot_filled != futures_filled

    @property
    def has_orphan_risk(self) -> bool:
        """One leg failed while other filled (critical)."""
        failed = (LegStatus.FAILED, LegStatus.CANCELLED)
        filled = (LegStatus.FILLED, LegStatus.PARTIAL)
        return ((self.spot_leg.status in failed and self.futures_leg.status in filled) or
                (self.futures_leg.status in failed and self.spot_leg.status in filled))
```

---

## OrderExecutor Class

### Constructor

```python
class OrderExecutor:
    PRICE_UPDATE_INTERVAL_MS = 1000  # 1 second between price checks

    def __init__(
        self,
        config: TradingConfig,
        spot_adapter: ExchangeAdapter,
        futures_adapter: ExchangeAdapter,
    ):
        self.config = config
        self.spot_adapter = spot_adapter
        self.futures_adapter = futures_adapter

        # Current order being executed
        self.active_order: Optional[SpreadOrder] = None

        # Callbacks
        self.on_fill: Optional[Callable[[SpreadOrder], None]] = None
        self.on_partial_fill: Optional[Callable[[SpreadOrder], None]] = None
        self.on_timeout: Optional[Callable[[SpreadOrder], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # Execution state
        self._executing = False
```

---

## Position Side Logic (OKX)

OKX uses `pos_side` to track position direction in long/short mode:

### LONG Spread Entry
```
Action: Buy spot, Sell futures
Result: Own BTC spot, Short BTC futures
- spot_side = "BUY"
- futures_side = "SELL"
- futures_pos_side = "short"  (opening a short position)
```

### LONG Spread Exit
```
Action: Sell spot, Buy futures
Result: Close spot, Close futures short
- spot_side = "SELL"
- futures_side = "BUY"
- futures_pos_side = "short"  (closing the short position)
```

### SHORT Spread Entry
```
Action: Sell spot, Buy futures
Result: Short BTC spot, Long BTC futures
- spot_side = "SELL"
- futures_side = "BUY"
- futures_pos_side = "long"  (opening a long position)
```

### SHORT Spread Exit
```
Action: Buy spot, Sell futures
Result: Close spot short, Close futures long
- spot_side = "BUY"
- futures_side = "SELL"
- futures_pos_side = "long"  (closing the long position)
```

**Critical**: Exit uses the SAME `pos_side` as entry to close the position.

---

## Entry Execution

```python
async def execute_entry(
    self,
    position_type: str,  # "LONG" or "SHORT"
    spot_tick: MarketTick,
    futures_tick: MarketTick,
    quantity: float,
) -> Optional[SpreadOrder]:
    """
    Execute entry trade for a spread position.

    LONG spread: Buy spot, Sell futures
    SHORT spread: Sell spot, Buy futures
    """
    if self._executing:
        logger.warning("Already executing an order")
        return None

    # Determine leg sides
    if position_type == "LONG":
        spot_side = "BUY"
        futures_side = "SELL"
        futures_pos_side = "short"
    else:
        spot_side = "SELL"
        futures_side = "BUY"
        futures_pos_side = "long"

    # Create spread order
    spread_order = SpreadOrder(
        spot_leg=LegOrder(
            symbol=self.config.spot_symbol,
            side=spot_side,
            quantity=quantity,
        ),
        futures_leg=LegOrder(
            symbol=self.config.futures_symbol,
            side=futures_side,
            quantity=quantity,
            pos_side=futures_pos_side,
        ),
        is_entry=True,
        position_type=position_type,
        timeout_at=datetime.utcnow() + timedelta(
            seconds=self.config.limit_order_timeout_sec
        ),
    )

    return await self._execute_spread(spread_order, spot_tick, futures_tick)
```

---

## Exit Execution

```python
async def execute_exit(
    self,
    position_type: str,  # Current position: "LONG" or "SHORT"
    spot_tick: MarketTick,
    futures_tick: MarketTick,
    quantity: float,
) -> Optional[SpreadOrder]:
    """
    Execute exit trade to close a spread position.

    Close LONG: Sell spot, Buy futures
    Close SHORT: Buy spot, Sell futures
    """
    # Opposite of entry, SAME pos_side (closing same position)
    if position_type == "LONG":
        spot_side = "SELL"
        futures_side = "BUY"
        futures_pos_side = "short"  # Closing the short
    else:
        spot_side = "BUY"
        futures_side = "SELL"
        futures_pos_side = "long"   # Closing the long

    spread_order = SpreadOrder(
        spot_leg=LegOrder(
            symbol=self.config.spot_symbol,
            side=spot_side,
            quantity=quantity,
        ),
        futures_leg=LegOrder(
            symbol=self.config.futures_symbol,
            side=futures_side,
            quantity=quantity,
            pos_side=futures_pos_side,
        ),
        is_entry=False,
        position_type=position_type,
        timeout_at=datetime.utcnow() + timedelta(
            seconds=self.config.limit_order_timeout_sec
        ),
    )

    return await self._execute_spread(spread_order, spot_tick, futures_tick)
```

---

## Market Order Execution

```python
async def _execute_market(self, spread_order: SpreadOrder) -> SpreadOrder:
    """Execute spread using market orders (immediate fill)."""
    logger.info("Executing spread with MARKET orders: %s %s",
                spread_order.position_type,
                "ENTRY" if spread_order.is_entry else "EXIT")

    # Execute BOTH legs simultaneously
    spot_task = self._place_market_order(
        self.spot_adapter,
        spread_order.spot_leg,
    )
    futures_task = self._place_market_order(
        self.futures_adapter,
        spread_order.futures_leg,
    )

    spot_result, futures_result = await asyncio.gather(
        spot_task, futures_task, return_exceptions=True
    )

    # Process results
    if isinstance(spot_result, Exception):
        spread_order.spot_leg.status = LegStatus.FAILED
    else:
        self._update_leg_from_result(spread_order.spot_leg, spot_result)

    if isinstance(futures_result, Exception):
        spread_order.futures_leg.status = LegStatus.FAILED
    else:
        self._update_leg_from_result(spread_order.futures_leg, futures_result)

    # Handle leg risk
    if spread_order.has_partial_fill:
        logger.warning("PARTIAL FILL - Leg risk detected!")
        await self._handle_leg_risk(spread_order)

    return spread_order
```

---

## Limit Order Execution

```python
async def _execute_limit(
    self,
    spread_order: SpreadOrder,
    spot_tick: MarketTick,
    futures_tick: MarketTick,
) -> SpreadOrder:
    """Execute spread using pegged limit orders."""
    timeout_sec = self.config.limit_order_timeout_sec

    # Calculate initial prices (passive, at bid/ask)
    self._update_target_prices(spread_order, spot_tick, futures_tick)

    # Place initial limit orders
    await self._place_limit_orders(spread_order)

    # Monitor and adjust until filled or timeout
    while not spread_order.is_complete and not spread_order.is_failed:
        # Check timeout
        if datetime.utcnow() >= spread_order.timeout_at:
            logger.warning("Limit order timeout")
            await self._handle_timeout(spread_order)
            break

        # Wait before next check
        await asyncio.sleep(self.PRICE_UPDATE_INTERVAL_MS / 1000)

        # Get fresh prices
        new_spot_tick = await self.spot_adapter.get_tick(self.config.spot_symbol)
        new_futures_tick = await self.futures_adapter.get_tick(self.config.futures_symbol)

        if not new_spot_tick or not new_futures_tick:
            continue

        # Check order status
        await self._check_order_status(spread_order)

        # Update prices if not filled
        if not spread_order.is_complete:
            old_spot_price = spread_order.spot_leg.target_price
            old_futures_price = spread_order.futures_leg.target_price

            self._update_target_prices(spread_order, new_spot_tick, new_futures_tick)

            # Amend if price changed > 5 basis points
            spot_change = abs(spread_order.spot_leg.target_price - old_spot_price) / old_spot_price
            futures_change = abs(spread_order.futures_leg.target_price - old_futures_price) / old_futures_price

            if spot_change > 0.0005 or futures_change > 0.0005:
                await self._amend_limit_orders(spread_order)

    # Handle partial fills
    if spread_order.has_partial_fill or spread_order.has_orphan_risk:
        await self._handle_leg_risk(spread_order)

    return spread_order
```

---

## Price Calculation

```python
def _update_target_prices(
    self,
    spread_order: SpreadOrder,
    spot_tick: MarketTick,
    futures_tick: MarketTick,
) -> None:
    """
    Calculate target prices for limit orders.

    For MAKER orders:
    - BUY: place at bid + offset, but NEVER >= ask
    - SELL: place at ask - offset, but NEVER <= bid

    Safety buffer prevents POST_ONLY rejection in tight spreads.
    """
    offset_bps = self.config.limit_order_price_offset_bps / 10000
    SAFETY_BUFFER = 0.5 / 10000  # 0.5 bps from opposite side

    # Spot leg
    if spread_order.spot_leg.side == "BUY":
        target = spot_tick.bid * (1 + offset_bps)
        max_price = spot_tick.ask * (1 - SAFETY_BUFFER)
        spread_order.spot_leg.target_price = round(min(target, max_price), 2)
    else:
        target = spot_tick.ask * (1 - offset_bps)
        min_price = spot_tick.bid * (1 + SAFETY_BUFFER)
        spread_order.spot_leg.target_price = round(max(target, min_price), 2)

    # Futures leg (same logic)
    if spread_order.futures_leg.side == "BUY":
        target = futures_tick.bid * (1 + offset_bps)
        max_price = futures_tick.ask * (1 - SAFETY_BUFFER)
        spread_order.futures_leg.target_price = round(min(target, max_price), 2)
    else:
        target = futures_tick.ask * (1 - offset_bps)
        min_price = futures_tick.bid * (1 + SAFETY_BUFFER)
        spread_order.futures_leg.target_price = round(max(target, min_price), 2)
```

---

## Order Status Checking

```python
async def _check_order_status(self, spread_order: SpreadOrder) -> None:
    """Check fill status of both legs."""
    # Check spot leg
    if spread_order.spot_leg.status == LegStatus.OPEN:
        status = await self.spot_adapter.get_order_status(
            spread_order.spot_leg.symbol,
            spread_order.spot_leg.order_id
        )
        if status:
            if status["state"] == "filled":
                spread_order.spot_leg.status = LegStatus.FILLED
                spread_order.spot_leg.filled_qty = status["filled_qty"]
                spread_order.spot_leg.filled_price = status["filled_price"]
            elif status["state"] == "partially_filled":
                spread_order.spot_leg.status = LegStatus.PARTIAL
                spread_order.spot_leg.filled_qty = status["filled_qty"]
            elif status["state"] == "canceled":
                spread_order.spot_leg.status = LegStatus.CANCELLED

    # Check futures leg (same logic)
    if spread_order.futures_leg.status == LegStatus.OPEN:
        status = await self.futures_adapter.get_order_status(
            spread_order.futures_leg.symbol,
            spread_order.futures_leg.order_id
        )
        # ... same processing

    # Cancel remaining leg if one failed
    await self._cancel_if_one_leg_failed(spread_order)
```

---

## Leg Risk Management

### Detection and Prevention

```python
async def _cancel_if_one_leg_failed(self, spread_order: SpreadOrder) -> None:
    """Cancel remaining leg if one failed to prevent orphans."""
    failed_states = (LegStatus.FAILED, LegStatus.CANCELLED)

    # Spot failed, futures still open → cancel futures
    if (spread_order.spot_leg.status in failed_states and
        spread_order.futures_leg.status == LegStatus.OPEN):
        logger.warning("Spot failed - cancelling futures to prevent orphan")
        await self.futures_adapter.cancel_order(
            spread_order.futures_leg.symbol,
            spread_order.futures_leg.order_id,
        )
        spread_order.futures_leg.status = LegStatus.CANCELLED

    # Futures failed, spot still open → cancel spot
    if (spread_order.futures_leg.status in failed_states and
        spread_order.spot_leg.status == LegStatus.OPEN):
        logger.warning("Futures failed - cancelling spot to prevent orphan")
        await self.spot_adapter.cancel_order(
            spread_order.spot_leg.symbol,
            spread_order.spot_leg.order_id,
        )
        spread_order.spot_leg.status = LegStatus.CANCELLED
```

### Recovery Strategy

```python
async def _handle_leg_risk(self, spread_order: SpreadOrder) -> None:
    """
    Handle leg risk when one leg filled but other didn't.

    Strategy:
    1. Try LIMIT order for unfilled leg (maker fees)
    2. Progressively move price toward market
    3. Market close as last resort (taker fees)
    """
    spot_filled = spread_order.spot_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)
    futures_filled = spread_order.futures_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)

    if spot_filled and not futures_filled:
        # Spot filled, futures didn't
        recovered = await self._attempt_maker_recovery(
            adapter=self.futures_adapter,
            leg=spread_order.futures_leg,
            label="futures",
            recovery_timeout_sec=self.config.orphan_recovery_timeout_sec,
        )
        if not recovered:
            # Market close spot (lose fees but prevent orphan)
            close_side = "SELL" if spread_order.spot_leg.side == "BUY" else "BUY"
            await self.spot_adapter.place_order(
                symbol=spread_order.spot_leg.symbol,
                side=close_side,
                order_type="MARKET",
                quantity=spread_order.spot_leg.filled_qty,
            )

    elif futures_filled and not spot_filled:
        # Futures filled, spot didn't
        recovered = await self._attempt_maker_recovery(
            adapter=self.spot_adapter,
            leg=spread_order.spot_leg,
            label="spot",
            recovery_timeout_sec=self.config.orphan_recovery_timeout_sec,
        )
        if not recovered:
            # Market close futures
            close_side = "SELL" if spread_order.futures_leg.side == "BUY" else "BUY"
            await self.futures_adapter.place_order(
                symbol=spread_order.futures_leg.symbol,
                side=close_side,
                order_type="MARKET",
                quantity=spread_order.futures_leg.filled_qty,
                pos_side=spread_order.futures_leg.pos_side,
                reduce_only=True,
            )
```

### Maker Recovery Attempt

```python
async def _attempt_maker_recovery(
    self,
    adapter: ExchangeAdapter,
    leg: LegOrder,
    label: str,
    recovery_timeout_sec: int = 60,
    price_step_bps: float = 1.0,
    max_price_steps: int = 10,
) -> bool:
    """
    Attempt to fill orphaned leg using LIMIT orders before market.

    Strategy:
    1. Place passive LIMIT at best bid/ask
    2. Check every second for fill
    3. Every (timeout/max_steps) seconds, nudge price 1 bps closer to market
    4. Return True if filled, False if gave up

    This preserves maker fees instead of paying taker.
    """
    step_interval = recovery_timeout_sec / max_price_steps
    price_offset_bps = 0.0
    deadline = datetime.utcnow() + timedelta(seconds=recovery_timeout_sec)
    step_deadline = datetime.utcnow() + timedelta(seconds=step_interval)

    # Get market price
    tick = await adapter.get_tick(leg.symbol)
    if not tick:
        return False

    # Calculate initial recovery price
    recovery_price = self._calc_recovery_price(leg.side, tick, price_offset_bps)

    # Place LIMIT order
    result = await adapter.place_order(
        symbol=leg.symbol,
        side=leg.side,
        order_type="LIMIT",
        quantity=leg.quantity,
        price=recovery_price,
        pos_side=leg.pos_side,
    )

    if not result.success:
        return False

    current_order_id = result.order_id

    # Poll until filled or deadline
    while datetime.utcnow() < deadline:
        await asyncio.sleep(1.0)

        # Check status
        status = await adapter.get_order_status(leg.symbol, current_order_id)
        if status and status["state"] == "filled":
            leg.status = LegStatus.FILLED
            leg.filled_qty = status["filled_qty"]
            leg.filled_price = status["filled_price"]
            return True

        # Time to nudge price?
        if datetime.utcnow() >= step_deadline:
            price_offset_bps += price_step_bps
            step_deadline = datetime.utcnow() + timedelta(seconds=step_interval)

            tick = await adapter.get_tick(leg.symbol)
            new_price = self._calc_recovery_price(leg.side, tick, price_offset_bps)

            # Cancel and replace
            if await adapter.cancel_order(leg.symbol, current_order_id):
                remaining_qty = leg.quantity - leg.filled_qty
                result = await adapter.place_order(
                    symbol=leg.symbol,
                    side=leg.side,
                    order_type="LIMIT",
                    quantity=remaining_qty,
                    price=new_price,
                    pos_side=leg.pos_side,
                )
                if result.success:
                    current_order_id = result.order_id

    # Timeout - cancel and return False
    await adapter.cancel_order(leg.symbol, current_order_id)
    return False

def _calc_recovery_price(self, side: str, tick: MarketTick, offset_bps: float) -> float:
    """Calculate passive price, optionally nudged toward market."""
    offset = offset_bps / 10000
    if side == "BUY":
        return round(tick.bid * (1 + offset), 2)
    else:
        return round(tick.ask * (1 - offset), 2)
```

---

## Timeout Handling

```python
async def _handle_timeout(self, spread_order: SpreadOrder) -> None:
    """Handle timeout - cancel unfilled orders and close partial fills."""
    # Cancel open spot order
    if spread_order.spot_leg.status == LegStatus.OPEN:
        await self.spot_adapter.cancel_order(
            spread_order.spot_leg.symbol,
            spread_order.spot_leg.order_id,
        )
        spread_order.spot_leg.status = LegStatus.CANCELLED

    # Cancel open futures order
    if spread_order.futures_leg.status == LegStatus.OPEN:
        await self.futures_adapter.cancel_order(
            spread_order.futures_leg.symbol,
            spread_order.futures_leg.order_id,
        )
        spread_order.futures_leg.status = LegStatus.CANCELLED

    # Handle any partial fills
    if spread_order.has_partial_fill:
        await self._handle_leg_risk(spread_order)
```

---

## Order Amendment

```python
async def _amend_limit_orders(self, spread_order: SpreadOrder) -> None:
    """
    Update limit order prices.

    IMPORTANT: Only place new order if cancel succeeds to prevent duplicates.
    """
    # Amend spot if still open
    if spread_order.spot_leg.status == LegStatus.OPEN:
        # Check if already filled
        status = await self.spot_adapter.get_order_status(
            spread_order.spot_leg.symbol,
            spread_order.spot_leg.order_id
        )
        if status and status["state"] == "filled":
            spread_order.spot_leg.status = LegStatus.FILLED
            spread_order.spot_leg.filled_qty = status["filled_qty"]
            spread_order.spot_leg.filled_price = status["filled_price"]
        elif status and status["state"] in ("live", "partially_filled"):
            # Cancel and replace
            if await self.spot_adapter.cancel_order(
                spread_order.spot_leg.symbol,
                spread_order.spot_leg.order_id,
            ):
                remaining_qty = spread_order.spot_leg.quantity - spread_order.spot_leg.filled_qty
                if remaining_qty > 0:
                    result = await self.spot_adapter.place_order(
                        symbol=spread_order.spot_leg.symbol,
                        side=spread_order.spot_leg.side,
                        order_type="LIMIT",
                        quantity=remaining_qty,
                        price=spread_order.spot_leg.target_price,
                        pos_side=spread_order.spot_leg.pos_side,
                    )
                    if result.success:
                        spread_order.spot_leg.order_id = result.order_id

    # Amend futures (same logic)
    # ...
```

---

## Execution Flow Diagram

```
execute_entry() / execute_exit()
         │
         ▼
_execute_spread()
         │
         ├─── MARKET mode ───► _execute_market()
         │                          │
         │                     asyncio.gather(spot, futures)
         │                          │
         │                     Check for partial fills
         │                          │
         │                     _handle_leg_risk() if needed
         │
         └─── LIMIT mode ────► _execute_limit()
                                    │
                         _update_target_prices()
                                    │
                         _place_limit_orders()
                                    │
                              ┌─────┴─────┐
                              │  LOOP     │
                              │           │
                              ▼           │
                         asyncio.sleep()  │
                              │           │
                         _check_order_status()
                              │           │
                         Both filled? ────┼── YES ──► Return
                              │           │
                         Timeout? ────────┼── YES ──► _handle_timeout()
                              │           │
                         _amend_limit_orders()
                              │           │
                              └───────────┘
```

---

## Integration Example

```python
# Initialize
config = TradingConfig(
    spot_symbol="BTC-USDT",
    futures_symbol="BTC-USDT-SWAP",
    entry_execution_mode="LIMIT",
    exit_execution_mode="MARKET",
    limit_order_timeout_sec=30,
    orphan_recovery_timeout_sec=60,
)

executor = OrderExecutor(config, spot_adapter, futures_adapter)

# Execute entry
spread_order = await executor.execute_entry(
    position_type="LONG",
    spot_tick=spot_tick,
    futures_tick=futures_tick,
    quantity=0.01,
)

if spread_order.is_complete:
    print(f"Entry complete: spot@{spread_order.spot_leg.filled_price}, "
          f"futures@{spread_order.futures_leg.filled_price}")

# Later, execute exit
spread_order = await executor.execute_exit(
    position_type="LONG",
    spot_tick=spot_tick,
    futures_tick=futures_tick,
    quantity=0.01,
)
```

---

## Configuration Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `entry_execution_mode` | str | "LIMIT" | Entry order type |
| `exit_execution_mode` | str | "MARKET" | Exit order type |
| `limit_order_timeout_sec` | int | 30 | Max wait for LIMIT fill |
| `limit_order_price_offset_bps` | float | 1.0 | Distance from bid/ask |
| `orphan_recovery_timeout_sec` | int | 60 | Time to recover orphan leg |
