"""
Database manager for the trading system.
Handles SQLite operations for configuration, exchanges, trades, and logs.
"""

import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from models import TradingConfig, Exchange, Trade, SDTouchEvent

logger = logging.getLogger(__name__)


class DatabaseManager:
    """
    SQLite database manager for the trading system.

    Tables:
    - trading_config: Singleton configuration settings
    - exchanges: Exchange API credentials and status
    - trades: Trade journal
    - signal_log: Historical signals
    - std_filter_log: STD filter events
    - sd_touch_log: SD level touch events
    """

    def __init__(self, db_path: str = "trading.db"):
        self.db_path = db_path
        self._init_database()

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _init_database(self) -> None:
        """Initialize database tables."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Trading configuration (singleton)
            cursor.execute("""
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
                    paper_trading INTEGER DEFAULT 1,
                    algo_enabled INTEGER DEFAULT 0,
                    order_execution_mode TEXT DEFAULT 'MARKET',
                    limit_order_timeout_sec INTEGER DEFAULT 30,
                    limit_order_price_offset_bps REAL DEFAULT 1.0,
                    taker_fee_bps REAL DEFAULT 5.0,
                    maker_fee_bps REAL DEFAULT 2.0,
                    estimated_costs_bps REAL DEFAULT 10.0,
                    CHECK (id = 1)
                )
            """)

            # Exchanges
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS exchanges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    exchange_type TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    secret_key TEXT NOT NULL,
                    passphrase TEXT DEFAULT '',
                    is_testnet INTEGER DEFAULT 1,
                    role TEXT DEFAULT 'BOTH',
                    is_active INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'DISCONNECTED',
                    last_error TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Trades
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    position_type TEXT NOT NULL,
                    entry_time TEXT,
                    entry_spot_price REAL,
                    entry_futures_price REAL,
                    entry_spread REAL,
                    entry_zscore REAL,
                    exit_time TEXT,
                    exit_spot_price REAL,
                    exit_futures_price REAL,
                    exit_spread REAL,
                    exit_zscore REAL,
                    exit_reason TEXT,
                    quantity REAL,
                    notional_usd REAL,
                    pnl_usd REAL DEFAULT 0,
                    pnl_percent REAL DEFAULT 0,
                    spot_order_id TEXT,
                    futures_order_id TEXT,
                    is_open INTEGER DEFAULT 1,
                    is_paper INTEGER DEFAULT 1
                )
            """)

            # Signal log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS signal_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    asset TEXT,
                    signal_type TEXT,
                    zscore REAL,
                    spread REAL,
                    spread_mean REAL,
                    spread_std REAL,
                    hurst REAL,
                    hurst_ok INTEGER,
                    std_filter_ok INTEGER,
                    regime TEXT,
                    current_position TEXT
                )
            """)

            # STD filter log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS std_filter_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    asset TEXT,
                    std_value REAL,
                    cost_threshold REAL,
                    profitability_ratio REAL,
                    passed INTEGER
                )
            """)

            # SD touch log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sd_touch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    asset TEXT,
                    sd_level REAL,
                    direction TEXT,
                    spread REAL,
                    zscore REAL,
                    spot_price REAL,
                    futures_price REAL
                )
            """)

            # Spread history for persistence/recovery
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS spread_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                    asset TEXT NOT NULL,
                    spot_price REAL,
                    futures_price REAL,
                    spread REAL
                )
            """)

            # Create index for faster queries
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_spread_history_asset_time
                ON spread_history (asset, timestamp DESC)
            """)

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

            logger.info("Database initialized: %s", self.db_path)

    # Trading Config Methods
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
                    exit_threshold=row["exit_threshold"],
                    stop_loss_threshold=row["stop_loss_threshold"],
                    lookback_period=row["lookback_period"],
                    stats_update_interval=row["stats_update_interval"] if "stats_update_interval" in row.keys() else 300,
                    hurst_enabled=bool(row["hurst_enabled"]),
                    hurst_threshold=row["hurst_threshold"],
                    std_filter_enabled=bool(row["std_filter_enabled"]),
                    min_std_multiple=row["min_std_multiple"],
                    position_size_usd=row["position_size_usd"],
                    max_position_size_usd=row["max_position_size_usd"],
                    paper_trading=bool(row["paper_trading"]),
                    algo_enabled=bool(row["algo_enabled"]),
                    order_execution_mode=row["order_execution_mode"] if "order_execution_mode" in row.keys() else "MARKET",
                    limit_order_timeout_sec=row["limit_order_timeout_sec"] if "limit_order_timeout_sec" in row.keys() else 30,
                    limit_order_price_offset_bps=row["limit_order_price_offset_bps"] if "limit_order_price_offset_bps" in row.keys() else 1.0,
                    taker_fee_bps=row["taker_fee_bps"] if "taker_fee_bps" in row.keys() else 5.0,
                    maker_fee_bps=row["maker_fee_bps"] if "maker_fee_bps" in row.keys() else 2.0,
                    estimated_costs_bps=row["estimated_costs_bps"],
                )

            return TradingConfig()

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
                config.entry_threshold,
                config.exit_threshold,
                config.stop_loss_threshold,
                config.lookback_period,
                config.stats_update_interval,
                int(config.hurst_enabled),
                config.hurst_threshold,
                int(config.std_filter_enabled),
                config.min_std_multiple,
                config.position_size_usd,
                config.max_position_size_usd,
                int(config.paper_trading),
                int(config.algo_enabled),
                config.order_execution_mode,
                config.limit_order_timeout_sec,
                config.limit_order_price_offset_bps,
                config.taker_fee_bps,
                config.maker_fee_bps,
                config.estimated_costs_bps,
            ))
            logger.info("Config saved")

    # Exchange Methods
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

    def get_exchange(self, exchange_id: int) -> Optional[Exchange]:
        """Get exchange by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM exchanges WHERE id = ?", (exchange_id,))
            row = cursor.fetchone()

            if row:
                return Exchange(
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

            return None

    def save_exchange(self, exchange: Exchange) -> int:
        """Save or update exchange."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if exchange.id:
                cursor.execute("""
                    UPDATE exchanges SET
                        name = ?,
                        exchange_type = ?,
                        api_key = ?,
                        secret_key = ?,
                        passphrase = ?,
                        is_testnet = ?,
                        role = ?,
                        is_active = ?,
                        status = ?,
                        last_error = ?
                    WHERE id = ?
                """, (
                    exchange.name,
                    exchange.exchange_type,
                    exchange.api_key,
                    exchange.secret_key,
                    exchange.passphrase,
                    int(exchange.is_testnet),
                    exchange.role,
                    int(exchange.is_active),
                    exchange.status,
                    exchange.last_error,
                    exchange.id,
                ))
                return exchange.id
            else:
                cursor.execute("""
                    INSERT INTO exchanges (
                        name, exchange_type, api_key, secret_key, passphrase,
                        is_testnet, role, is_active, status, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    exchange.name,
                    exchange.exchange_type,
                    exchange.api_key,
                    exchange.secret_key,
                    exchange.passphrase,
                    int(exchange.is_testnet),
                    exchange.role,
                    int(exchange.is_active),
                    exchange.status,
                    exchange.last_error,
                ))
                return cursor.lastrowid

    def delete_exchange(self, exchange_id: int) -> None:
        """Delete exchange."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM exchanges WHERE id = ?", (exchange_id,))
            logger.info("Exchange deleted: %d", exchange_id)

    def update_exchange_status(self, exchange_id: int, status: str, error: str = "") -> None:
        """Update exchange connection status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE exchanges SET status = ?, last_error = ?
                WHERE id = ?
            """, (status, error, exchange_id))

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

            logger.info("Active exchanges set: spot=%s, futures=%s", spot_id, futures_id)

    # Trade Methods
    def save_trade(self, trade: Trade) -> int:
        """Save or update trade."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if trade.id:
                cursor.execute("""
                    UPDATE trades SET
                        exit_time = ?,
                        exit_spot_price = ?,
                        exit_futures_price = ?,
                        exit_spread = ?,
                        exit_zscore = ?,
                        exit_reason = ?,
                        pnl_usd = ?,
                        pnl_percent = ?,
                        is_open = ?
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
                cursor.execute("""
                    INSERT INTO trades (
                        asset, position_type, entry_time, entry_spot_price,
                        entry_futures_price, entry_spread, entry_zscore,
                        quantity, notional_usd, spot_order_id, futures_order_id,
                        is_open, is_paper
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade.asset,
                    trade.position_type,
                    trade.entry_time.isoformat() if trade.entry_time else None,
                    trade.entry_spot_price,
                    trade.entry_futures_price,
                    trade.entry_spread,
                    trade.entry_zscore,
                    trade.quantity,
                    trade.notional_usd,
                    trade.spot_order_id,
                    trade.futures_order_id,
                    int(trade.is_open),
                    int(trade.is_paper),
                ))
                return cursor.lastrowid

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

    def _row_to_trade(self, row) -> Trade:
        """Convert database row to Trade object."""
        return Trade(
            id=row["id"],
            asset=row["asset"],
            position_type=row["position_type"],
            entry_time=datetime.fromisoformat(row["entry_time"]) if row["entry_time"] else None,
            entry_spot_price=row["entry_spot_price"] or 0,
            entry_futures_price=row["entry_futures_price"] or 0,
            entry_spread=row["entry_spread"] or 0,
            entry_zscore=row["entry_zscore"] or 0,
            exit_time=datetime.fromisoformat(row["exit_time"]) if row["exit_time"] else None,
            exit_spot_price=row["exit_spot_price"] or 0,
            exit_futures_price=row["exit_futures_price"] or 0,
            exit_spread=row["exit_spread"] or 0,
            exit_zscore=row["exit_zscore"] or 0,
            exit_reason=row["exit_reason"] or "",
            quantity=row["quantity"] or 0,
            notional_usd=row["notional_usd"] or 0,
            pnl_usd=row["pnl_usd"] or 0,
            pnl_percent=row["pnl_percent"] or 0,
            spot_order_id=row["spot_order_id"] or "",
            futures_order_id=row["futures_order_id"] or "",
            is_open=bool(row["is_open"]),
            is_paper=bool(row["is_paper"]),
        )

    # Logging Methods
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

    def get_sd_touches(self, asset: Optional[str] = None, limit: int = 1000) -> List[SDTouchEvent]:
        """Get SD touch events."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = "SELECT * FROM sd_touch_log"
            params = []

            if asset:
                query += " WHERE asset = ?"
                params.append(asset)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)
            rows = cursor.fetchall()

            return [
                SDTouchEvent(
                    id=row["id"],
                    asset=row["asset"],
                    timestamp=datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None,
                    sd_level=row["sd_level"],
                    direction=row["direction"],
                    spread=row["spread"],
                    zscore=row["zscore"],
                    spot_price=row["spot_price"],
                    futures_price=row["futures_price"],
                )
                for row in rows
            ]

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

    # Spread History Methods (for persistence/recovery)
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

    # Reset/Clear Methods
    def clear_trades(self, asset: Optional[str] = None) -> int:
        """Clear all trades (or for a specific asset). Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if asset:
                cursor.execute("DELETE FROM trades WHERE asset = ?", (asset,))
            else:
                cursor.execute("DELETE FROM trades")
            deleted = cursor.rowcount
            logger.info("Cleared %d trades%s", deleted, f" for {asset}" if asset else "")
            return deleted

    def clear_sd_touches(self, asset: Optional[str] = None) -> int:
        """Clear all SD touch events (or for a specific asset). Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if asset:
                cursor.execute("DELETE FROM sd_touch_log WHERE asset = ?", (asset,))
            else:
                cursor.execute("DELETE FROM sd_touch_log")
            deleted = cursor.rowcount
            logger.info("Cleared %d SD touches%s", deleted, f" for {asset}" if asset else "")
            return deleted

    def clear_signal_log(self, asset: Optional[str] = None) -> int:
        """Clear signal log (or for a specific asset). Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if asset:
                cursor.execute("DELETE FROM signal_log WHERE asset = ?", (asset,))
            else:
                cursor.execute("DELETE FROM signal_log")
            deleted = cursor.rowcount
            logger.info("Cleared %d signal log entries%s", deleted, f" for {asset}" if asset else "")
            return deleted

    def clear_spread_history(self, asset: Optional[str] = None) -> int:
        """Clear spread history (or for a specific asset). Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if asset:
                cursor.execute("DELETE FROM spread_history WHERE asset = ?", (asset,))
            else:
                cursor.execute("DELETE FROM spread_history")
            deleted = cursor.rowcount
            logger.info("Cleared %d spread history entries%s", deleted, f" for {asset}" if asset else "")
            return deleted

    def delete_trade(self, trade_id: int) -> bool:
        """Delete a specific trade by ID. Returns True if deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info("Deleted trade ID %d", trade_id)
            return deleted

    def close_trade(self, trade_id: int, exit_reason: str = "MANUAL") -> bool:
        """
        Manually close an open trade.
        Returns True if closed, False if not found or already closed.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Check if trade exists and is open
            cursor.execute("SELECT * FROM trades WHERE id = ? AND is_open = 1", (trade_id,))
            row = cursor.fetchone()
            if not row:
                return False

            # Update to closed
            cursor.execute("""
                UPDATE trades SET
                    exit_time = ?,
                    exit_reason = ?,
                    is_open = 0
                WHERE id = ?
            """, (datetime.utcnow().isoformat(), exit_reason, trade_id))
            logger.info("Manually closed trade ID %d", trade_id)
            return True
