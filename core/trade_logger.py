"""
Trade and Alert Logger - CSV logging for unattended monitoring.

Writes key events to a CSV file for easy post-analysis:
- Trade entries/exits
- Order attempts and failures
- Critical alerts
- System status
"""

import csv
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class TradeLogger:
    """CSV logger for trades and alerts."""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

        # Create dated log file
        date_str = datetime.now().strftime("%Y%m%d")
        self.trades_file = self.log_dir / f"trades_{date_str}.csv"
        self.alerts_file = self.log_dir / f"alerts_{date_str}.csv"

        # Initialize files with headers if they don't exist
        self._init_trades_file()
        self._init_alerts_file()

        logger.info("Trade logger initialized: trades=%s, alerts=%s",
                   self.trades_file, self.alerts_file)

    def _init_trades_file(self):
        """Initialize trades CSV with headers."""
        if not self.trades_file.exists():
            with open(self.trades_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'event_type', 'position_type', 'quantity',
                    'spot_price', 'futures_price', 'spread_bps',
                    'spot_order_id', 'futures_order_id',
                    'spot_status', 'futures_status',
                    'pnl_usd', 'fees_usd', 'notes'
                ])

    def _init_alerts_file(self):
        """Initialize alerts CSV with headers."""
        if not self.alerts_file.exists():
            with open(self.alerts_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'level', 'category', 'message',
                    'spot_attempts', 'spot_failures', 'futures_attempts', 'futures_failures',
                    'details'
                ])

    def log_trade(
        self,
        event_type: str,  # ENTRY, EXIT, PARTIAL_FILL, ORPHAN_CLOSE
        position_type: str,  # LONG, SHORT
        quantity: float,
        spot_price: float = 0,
        futures_price: float = 0,
        spot_order_id: str = "",
        futures_order_id: str = "",
        spot_status: str = "",
        futures_status: str = "",
        pnl_usd: float = 0,
        fees_usd: float = 0,
        notes: str = ""
    ):
        """Log a trade event to CSV."""
        try:
            spread_bps = 0
            if spot_price > 0 and futures_price > 0:
                spread_bps = ((futures_price - spot_price) / spot_price) * 10000

            with open(self.trades_file, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.utcnow().isoformat(),
                    event_type,
                    position_type,
                    f"{quantity:.8f}",
                    f"{spot_price:.2f}",
                    f"{futures_price:.2f}",
                    f"{spread_bps:.2f}",
                    spot_order_id,
                    futures_order_id,
                    spot_status,
                    futures_status,
                    f"{pnl_usd:.4f}",
                    f"{fees_usd:.4f}",
                    notes
                ])
        except Exception as e:
            logger.error("Failed to log trade: %s", e)

    def log_alert(
        self,
        level: str,  # INFO, WARNING, ERROR, CRITICAL
        category: str,  # ORDER_STATS, PATTERN_DETECTED, LEVERAGE, ORPHAN, TIMEOUT
        message: str,
        spot_attempts: int = 0,
        spot_failures: int = 0,
        futures_attempts: int = 0,
        futures_failures: int = 0,
        details: str = ""
    ):
        """Log an alert to CSV."""
        try:
            with open(self.alerts_file, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.utcnow().isoformat(),
                    level,
                    category,
                    message,
                    spot_attempts,
                    spot_failures,
                    futures_attempts,
                    futures_failures,
                    details
                ])
        except Exception as e:
            logger.error("Failed to log alert: %s", e)

    def log_startup(self, config: Dict[str, Any]):
        """Log startup configuration summary."""
        self.log_alert(
            level="INFO",
            category="STARTUP",
            message="Engine started",
            details=f"asset={config.get('asset')}, paper={config.get('paper_trading')}, "
                   f"pos_size=${config.get('position_size_usd')}, "
                   f"leverage={config.get('futures_leverage')}x, "
                   f"timeout={config.get('limit_order_timeout_sec')}s"
        )

    def log_order_stats(
        self,
        spot_attempts: int,
        spot_failures: int,
        futures_attempts: int,
        futures_failures: int
    ):
        """Log periodic order statistics."""
        spot_success = (1 - spot_failures / spot_attempts) * 100 if spot_attempts > 0 else 100
        fut_success = (1 - futures_failures / futures_attempts) * 100 if futures_attempts > 0 else 100

        self.log_alert(
            level="INFO",
            category="ORDER_STATS",
            message=f"Spot {spot_success:.0f}% success, Futures {fut_success:.0f}% success",
            spot_attempts=spot_attempts,
            spot_failures=spot_failures,
            futures_attempts=futures_attempts,
            futures_failures=futures_failures
        )

    def log_spot_failure_pattern(
        self,
        spot_attempts: int,
        spot_failures: int,
        futures_attempts: int,
        futures_failures: int
    ):
        """Log critical spot-only failure pattern detection."""
        self.log_alert(
            level="CRITICAL",
            category="PATTERN_DETECTED",
            message="Spot-only failure pattern detected!",
            spot_attempts=spot_attempts,
            spot_failures=spot_failures,
            futures_attempts=futures_attempts,
            futures_failures=futures_failures,
            details="Spot orders failing while futures succeed - check spot adapter/permissions"
        )

    def log_orphan_risk(
        self,
        filled_leg: str,  # SPOT or FUTURES
        failed_leg: str,
        quantity: float,
        recovery_action: str  # MAKER_RECOVERY, MARKET_CLOSE
    ):
        """Log orphan leg risk event."""
        self.log_alert(
            level="ERROR",
            category="ORPHAN_RISK",
            message=f"{filled_leg} filled but {failed_leg} failed",
            details=f"qty={quantity:.6f}, action={recovery_action}"
        )

    def log_timeout(self, order_type: str, timeout_sec: int, spot_status: str, futures_status: str):
        """Log order timeout event."""
        self.log_alert(
            level="WARNING",
            category="TIMEOUT",
            message=f"{order_type} order timed out after {timeout_sec}s",
            details=f"spot={spot_status}, futures={futures_status}"
        )

    def log_leverage_check(self, configured: int, actual: int, corrected: bool):
        """Log leverage verification result."""
        if configured != actual:
            self.log_alert(
                level="WARNING" if corrected else "ERROR",
                category="LEVERAGE",
                message=f"Leverage mismatch: config={configured}x, exchange={actual}x",
                details=f"corrected={corrected}"
            )
        else:
            self.log_alert(
                level="INFO",
                category="LEVERAGE",
                message=f"Leverage verified: {actual}x"
            )


# Global instance
_trade_logger: Optional[TradeLogger] = None


def get_trade_logger() -> TradeLogger:
    """Get or create the global trade logger instance."""
    global _trade_logger
    if _trade_logger is None:
        _trade_logger = TradeLogger()
    return _trade_logger
