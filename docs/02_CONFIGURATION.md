# Configuration System

## Overview

The configuration system manages all trading parameters through a singleton `TradingConfig` object stored in SQLite. Configuration can be modified via the web UI (Settings page) or programmatically.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     SETTINGS PAGE                           │
│                  (templates/settings.html)                  │
│                                                             │
│   Form inputs → POST /api/config → Save to database         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     DATABASE MANAGER                        │
│                  (database/manager.py)                      │
│                                                             │
│   get_config() ← trading_config table (id=1)               │
│   save_config() → trading_config table                     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    TRADING CONFIG                           │
│                      (models.py)                            │
│                                                             │
│   Singleton dataclass with all trading parameters          │
└─────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
   Trading Engine      Signal Generator     Order Executor
```

## Configuration Parameters

### Asset Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `asset` | str | "BTC" | Trading asset (BTC, ETH, SOL, etc.) |
| `spot_symbol` | str | "BTC-USDT" | Exchange-specific spot symbol |
| `futures_symbol` | str | "BTC-USDT-SWAP" | Exchange-specific futures symbol |

**Symbol Mapping:**
```python
# When asset changes, symbols auto-update based on exchange
from models import get_symbols_for_asset

spot, futures = get_symbols_for_asset("ETH", "okx")
# Returns: ("ETH-USDT", "ETH-USDT-SWAP")
```

### Z-Score Thresholds

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `entry_threshold` | float | 2.0 | 1.0-5.0 | Enter when \|Z\| >= threshold |
| `exit_threshold` | float | 0.5 | 0.0-2.0 | Exit when \|Z\| <= threshold |
| `stop_loss_threshold` | float | 4.0 | 2.0-10.0 | Emergency exit when \|Z\| >= threshold |

**Threshold Logic:**
```python
# Entry signals (position == NONE)
if zscore >= entry_threshold:      # Z >= 2.0
    signal = LONG
elif zscore <= -entry_threshold:   # Z <= -2.0
    signal = SHORT

# Exit signals (with position)
if position == LONG:
    if zscore <= exit_threshold:       # Z <= 0.5
        signal = EXIT
    elif zscore >= stop_loss_threshold: # Z >= 4.0
        signal = STOP_LOSS

if position == SHORT:
    if zscore >= -exit_threshold:      # Z >= -0.5
        signal = EXIT
    elif zscore <= -stop_loss_threshold: # Z <= -4.0
        signal = STOP_LOSS
```

### Rolling Window Settings

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `lookback_period` | int | 100 | Number of ticks for rolling statistics |
| `stats_update_interval` | int | 300 | Seconds between mean/std recalculation |

**How It Works:**
- `lookback_period`: Controls the deque size for spread history
- `stats_update_interval`: Mean and std are recalculated every N seconds, not every tick
- Z-score is calculated on every tick using cached mean/std
- This provides stable bands while remaining responsive

```python
# Signal Generator behavior
if time_since_last_update >= stats_update_interval:
    self.current_mean = np.mean(self.spread_history)
    self.current_std = np.std(self.spread_history)

# Z-score always current
self.current_zscore = (current_spread - self.current_mean) / self.current_std
```

### Statistical Filters

#### Hurst Exponent Filter

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hurst_enabled` | bool | True | Enable regime detection |
| `hurst_threshold` | float | 0.5 | H < threshold = mean-reverting |

**Interpretation:**
- H < 0.4: Strong mean reversion (ideal for strategy)
- H = 0.5: Random walk (neutral)
- H > 0.6: Trending (avoid trades)

#### STD Profitability Filter

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `std_filter_enabled` | bool | True | Enable volatility check |
| `min_std_multiple` | float | 1.5 | STD must be > costs × multiple |

**Calculation:**
```python
# Calculate trading costs in price terms
entry_fees = spot_maker_fee_bps + futures_maker_fee_bps  # 10 bps
exit_fees = spot_taker_fee_bps + futures_taker_fee_bps   # 15 bps
total_fees_bps = entry_fees + exit_fees  # 25 bps

cost_in_price = (total_fees_bps / 10000) * spot_price

# Check profitability
profitability_ratio = current_std / cost_in_price

if profitability_ratio >= min_std_multiple:
    std_filter_ok = True  # Volatility sufficient
else:
    std_filter_ok = False # Block trades
```

### Position Sizing

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `position_size_usd` | float | 1000.0 | 100-∞ | Entry size in USD |
| `max_position_size_usd` | float | 10000.0 | 1000-∞ | Safety limit |

**Quantity Calculation:**
```python
quantity = min(position_size_usd, max_position_size_usd) / spot_price
```

### Leverage Settings

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `spot_leverage` | int | 1 | 1-10 | Spot margin multiplier |
| `futures_leverage` | int | 1 | 1-125 | Futures leverage |

**Applied on Engine Start:**
```python
async def start(self):
    # Apply leverage settings to exchange
    if not self.state.paper_trading:
        await self.futures_adapter.set_leverage(
            self.config.futures_symbol,
            self.config.futures_leverage
        )
```

### Trading Mode

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `paper_trading` | bool | True | Simulate trades without real orders |
| `algo_enabled` | bool | False | Master switch for automated trading |

**Mode Behavior:**
- `paper_trading=True`: Engine generates signals and simulates fills
- `paper_trading=False`: Real orders sent to exchange
- `algo_enabled=False`: Engine runs but doesn't execute trades
- `algo_enabled=True`: Full automated trading

### Order Execution Mode

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `order_execution_mode` | str | "MARKET" | Legacy field |
| `entry_execution_mode` | str | "LIMIT" | Mode for entry orders |
| `exit_execution_mode` | str | "MARKET" | Mode for exit orders |
| `limit_order_timeout_sec` | int | 30 | Max wait for LIMIT fill |
| `limit_order_price_offset_bps` | float | 1.0 | Passive pricing offset |

**Why Different Modes:**
- **Entries (LIMIT)**: Less urgent, save fees with maker pricing
- **Exits (MARKET)**: More urgent, prioritize execution speed

### Fee Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `spot_maker_fee_bps` | float | 8.0 | Spot LIMIT order fee |
| `spot_taker_fee_bps` | float | 10.0 | Spot MARKET order fee |
| `futures_maker_fee_bps` | float | 2.0 | Futures LIMIT order fee |
| `futures_taker_fee_bps` | float | 5.0 | Futures MARKET order fee |

**OKX Fee Structure (non-VIP):**
| Market | Maker | Taker |
|--------|-------|-------|
| Spot | 0.08% (8 bps) | 0.10% (10 bps) |
| Futures | 0.02% (2 bps) | 0.05% (5 bps) |

### Safety Settings

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `entry_cooldown_seconds` | int | 60 | Min seconds between trades |
| `verify_exchange_position` | bool | True | Check exchange before entry |
| `orphan_recovery_timeout_sec` | int | 60 | Timeout for leg recovery |

---

## Environment Variables

Located in `.env` file:

```bash
# Flask Configuration
FLASK_SECRET_KEY=your-secret-key-32-chars-minimum
FLASK_DEBUG=false

# Trading Options
USE_WEBSOCKET=true  # Enable real-time WebSocket (~100ms vs 500ms polling)

# Database
DATABASE_PATH=trading.db
```

**Loading Process:**
```python
# app.py
from dotenv import load_dotenv
load_dotenv()

DATABASE_PATH = os.getenv('DATABASE_PATH', 'trading.db')
USE_WEBSOCKET = os.getenv('USE_WEBSOCKET', 'true').lower() == 'true'
```

---

## Database Schema

### trading_config Table

```sql
CREATE TABLE IF NOT EXISTS trading_config (
    id INTEGER PRIMARY KEY DEFAULT 1,
    asset TEXT DEFAULT 'BTC',
    spot_symbol TEXT DEFAULT 'BTC-USDT',
    futures_symbol TEXT DEFAULT 'BTC-USDT-SWAP',
    entry_threshold REAL DEFAULT 2.0,
    exit_threshold REAL DEFAULT 0.5,
    stop_loss_threshold REAL DEFAULT 4.0,
    lookback_period INTEGER DEFAULT 100,
    stats_update_interval INTEGER DEFAULT 300,
    hurst_enabled INTEGER DEFAULT 1,
    hurst_threshold REAL DEFAULT 0.5,
    std_filter_enabled INTEGER DEFAULT 1,
    min_std_multiple REAL DEFAULT 1.5,
    position_size_usd REAL DEFAULT 1000.0,
    max_position_size_usd REAL DEFAULT 10000.0,
    spot_leverage INTEGER DEFAULT 1,
    futures_leverage INTEGER DEFAULT 1,
    paper_trading INTEGER DEFAULT 1,
    algo_enabled INTEGER DEFAULT 0,
    order_execution_mode TEXT DEFAULT 'MARKET',
    entry_execution_mode TEXT DEFAULT 'LIMIT',
    exit_execution_mode TEXT DEFAULT 'MARKET',
    limit_order_timeout_sec INTEGER DEFAULT 30,
    limit_order_price_offset_bps REAL DEFAULT 1.0,
    spot_maker_fee_bps REAL DEFAULT 8.0,
    spot_taker_fee_bps REAL DEFAULT 10.0,
    futures_maker_fee_bps REAL DEFAULT 2.0,
    futures_taker_fee_bps REAL DEFAULT 5.0,
    taker_fee_bps REAL DEFAULT 5.0,
    maker_fee_bps REAL DEFAULT 2.0,
    estimated_costs_bps REAL DEFAULT 10.0,
    entry_cooldown_seconds INTEGER DEFAULT 60,
    verify_exchange_position INTEGER DEFAULT 1,
    orphan_recovery_timeout_sec INTEGER DEFAULT 60
);
```

### Database Operations

```python
# database/manager.py

def get_config(self) -> TradingConfig:
    """Load configuration from database."""
    cursor = self.conn.execute("SELECT * FROM trading_config WHERE id = 1")
    row = cursor.fetchone()
    if row:
        return TradingConfig.from_dict(dict(row))
    return TradingConfig()  # Return defaults if no config

def save_config(self, config: TradingConfig) -> None:
    """Save configuration to database."""
    data = config.to_dict()
    columns = ', '.join(data.keys())
    placeholders = ', '.join(['?' for _ in data])
    values = list(data.values())

    self.conn.execute(f'''
        INSERT OR REPLACE INTO trading_config ({columns})
        VALUES ({placeholders})
    ''', values)
    self.conn.commit()
```

---

## API Endpoints

### GET /api/config

Returns current configuration.

**Response:**
```json
{
    "asset": "BTC",
    "spot_symbol": "BTC-USDT",
    "futures_symbol": "BTC-USDT-SWAP",
    "entry_threshold": 2.0,
    "exit_threshold": 0.5,
    "stop_loss_threshold": 4.0,
    "lookback_period": 100,
    "stats_update_interval": 300,
    "hurst_enabled": true,
    "hurst_threshold": 0.5,
    "std_filter_enabled": true,
    "min_std_multiple": 1.5,
    "position_size_usd": 1000.0,
    "max_position_size_usd": 10000.0,
    "spot_leverage": 1,
    "futures_leverage": 1,
    "paper_trading": true,
    "algo_enabled": false,
    "entry_execution_mode": "LIMIT",
    "exit_execution_mode": "MARKET",
    "spot_maker_fee_bps": 8.0,
    "spot_taker_fee_bps": 10.0,
    "futures_maker_fee_bps": 2.0,
    "futures_taker_fee_bps": 5.0
}
```

### POST /api/config

Updates configuration.

**Request Body:**
```json
{
    "asset": "ETH",
    "entry_threshold": 2.5,
    "position_size_usd": 2000.0
}
```

**Response:**
```json
{
    "success": true
}
```

**Backend Processing:**
```python
@app.route('/api/config', methods=['POST'])
def save_config():
    data = request.json

    # Update config object
    for key, value in data.items():
        if hasattr(config, key):
            setattr(config, key, value)

    # Handle asset change - update symbols
    if 'asset' in data:
        spot, futures = get_symbols_for_asset(config.asset, "okx")
        config.spot_symbol = spot
        config.futures_symbol = futures

    # Save to database
    db.save_config(config)

    # Update engine with new config
    engine.update_config(config)
    engine.signal_generator.update_config(config)

    return jsonify({'success': True})
```

---

## Settings Page Implementation

### Form Structure (templates/settings.html)

```html
<form id="settings-form">
    <!-- Asset Selection -->
    <select name="asset" id="asset-select">
        <option value="BTC">Bitcoin (BTC)</option>
        <option value="ETH">Ethereum (ETH)</option>
        <option value="SOL">Solana (SOL)</option>
        <!-- ... more assets -->
    </select>

    <!-- Z-Score Thresholds -->
    <input type="number" name="entry_threshold" step="0.1" min="1.0" max="5.0">
    <input type="number" name="exit_threshold" step="0.1" min="0.0" max="2.0">
    <input type="number" name="stop_loss_threshold" step="0.1" min="2.0" max="10.0">

    <!-- Filters -->
    <input type="checkbox" name="hurst_enabled">
    <input type="number" name="hurst_threshold" step="0.05">
    <input type="checkbox" name="std_filter_enabled">
    <input type="number" name="min_std_multiple" step="0.1">

    <!-- Position Sizing -->
    <input type="number" name="position_size_usd" step="100">
    <input type="number" name="max_position_size_usd" step="100">

    <!-- Leverage -->
    <input type="number" name="spot_leverage" min="1" max="10">
    <input type="number" name="futures_leverage" min="1" max="125">

    <!-- Order Execution -->
    <select name="entry_execution_mode">
        <option value="LIMIT">LIMIT (Lower Fees)</option>
        <option value="MARKET">MARKET (Faster)</option>
    </select>
    <select name="exit_execution_mode">
        <option value="MARKET">MARKET (Faster)</option>
        <option value="LIMIT">LIMIT (Lower Fees)</option>
    </select>

    <!-- Fee Configuration -->
    <input type="number" name="spot_maker_fee_bps" step="0.5">
    <input type="number" name="spot_taker_fee_bps" step="0.5">
    <input type="number" name="futures_maker_fee_bps" step="0.5">
    <input type="number" name="futures_taker_fee_bps" step="0.5">

    <button type="submit">Save Settings</button>
</form>
```

### JavaScript Submission

```javascript
document.getElementById('settings-form').addEventListener('submit', function(e) {
    e.preventDefault();

    const formData = new FormData(this);
    const data = {};

    // Convert form data to JSON
    for (let [key, value] of formData.entries()) {
        // Handle checkboxes
        if (this.elements[key].type === 'checkbox') {
            data[key] = this.elements[key].checked;
        }
        // Handle numbers
        else if (this.elements[key].type === 'number') {
            data[key] = parseFloat(value);
        }
        else {
            data[key] = value;
        }
    }

    fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(result => {
        if (result.success) {
            showToast('Settings saved successfully', 'success');
        } else {
            showToast('Failed to save settings', 'error');
        }
    });
});
```

---

## Configuration Propagation

When configuration changes, it must propagate to all components:

```python
# app.py - POST /api/config handler

# 1. Update global config object
config.entry_threshold = new_value

# 2. Save to database
db.save_config(config)

# 3. Update trading engine
engine.update_config(config)

# 4. Update signal generator
engine.signal_generator.update_config(config)

# 5. Update order executor (if execution mode changed)
if 'entry_execution_mode' in data or 'exit_execution_mode' in data:
    engine.order_executor.update_config(config)
```

### Signal Generator Update

```python
# core/signals.py

def update_config(self, config: TradingConfig) -> None:
    """Update configuration dynamically."""
    self.config = config
    self.lookback = config.lookback_period

    # Resize deques if lookback changed
    if len(self.spread_history) > self.lookback:
        # Keep only last N entries
        new_deque = deque(
            list(self.spread_history)[-self.lookback:],
            maxlen=self.lookback
        )
        self.spread_history = new_deque

    # Reset statistics with new lookback
    self._update_statistics()
```

---

## Validation Rules

### Server-Side Validation

```python
def validate_config(data: dict) -> tuple:
    """Validate configuration parameters.

    Returns:
        (is_valid: bool, error_message: str)
    """
    errors = []

    # Z-score thresholds
    if data.get('entry_threshold', 2.0) <= data.get('exit_threshold', 0.5):
        errors.append("Entry threshold must be > exit threshold")

    if data.get('stop_loss_threshold', 4.0) <= data.get('entry_threshold', 2.0):
        errors.append("Stop-loss threshold must be > entry threshold")

    # Position sizing
    if data.get('position_size_usd', 1000) > data.get('max_position_size_usd', 10000):
        errors.append("Position size must be <= max position size")

    # Leverage
    if data.get('spot_leverage', 1) < 1 or data.get('spot_leverage', 1) > 10:
        errors.append("Spot leverage must be 1-10")

    if data.get('futures_leverage', 1) < 1 or data.get('futures_leverage', 1) > 125:
        errors.append("Futures leverage must be 1-125")

    # Fees (sanity check)
    for fee_field in ['spot_maker_fee_bps', 'spot_taker_fee_bps',
                      'futures_maker_fee_bps', 'futures_taker_fee_bps']:
        if data.get(fee_field, 0) < 0 or data.get(fee_field, 0) > 100:
            errors.append(f"{fee_field} must be 0-100 bps")

    return (len(errors) == 0, "; ".join(errors))
```

---

## Recommended Configurations

### Conservative (Beginner)
```json
{
    "entry_threshold": 3.0,
    "exit_threshold": 0.5,
    "stop_loss_threshold": 5.0,
    "hurst_enabled": true,
    "hurst_threshold": 0.45,
    "std_filter_enabled": true,
    "min_std_multiple": 2.0,
    "position_size_usd": 500,
    "spot_leverage": 1,
    "futures_leverage": 1,
    "entry_execution_mode": "LIMIT",
    "exit_execution_mode": "MARKET"
}
```

### Moderate (Intermediate)
```json
{
    "entry_threshold": 2.0,
    "exit_threshold": 0.5,
    "stop_loss_threshold": 4.0,
    "hurst_enabled": true,
    "hurst_threshold": 0.5,
    "std_filter_enabled": true,
    "min_std_multiple": 1.5,
    "position_size_usd": 1000,
    "spot_leverage": 1,
    "futures_leverage": 2,
    "entry_execution_mode": "LIMIT",
    "exit_execution_mode": "MARKET"
}
```

### Aggressive (Advanced)
```json
{
    "entry_threshold": 1.5,
    "exit_threshold": 0.3,
    "stop_loss_threshold": 3.0,
    "hurst_enabled": false,
    "std_filter_enabled": true,
    "min_std_multiple": 1.2,
    "position_size_usd": 5000,
    "spot_leverage": 3,
    "futures_leverage": 5,
    "entry_execution_mode": "MARKET",
    "exit_execution_mode": "MARKET"
}
```

---

## Troubleshooting

### Configuration Not Saving
1. Check browser console for JavaScript errors
2. Verify database file is writable
3. Check Flask logs for validation errors

### Changes Not Taking Effect
1. Configuration requires engine restart for some settings
2. Signal generator resets statistics on lookback change
3. Leverage changes require exchange API call

### Asset Change Issues
1. Ensure exchange supports the new asset
2. Check symbol mapping in `CRYPTO_ASSETS`
3. Verify WebSocket subscription updates
