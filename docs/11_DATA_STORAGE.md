# Data Storage

## Overview

The system uses SQLite for persistent storage of configuration, exchange credentials, trades, and historical data. The `DatabaseManager` class provides a clean interface with connection pooling, transaction management, and automatic schema migrations.

## File Location
```
database/manager.py  # SQLite database manager (~830 lines)
trading.db           # SQLite database file (auto-created)
```

---

## Database Schema

### Tables Overview

| Table | Purpose |
|-------|---------|
| `trading_config` | Singleton configuration settings |
| `exchanges` | Exchange API credentials and status |
| `trades` | Trade journal with entry/exit data |
| `signal_log` | Historical signal events |
| `std_filter_log` | STD filter pass/fail events |
| `sd_touch_log` | SD level touch events |
| `spread_history` | Spread data for recovery after reconnection |

---

## Table Definitions

### trading_config (Singleton)

```sql
CREATE TABLE IF NOT EXISTS trading_config (
    id INTEGER PRIMARY KEY DEFAULT 1,

    -- Asset Configuration
    asset TEXT DEFAULT 'BTC',
    spot_symbol TEXT DEFAULT 'BTC-USDT',
    futures_symbol TEXT DEFAULT 'BTC-USDT-SWAP',

    -- Signal Thresholds
    entry_threshold REAL DEFAULT 2.0,
    exit_threshold REAL DEFAULT 0.5,
    stop_loss_threshold REAL DEFAULT 4.0,
    lookback_period INTEGER DEFAULT 100,
    stats_update_interval INTEGER DEFAULT 300,

    -- Filters
    hurst_enabled INTEGER DEFAULT 1,
    hurst_threshold REAL DEFAULT 0.5,
    std_filter_enabled INTEGER DEFAULT 1,
    min_std_multiple REAL DEFAULT 1.5,

    -- Position Sizing
    position_size_usd REAL DEFAULT 1000.0,
    max_position_size_usd REAL DEFAULT 10000.0,
    spot_leverage INTEGER DEFAULT 1,
    futures_leverage INTEGER DEFAULT 1,

    -- Execution Settings
    paper_trading INTEGER DEFAULT 1,
    algo_enabled INTEGER DEFAULT 0,
    order_execution_mode TEXT DEFAULT 'MARKET',
    limit_order_timeout_sec INTEGER DEFAULT 30,
    limit_order_price_offset_bps REAL DEFAULT 1.0,

    -- Fee Configuration
    taker_fee_bps REAL DEFAULT 5.0,
    maker_fee_bps REAL DEFAULT 2.0,
    estimated_costs_bps REAL DEFAULT 10.0,

    CHECK (id = 1)  -- Ensures singleton
)
```

**Key Points:**
- Only one row allowed (id=1 enforced by CHECK constraint)
- Booleans stored as INTEGER (0/1)
- Auto-migrates new columns with ALTER TABLE

### exchanges

```sql
CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                    -- User-friendly name
    exchange_type TEXT NOT NULL,           -- 'OKX', 'BINANCE', 'BYBIT'
    api_key TEXT NOT NULL,
    secret_key TEXT NOT NULL,
    passphrase TEXT DEFAULT '',            -- OKX requires passphrase
    is_testnet INTEGER DEFAULT 1,
    role TEXT DEFAULT 'BOTH',              -- 'SPOT', 'FUTURES', 'BOTH'
    is_active INTEGER DEFAULT 0,           -- Currently selected for trading
    status TEXT DEFAULT 'DISCONNECTED',    -- 'CONNECTED', 'DISCONNECTED', 'ERROR'
    last_error TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
```

### trades

```sql
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    position_type TEXT NOT NULL,           -- 'LONG' or 'SHORT'

    -- Entry Data
    entry_time TEXT,
    entry_spot_price REAL,
    entry_futures_price REAL,
    entry_spread REAL,
    entry_zscore REAL,

    -- Exit Data
    exit_time TEXT,
    exit_spot_price REAL,
    exit_futures_price REAL,
    exit_spread REAL,
    exit_zscore REAL,
    exit_reason TEXT,                      -- 'EXIT_SIGNAL', 'STOP_LOSS', 'MANUAL'

    -- Size and P&L
    quantity REAL,
    notional_usd REAL,
    pnl_usd REAL DEFAULT 0,
    pnl_percent REAL DEFAULT 0,

    -- Order IDs
    spot_order_id TEXT,
    futures_order_id TEXT,

    -- Status
    is_open INTEGER DEFAULT 1,
    is_paper INTEGER DEFAULT 1
)
```

### signal_log

```sql
CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    asset TEXT,
    signal_type TEXT,       -- 'LONG', 'SHORT', 'EXIT', 'HOLD'
    zscore REAL,
    spread REAL,
    spread_mean REAL,
    spread_std REAL,
    hurst REAL,
    hurst_ok INTEGER,       -- Did Hurst filter pass?
    std_filter_ok INTEGER,  -- Did STD filter pass?
    regime TEXT,            -- 'MEAN_REVERTING', 'TRENDING', 'UNKNOWN'
    current_position TEXT   -- Existing position at time of signal
)
```

### std_filter_log

```sql
CREATE TABLE IF NOT EXISTS std_filter_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    asset TEXT,
    std_value REAL,
    cost_threshold REAL,
    profitability_ratio REAL,
    passed INTEGER
)
```

### sd_touch_log

```sql
CREATE TABLE IF NOT EXISTS sd_touch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    asset TEXT,
    sd_level REAL,           -- 1.0, 2.0, 3.0, etc.
    direction TEXT,          -- 'UP' or 'DOWN'
    spread REAL,
    zscore REAL,
    spot_price REAL,
    futures_price REAL
)
```

### spread_history

```sql
CREATE TABLE IF NOT EXISTS spread_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    asset TEXT NOT NULL,
    spot_price REAL,
    futures_price REAL,
    spread REAL
)

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_spread_history_asset_time
ON spread_history (asset, timestamp DESC)
```

---

## DatabaseManager Class

### Initialization

```python
# database/manager.py

class DatabaseManager:
    """SQLite database manager for the trading system."""

    def __init__(self, db_path: str = "trading.db"):
        self.db_path = db_path
        self._init_database()
```

### Connection Management

```python
@contextmanager
def _get_connection(self):
    """Context manager for database connections."""
    conn = sqlite3.connect(self.db_path)
    conn.row_factory = sqlite3.Row  # Enable dict-like access
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()
```

**Features:**
- Auto-commit on success
- Auto-rollback on error
- Connection cleanup in finally block
- `sqlite3.Row` enables column access by name

### Schema Migrations

```python
def _init_database(self) -> None:
    """Initialize database tables."""
    with self._get_connection() as conn:
        cursor = conn.cursor()

        # Create tables (CREATE TABLE IF NOT EXISTS ...)
        # ...

        # Insert default config if not exists
        cursor.execute("SELECT COUNT(*) FROM trading_config")
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO trading_config (id) VALUES (1)")

        # Migrations: Add new columns if they don't exist
        cursor.execute("PRAGMA table_info(trading_config)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        if 'taker_fee_bps' not in existing_columns:
            cursor.execute("ALTER TABLE trading_config ADD COLUMN taker_fee_bps REAL DEFAULT 5.0")
        if 'maker_fee_bps' not in existing_columns:
            cursor.execute("ALTER TABLE trading_config ADD COLUMN maker_fee_bps REAL DEFAULT 2.0")
        # ... more migrations
```

---

## Configuration Methods

### Get Config

```python
def get_config(self) -> TradingConfig:
    """Get trading configuration."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM trading_config WHERE id = 1")
        row = cursor.fetchone()

        if row:
            return TradingConfig(
                id=row["id"],
                asset=row["asset"],
                spot_symbol=row["spot_symbol"],
                futures_symbol=row["futures_symbol"],
                entry_threshold=row["entry_threshold"],
                # ... all fields mapped
                # Handle missing columns for backward compatibility:
                taker_fee_bps=row["taker_fee_bps"] if "taker_fee_bps" in row.keys() else 5.0,
            )

        return TradingConfig()  # Return defaults if no row
```

### Save Config

```python
def save_config(self, config: TradingConfig) -> None:
    """Save trading configuration."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE trading_config SET
                asset = ?,
                spot_symbol = ?,
                futures_symbol = ?,
                entry_threshold = ?,
                exit_threshold = ?,
                stop_loss_threshold = ?,
                lookback_period = ?,
                stats_update_interval = ?,
                hurst_enabled = ?,
                hurst_threshold = ?,
                std_filter_enabled = ?,
                min_std_multiple = ?,
                position_size_usd = ?,
                max_position_size_usd = ?,
                spot_leverage = ?,
                futures_leverage = ?,
                paper_trading = ?,
                algo_enabled = ?,
                order_execution_mode = ?,
                limit_order_timeout_sec = ?,
                limit_order_price_offset_bps = ?,
                taker_fee_bps = ?,
                maker_fee_bps = ?,
                estimated_costs_bps = ?
            WHERE id = 1
        """, (
            config.asset,
            config.spot_symbol,
            config.futures_symbol,
            # ... all values in same order as columns
            int(config.hurst_enabled),  # Convert bool to int
            int(config.paper_trading),
            # ...
        ))
```

---

## Exchange Methods

### Get All Exchanges

```python
def get_exchanges(self) -> List[Exchange]:
    """Get all exchanges."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM exchanges ORDER BY created_at DESC")
        rows = cursor.fetchall()

        return [
            Exchange(
                id=row["id"],
                name=row["name"],
                exchange_type=row["exchange_type"],
                api_key=row["api_key"],
                secret_key=row["secret_key"],
                passphrase=row["passphrase"],
                is_testnet=bool(row["is_testnet"]),
                role=row["role"],
                is_active=bool(row["is_active"]),
                status=row["status"],
                last_error=row["last_error"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            )
            for row in rows
        ]
```

### Save/Update Exchange

```python
def save_exchange(self, exchange: Exchange) -> int:
    """Save or update exchange."""
    with self._get_connection() as conn:
        cursor = conn.cursor()

        if exchange.id:
            # Update existing
            cursor.execute("""
                UPDATE exchanges SET
                    name = ?, exchange_type = ?, api_key = ?, secret_key = ?,
                    passphrase = ?, is_testnet = ?, role = ?, is_active = ?,
                    status = ?, last_error = ?
                WHERE id = ?
            """, (..., exchange.id))
            return exchange.id
        else:
            # Insert new
            cursor.execute("""
                INSERT INTO exchanges (
                    name, exchange_type, api_key, secret_key, passphrase,
                    is_testnet, role, is_active, status, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (...))
            return cursor.lastrowid
```

### Set Active Exchanges

```python
def set_active_exchanges(self, spot_id: Optional[int], futures_id: Optional[int]) -> None:
    """Set active exchanges for trading."""
    with self._get_connection() as conn:
        cursor = conn.cursor()

        # Deactivate all
        cursor.execute("UPDATE exchanges SET is_active = 0")

        # Activate selected
        if spot_id:
            cursor.execute(
                "UPDATE exchanges SET is_active = 1, role = 'SPOT' WHERE id = ?",
                (spot_id,)
            )
        if futures_id:
            cursor.execute(
                "UPDATE exchanges SET is_active = 1, role = 'FUTURES' WHERE id = ?",
                (futures_id,)
            )
```

---

## Trade Methods

### Save Trade

```python
def save_trade(self, trade: Trade) -> int:
    """Save or update trade."""
    with self._get_connection() as conn:
        cursor = conn.cursor()

        if trade.id:
            # Update existing (for exit data)
            cursor.execute("""
                UPDATE trades SET
                    exit_time = ?, exit_spot_price = ?, exit_futures_price = ?,
                    exit_spread = ?, exit_zscore = ?, exit_reason = ?,
                    pnl_usd = ?, pnl_percent = ?, is_open = ?
                WHERE id = ?
            """, (
                trade.exit_time.isoformat() if trade.exit_time else None,
                trade.exit_spot_price,
                trade.exit_futures_price,
                trade.exit_spread,
                trade.exit_zscore,
                trade.exit_reason,
                trade.pnl_usd,
                trade.pnl_percent,
                int(trade.is_open),
                trade.id,
            ))
            return trade.id
        else:
            # Insert new (entry)
            cursor.execute("""
                INSERT INTO trades (
                    asset, position_type, entry_time, entry_spot_price,
                    entry_futures_price, entry_spread, entry_zscore,
                    quantity, notional_usd, spot_order_id, futures_order_id,
                    is_open, is_paper
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (...))
            return cursor.lastrowid
```

### Get Trades

```python
def get_trades(self, limit: int = 100, open_only: bool = False) -> List[Trade]:
    """Get trades."""
    with self._get_connection() as conn:
        cursor = conn.cursor()

        query = "SELECT * FROM trades"
        if open_only:
            query += " WHERE is_open = 1"
        query += " ORDER BY entry_time DESC LIMIT ?"

        cursor.execute(query, (limit,))
        rows = cursor.fetchall()

        return [self._row_to_trade(row) for row in rows]

def get_open_trade(self) -> Optional[Trade]:
    """Get current open trade."""
    trades = self.get_trades(limit=1, open_only=True)
    return trades[0] if trades else None
```

### Trade Statistics

```python
def get_trade_statistics(self) -> Dict[str, Any]:
    """Get trade statistics for analysis."""
    with self._get_connection() as conn:
        cursor = conn.cursor()

        # Total trades
        cursor.execute("SELECT COUNT(*) FROM trades WHERE is_open = 0")
        total_trades = cursor.fetchone()[0]

        # Winning trades
        cursor.execute("SELECT COUNT(*) FROM trades WHERE is_open = 0 AND pnl_usd > 0")
        winning_trades = cursor.fetchone()[0]

        # Total P&L
        cursor.execute("SELECT SUM(pnl_usd) FROM trades WHERE is_open = 0")
        total_pnl = cursor.fetchone()[0] or 0

        # Average P&L
        cursor.execute("SELECT AVG(pnl_usd) FROM trades WHERE is_open = 0")
        avg_pnl = cursor.fetchone()[0] or 0

        # Win rate
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

        return {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": total_trades - winning_trades,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
        }
```

---

## Spread History (Recovery)

### Save Spread

```python
def save_spread(
    self,
    asset: str,
    spot_price: float,
    futures_price: float,
    spread: float,
) -> None:
    """Save a spread data point."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO spread_history (asset, spot_price, futures_price, spread)
            VALUES (?, ?, ?, ?)
        """, (asset, spot_price, futures_price, spread))
```

### Get Spread History

```python
def get_spread_history(self, asset: str, limit: int = 500) -> List[Dict[str, Any]]:
    """
    Get spread history for an asset (for recovery after reconnection).

    Returns list of dicts with timestamp, spot_price, futures_price, spread.
    Results are ordered oldest first for correct loading order.
    """
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT timestamp, spot_price, futures_price, spread
            FROM spread_history
            WHERE asset = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (asset, limit))
        rows = cursor.fetchall()

        # Reverse to get oldest first (for correct loading order)
        return [
            {
                'timestamp': row['timestamp'],
                'spot_price': row['spot_price'],
                'futures_price': row['futures_price'],
                'spread': row['spread'],
            }
            for row in reversed(rows)
        ]
```

### Cleanup Old History

```python
def cleanup_old_spread_history(self, asset: str, keep_count: int = 1000) -> None:
    """Remove old spread history entries, keeping only the most recent."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM spread_history
            WHERE asset = ? AND id NOT IN (
                SELECT id FROM spread_history
                WHERE asset = ?
                ORDER BY timestamp DESC
                LIMIT ?
            )
        """, (asset, asset, keep_count))
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("Cleaned up %d old spread history entries for %s", deleted, asset)
```

---

## Logging Methods

### Log Signal

```python
def log_signal(self, signal_data: Dict[str, Any]) -> None:
    """Log a signal event."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO signal_log (
                asset, signal_type, zscore, spread, spread_mean, spread_std,
                hurst, hurst_ok, std_filter_ok, regime, current_position
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal_data.get("asset"),
            signal_data.get("signal_type"),
            signal_data.get("zscore"),
            signal_data.get("spread"),
            signal_data.get("spread_mean"),
            signal_data.get("spread_std"),
            signal_data.get("hurst"),
            int(signal_data.get("hurst_ok", True)),
            int(signal_data.get("std_filter_ok", True)),
            signal_data.get("regime"),
            signal_data.get("current_position"),
        ))
```

### Log STD Filter

```python
def log_std_filter(
    self,
    asset: str,
    std_value: float,
    cost_threshold: float,
    profitability_ratio: float,
    passed: bool,
) -> None:
    """Log STD filter event."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO std_filter_log (
                asset, std_value, cost_threshold, profitability_ratio, passed
            ) VALUES (?, ?, ?, ?, ?)
        """, (asset, std_value, cost_threshold, profitability_ratio, int(passed)))
```

### Log SD Touch

```python
def log_sd_touch(self, event: SDTouchEvent) -> None:
    """Log SD touch event."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sd_touch_log (
                asset, sd_level, direction, spread, zscore, spot_price, futures_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            event.asset,
            event.sd_level,
            event.direction,
            event.spread,
            event.zscore,
            event.spot_price,
            event.futures_price,
        ))
```

---

## Clear/Reset Methods

```python
def clear_trades(self, asset: Optional[str] = None) -> int:
    """Clear all trades (or for a specific asset). Returns count deleted."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        if asset:
            cursor.execute("DELETE FROM trades WHERE asset = ?", (asset,))
        else:
            cursor.execute("DELETE FROM trades")
        return cursor.rowcount

def clear_sd_touches(self, asset: Optional[str] = None) -> int:
    """Clear all SD touch events."""
    # Similar pattern

def clear_signal_log(self, asset: Optional[str] = None) -> int:
    """Clear signal log."""
    # Similar pattern

def clear_spread_history(self, asset: Optional[str] = None) -> int:
    """Clear spread history."""
    # Similar pattern

def delete_trade(self, trade_id: int) -> bool:
    """Delete a specific trade by ID."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        return cursor.rowcount > 0

def close_trade(self, trade_id: int, exit_reason: str = "MANUAL") -> bool:
    """Manually close an open trade."""
    with self._get_connection() as conn:
        cursor = conn.cursor()
        # Check if trade exists and is open
        cursor.execute("SELECT * FROM trades WHERE id = ? AND is_open = 1", (trade_id,))
        if not cursor.fetchone():
            return False

        # Update to closed
        cursor.execute("""
            UPDATE trades SET
                exit_time = ?,
                exit_reason = ?,
                is_open = 0
            WHERE id = ?
        """, (datetime.utcnow().isoformat(), exit_reason, trade_id))
        return True
```

---

## Usage in Application

### Initialization (app.py)

```python
from database.manager import DatabaseManager

# Create singleton instance
db = DatabaseManager()

# Load config
config = db.get_config()

# Get active exchanges
exchanges = db.get_exchanges()
active_spot = next((e for e in exchanges if e.is_active and e.role in ['SPOT', 'BOTH']), None)
active_futures = next((e for e in exchanges if e.is_active and e.role in ['FUTURES', 'BOTH']), None)
```

### In Trading Engine

```python
# Save new trade on entry
trade = Trade(
    asset=config.asset,
    position_type=signal.signal_type,
    entry_time=datetime.utcnow(),
    entry_spot_price=spot_tick.last,
    entry_futures_price=futures_tick.last,
    entry_spread=spread,
    entry_zscore=zscore,
    quantity=quantity,
    notional_usd=notional,
    is_paper=config.paper_trading,
)
trade.id = db.save_trade(trade)

# Update on exit
trade.exit_time = datetime.utcnow()
trade.exit_spot_price = spot_tick.last
trade.exit_futures_price = futures_tick.last
trade.exit_spread = exit_spread
trade.exit_zscore = exit_zscore
trade.exit_reason = "EXIT_SIGNAL"
trade.pnl_usd = calculated_pnl
trade.pnl_percent = (calculated_pnl / trade.notional_usd) * 100
trade.is_open = False
db.save_trade(trade)
```

---

## Backup & Recovery

### Manual Backup

```bash
# Copy database file
cp trading.db trading_backup_$(date +%Y%m%d).db

# Or use SQLite .backup command
sqlite3 trading.db ".backup 'trading_backup.db'"
```

### Recovery from Spread History

```python
# After reconnection, reload spread data into signal generator
history = db.get_spread_history(config.asset, limit=config.lookback_period)
for point in history:
    signal_generator.add_spread_data(
        spot_price=point['spot_price'],
        futures_price=point['futures_price'],
    )
```

### Query Examples

```bash
# SQLite CLI
sqlite3 trading.db

# View recent trades
SELECT * FROM trades ORDER BY entry_time DESC LIMIT 10;

# Trade statistics
SELECT
    COUNT(*) as total,
    SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
    SUM(pnl_usd) as total_pnl
FROM trades WHERE is_open = 0;

# SD touches by level
SELECT sd_level, COUNT(*) as count
FROM sd_touch_log
GROUP BY sd_level
ORDER BY sd_level;

# Recent signals
SELECT * FROM signal_log ORDER BY timestamp DESC LIMIT 20;
```

---

## Thread Safety

SQLite in Python is thread-safe when:
1. Each thread uses its own connection
2. Using `sqlite3.connect()` with default settings

The `DatabaseManager` achieves this by:
- Creating new connection for each operation via context manager
- Connection is closed after each operation
- No connection pooling/sharing between threads

For high-frequency operations, consider:
- Write-ahead logging: `PRAGMA journal_mode=WAL`
- Busy timeout: `PRAGMA busy_timeout=5000`
