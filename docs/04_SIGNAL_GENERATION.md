# Signal Generation

## Overview

The Signal Generator (`core/signals.py`, ~524 lines) implements the core statistical arbitrage logic. It calculates Z-scores, applies statistical filters, and generates trading signals.

## File Location
```
core/signals.py
```

## Dependencies
```python
import numpy as np
from collections import deque
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any, Callable
import logging

from models import Signal, TradingConfig, MarketTick, SDTouchEvent
```

---

## Core Concept: Spread Z-Score

The system trades the spread between futures and spot prices:

```
Spread = Futures_Price - Spot_Price
Z-Score = (Spread - Rolling_Mean) / Rolling_Std
```

### Why This Works

1. **Normal Market**: Futures typically trade at a small premium to spot (basis)
2. **Deviation**: When the spread deviates significantly, it tends to revert
3. **Entry**: Enter when Z-score is extreme (deviation from normal)
4. **Exit**: Exit when Z-score returns toward zero (reversion to mean)

---

## Signal Types

| Signal | Condition | Position Action |
|--------|-----------|-----------------|
| `NONE` | No conditions met | Do nothing |
| `LONG` | Z >= +2.0 (no position) | Buy spot, sell futures |
| `SHORT` | Z <= -2.0 (no position) | Sell spot, buy futures |
| `EXIT` | Z crossed exit threshold | Close position |
| `STOP_LOSS` | Z exceeded stop-loss | Emergency close |

### Entry Logic (Position = NONE)

```python
# Z-score conditions
z_triggers_long = zscore >= entry_threshold   # e.g., >= 2.0
z_triggers_short = zscore <= -entry_threshold # e.g., <= -2.0

# Filters must also pass
if z_triggers_long and hurst_ok and std_ok:
    signal = "LONG"
elif z_triggers_short and hurst_ok and std_ok:
    signal = "SHORT"
```

### Exit Logic (With Position)

```python
# LONG position exit
if current_position == "LONG":
    if zscore <= exit_threshold:      # e.g., <= 0.5
        signal = "EXIT"
    elif zscore >= stop_loss_threshold:  # e.g., >= 4.0
        signal = "STOP_LOSS"

# SHORT position exit
if current_position == "SHORT":
    if zscore >= -exit_threshold:     # e.g., >= -0.5
        signal = "EXIT"
    elif zscore <= -stop_loss_threshold: # e.g., <= -4.0
        signal = "STOP_LOSS"
```

**Important**: Exit signals ignore filters. Once in a position, exits are based solely on Z-score thresholds.

---

## SignalGenerator Class

### Constructor

```python
def __init__(self, config: TradingConfig):
    self.config = config
    self.lookback = config.lookback_period        # e.g., 100
    self.stats_update_interval = config.stats_update_interval  # e.g., 300s

    # Rolling data storage
    self.spread_history: deque = deque(maxlen=self.lookback)
    self.spot_prices: deque = deque(maxlen=self.lookback)
    self.futures_prices: deque = deque(maxlen=self.lookback)

    # Current state
    self.current_zscore: float = 0.0
    self.current_spread: float = 0.0
    self.current_mean: float = 0.0
    self.current_std: float = 0.0
    self.current_hurst: float = 0.5

    # Stats timing
    self.last_stats_update: Optional[datetime] = None
    self._stats_initialized: bool = False

    # SD touch tracking
    self.last_sd_level: float = 0.0
    self.sd_touch_events: List[SDTouchEvent] = []
    self.on_sd_touch: Optional[Callable[[SDTouchEvent], None]] = None

    # Position context
    self.current_position: str = "NONE"

    # Diagnostics
    self.last_blocked_signal: Optional[Dict[str, Any]] = None
```

### Adding Ticks

```python
def add_tick(self, spot_tick: MarketTick, futures_tick: MarketTick) -> None:
    """Add a new tick and update calculations."""
    spot_price = spot_tick.mid
    futures_price = futures_tick.mid

    if spot_price <= 0 or futures_price <= 0:
        logger.warning("Invalid tick prices")
        return

    spread = futures_price - spot_price

    # Append to rolling windows
    self.spot_prices.append(spot_price)
    self.futures_prices.append(futures_price)
    self.spread_history.append(spread)

    self.current_spread = spread
    self._update_statistics()
```

---

## Statistics Update

Statistics are updated at a configurable interval (default: 300 seconds), not on every tick. This provides stable bands for easier entry/exit tracking.

```python
def _update_statistics(self) -> None:
    """Update rolling statistics."""
    if len(self.spread_history) < 2:
        return

    now = datetime.utcnow()

    # Check if recalculation needed
    should_update = (
        not self._stats_initialized or
        self.last_stats_update is None or
        (now - self.last_stats_update).total_seconds() >= self.stats_update_interval
    )

    if should_update:
        spreads = np.array(self.spread_history)
        self.current_mean = float(np.mean(spreads))
        self.current_std = float(np.std(spreads, ddof=1))

        # Update Hurst if enough data
        if len(self.spread_history) >= 20:
            self.current_hurst = self._calculate_hurst(spreads)

        self.last_stats_update = now
        self._stats_initialized = True

    # Always update Z-score with current spread
    if self.current_std > 0:
        self.current_zscore = (self.current_spread - self.current_mean) / self.current_std
    else:
        self.current_zscore = 0.0
```

---

## Hurst Exponent (R/S Analysis)

The Hurst exponent detects whether the spread is mean-reverting or trending.

### Interpretation
| Hurst Value | Regime | Trading Suitability |
|-------------|--------|---------------------|
| H < 0.4 | Strong Mean Reversion | Excellent |
| H = 0.5 | Random Walk | Neutral |
| H > 0.6 | Trending | Poor (avoid) |

### Algorithm

```python
def _calculate_hurst(self, series: np.ndarray) -> float:
    """
    Calculate Hurst exponent using R/S (Rescaled Range) analysis.

    Method:
    1. Divide series into subseries of varying lengths (k)
    2. For each subseries:
       - Calculate mean deviation from subseries mean
       - Compute cumulative deviations
       - R = max(cumulative) - min(cumulative)
       - S = standard deviation of subseries
       - R/S = rescaled range
    3. Average R/S values for each length k
    4. Fit log(R/S) vs log(k) - slope is Hurst exponent
    """
    n = len(series)
    if n < 20:
        return 0.5

    max_k = min(n // 2, 50)
    min_k = 10

    if max_k <= min_k:
        return 0.5

    rs_values = []
    n_values = []

    for k in range(min_k, max_k + 1, 5):
        rs_list = []

        for start in range(0, n - k + 1, k):
            subseries = series[start:start + k]
            if len(subseries) < k:
                continue

            mean_val = np.mean(subseries)
            deviations = subseries - mean_val
            cumulative_deviations = np.cumsum(deviations)

            R = np.max(cumulative_deviations) - np.min(cumulative_deviations)
            S = np.std(subseries, ddof=1)

            if S > 0:
                rs_list.append(R / S)

        if rs_list:
            rs_values.append(np.mean(rs_list))
            n_values.append(k)

    if len(rs_values) < 2:
        return 0.5

    # Linear regression in log-log space
    log_n = np.log(n_values)
    log_rs = np.log(rs_values)

    try:
        slope, _ = np.polyfit(log_n, log_rs, 1)
        return float(np.clip(slope, 0.0, 1.0))
    except:
        return 0.5
```

---

## STD Filter

The STD filter ensures that spread volatility is sufficient to cover trading costs.

### Calculation

```python
def _check_std_filter(self) -> Tuple[bool, float]:
    """
    Check if STD covers trading costs.

    Fee structure (OKX non-VIP):
      Spot:    Maker 8 bps, Taker 10 bps
      Futures: Maker 2 bps, Taker 5 bps

    Round-trip = Entry + Exit costs

    Returns:
        (passed: bool, profitability_ratio: float)
    """
    if not self.config.std_filter_enabled:
        return True, float('inf')

    if self.current_std <= 0:
        return False, 0.0

    spot_price = self.spot_prices[-1] if self.spot_prices else 0
    if spot_price <= 0:
        return False, 0.0

    # Get fee configuration
    spot_maker = self.config.spot_maker_fee_bps    # 8
    spot_taker = self.config.spot_taker_fee_bps    # 10
    fut_maker = self.config.futures_maker_fee_bps  # 2
    fut_taker = self.config.futures_taker_fee_bps  # 5

    # Entry fees based on entry mode
    if self.config.entry_execution_mode == "LIMIT":
        entry_cost_bps = spot_maker + fut_maker  # 10 bps
    else:
        entry_cost_bps = spot_taker + fut_taker  # 15 bps

    # Exit fees based on exit mode
    if self.config.exit_execution_mode == "LIMIT":
        exit_cost_bps = spot_maker + fut_maker   # 10 bps
    else:
        exit_cost_bps = spot_taker + fut_taker   # 15 bps

    # Total round-trip
    total_cost_bps = entry_cost_bps + exit_cost_bps  # e.g., 25 bps
    costs_price = (total_cost_bps / 10000) * spot_price

    # Profitability ratio
    ratio = self.current_std / costs_price

    # Must exceed minimum multiple (e.g., 1.5x)
    passed = ratio >= self.config.min_std_multiple

    return passed, ratio
```

### Example

```
BTC at $65,000
Total fees: 25 bps = 0.25%
Cost in price: $65,000 × 0.0025 = $162.50

Current STD: $250

Profitability ratio: $250 / $162.50 = 1.54x

Min required: 1.5x
Result: PASS (1.54 >= 1.5)
```

---

## SD Touch Tracking

Tracks when Z-score crosses standard deviation levels for analysis.

```python
def _track_sd_touch(self, zscore: float, spot_price: float,
                    futures_price: float) -> Optional[SDTouchEvent]:
    """Track when Z-score crosses SD levels."""
    current_sd_level = 0.0

    # Determine current SD level (-3, -2, -1, 0, 1, 2, 3)
    for level in [-3, -2, -1, 1, 2, 3]:
        if level < 0:
            if zscore <= level and zscore > level - 1:
                current_sd_level = level
                break
        else:
            if zscore >= level and zscore < level + 1:
                current_sd_level = level
                break

    # Check for level crossing
    if current_sd_level != 0 and current_sd_level != self.last_sd_level:
        direction = "DOWN" if current_sd_level < self.last_sd_level else "UP"

        event = SDTouchEvent(
            asset=self.config.asset,
            timestamp=datetime.utcnow(),
            sd_level=current_sd_level,
            direction=direction,
            spread=self.current_spread,
            zscore=zscore,
            spot_price=spot_price,
            futures_price=futures_price,
        )

        self.sd_touch_events.append(event)
        self.last_sd_level = current_sd_level

        # Trigger callback for database logging
        if self.on_sd_touch:
            self.on_sd_touch(event)

        return event

    self.last_sd_level = current_sd_level
    return None
```

---

## Signal Generation

```python
def generate_signal(self) -> Signal:
    """Generate trading signal based on current state."""
    timestamp = datetime.utcnow()

    # Must have full lookback period before trading
    if len(self.spread_history) < self.lookback:
        return Signal(
            signal_type="NONE",
            zscore=self.current_zscore,
            spread=self.current_spread,
            spread_mean=self.current_mean,
            spread_std=self.current_std,
            hurst=self.current_hurst,
            hurst_ok=None,      # Unknown until full data
            std_filter_ok=None, # Unknown until full data
            regime="COLLECTING",
            current_position=self.current_position,
            timestamp=timestamp,
        )

    # Check filters
    hurst_ok = not self.config.hurst_enabled or self.current_hurst < self.config.hurst_threshold
    std_ok, _ = self._check_std_filter()

    # Determine regime
    if self.current_hurst < 0.4:
        regime = "MEAN_REVERTING"
    elif self.current_hurst > 0.6:
        regime = "TRENDING"
    else:
        regime = "NEUTRAL"

    # Track SD touches
    self._track_sd_touch(self.current_zscore,
                        self.spot_prices[-1],
                        self.futures_prices[-1])

    # Determine signal
    signal_type = "NONE"
    blocked_reason = None

    if self.current_position == "NONE":
        # Entry signals
        z_long = self.current_zscore >= self.config.entry_threshold
        z_short = self.current_zscore <= -self.config.entry_threshold

        if z_long or z_short:
            if not hurst_ok:
                blocked_reason = f"Hurst filter (H={self.current_hurst:.3f})"
            elif not std_ok:
                blocked_reason = "STD filter (volatility too low)"
            elif z_long and self.current_zscore >= self.config.stop_loss_threshold:
                blocked_reason = "Z at stop-loss level"
            elif z_short and self.current_zscore <= -self.config.stop_loss_threshold:
                blocked_reason = "Z at stop-loss level"
            else:
                signal_type = "LONG" if z_long else "SHORT"

            if blocked_reason:
                self.last_blocked_signal = {
                    'timestamp': timestamp.isoformat(),
                    'would_be_signal': 'LONG' if z_long else 'SHORT',
                    'zscore': round(self.current_zscore, 4),
                    'reason': blocked_reason,
                }

    elif self.current_position == "LONG":
        # Exit signals for LONG (filters don't apply)
        if self.current_zscore <= self.config.exit_threshold:
            signal_type = "EXIT"
        elif self.current_zscore >= self.config.stop_loss_threshold:
            signal_type = "STOP_LOSS"

    elif self.current_position == "SHORT":
        # Exit signals for SHORT (filters don't apply)
        if self.current_zscore >= -self.config.exit_threshold:
            signal_type = "EXIT"
        elif self.current_zscore <= -self.config.stop_loss_threshold:
            signal_type = "STOP_LOSS"

    return Signal(
        signal_type=signal_type,
        zscore=self.current_zscore,
        spread=self.current_spread,
        spread_mean=self.current_mean,
        spread_std=self.current_std,
        hurst=self.current_hurst,
        hurst_ok=hurst_ok,
        std_filter_ok=std_ok,
        regime=regime,
        current_position=self.current_position,
        timestamp=timestamp,
    )
```

---

## State Reporting

```python
def get_state(self) -> Dict[str, Any]:
    """Get current state for dashboard."""
    # Time until next stats update
    if self.last_stats_update:
        elapsed = (datetime.utcnow() - self.last_stats_update).total_seconds()
        next_update_in = max(0, self.stats_update_interval - elapsed)
    else:
        next_update_in = 0

    # Filter status
    hurst_ok = not self.config.hurst_enabled or self.current_hurst < self.config.hurst_threshold
    std_ok, std_ratio = self._check_std_filter()

    # Data readiness
    data_ready = len(self.spread_history) >= self.lookback

    # Regime
    if self.current_hurst < 0.4:
        regime = "MEAN_REVERTING"
    elif self.current_hurst > 0.6:
        regime = "TRENDING"
    else:
        regime = "NEUTRAL"

    return {
        'zscore': round(self.current_zscore, 4),
        'spread': round(self.current_spread, 6),
        'spread_mean': round(self.current_mean, 6),
        'spread_std': round(self.current_std, 6),
        'hurst': round(self.current_hurst, 4),
        'hurst_ok': hurst_ok if data_ready else None,
        'std_filter_ok': std_ok if data_ready else None,
        'std_ratio': round(std_ratio, 2) if std_ratio != float('inf') else None,
        'std_ratio_required': self.config.min_std_multiple,
        'regime': regime if data_ready else "COLLECTING",
        'data_points': len(self.spread_history),
        'lookback': self.lookback,
        'data_ready': data_ready,
        'current_position': self.current_position,
        'next_stats_update_in': round(next_update_in),
        'last_blocked_signal': self.last_blocked_signal,
    }
```

---

## History Management

### Loading Historical Data

```python
def load_spread_history(self, spreads: List[float]) -> None:
    """Load spread history for recovery after restart."""
    self.spread_history.clear()
    for spread in spreads[-self.lookback:]:
        self.spread_history.append(spread)

    if len(self.spread_history) >= 2:
        self._update_statistics()

    logger.info("Loaded %d spread values", len(self.spread_history))
```

### Getting History for Charts

```python
def get_spread_history(self, n: int = 100) -> List[float]:
    """Get last n spread values."""
    return list(self.spread_history)[-n:]

def get_zscore_history(self, n: int = 100) -> List[float]:
    """Calculate Z-score history for charting."""
    if len(self.spread_history) < 20:
        return []

    spreads = list(self.spread_history)
    zscores = []

    for i in range(19, len(spreads)):
        window = spreads[max(0, i - self.lookback + 1):i + 1]
        mean = np.mean(window)
        std = np.std(window, ddof=1)
        if std > 0:
            z = (spreads[i] - mean) / std
            zscores.append(float(z))
        else:
            zscores.append(0.0)

    return zscores[-n:]
```

---

## Reset

```python
def reset(self) -> None:
    """Reset all state."""
    self.spread_history.clear()
    self.spot_prices.clear()
    self.futures_prices.clear()
    self.current_zscore = 0.0
    self.current_spread = 0.0
    self.current_mean = 0.0
    self.current_std = 0.0
    self.current_hurst = 0.5
    self.last_sd_level = 0.0
    self.sd_touch_events.clear()
    self.current_position = "NONE"
    self.last_stats_update = None
    self._stats_initialized = False
    self.last_blocked_signal = None
```

---

## Integration Example

```python
# Initialize
config = TradingConfig(
    entry_threshold=2.0,
    exit_threshold=0.5,
    stop_loss_threshold=4.0,
    lookback_period=100,
    stats_update_interval=300,
    hurst_enabled=True,
    hurst_threshold=0.5,
    std_filter_enabled=True,
    min_std_multiple=1.5,
)

generator = SignalGenerator(config)

# Set position context
generator.set_position("NONE")

# Process ticks
for spot_tick, futures_tick in tick_stream:
    generator.add_tick(spot_tick, futures_tick)
    signal = generator.generate_signal()

    if signal.signal_type == "LONG":
        # Execute long entry
        generator.set_position("LONG")
    elif signal.signal_type == "SHORT":
        # Execute short entry
        generator.set_position("SHORT")
    elif signal.signal_type in ("EXIT", "STOP_LOSS"):
        # Close position
        generator.set_position("NONE")
```

---

## Visual Representation

```
Z-Score Timeline
----------------

+4.0 ─────────────────── STOP_LOSS (LONG) ───────────────────
     │
+2.0 ─────────────────── ENTRY (LONG) ────────────────────────
     │                    ╱╲
+0.5 ────────────────────╱──╲─ EXIT (LONG) ───────────────────
     │                  ╱    ╲
 0.0 ─────────────────────────────────────────────────────────
     │                        ╲    ╱
-0.5 ─────────────────────────╲──╱─ EXIT (SHORT) ─────────────
     │                         ╲╱
-2.0 ─────────────────── ENTRY (SHORT) ───────────────────────
     │
-4.0 ─────────────────── STOP_LOSS (SHORT) ──────────────────

Time →
```

---

## Filter Decision Tree

```
Signal Generation Flow
----------------------

Is data ready? (spread_history >= lookback)
├── NO  → Signal = NONE, regime = COLLECTING
│
└── YES → Check position
          │
          ├── Position = NONE (Looking for entry)
          │   │
          │   ├── Z >= entry_threshold?
          │   │   ├── Hurst OK? (H < threshold or disabled)
          │   │   │   ├── STD OK? (ratio >= min_multiple or disabled)
          │   │   │   │   ├── Z < stop_loss? → Signal = LONG
          │   │   │   │   └── Z >= stop_loss? → BLOCKED (at stop level)
          │   │   │   └── STD NOT OK → BLOCKED (volatility too low)
          │   │   └── Hurst NOT OK → BLOCKED (trending regime)
          │   │
          │   ├── Z <= -entry_threshold?
          │   │   └── (same filter checks) → Signal = SHORT
          │   │
          │   └── Z in range → Signal = NONE
          │
          ├── Position = LONG
          │   ├── Z <= exit_threshold → Signal = EXIT
          │   ├── Z >= stop_loss_threshold → Signal = STOP_LOSS
          │   └── Otherwise → Signal = NONE
          │
          └── Position = SHORT
              ├── Z >= -exit_threshold → Signal = EXIT
              ├── Z <= -stop_loss_threshold → Signal = STOP_LOSS
              └── Otherwise → Signal = NONE
```
