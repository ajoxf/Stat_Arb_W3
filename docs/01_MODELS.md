# Models & Data Structures

## Overview

The `models.py` file (~544 lines) defines all data structures used throughout the system. These include enumerations for type-safety, dataclasses for data transfer, and configuration objects.

## File Location
```
models.py
```

## Dependencies
```python
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime
```

---

## Enumerations

### SignalType
Defines the types of trading signals the system can generate.

```python
class SignalType(Enum):
    """Trading signal types."""
    NONE = "NONE"           # No action required
    LONG = "LONG"           # Long spread (buy spot, sell futures)
    SHORT = "SHORT"         # Short spread (sell spot, buy futures)
    EXIT = "EXIT"           # Exit current position normally
    STOP_LOSS = "STOP_LOSS" # Emergency exit (stop loss triggered)
```

**Usage:**
- `NONE`: No trade conditions met, or filters blocking entry
- `LONG`: Z-score >= entry_threshold, expect spread to decrease
- `SHORT`: Z-score <= -entry_threshold, expect spread to increase
- `EXIT`: Z-score crossed exit threshold in profitable direction
- `STOP_LOSS`: Z-score exceeded stop-loss threshold

### PositionType
Tracks the current position state.

```python
class PositionType(Enum):
    """Position types."""
    NONE = "NONE"   # No open position
    LONG = "LONG"   # Long spread position
    SHORT = "SHORT" # Short spread position
```

### OrderSide
Direction of an order.

```python
class OrderSide(Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"
```

### OrderType
Execution style of an order.

```python
class OrderType(Enum):
    """Order type."""
    MARKET = "MARKET"  # Immediate fill at best available price
    LIMIT = "LIMIT"    # Fill at specified price or better
```

### ExchangeStatus
Connection state of an exchange.

```python
class ExchangeStatus(Enum):
    """Exchange connection status."""
    DISCONNECTED = "DISCONNECTED"  # Not connected
    CONNECTING = "CONNECTING"      # Connection in progress
    CONNECTED = "CONNECTED"        # Successfully connected
    ERROR = "ERROR"                # Connection failed
```

### ExchangeRole
Role of an exchange in trading.

```python
class ExchangeRole(Enum):
    """Exchange role in trading."""
    SPOT = "SPOT"         # Only for spot trading
    FUTURES = "FUTURES"   # Only for futures trading
    BOTH = "BOTH"         # For both spot and futures
```

---

## Core Data Classes

### TradingConfig
**Singleton configuration for the entire trading system.**

```python
@dataclass
class TradingConfig:
    """Trading configuration settings."""
    id: int = 1  # Singleton - always id=1 in database

    # Asset Configuration
    asset: str = "BTC"
    spot_symbol: str = "BTC-USDT"
    futures_symbol: str = "BTC-USDT-SWAP"

    # Z-Score Thresholds
    entry_threshold: float = 2.0      # Enter when |Z| >= 2.0
    exit_threshold: float = 0.5       # Exit when |Z| <= 0.5
    stop_loss_threshold: float = 4.0  # Emergency exit when |Z| >= 4.0

    # Rolling Window Settings
    lookback_period: int = 100        # Number of ticks for rolling stats
    stats_update_interval: int = 300  # Seconds between mean/std recalculation

    # Filters
    hurst_enabled: bool = True        # Enable Hurst exponent filter
    hurst_threshold: float = 0.5      # H < 0.5 = mean reverting (tradeable)
    std_filter_enabled: bool = True   # Enable volatility filter
    min_std_multiple: float = 1.5     # STD must be > costs * 1.5

    # Position Sizing
    position_size_usd: float = 1000.0     # Entry size in USD
    max_position_size_usd: float = 10000.0 # Safety limit

    # Leverage Settings
    spot_leverage: int = 1       # 1 = no margin, 2-10 for spot margin
    futures_leverage: int = 1    # 1-125 for futures

    # Trading Mode
    paper_trading: bool = True   # Simulate trades without real orders
    algo_enabled: bool = False   # Master switch for automated trading

    # Order Execution Mode
    order_execution_mode: str = "MARKET"  # Legacy field
    entry_execution_mode: str = "LIMIT"   # Entries use maker fees
    exit_execution_mode: str = "MARKET"   # Exits prioritize speed
    limit_order_timeout_sec: int = 30     # Max wait for LIMIT fill
    limit_order_price_offset_bps: float = 1.0  # Passive pricing offset

    # Fee Estimates (basis points per side)
    spot_maker_fee_bps: float = 8.0    # Spot LIMIT orders
    spot_taker_fee_bps: float = 10.0   # Spot MARKET orders
    futures_maker_fee_bps: float = 2.0  # Futures LIMIT orders
    futures_taker_fee_bps: float = 5.0  # Futures MARKET orders

    # Legacy Fields (backward compatibility)
    taker_fee_bps: float = 5.0
    maker_fee_bps: float = 2.0
    estimated_costs_bps: float = 10.0

    # Safety Settings
    entry_cooldown_seconds: int = 60      # Min seconds between trades
    verify_exchange_position: bool = True # Check exchange before entry
    orphan_recovery_timeout_sec: int = 60 # Timeout for orphan leg recovery
```

**Key Methods:**
```python
def to_dict(self) -> Dict[str, Any]:
    """Convert to dictionary for JSON serialization."""

@classmethod
def from_dict(cls, data: Dict[str, Any]) -> 'TradingConfig':
    """Create instance from dictionary."""
```

**Fee Calculation:**
```python
# Entry fees (using LIMIT mode)
entry_fees = spot_maker_fee_bps + futures_maker_fee_bps  # 8 + 2 = 10 bps

# Exit fees (using MARKET mode)
exit_fees = spot_taker_fee_bps + futures_taker_fee_bps   # 10 + 5 = 15 bps

# Total round-trip cost
total_cost_bps = entry_fees + exit_fees  # 25 bps = 0.25%
```

---

### Exchange
**Exchange credentials and connection state.**

```python
@dataclass
class Exchange:
    """Exchange configuration and credentials."""
    id: Optional[int] = None        # Database ID
    name: str = ""                  # Display name
    exchange_type: str = ""         # "okx", "binance", "bybit"
    api_key: str = ""
    secret_key: str = ""
    passphrase: str = ""            # OKX-specific
    is_testnet: bool = True         # Use demo/testnet APIs
    role: str = "BOTH"              # SPOT, FUTURES, or BOTH
    is_active: bool = False         # Currently selected for trading
    status: str = "DISCONNECTED"    # Connection status
    last_error: str = ""            # Last error message
    created_at: Optional[datetime] = None
```

**Key Methods:**
```python
def to_dict(self) -> Dict[str, Any]:
    """Convert to dict with MASKED secrets for API responses."""
    # secret_key and passphrase shown as '***'

def to_dict_with_secrets(self) -> Dict[str, Any]:
    """Convert to dict WITH secrets for internal use."""
```

---

### Trade
**Complete trade record from entry to exit.**

```python
@dataclass
class Trade:
    """Trade record."""
    id: Optional[int] = None
    asset: str = ""
    position_type: str = "LONG"  # LONG or SHORT

    # Entry Details
    entry_time: Optional[datetime] = None
    entry_spot_price: float = 0.0
    entry_futures_price: float = 0.0
    entry_spread: float = 0.0
    entry_zscore: float = 0.0

    # Exit Details
    exit_time: Optional[datetime] = None
    exit_spot_price: float = 0.0
    exit_futures_price: float = 0.0
    exit_spread: float = 0.0
    exit_zscore: float = 0.0
    exit_reason: str = ""  # EXIT, STOP_LOSS, MANUAL

    # Position Details
    quantity: float = 0.0      # Asset quantity
    notional_usd: float = 0.0  # USD value at entry

    # P&L
    pnl_usd: float = 0.0       # Realized P&L in USD
    pnl_percent: float = 0.0   # P&L as percentage

    # Order IDs (for audit trail)
    spot_order_id: str = ""
    futures_order_id: str = ""

    # Status
    is_open: bool = True       # Position still open
    is_paper: bool = True      # Simulated trade
```

**P&L Calculation:**
```python
# For LONG position (bought spot, sold futures):
spread_change = entry_spread - exit_spread
pnl_usd = spread_change * quantity

# For SHORT position (sold spot, bought futures):
spread_change = exit_spread - entry_spread
pnl_usd = spread_change * quantity

# Percentage
pnl_percent = (pnl_usd / notional_usd) * 100
```

---

### MarketTick
**Real-time price data from exchange.**

```python
@dataclass
class MarketTick:
    """Market tick data."""
    symbol: str = ""
    bid: float = 0.0          # Best bid price
    ask: float = 0.0          # Best ask price
    last: float = 0.0         # Last traded price
    volume_24h: float = 0.0   # 24h volume
    timestamp: Optional[datetime] = None
```

**Computed Properties:**
```python
@property
def mid(self) -> float:
    """Mid price between bid and ask."""
    return (self.bid + self.ask) / 2 if self.bid and self.ask else self.last

@property
def spread_bps(self) -> float:
    """Bid-ask spread in basis points."""
    if self.mid > 0:
        return ((self.ask - self.bid) / self.mid) * 10000
    return 0.0
```

---

### Signal
**Trading signal with all context.**

```python
@dataclass
class Signal:
    """Trading signal."""
    signal_type: str = "NONE"      # NONE, LONG, SHORT, EXIT, STOP_LOSS
    zscore: float = 0.0            # Current Z-score
    spread: float = 0.0            # Current spread (futures - spot)
    spread_mean: float = 0.0       # Rolling mean
    spread_std: float = 0.0        # Rolling standard deviation
    hurst: float = 0.5             # Hurst exponent
    hurst_ok: Optional[bool] = True    # None during data collection
    std_filter_ok: Optional[bool] = True
    regime: str = "UNKNOWN"        # MEAN_REVERTING, TRENDING, COLLECTING
    timestamp: Optional[datetime] = None
    current_position: str = "NONE" # Current position context
```

**Regime Classification:**
- `COLLECTING`: Not enough data yet (< lookback_period)
- `MEAN_REVERTING`: Hurst < 0.4 (favorable for strategy)
- `TRENDING`: Hurst > 0.6 (unfavorable, avoid trades)
- `NEUTRAL`: Hurst between 0.4 and 0.6

---

### OrderResult
**Result of an order execution.**

```python
@dataclass
class OrderResult:
    """Order execution result."""
    success: bool = False      # Order succeeded
    order_id: str = ""         # Exchange order ID
    filled_qty: float = 0.0    # Quantity filled
    filled_price: float = 0.0  # Average fill price
    commission: float = 0.0    # Trading fees paid
    error: str = ""            # Error message if failed
```

---

### Position
**Current position on exchange.**

```python
@dataclass
class Position:
    """Current position."""
    symbol: str = ""
    side: str = ""            # LONG, SHORT
    quantity: float = 0.0
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: float = 1.0
```

---

### AccountInfo
**Comprehensive account state.**

```python
@dataclass
class AccountInfo:
    """Account information with margin details."""
    exchange: str = ""
    balance_usd: float = 0.0
    available_balance_usd: float = 0.0
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0

    # Account Identification
    uid: str = ""                      # User ID from exchange
    account_level: str = ""            # VIP level

    # Margin Details
    total_equity: float = 0.0          # Total account equity
    initial_margin: float = 0.0        # IMR
    maintenance_margin: float = 0.0    # MMR
    margin_ratio: float = 0.0          # Current margin ratio (%)
    available_margin: float = 0.0      # Available for new positions

    # Position-Level Details
    spot_margin_used: float = 0.0
    futures_margin_used: float = 0.0
    spot_unrealized_pnl: float = 0.0
    futures_unrealized_pnl: float = 0.0

    # Risk Metrics
    liquidation_price: Optional[float] = None
    mark_price: Optional[float] = None
    leverage_used: float = 1.0
```

---

### SDTouchEvent
**Records when price touches a standard deviation level.**

```python
@dataclass
class SDTouchEvent:
    """Standard deviation touch event for analysis."""
    id: Optional[int] = None
    asset: str = ""
    timestamp: Optional[datetime] = None
    sd_level: float = 0.0      # -3, -2, -1, 1, 2, 3
    direction: str = ""        # UP or DOWN (direction of crossing)
    spread: float = 0.0
    zscore: float = 0.0
    spot_price: float = 0.0
    futures_price: float = 0.0
```

**Purpose:**
Used for backtesting and strategy analysis. Tracks when Z-score crosses key levels (1, 2, 3 standard deviations) and from which direction.

---

## Asset Configuration

### CRYPTO_ASSETS Dictionary
Maps asset symbols to exchange-specific trading pairs.

```python
CRYPTO_ASSETS: Dict[str, Dict[str, str]] = {
    'BTC': {
        'name': 'Bitcoin',
        'okx_spot': 'BTC-USDT',
        'okx_futures': 'BTC-USDT-SWAP',
        'binance_spot': 'BTCUSDT',
        'binance_futures': 'BTCUSDT',
        'bybit_spot': 'BTCUSDT',
        'bybit_futures': 'BTCUSDT',
    },
    'ETH': {
        'name': 'Ethereum',
        'okx_spot': 'ETH-USDT',
        'okx_futures': 'ETH-USDT-SWAP',
        # ... similar mappings
    },
    'SOL': { ... },
    'XRP': { ... },
    'DOGE': { ... },
    'AVAX': { ... },
    'LINK': { ... },
}
```

### get_symbols_for_asset Function
Helper to retrieve exchange-specific symbols.

```python
def get_symbols_for_asset(asset: str, exchange_type: str) -> tuple:
    """Get spot and futures symbols for an asset on a specific exchange.

    Args:
        asset: Asset code (e.g., "BTC", "ETH")
        exchange_type: Exchange name (e.g., "okx", "binance", "bybit")

    Returns:
        Tuple of (spot_symbol, futures_symbol)

    Raises:
        ValueError: If asset or exchange is unknown

    Example:
        >>> get_symbols_for_asset("BTC", "okx")
        ('BTC-USDT', 'BTC-USDT-SWAP')

        >>> get_symbols_for_asset("ETH", "binance")
        ('ETHUSDT', 'ETHUSDT')
    """
```

---

## Implementation Notes

### 1. Dataclass Benefits
- Automatic `__init__`, `__repr__`, `__eq__` methods
- Type hints for IDE support
- Default values for optional fields

### 2. Serialization
All dataclasses include `to_dict()` methods for JSON serialization. This is critical for:
- API responses
- WebSocket events
- Database storage

### 3. Singleton Pattern
`TradingConfig` uses `id=1` as a singleton. Only one configuration exists in the database.

### 4. Security
`Exchange.to_dict()` masks secrets with '***'. Use `to_dict_with_secrets()` only for internal operations.

### 5. Backward Compatibility
Legacy fields (e.g., `taker_fee_bps`, `maker_fee_bps`) are kept to support older database records.

---

## Example Usage

### Creating a Trade Record
```python
trade = Trade(
    asset="BTC",
    position_type="LONG",
    entry_time=datetime.utcnow(),
    entry_spot_price=50000.0,
    entry_futures_price=50050.0,
    entry_spread=50.0,
    entry_zscore=2.5,
    quantity=0.02,
    notional_usd=1000.0,
    is_paper=False,
)

# Save to database
trade.id = db.save_trade(trade)

# Later, close the trade
trade.exit_time = datetime.utcnow()
trade.exit_spot_price = 50100.0
trade.exit_futures_price=50120.0
trade.exit_spread = 20.0
trade.exit_zscore = 0.4
trade.exit_reason = "EXIT"
trade.pnl_usd = (50.0 - 20.0) * 0.02  # $0.60
trade.pnl_percent = (0.60 / 1000.0) * 100  # 0.06%
trade.is_open = False

db.close_trade(trade)
```

### Processing Market Ticks
```python
spot_tick = MarketTick(
    symbol="BTC-USDT",
    bid=50000.0,
    ask=50001.0,
    last=50000.5,
    volume_24h=1000000.0,
    timestamp=datetime.utcnow(),
)

futures_tick = MarketTick(
    symbol="BTC-USDT-SWAP",
    bid=50050.0,
    ask=50051.0,
    last=50050.5,
    volume_24h=2000000.0,
    timestamp=datetime.utcnow(),
)

# Calculate spread
spread = futures_tick.mid - spot_tick.mid  # 50.25

# Check bid-ask spreads
print(f"Spot spread: {spot_tick.spread_bps:.2f} bps")   # ~0.2 bps
print(f"Futures spread: {futures_tick.spread_bps:.2f} bps")
```

### Using Configuration
```python
config = TradingConfig()

# Check if entry is allowed
def should_enter(zscore: float, hurst: float, std_ratio: float) -> bool:
    if abs(zscore) < config.entry_threshold:
        return False  # Z-score not extreme enough

    if config.hurst_enabled and hurst >= config.hurst_threshold:
        return False  # Market is trending

    if config.std_filter_enabled and std_ratio < config.min_std_multiple:
        return False  # Volatility too low for costs

    return True
```
