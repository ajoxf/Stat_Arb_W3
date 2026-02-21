# Error & Trade Logging

## Overview

The logging system provides comprehensive event tracking through both Python logging and CSV files for post-analysis during unattended operation.

## File Locations
```
core/trade_logger.py    # CSV logging module (~238 lines)

logs/                   # Auto-created directory
├── trades_YYYYMMDD.csv
└── alerts_YYYYMMDD.csv
```

---

## Python Logging Configuration

### Setup in app.py

```python
import logging

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Console output
    ]
)

# Suppress noisy HTTP logs
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Get module loggers
logger = logging.getLogger(__name__)
```

### Log Levels

| Level | Usage |
|-------|-------|
| DEBUG | Detailed diagnostics (tick processing, price updates) |
| INFO | Normal operations (trades, connections, config changes) |
| WARNING | Recoverable issues (timeout, partial fill, cooldown active) |
| ERROR | Failures (order failed, connection lost) |
| CRITICAL | Severe issues (spot-only failure pattern, orphan risk) |

---

## CSV Trade Logger

### TradeLogger Class

```python
# core/trade_logger.py

import csv
import os
from datetime import datetime
from typing import Dict, Any, Optional

class TradeLogger:
    """CSV logging for trades and alerts."""

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        self._current_date: Optional[str] = None
        self._trades_file = None
        self._alerts_file = None
        self._trades_writer = None
        self._alerts_writer = None

    def _get_date_str(self) -> str:
        return datetime.utcnow().strftime('%Y%m%d')

    def _ensure_files(self) -> None:
        """Rotate files daily."""
        date_str = self._get_date_str()

        if date_str != self._current_date:
            self._close_files()
            self._current_date = date_str
            self._open_files()

    def _open_files(self) -> None:
        """Open CSV files for current date."""
        trades_path = os.path.join(self.log_dir, f"trades_{self._current_date}.csv")
        alerts_path = os.path.join(self.log_dir, f"alerts_{self._current_date}.csv")

        # Check if files exist for headers
        trades_exists = os.path.exists(trades_path)
        alerts_exists = os.path.exists(alerts_path)

        self._trades_file = open(trades_path, 'a', newline='')
        self._alerts_file = open(alerts_path, 'a', newline='')

        self._trades_writer = csv.writer(self._trades_file)
        self._alerts_writer = csv.writer(self._alerts_file)

        # Write headers if new file
        if not trades_exists:
            self._trades_writer.writerow([
                'timestamp', 'event_type', 'position_type', 'quantity',
                'spot_price', 'futures_price', 'spread_bps',
                'spot_order_id', 'futures_order_id',
                'spot_status', 'futures_status', 'pnl_usd', 'notes'
            ])

        if not alerts_exists:
            self._alerts_writer.writerow([
                'timestamp', 'level', 'category', 'message',
                'spot_attempts', 'spot_failures',
                'futures_attempts', 'futures_failures'
            ])

    def _close_files(self) -> None:
        """Close open files."""
        if self._trades_file:
            self._trades_file.close()
        if self._alerts_file:
            self._alerts_file.close()
```

### Log Trade

```python
def log_trade(
    self,
    event_type: str,          # ENTRY, EXIT, PARTIAL, ORPHAN_CLOSE
    position_type: str,       # LONG, SHORT
    quantity: float,
    spot_price: float,
    futures_price: float,
    spot_order_id: str = "",
    futures_order_id: str = "",
    spot_status: str = "",    # FILLED, FAILED, CANCELLED
    futures_status: str = "",
    pnl_usd: float = 0.0,
    notes: str = ""
) -> None:
    """Log a trade event."""
    self._ensure_files()

    spread_bps = ((futures_price - spot_price) / spot_price) * 10000

    self._trades_writer.writerow([
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
        notes
    ])
    self._trades_file.flush()
```

### Log Alert

```python
def log_alert(
    self,
    level: str,       # INFO, WARNING, ERROR, CRITICAL
    category: str,    # ORDER_STATS, PATTERN_DETECTED, LEVERAGE, ORPHAN, TIMEOUT
    message: str,
    spot_attempts: int = 0,
    spot_failures: int = 0,
    futures_attempts: int = 0,
    futures_failures: int = 0,
) -> None:
    """Log an alert event."""
    self._ensure_files()

    self._alerts_writer.writerow([
        datetime.utcnow().isoformat(),
        level,
        category,
        message,
        spot_attempts,
        spot_failures,
        futures_attempts,
        futures_failures
    ])
    self._alerts_file.flush()
```

### Specialized Log Methods

```python
def log_startup(self, config: Dict[str, Any]) -> None:
    """Log configuration at startup."""
    self.log_alert(
        level="INFO",
        category="STARTUP",
        message=f"Engine started: asset={config.get('asset')}, "
                f"paper={config.get('paper_trading')}, "
                f"position_size=${config.get('position_size_usd')}"
    )

def log_order_stats(
    self,
    spot_attempts: int,
    spot_failures: int,
    futures_attempts: int,
    futures_failures: int
) -> None:
    """Log periodic order execution statistics."""
    spot_success = spot_attempts - spot_failures
    futures_success = futures_attempts - futures_failures

    self.log_alert(
        level="INFO",
        category="ORDER_STATS",
        message=f"Spot: {spot_success}/{spot_attempts}, "
                f"Futures: {futures_success}/{futures_attempts}",
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
) -> None:
    """Log detection of spot-only failure pattern."""
    spot_rate = (spot_failures / spot_attempts * 100) if spot_attempts > 0 else 0
    futures_rate = (futures_failures / futures_attempts * 100) if futures_attempts > 0 else 0

    self.log_alert(
        level="CRITICAL",
        category="PATTERN_DETECTED",
        message=f"Spot-only failures: Spot {spot_rate:.0f}% fail, Futures {futures_rate:.0f}% fail",
        spot_attempts=spot_attempts,
        spot_failures=spot_failures,
        futures_attempts=futures_attempts,
        futures_failures=futures_failures
    )

def log_orphan_risk(
    self,
    filled_leg: str,    # "spot" or "futures"
    filled_qty: float,
    filled_price: float,
    recovery_attempted: bool,
    recovery_success: bool
) -> None:
    """Log orphan/leg risk event."""
    status = "recovered" if recovery_success else ("attempting" if recovery_attempted else "UNRESOLVED")

    self.log_alert(
        level="ERROR" if not recovery_success else "WARNING",
        category="ORPHAN",
        message=f"Orphan {filled_leg}: qty={filled_qty:.6f} @ {filled_price:.2f}, status={status}"
    )

def log_timeout(self, order_type: str, timeout_sec: int) -> None:
    """Log order timeout event."""
    self.log_alert(
        level="WARNING",
        category="TIMEOUT",
        message=f"{order_type} order timeout after {timeout_sec}s"
    )

def log_leverage_check(
    self,
    configured: int,
    actual: int,
    corrected: bool
) -> None:
    """Log leverage verification result."""
    if configured != actual:
        self.log_alert(
            level="WARNING" if corrected else "ERROR",
            category="LEVERAGE",
            message=f"Leverage mismatch: config={configured}x, exchange={actual}x, "
                    f"{'corrected' if corrected else 'NOT CORRECTED'}"
        )
```

---

## Singleton Access

```python
# Global instance
_trade_logger: Optional[TradeLogger] = None

def get_trade_logger() -> TradeLogger:
    """Get singleton trade logger instance."""
    global _trade_logger
    if _trade_logger is None:
        _trade_logger = TradeLogger()
    return _trade_logger
```

---

## Usage in Trading Engine

```python
# core/trading_engine.py

from core.trade_logger import get_trade_logger

async def _execute_entry_orders(self, trade, signal):
    csv_logger = get_trade_logger()

    try:
        spread_order = await self.order_executor.execute_entry(...)

        if spread_order.is_complete:
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
                notes=f"mode={self.config.order_execution_mode}"
            )
            return True
        else:
            csv_logger.log_trade(
                event_type="ENTRY_FAILED",
                position_type=signal.signal_type,
                quantity=trade.quantity,
                spot_price=trade.entry_spot_price,
                futures_price=trade.entry_futures_price,
                spot_status=spread_order.spot_leg.status.name,
                futures_status=spread_order.futures_leg.status.name,
            )
            return False

    except Exception as e:
        csv_logger.log_alert(
            level="ERROR",
            category="EXECUTION",
            message=f"Entry execution error: {str(e)}"
        )
        return False
```

---

## CSV File Examples

### trades_20240115.csv

```csv
timestamp,event_type,position_type,quantity,spot_price,futures_price,spread_bps,spot_order_id,futures_order_id,spot_status,futures_status,pnl_usd,notes
2024-01-15T10:30:45.123456,ENTRY,LONG,0.00150000,65432.00,65482.00,7.64,1234567890,9876543210,FILLED,FILLED,0.0000,mode=LIMIT
2024-01-15T10:45:32.654321,EXIT,LONG,0.00150000,65500.00,65520.00,3.05,1234567891,9876543211,FILLED,FILLED,0.4500,
2024-01-15T11:15:22.111111,ENTRY,SHORT,0.00200000,65100.00,65080.00,-3.07,1234567892,9876543212,FILLED,FILLED,0.0000,mode=LIMIT
2024-01-15T11:30:45.222222,ORPHAN_CLOSE,SHORT,0.00200000,65200.00,65190.00,-1.54,,9876543213,,FILLED,-0.2000,spot failed - closed futures
```

### alerts_20240115.csv

```csv
timestamp,level,category,message,spot_attempts,spot_failures,futures_attempts,futures_failures
2024-01-15T10:00:00.000000,INFO,STARTUP,"Engine started: asset=BTC, paper=False, position_size=$1000",0,0,0,0
2024-01-15T10:30:00.000000,INFO,ORDER_STATS,"Spot: 10/10, Futures: 10/10",10,0,10,0
2024-01-15T11:00:00.000000,WARNING,LEVERAGE,"Leverage mismatch: config=3x, exchange=1x, corrected",0,0,0,0
2024-01-15T11:15:30.000000,ERROR,ORPHAN,"Orphan spot: qty=0.002000 @ 65100.00, status=UNRESOLVED",12,1,12,0
2024-01-15T11:30:00.000000,CRITICAL,PATTERN_DETECTED,"Spot-only failures: Spot 50% fail, Futures 0% fail",14,7,14,0
```

---

## Log Analysis

### Python Analysis Script

```python
import pandas as pd

# Load trades
trades = pd.read_csv('logs/trades_20240115.csv')
trades['timestamp'] = pd.to_datetime(trades['timestamp'])

# Calculate statistics
total_pnl = trades['pnl_usd'].sum()
win_rate = (trades[trades['pnl_usd'] > 0].shape[0] /
            trades[trades['event_type'] == 'EXIT'].shape[0] * 100)

print(f"Total P&L: ${total_pnl:.2f}")
print(f"Win Rate: {win_rate:.1f}%")

# Load alerts
alerts = pd.read_csv('logs/alerts_20240115.csv')
critical_alerts = alerts[alerts['level'] == 'CRITICAL']
print(f"Critical alerts: {len(critical_alerts)}")
```

### Shell Analysis

```bash
# Count entries/exits
grep ",ENTRY," logs/trades_*.csv | wc -l
grep ",EXIT," logs/trades_*.csv | wc -l

# Find orphan events
grep "ORPHAN" logs/alerts_*.csv

# Sum P&L
awk -F',' 'NR>1 {sum += $12} END {print "Total P&L: $" sum}' logs/trades_*.csv

# Critical alerts today
grep "CRITICAL" logs/alerts_$(date +%Y%m%d).csv
```

---

## File Rotation

Files are automatically rotated daily:
- `trades_20240115.csv` → `trades_20240116.csv`
- `alerts_20240115.csv` → `alerts_20240116.csv`

### Manual Cleanup

```bash
# Keep last 30 days
find logs/ -name "*.csv" -mtime +30 -delete

# Archive old files
tar -czf logs_archive_$(date +%Y%m).tar.gz logs/trades_2024*.csv logs/alerts_2024*.csv
```

---

## Monitoring Integration

### Watch Logs in Real-Time

```bash
# Watch trades
tail -f logs/trades_$(date +%Y%m%d).csv

# Watch alerts
tail -f logs/alerts_$(date +%Y%m%d).csv | grep -E "ERROR|CRITICAL"
```

### Alert on Critical Events

```bash
#!/bin/bash
# monitor_alerts.sh

while true; do
    if tail -1 logs/alerts_$(date +%Y%m%d).csv | grep -q "CRITICAL"; then
        # Send notification (email, Slack, etc.)
        echo "CRITICAL ALERT DETECTED" | mail -s "Trading Alert" admin@example.com
    fi
    sleep 60
done
```
