"""
Telegram Bot Integration - Real-time trade notifications and interactive commands.

Sends trade entry/exit alerts, signals, and errors to a Telegram chat.
Supports interactive commands: /status, /positions, /trades, /balance, /pnl, /eod, /closeall

Only requires the 'requests' library (already in requirements.txt).
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """
    Send an HTML-formatted message to a Telegram chat.

    Returns True on success, False on failure (errors are logged, never raised).
    """
    if not token or not chat_id:
        return False
    try:
        url = f"{TELEGRAM_API_BASE.format(token=token)}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        logger.warning("Telegram sendMessage failed (%d): %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.error("Telegram sendMessage error: %s", e)
        return False


class TelegramNotifier:
    """
    Sends trade notifications to a Telegram chat and handles interactive commands.

    Instantiate once and keep alive for the session. Call update_config() whenever
    the trading config changes. Call start_polling() to begin command handling.
    """

    def __init__(self):
        self._enabled = False
        self._token = ""
        self._chat_id = ""
        self._notify_trades = True
        self._notify_signals = False
        self._notify_errors = True

        # Callbacks: set by app.py after engine is available
        self.get_status_cb: Optional[Callable[[], Dict[str, Any]]] = None
        self.get_trades_cb: Optional[Callable[[], list]] = None
        self.get_balance_cb: Optional[Callable[[], Dict[str, Any]]] = None
        self.close_all_cb: Optional[Callable[[], Dict[str, Any]]] = None

        # Polling state
        self._poll_thread: Optional[threading.Thread] = None
        self._polling = False
        self._last_update_id = 0

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def update_config(self, config) -> None:
        """Update from a TradingConfig object."""
        self._enabled = getattr(config, 'telegram_enabled', False)
        self._token = getattr(config, 'telegram_bot_token', '')
        self._chat_id = getattr(config, 'telegram_chat_id', '')
        self._notify_trades = getattr(config, 'telegram_notify_trades', True)
        self._notify_signals = getattr(config, 'telegram_notify_signals', False)
        self._notify_errors = getattr(config, 'telegram_notify_errors', True)

    def is_ready(self) -> bool:
        return bool(self._enabled and self._token and self._chat_id)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def notify_trade_entry(self, trade, signal=None) -> None:
        """Send a trade entry notification."""
        if not self.is_ready() or not self._notify_trades:
            return
        try:
            direction = trade.position_type
            C = 13
            SEP = "\u2500" * 24

            entry_str = (
                trade.entry_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                if trade.entry_time else "—"
            )
            placed_str = (
                trade.entry_placed_at.strftime("%H:%M:%S.%f")[:-3] + " UTC"
                if trade.entry_placed_at else ("simulated" if trade.is_paper else "—")
            )
            filled_str = (
                trade.entry_filled_at.strftime("%H:%M:%S.%f")[:-3] + " UTC"
                if trade.entry_filled_at else ("simulated" if trade.is_paper else "—")
            )
            latency_str = (
                f"{trade.entry_latency_ms:.0f} ms"
                if trade.entry_latency_ms is not None else ("simulated" if trade.is_paper else "—")
            )

            spread_bps = 0.0
            if trade.entry_spot_price > 0:
                spread_bps = (trade.entry_spread / trade.entry_spot_price) * 10000

            leverage_x = round(trade.notional_usd / trade.margin_usd) if trade.margin_usd > 0 else 0
            margin_str = (
                f"${trade.margin_usd:,.2f}  ({leverage_x}x)"
                if leverage_x > 0 else f"${trade.margin_usd:,.2f}"
            )

            rows = [
                f"{'ID':<{C}}#{trade.id or 'pending'}",
                f"{'Entry Time':<{C}}{entry_str}",
                SEP,
                f"{'Lots':<{C}}{trade.quantity:.6f} {trade.asset}",
                f"{'Notional':<{C}}${trade.notional_usd:,.2f}",
                f"{'Margin Req':<{C}}{margin_str}",
                SEP,
                f"{'Spot Entry':<{C}}${trade.entry_spot_price:,.4f}",
                f"{'Fut Entry':<{C}}${trade.entry_futures_price:,.4f}",
                f"{'Spread':<{C}}{trade.entry_spread:+.4f}  ({spread_bps:+.2f} bps)",
                SEP,
                f"{'Z-score':<{C}}{trade.entry_zscore:+.4f}",
            ]
            if signal:
                std = getattr(signal, 'spread_std', None)
                spread_mean = getattr(signal, 'spread_mean', None)
                hurst = getattr(signal, 'hurst', None)
                hurst_ok = getattr(signal, 'hurst_ok', None)
                regime = getattr(signal, 'regime', None)
                if std:
                    rows.append(f"{'Spread SD':<{C}}{std:.6f}")
                if spread_mean:
                    rows.append(f"{'Spread Mean':<{C}}{spread_mean:+.4f}")
                if hurst is not None:
                    hurst_tag = "  [mean-rev]" if hurst_ok else "  [trending]" if hurst_ok is False else ""
                    rows.append(f"{'Hurst':<{C}}{hurst:.4f}{hurst_tag}")
                if regime:
                    rows.append(f"{'Regime':<{C}}{regime}")
            rows += [
                SEP,
                f"{'Orders at':<{C}}{placed_str}",
                f"{'Filled at':<{C}}{filled_str}",
                f"{'Latency':<{C}}{latency_str}",
            ]
            parts = [
                f"<b>TRADE ENTRY  ·  {direction} {trade.asset}</b>",
                "<pre>" + "\n".join(rows) + "</pre>",
            ]
            if trade.is_paper:
                parts.append("<i>Paper Trading</i>")
            self._send("\n".join(parts))
        except Exception as e:
            logger.error("Error building trade entry notification: %s", e)

    def notify_trade_exit(self, trade) -> None:
        """Send a trade exit notification with full P&L breakdown."""
        if not self.is_ready() or not self._notify_trades:
            return
        try:
            direction = trade.position_type
            exit_reason = trade.exit_reason or "EXIT"
            C = 14
            SEP = "\u2500" * 24

            exit_str = "—"
            duration_str = "—"
            if trade.exit_time:
                exit_str = trade.exit_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                if trade.entry_time:
                    total_sec = int((trade.exit_time - trade.entry_time).total_seconds())
                    if total_sec < 3600:
                        duration_str = f"{total_sec // 60}m {total_sec % 60}s"
                    elif total_sec < 86400:
                        duration_str = f"{total_sec // 3600}h {(total_sec % 3600) // 60}m"
                    else:
                        duration_str = f"{total_sec // 86400}d {(total_sec % 86400) // 3600}h"

            placed_str = (
                trade.exit_placed_at.strftime("%H:%M:%S.%f")[:-3] + " UTC"
                if trade.exit_placed_at else ("simulated" if trade.is_paper else "—")
            )
            filled_str = (
                trade.exit_filled_at.strftime("%H:%M:%S.%f")[:-3] + " UTC"
                if trade.exit_filled_at else ("simulated" if trade.is_paper else "—")
            )
            latency_str = (
                f"{trade.exit_latency_ms:.0f} ms"
                if trade.exit_latency_ms is not None else ("simulated" if trade.is_paper else "—")
            )

            entry_spread = trade.entry_spread
            exit_spread = trade.exit_spread
            spread_change = (
                (exit_spread - entry_spread) if direction == "SHORT"
                else (entry_spread - exit_spread)
            )
            gross_pnl = spread_change * trade.quantity
            est_fees = trade.notional_usd * 0.0020
            result = "PROFIT" if trade.pnl_usd >= 0 else "LOSS"

            rows = [
                f"{'Reason':<{C}}{exit_reason}",
                f"{'Duration':<{C}}{duration_str}",
                f"{'Exit Time':<{C}}{exit_str}",
                SEP,
                f"{'Spot Entry':<{C}}${trade.entry_spot_price:,.4f}",
                f"{'Spot Exit':<{C}}${trade.exit_spot_price:,.4f}",
                f"{'Fut Entry':<{C}}${trade.entry_futures_price:,.4f}",
                f"{'Fut Exit':<{C}}${trade.exit_futures_price:,.4f}",
                SEP,
                f"{'Entry Spread':<{C}}{entry_spread:+.4f}  (Z: {trade.entry_zscore:+.4f})",
                f"{'Exit Spread':<{C}}{exit_spread:+.4f}  (Z: {trade.exit_zscore:+.4f})",
                f"{'Spread Chg':<{C}}{spread_change:+.4f}",
                SEP,
                f"{'Orders at':<{C}}{placed_str}",
                f"{'Filled at':<{C}}{filled_str}",
                f"{'Latency':<{C}}{latency_str}",
                SEP,
                f"{'Gross PnL':<{C}}${gross_pnl:+.4f}",
                f"{'Est. Fees':<{C}}-${est_fees:.4f}",
                f"{'Net PnL':<{C}}${trade.pnl_usd:+.4f}  ({trade.pnl_percent:+.4f}%)",
            ]
            parts = [
                f"<b>TRADE EXIT  ·  {direction} {trade.asset}  ·  {result}</b>",
                "<pre>" + "\n".join(rows) + "</pre>",
            ]
            if trade.is_paper:
                parts.append("<i>Paper Trading</i>")
            self._send("\n".join(parts))
        except Exception as e:
            logger.error("Error building trade exit notification: %s", e)

    def notify_signal(self, signal) -> None:
        """Send a trading signal notification (non-NONE signals only)."""
        if not self.is_ready() or not self._notify_signals:
            return
        if not signal or signal.signal_type == "NONE":
            return
        try:
            sig_type = signal.signal_type
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            C = 13
            rows = [
                f"{'Z-score':<{C}}{signal.zscore:+.4f}",
                f"{'Spread':<{C}}{signal.spread:+.6f}",
            ]
            spread_mean = getattr(signal, 'spread_mean', None)
            spread_std = getattr(signal, 'spread_std', None)
            hurst = getattr(signal, 'hurst', None)
            hurst_ok = getattr(signal, 'hurst_ok', None)
            std_ok = getattr(signal, 'std_filter_ok', None)
            if spread_mean:
                rows.append(f"{'Spread Mean':<{C}}{spread_mean:+.6f}")
            if spread_std:
                rows.append(f"{'Spread SD':<{C}}{spread_std:.6f}")
            if hurst is not None:
                hurst_tag = "  [mean-rev]" if hurst_ok else "  [trending]" if hurst_ok is False else ""
                rows.append(f"{'Hurst':<{C}}{hurst:.4f}{hurst_tag}")
            filters = []
            if hurst_ok is not None:
                filters.append(f"hurst={'OK' if hurst_ok else 'FAIL'}")
            if std_ok is not None:
                filters.append(f"std={'OK' if std_ok else 'FAIL'}")
            if filters:
                rows.append(f"{'Filters':<{C}}{', '.join(filters)}")
            rows += [
                f"{'Regime':<{C}}{getattr(signal, 'regime', 'N/A')}",
                f"{'Time':<{C}}{ts}",
            ]
            self._send(
                f"<b>SIGNAL  ·  {sig_type}</b>\n"
                "<pre>" + "\n".join(rows) + "</pre>"
            )
        except Exception as e:
            logger.error("Error building signal notification: %s", e)

    def notify_error(self, error_msg: str) -> None:
        """Send a critical error notification."""
        if not self.is_ready() or not self._notify_errors:
            return
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self._send(
                "<b>SYSTEM ERROR</b>\n"
                f"<pre>{ts}\n\n{error_msg[:500]}</pre>"
            )
        except Exception as e:
            logger.error("Error building error notification: %s", e)

    def notify_test(self) -> bool:
        """Send a test notification. Returns True if successful."""
        if not self._token or not self._chat_id:
            return False
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        C = 13
        cmd_rows = [
            f"{'/status':<{C}}engine &amp; algo state",
            f"{'/positions':<{C}}open positions",
            f"{'/trades':<{C}}recent closed trades",
            f"{'/balance':<{C}}account balance",
            f"{'/pnl':<{C}}P&amp;L summary",
            f"{'/eod':<{C}}end-of-day report",
            f"{'/closeall':<{C}}emergency: close all",
        ]
        msg = (
            "<b>Nexus Stat-Arb</b>\n"
            f"Connected.  <i>{ts}</i>\n"
            "<pre>" + "\n".join(cmd_rows) + "</pre>"
        )
        return send_telegram_message(self._token, self._chat_id, msg)

    # ------------------------------------------------------------------
    # Command Polling
    # ------------------------------------------------------------------

    def start_polling(self) -> None:
        """Start background thread that polls Telegram for commands."""
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._polling = True
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-poll",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info("Telegram command polling started")

    def stop_polling(self) -> None:
        """Stop the command polling thread."""
        self._polling = False
        if self._poll_thread:
            self._poll_thread.join(timeout=3)
        logger.info("Telegram command polling stopped")

    def _poll_loop(self) -> None:
        """Long-poll Telegram getUpdates endpoint for commands."""
        while self._polling:
            if not self.is_ready():
                time.sleep(5)
                continue
            try:
                url = f"{TELEGRAM_API_BASE.format(token=self._token)}/getUpdates"
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                }
                resp = requests.get(url, params=params, timeout=35)
                if resp.status_code != 200:
                    time.sleep(5)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]
                    self._handle_update(update)

            except requests.exceptions.Timeout:
                pass  # Normal for long-polling
            except Exception as e:
                logger.error("Telegram poll error: %s", e)
                time.sleep(10)

    def _handle_update(self, update: Dict[str, Any]) -> None:
        """Route an incoming Telegram update to the appropriate command handler."""
        msg = update.get("message", {})
        if not msg:
            return

        # Security: only respond to the authorised chat ID
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            logger.warning("Ignoring message from unknown chat_id: %s", chat_id)
            return

        text = msg.get("text", "").strip().lower()
        command = text.split("@")[0]  # Strip bot username suffix if present

        handlers = {
            "/start": self._cmd_start,
            "/help": self._cmd_start,
            "/status": self._cmd_status,
            "/positions": self._cmd_positions,
            "/trades": self._cmd_trades,
            "/balance": self._cmd_balance,
            "/pnl": self._cmd_pnl,
            "/eod": self._cmd_eod,
            "/closeall": self._cmd_closeall,
        }

        handler = handlers.get(command)
        if handler:
            try:
                handler()
            except Exception as e:
                logger.error("Telegram command handler error (%s): %s", command, e)
                self._send(f"Error handling command {command}: {e}")
        elif text.startswith("/"):
            self._send(
                "Unknown command. Available:\n"
                "/status /positions /trades /balance /pnl /eod /closeall"
            )

    # ------------------------------------------------------------------
    # Command Handlers
    # ------------------------------------------------------------------

    def _cmd_start(self) -> None:
        """Handle /start and /help commands."""
        C = 13
        cmd_rows = [
            f"{'/status':<{C}}engine &amp; algo state",
            f"{'/positions':<{C}}open positions",
            f"{'/trades':<{C}}recent closed trades",
            f"{'/balance':<{C}}account balance",
            f"{'/pnl':<{C}}P&amp;L summary",
            f"{'/eod':<{C}}end-of-day report",
            f"{'/closeall':<{C}}emergency: close all",
        ]
        self._send(
            "<b>Nexus Stat-Arb Bot</b>\n"
            "Notifications active.\n"
            "<pre>" + "\n".join(cmd_rows) + "</pre>"
        )

    def _cmd_status(self) -> None:
        """Handle /status command."""
        status = self.get_status_cb() if self.get_status_cb else {}
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        is_running = status.get("is_running", False)
        algo_enabled = status.get("algo_enabled", False)
        paper = status.get("paper_trading", True)
        position = status.get("position", "NONE")
        asset = status.get("asset", "N/A")
        error = status.get("error", "")

        sig = status.get("signal") or {}
        zscore = sig.get("zscore", 0.0)
        regime = sig.get("regime", "N/A")

        C = 10
        rows = [
            f"{'Engine':<{C}}{'Running' if is_running else 'Stopped'}",
            f"{'Algo':<{C}}{'Enabled' if algo_enabled else 'Disabled'}",
            f"{'Mode':<{C}}{'Paper' if paper else 'Live'}",
            f"{'Asset':<{C}}{asset}",
            f"{'Position':<{C}}{position}",
            f"{'Z-score':<{C}}{zscore:+.4f}",
            f"{'Regime':<{C}}{regime}",
        ]
        if error:
            rows += ["\u2500" * 24, f"Error: {error[:200]}"]
        self._send(
            f"<b>SYSTEM STATUS  ·  {ts}</b>\n"
            "<pre>" + "\n".join(rows) + "</pre>"
        )

    def _cmd_positions(self) -> None:
        """Handle /positions command."""
        status = self.get_status_cb() if self.get_status_cb else {}
        position = status.get("position", "NONE")
        open_trade = status.get("open_trade")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        if position == "NONE" or not open_trade:
            self._send(
                f"<b>OPEN POSITIONS  ·  {ts}</b>\n"
                "<pre>No open positions.</pre>"
            )
            return

        asset = status.get("asset", "N/A")
        entry_time = open_trade.get("entry_time", "—")
        entry_spot = open_trade.get("entry_spot_price", 0)
        entry_fut = open_trade.get("entry_futures_price", 0)
        entry_spread = open_trade.get("entry_spread", 0)
        entry_z = open_trade.get("entry_zscore", 0)
        notional = open_trade.get("notional_usd", 0)
        qty = open_trade.get("quantity", 0)

        sig = status.get("signal") or {}
        current_z = sig.get("zscore", 0.0)
        current_spread = sig.get("spread", 0.0)

        margin_usd = open_trade.get("margin_usd", 0)
        entry_latency = open_trade.get("entry_latency_ms")
        placed_str = open_trade.get("entry_placed_at") or ("simulated" if open_trade.get("is_paper") else "—")
        if placed_str and len(placed_str) > 10:
            placed_str = placed_str[11:23] + " UTC"  # ISO → HH:MM:SS.mmm UTC
        filled_str = open_trade.get("entry_filled_at") or ("simulated" if open_trade.get("is_paper") else "—")
        if filled_str and len(filled_str) > 10:
            filled_str = filled_str[11:23] + " UTC"
        latency_str = f"{entry_latency:.0f} ms" if entry_latency is not None else "—"

        leverage_x = round(notional / margin_usd) if margin_usd > 0 else 0
        margin_str = f"${margin_usd:,.2f}  ({leverage_x}x)" if leverage_x > 0 else f"${margin_usd:,.2f}"

        spot_tick = status.get("spot_tick") or {}
        futures_tick = status.get("futures_tick") or {}
        current_spot = spot_tick.get("last", 0)
        current_fut = futures_tick.get("last", 0)

        C = 14
        SEP = "\u2500" * 24
        rows = [
            f"{position} {asset}",
            "",
            f"{'Lots':<{C}}{qty:.6f} {asset}",
            f"{'Notional':<{C}}${notional:,.2f}",
            f"{'Margin Req':<{C}}{margin_str}",
            f"{'Entry Time':<{C}}{entry_time}",
            SEP,
            f"{'Spot Entry':<{C}}${entry_spot:,.4f}",
            f"{'Fut Entry':<{C}}${entry_fut:,.4f}",
            f"{'Entry Spread':<{C}}{entry_spread:+.4f}  (Z: {entry_z:+.4f})",
            SEP,
        ]
        if current_spot:
            rows.append(f"{'Spot Now':<{C}}${current_spot:,.4f}")
        if current_fut:
            rows.append(f"{'Fut Now':<{C}}${current_fut:,.4f}")
        rows.append(f"{'Spread Now':<{C}}{current_spread:+.4f}  (Z: {current_z:+.4f})")
        rows += [
            SEP,
            f"{'Orders at':<{C}}{placed_str}",
            f"{'Filled at':<{C}}{filled_str}",
            f"{'Latency':<{C}}{latency_str}",
        ]
        self._send(
            f"<b>OPEN POSITIONS  ·  {ts}</b>\n"
            "<pre>" + "\n".join(rows) + "</pre>"
        )

    def _cmd_trades(self) -> None:
        """Handle /trades command - show 5 most recent closed trades."""
        trades = self.get_trades_cb() if self.get_trades_cb else []
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        # Filter to closed trades only, most recent first
        closed = [t for t in trades if not t.get("is_open", True)][:5]

        if not closed:
            self._send(
                f"<b>RECENT TRADES  ·  {ts}</b>\n"
                "<pre>No closed trades yet.</pre>"
            )
            return

        SEP = "\u2500" * 24
        rows = []
        for i, t in enumerate(closed):
            if i > 0:
                rows.append(SEP)
            pnl = t.get("pnl_usd", 0)
            pct = t.get("pnl_percent", 0)
            result = "PROFIT" if pnl >= 0 else "LOSS"
            rows += [
                f"#{t.get('id')}  {t.get('position_type')} {t.get('asset')}",
                f"{'PnL':<7}${pnl:+.2f}  ({pct:+.2f}%)  {result}",
                f"{'Exit':<7}{t.get('exit_reason')}",
                f"{'Z':<7}{t.get('entry_zscore', 0):+.2f} -> {t.get('exit_zscore', 0):+.2f}",
            ]
        self._send(
            f"<b>RECENT TRADES  ·  {ts}</b>\n"
            "<pre>" + "\n".join(rows) + "</pre>"
        )

    def _cmd_balance(self) -> None:
        """Handle /balance command."""
        balance_data = self.get_balance_cb() if self.get_balance_cb else {}
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        if not balance_data or not balance_data.get("connected"):
            self._send(
                f"<b>ACCOUNT BALANCE  ·  {ts}</b>\n"
                "<pre>Exchange not connected or API keys not configured.</pre>"
            )
            return

        equity = balance_data.get("total_equity", 0)
        available = balance_data.get("available_margin", 0)
        margin_used = balance_data.get("margin_used", 0)
        margin_ratio = balance_data.get("margin_ratio", 0)
        upnl = balance_data.get("unrealized_pnl", 0)
        health = balance_data.get("margin_health", "N/A")
        exchange = balance_data.get("exchange", "N/A")
        mode = "Demo" if balance_data.get("is_demo") else "Live"

        C = 12
        rows = [
            f"{'Exchange':<{C}}{exchange}  ({mode})",
            f"{'Equity':<{C}}${equity:,.2f}",
            f"{'Available':<{C}}${available:,.2f}",
            f"{'Used':<{C}}${margin_used:,.2f}",
            f"{'Margin':<{C}}{margin_ratio:.1f}%  [{health}]",
            f"{'Unrealized':<{C}}${upnl:+.2f}",
        ]
        self._send(
            f"<b>ACCOUNT BALANCE  ·  {ts}</b>\n"
            "<pre>" + "\n".join(rows) + "</pre>"
        )

    def _cmd_pnl(self) -> None:
        """Handle /pnl command - comprehensive P&L summary."""
        trades = self.get_trades_cb() if self.get_trades_cb else []
        balance_data = self.get_balance_cb() if self.get_balance_cb else {}
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        closed = [t for t in trades if not t.get("is_open", True)]
        total_pnl = sum(t.get("pnl_usd", 0) for t in closed)
        winners = [t for t in closed if t.get("pnl_usd", 0) > 0]
        losers = [t for t in closed if t.get("pnl_usd", 0) <= 0]

        win_rate = (len(winners) / len(closed) * 100) if closed else 0
        avg_win = (sum(t["pnl_usd"] for t in winners) / len(winners)) if winners else 0
        avg_loss = (sum(t["pnl_usd"] for t in losers) / len(losers)) if losers else 0

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_trades = [t for t in closed if (t.get("exit_time") or "").startswith(today)]
        today_pnl = sum(t.get("pnl_usd", 0) for t in today_trades)
        upnl = balance_data.get("unrealized_pnl", 0)

        C = 15
        SEP = "\u2500" * 24
        rows = [
            f"{'Closed Trades':<{C}}{len(closed)}",
            f"{'Win Rate':<{C}}{win_rate:.1f}%  ({len(winners)}W / {len(losers)}L)",
            f"{'Avg Win':<{C}}${avg_win:+.2f}",
            f"{'Avg Loss':<{C}}${avg_loss:+.2f}",
            SEP,
            f"{'Today':<{C}}${today_pnl:+.2f}  ({len(today_trades)} trades)",
            f"{'All-time':<{C}}${total_pnl:+.2f}",
            f"{'Unrealized':<{C}}${upnl:+.2f}",
        ]
        self._send(
            f"<b>P&amp;L SUMMARY  ·  {ts}</b>\n"
            "<pre>" + "\n".join(rows) + "</pre>"
        )

    def _cmd_eod(self) -> None:
        """Handle /eod command - end-of-day summary."""
        trades = self.get_trades_cb() if self.get_trades_cb else []
        balance_data = self.get_balance_cb() if self.get_balance_cb else {}
        status = self.get_status_cb() if self.get_status_cb else {}
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        closed = [t for t in trades if not t.get("is_open", True)]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_closed = [t for t in closed if (t.get("exit_time") or "").startswith(today)]
        today_pnl = sum(t.get("pnl_usd", 0) for t in today_closed)
        today_wins = sum(1 for t in today_closed if t.get("pnl_usd", 0) > 0)

        equity = balance_data.get("total_equity", 0)
        upnl = balance_data.get("unrealized_pnl", 0)
        position = status.get("position", "NONE")
        asset = status.get("asset", "N/A")

        sig = status.get("signal") or {}
        regime = sig.get("regime", "N/A")
        zscore = sig.get("zscore", 0.0)

        C = 12
        SEP = "\u2500" * 24
        rows = [
            f"{'Trades':<{C}}{len(today_closed)}  ({today_wins} wins)",
            f"{'PnL':<{C}}${today_pnl:+.2f}",
            SEP,
            f"{'Equity':<{C}}${equity:,.2f}",
            f"{'Unrealized':<{C}}${upnl:+.2f}",
            SEP,
            f"{'Position':<{C}}{position}  ({asset})",
            f"{'Z-score':<{C}}{zscore:+.4f}",
            f"{'Regime':<{C}}{regime}",
        ]
        self._send(
            f"<b>END OF DAY  ·  {ts}</b>\n"
            "<pre>" + "\n".join(rows) + "</pre>"
        )

    def _cmd_closeall(self) -> None:
        """Handle /closeall emergency command - immediately close all open positions."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        if not self.close_all_cb:
            self._send(
                f"<b>EMERGENCY CLOSE  ·  {ts}</b>\n"
                "<pre>Not configured.</pre>"
            )
            return
        try:
            result = self.close_all_cb()
            C = 12
            if result.get("success"):
                rows = [f"{'Status':<{C}}Executed"]
                closed_count = result.get("closed_count")
                if closed_count is not None:
                    rows.append(f"{'Closed':<{C}}{closed_count} position(s)")
                detail = result.get("message", "")
                if detail:
                    rows.append(f"{'Info':<{C}}{detail[:120]}")
            else:
                rows = [
                    f"{'Status':<{C}}FAILED",
                    f"{'Error':<{C}}{result.get('error', 'Unknown error')[:120]}",
                ]
            self._send(
                f"<b>EMERGENCY CLOSE  ·  {ts}</b>\n"
                "<pre>" + "\n".join(rows) + "</pre>"
            )
        except Exception as e:
            self._send(
                f"<b>EMERGENCY CLOSE  ·  {ts}</b>\n"
                f"<pre>Error: {e}</pre>"
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send(self, text: str) -> None:
        """Send a message in a background thread to avoid blocking the caller."""
        if not self.is_ready():
            return
        token = self._token
        chat_id = self._chat_id

        def _do_send():
            send_telegram_message(token, chat_id, text)

        t = threading.Thread(target=_do_send, daemon=True)
        t.start()


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """Return the global TelegramNotifier singleton (created on first call)."""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
