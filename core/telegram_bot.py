"""
Telegram Bot Integration - Real-time trade notifications and interactive commands.

Sends trade entry/exit alerts, signals, and errors to a Telegram chat.
Supports interactive commands: /status, /positions, /trades, /balance, /pnl, /eod

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
            direction = trade.position_type  # LONG or SHORT
            direction_icon = "" if direction == "LONG" else ""
            entry_time_str = ""
            if trade.entry_time:
                entry_time_str = trade.entry_time.strftime("%Y-%m-%d %H:%M:%S UTC")

            spread_bps = 0.0
            if trade.entry_spot_price > 0:
                spread_bps = (trade.entry_spread / trade.entry_spot_price) * 10000

            msg_lines = [
                f"{direction_icon} <b>TRADE ENTRY — {direction} {trade.asset}</b>",
                "",
                f"<b>Time:</b> {entry_time_str}",
                f"<b>Trade ID:</b> #{trade.id or 'pending'}",
                f"<b>Direction:</b> {direction}",
                f"<b>Size:</b> {trade.quantity:.6f} {trade.asset} (${trade.notional_usd:,.2f})",
                "",
                f"<b>Spot Price:</b>  ${trade.entry_spot_price:,.4f}",
                f"<b>Futures Price:</b> ${trade.entry_futures_price:,.4f}",
                f"<b>Spread:</b> {trade.entry_spread:+.4f} ({spread_bps:+.2f} bps)",
                f"<b>Z-score:</b> {trade.entry_zscore:+.4f}",
            ]

            if signal:
                z_entry = getattr(signal, 'spread_std', None)
                if z_entry:
                    msg_lines.append(f"<b>Spread Std:</b> {z_entry:.6f}")
                msg_lines.append(f"<b>Regime:</b> {getattr(signal, 'regime', 'N/A')}")

            if trade.is_paper:
                msg_lines.append("")
                msg_lines.append("<i>Mode: Paper Trading</i>")

            self._send("\n".join(msg_lines))
        except Exception as e:
            logger.error("Error building trade entry notification: %s", e)

    def notify_trade_exit(self, trade) -> None:
        """Send a trade exit notification with full P&L breakdown."""
        if not self.is_ready() or not self._notify_trades:
            return
        try:
            direction = trade.position_type
            exit_reason = trade.exit_reason or "EXIT"

            icon = "" if trade.pnl_usd >= 0 else ""
            direction_icon = "" if direction == "LONG" else ""

            exit_time_str = ""
            duration_str = ""
            if trade.exit_time:
                exit_time_str = trade.exit_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                if trade.entry_time:
                    delta = trade.exit_time - trade.entry_time
                    total_sec = int(delta.total_seconds())
                    if total_sec < 3600:
                        duration_str = f"{total_sec // 60}m {total_sec % 60}s"
                    elif total_sec < 86400:
                        duration_str = f"{total_sec // 3600}h {(total_sec % 3600) // 60}m"
                    else:
                        duration_str = f"{delta.days}d {total_sec % 86400 // 3600}h"

            # Spread-based P&L components
            entry_spread = trade.entry_spread
            exit_spread = trade.exit_spread
            spread_change = (exit_spread - entry_spread) if direction == "SHORT" else (entry_spread - exit_spread)
            gross_pnl = spread_change * trade.quantity

            # Fee estimate (rough: 2× entry + 2× exit, using notional)
            # We don't store per-trade fees so we approximate
            est_fee_pct = 0.0020  # ~20 bps round-trip (conservative estimate)
            est_fees = trade.notional_usd * est_fee_pct

            msg_lines = [
                f"{icon} <b>TRADE EXIT — {direction_icon} {direction} {trade.asset}</b>",
                "",
                f"<b>Exit Time:</b> {exit_time_str}",
                f"<b>Duration:</b> {duration_str}",
                f"<b>Exit Reason:</b> {exit_reason}",
                "",
                "<b>Prices:</b>",
                f"  Entry Spot:    ${trade.entry_spot_price:,.4f}",
                f"  Exit Spot:     ${trade.exit_spot_price:,.4f}",
                f"  Entry Futures: ${trade.entry_futures_price:,.4f}",
                f"  Exit Futures:  ${trade.exit_futures_price:,.4f}",
                "",
                "<b>Spread:</b>",
                f"  Entry: {entry_spread:+.4f}",
                f"  Exit:  {exit_spread:+.4f}",
                f"  Entry Z-score: {trade.entry_zscore:+.4f}",
                f"  Exit Z-score:  {trade.exit_zscore:+.4f}",
                "",
                "<b>P&amp;L:</b>",
                f"  Gross:    ${gross_pnl:+.4f}",
                f"  Est. Fees: -${est_fees:.4f}",
                f"  <b>Net:      ${trade.pnl_usd:+.4f} ({trade.pnl_percent:+.4f}%)</b>",
            ]

            if trade.is_paper:
                msg_lines.append("")
                msg_lines.append("<i>Mode: Paper Trading</i>")

            self._send("\n".join(msg_lines))
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
            icons = {"LONG": "", "SHORT": "", "EXIT": "⏹️", "STOP_LOSS": ""}
            icon = icons.get(sig_type, "")
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

            msg = (
                f"{icon} <b>SIGNAL: {sig_type}</b>\n"
                f"<b>Z-score:</b> {signal.zscore:+.4f}  |  "
                f"<b>Spread:</b> {signal.spread:+.6f}\n"
                f"<b>Regime:</b> {getattr(signal, 'regime', 'N/A')}  |  "
                f"<b>Time:</b> {ts}"
            )
            self._send(msg)
        except Exception as e:
            logger.error("Error building signal notification: %s", e)

    def notify_error(self, error_msg: str) -> None:
        """Send a critical error notification."""
        if not self.is_ready() or not self._notify_errors:
            return
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            msg = (
                f" <b>SYSTEM ERROR</b>\n"
                f"<b>Time:</b> {ts}\n"
                f"<b>Error:</b> {error_msg[:500]}"
            )
            self._send(msg)
        except Exception as e:
            logger.error("Error building error notification: %s", e)

    def notify_test(self) -> bool:
        """Send a test notification. Returns True if successful."""
        if not self._token or not self._chat_id:
            return False
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = (
            f" <b>Nexus Stat-Arb — Telegram Connected</b>\n\n"
            f"Notifications are active.\n"
            f"<b>Time:</b> {ts}\n\n"
            f"<b>Available commands:</b>\n"
            f"/status — engine status\n"
            f"/positions — open positions\n"
            f"/trades — recent trades\n"
            f"/balance — account balance\n"
            f"/pnl — P&amp;L summary\n"
            f"/eod — end-of-day summary"
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
            "/status": self._cmd_status,
            "/positions": self._cmd_positions,
            "/trades": self._cmd_trades,
            "/balance": self._cmd_balance,
            "/pnl": self._cmd_pnl,
            "/eod": self._cmd_eod,
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
                "/status /positions /trades /balance /pnl /eod"
            )

    # ------------------------------------------------------------------
    # Command Handlers
    # ------------------------------------------------------------------

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

        run_icon = "" if is_running else "⏹️"
        algo_icon = "" if algo_enabled else ""
        mode_str = "PAPER" if paper else "LIVE"

        sig = status.get("signal") or {}
        zscore = sig.get("zscore", 0.0)
        regime = sig.get("regime", "N/A")

        msg_lines = [
            f" <b>System Status</b>  [{ts}]",
            "",
            f"<b>Engine:</b>  {run_icon} {'Running' if is_running else 'Stopped'}",
            f"<b>Algo:</b>    {algo_icon} {'Enabled' if algo_enabled else 'Disabled'}",
            f"<b>Mode:</b>    {mode_str}",
            f"<b>Asset:</b>   {asset}",
            f"<b>Position:</b> {position}",
            f"<b>Z-score:</b>  {zscore:+.4f}",
            f"<b>Regime:</b>   {regime}",
        ]
        if error:
            msg_lines.append(f"\n<b>Last Error:</b> {error[:200]}")
        self._send("\n".join(msg_lines))

    def _cmd_positions(self) -> None:
        """Handle /positions command."""
        status = self.get_status_cb() if self.get_status_cb else {}
        position = status.get("position", "NONE")
        open_trade = status.get("open_trade")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        if position == "NONE" or not open_trade:
            self._send(f" <b>Open Positions</b>  [{ts}]\n\nNo open positions.")
            return

        asset = status.get("asset", "N/A")
        entry_time = open_trade.get("entry_time", "N/A")
        entry_spot = open_trade.get("entry_spot_price", 0)
        entry_fut = open_trade.get("entry_futures_price", 0)
        entry_spread = open_trade.get("entry_spread", 0)
        entry_z = open_trade.get("entry_zscore", 0)
        notional = open_trade.get("notional_usd", 0)
        qty = open_trade.get("quantity", 0)

        sig = status.get("signal") or {}
        current_z = sig.get("zscore", 0.0)
        current_spread = sig.get("spread", 0.0)

        msg = (
            f" <b>Open Positions</b>  [{ts}]\n\n"
            f"<b>{position} {asset}</b>\n"
            f"<b>Size:</b> {qty:.6f} {asset} (${notional:,.2f})\n"
            f"<b>Entry Time:</b> {entry_time}\n"
            f"<b>Entry Spread:</b> {entry_spread:+.4f}  (Z: {entry_z:+.4f})\n"
            f"<b>Current Spread:</b> {current_spread:+.4f}  (Z: {current_z:+.4f})\n"
            f"<b>Entry Spot:</b>  ${entry_spot:,.4f}\n"
            f"<b>Entry Futures:</b> ${entry_fut:,.4f}"
        )
        self._send(msg)

    def _cmd_trades(self) -> None:
        """Handle /trades command - show 5 most recent closed trades."""
        trades = self.get_trades_cb() if self.get_trades_cb else []
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        # Filter to closed trades only, most recent first
        closed = [t for t in trades if not t.get("is_open", True)][:5]

        if not closed:
            self._send(f" <b>Recent Trades</b>  [{ts}]\n\nNo closed trades yet.")
            return

        lines = [f" <b>Recent Trades</b>  [{ts}]", ""]
        for t in closed:
            pnl = t.get("pnl_usd", 0)
            pct = t.get("pnl_percent", 0)
            icon = "" if pnl >= 0 else ""
            lines.append(
                f"{icon} <b>#{t.get('id')} {t.get('position_type')} {t.get('asset')}</b> "
                f"${pnl:+.2f} ({pct:+.2f}%)\n"
                f"   Exit: {t.get('exit_reason')}  |  "
                f"Z-in: {t.get('entry_zscore', 0):+.2f}  "
                f"Z-out: {t.get('exit_zscore', 0):+.2f}"
            )
        self._send("\n".join(lines))

    def _cmd_balance(self) -> None:
        """Handle /balance command."""
        balance_data = self.get_balance_cb() if self.get_balance_cb else {}
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        if not balance_data or not balance_data.get("connected"):
            self._send(
                f" <b>Account Balance</b>  [{ts}]\n\n"
                "Exchange not connected or API keys not configured."
            )
            return

        equity = balance_data.get("total_equity", 0)
        available = balance_data.get("available_margin", 0)
        margin_used = balance_data.get("margin_used", 0)
        margin_ratio = balance_data.get("margin_ratio", 0)
        upnl = balance_data.get("unrealized_pnl", 0)
        health = balance_data.get("margin_health", "N/A")
        exchange = balance_data.get("exchange", "N/A")
        mode = "DEMO" if balance_data.get("is_demo") else "LIVE"

        health_icon = {"SAFE": "", "WARNING": "", "DANGER": ""}.get(health, "")

        msg = (
            f" <b>Account Balance</b>  [{ts}]\n\n"
            f"<b>Exchange:</b> {exchange} ({mode})\n"
            f"<b>Total Equity:</b> ${equity:,.2f}\n"
            f"<b>Available:</b>   ${available:,.2f}\n"
            f"<b>Margin Used:</b> ${margin_used:,.2f}\n"
            f"<b>Margin Ratio:</b> {margin_ratio:.1f}%  {health_icon} {health}\n"
            f"<b>Unrealized P&amp;L:</b> ${upnl:+.2f}"
        )
        self._send(msg)

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

        # Today's trades
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_trades = [t for t in closed if (t.get("exit_time") or "").startswith(today)]
        today_pnl = sum(t.get("pnl_usd", 0) for t in today_trades)

        daily_pnl = balance_data.get("daily_pnl", today_pnl)
        upnl = balance_data.get("unrealized_pnl", 0)

        total_icon = "" if total_pnl >= 0 else ""
        today_icon = "" if today_pnl >= 0 else ""

        msg = (
            f" <b>P&amp;L Summary</b>  [{ts}]\n\n"
            f"<b>Closed Trades:</b> {len(closed)}\n"
            f"<b>Win Rate:</b>      {win_rate:.1f}%  "
            f"({len(winners)} wins / {len(losers)} losses)\n"
            f"<b>Avg Win:</b>  ${avg_win:+.2f}  |  <b>Avg Loss:</b> ${avg_loss:+.2f}\n\n"
            f"{today_icon} <b>Today:</b>   ${today_pnl:+.2f}  ({len(today_trades)} trades)\n"
            f"{total_icon} <b>All-time:</b> ${total_pnl:+.2f}\n"
            f" <b>Unrealized:</b> ${upnl:+.2f}"
        )
        self._send(msg)

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

        pnl_icon = "" if today_pnl >= 0 else ""

        msg_lines = [
            f" <b>End-of-Day Summary</b>",
            f"<b>{ts}</b>",
            "",
            f"<b>Today's Trades:</b> {len(today_closed)}  ({today_wins} wins)",
            f"{pnl_icon} <b>Today's P&amp;L:</b> ${today_pnl:+.2f}",
            "",
            f"<b>Account Equity:</b> ${equity:,.2f}",
            f"<b>Unrealized P&amp;L:</b> ${upnl:+.2f}",
            "",
            f"<b>Current Position:</b> {position}  ({asset})",
            f"<b>Z-score:</b> {zscore:+.4f}",
            f"<b>Regime:</b> {regime}",
        ]
        self._send("\n".join(msg_lines))

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
