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
            direction = trade.position_type
            entry_time_str = (
                trade.entry_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                if trade.entry_time else "—"
            )
            spread_bps = 0.0
            if trade.entry_spot_price > 0:
                spread_bps = (trade.entry_spread / trade.entry_spot_price) * 10000

            lines = [
                f"<b>TRADE ENTRY  ·  {direction} {trade.asset}</b>",
                "────────────────────────",
                f"ID        #{trade.id or 'pending'}",
                f"Time      {entry_time_str}",
                f"Size      {trade.quantity:.6f} {trade.asset}  (${trade.notional_usd:,.2f})",
                "",
                f"Spot      ${trade.entry_spot_price:,.4f}",
                f"Futures   ${trade.entry_futures_price:,.4f}",
                f"Spread    {trade.entry_spread:+.4f}  ({spread_bps:+.2f} bps)",
                f"Z-score   {trade.entry_zscore:+.4f}",
            ]
            if signal:
                std = getattr(signal, 'spread_std', None)
                if std:
                    lines.append(f"Spread SD  {std:.6f}")
                regime = getattr(signal, 'regime', None)
                if regime:
                    lines.append(f"Regime    {regime}")
            if trade.is_paper:
                lines += ["────────────────────────", "<i>Paper Trading</i>"]

            self._send("\n".join(lines))
        except Exception as e:
            logger.error("Error building trade entry notification: %s", e)

    def notify_trade_exit(self, trade) -> None:
        """Send a trade exit notification with full P&L breakdown."""
        if not self.is_ready() or not self._notify_trades:
            return
        try:
            direction = trade.position_type
            exit_reason = trade.exit_reason or "EXIT"

            exit_time_str = "—"
            duration_str = "—"
            if trade.exit_time:
                exit_time_str = trade.exit_time.strftime("%Y-%m-%d %H:%M:%S UTC")
                if trade.entry_time:
                    total_sec = int((trade.exit_time - trade.entry_time).total_seconds())
                    if total_sec < 3600:
                        duration_str = f"{total_sec // 60}m {total_sec % 60}s"
                    elif total_sec < 86400:
                        duration_str = f"{total_sec // 3600}h {(total_sec % 3600) // 60}m"
                    else:
                        duration_str = f"{total_sec // 86400}d {(total_sec % 86400) // 3600}h"

            entry_spread = trade.entry_spread
            exit_spread = trade.exit_spread
            spread_change = (
                (exit_spread - entry_spread) if direction == "SHORT"
                else (entry_spread - exit_spread)
            )
            gross_pnl = spread_change * trade.quantity
            est_fees = trade.notional_usd * 0.0020

            result = "PROFIT" if trade.pnl_usd >= 0 else "LOSS"

            lines = [
                f"<b>TRADE EXIT  ·  {direction} {trade.asset}  ·  {result}</b>",
                "────────────────────────",
                f"Reason    {exit_reason}",
                f"Duration  {duration_str}",
                f"Exit      {exit_time_str}",
                "",
                f"Entry Spot     ${trade.entry_spot_price:,.4f}",
                f"Exit Spot      ${trade.exit_spot_price:,.4f}",
                f"Entry Futures  ${trade.entry_futures_price:,.4f}",
                f"Exit Futures   ${trade.exit_futures_price:,.4f}",
                "",
                f"Entry Spread   {entry_spread:+.4f}  (Z: {trade.entry_zscore:+.4f})",
                f"Exit Spread    {exit_spread:+.4f}  (Z: {trade.exit_zscore:+.4f})",
                "────────────────────────",
                f"Gross     ${gross_pnl:+.4f}",
                f"Est. Fees  -${est_fees:.4f}",
                f"<b>Net       ${trade.pnl_usd:+.4f}  ({trade.pnl_percent:+.4f}%)</b>",
            ]
            if trade.is_paper:
                lines += ["────────────────────────", "<i>Paper Trading</i>"]

            self._send("\n".join(lines))
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
            msg = (
                f"<b>SIGNAL  ·  {sig_type}</b>\n"
                "────────────────────────\n"
                f"Z-score  {signal.zscore:+.4f}\n"
                f"Spread   {signal.spread:+.6f}\n"
                f"Regime   {getattr(signal, 'regime', 'N/A')}\n"
                f"Time     {ts}"
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
                f"<b>SYSTEM ERROR</b>\n"
                "────────────────────────\n"
                f"{ts}\n\n"
                f"{error_msg[:500]}"
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
            "<b>Nexus Stat-Arb</b>\n"
            "────────────────────────\n"
            f"Connected and ready.  {ts}\n\n"
            "<b>Commands</b>\n"
            "/status     engine &amp; algo state\n"
            "/positions  open positions\n"
            "/trades     recent closed trades\n"
            "/balance    account balance\n"
            "/pnl        P&amp;L summary\n"
            "/eod        end-of-day report"
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

    def _cmd_start(self) -> None:
        """Handle /start and /help commands."""
        self._send(
            "<b>Nexus Stat-Arb Bot</b>\n"
            "────────────────────────\n"
            "Notifications active.  Use a command below.\n\n"
            "<b>Commands</b>\n"
            "/status     engine &amp; algo state\n"
            "/positions  open positions\n"
            "/trades     recent closed trades\n"
            "/balance    account balance\n"
            "/pnl        P&amp;L summary\n"
            "/eod        end-of-day report"
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

        mode_str = "Paper" if paper else "Live"

        sig = status.get("signal") or {}
        zscore = sig.get("zscore", 0.0)
        regime = sig.get("regime", "N/A")

        msg_lines = [
            f"<b>SYSTEM STATUS</b>  ·  {ts}",
            "────────────────────────",
            f"Engine    {'Running' if is_running else 'Stopped'}",
            f"Algo      {'Enabled' if algo_enabled else 'Disabled'}",
            f"Mode      {mode_str}",
            f"Asset     {asset}",
            f"Position  {position}",
            f"Z-score   {zscore:+.4f}",
            f"Regime    {regime}",
        ]
        if error:
            msg_lines += ["────────────────────────", f"<b>Error</b>  {error[:200]}"]
        self._send("\n".join(msg_lines))

    def _cmd_positions(self) -> None:
        """Handle /positions command."""
        status = self.get_status_cb() if self.get_status_cb else {}
        position = status.get("position", "NONE")
        open_trade = status.get("open_trade")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        if position == "NONE" or not open_trade:
            self._send(
                f"<b>OPEN POSITIONS</b>  ·  {ts}\n"
                "────────────────────────\n"
                "No open positions."
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

        msg = (
            f"<b>OPEN POSITIONS</b>  ·  {ts}\n"
            "────────────────────────\n"
            f"<b>{position} {asset}</b>\n\n"
            f"Size          {qty:.6f} {asset}  (${notional:,.2f})\n"
            f"Entry Time    {entry_time}\n"
            f"Entry Spot    ${entry_spot:,.4f}\n"
            f"Entry Futures ${entry_fut:,.4f}\n"
            f"Entry Spread  {entry_spread:+.4f}  (Z: {entry_z:+.4f})\n"
            f"Current       {current_spread:+.4f}  (Z: {current_z:+.4f})"
        )
        self._send(msg)

    def _cmd_trades(self) -> None:
        """Handle /trades command - show 5 most recent closed trades."""
        trades = self.get_trades_cb() if self.get_trades_cb else []
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        # Filter to closed trades only, most recent first
        closed = [t for t in trades if not t.get("is_open", True)][:5]

        if not closed:
            self._send(
                f"<b>RECENT TRADES</b>  ·  {ts}\n"
                "────────────────────────\n"
                "No closed trades yet."
            )
            return

        lines = [f"<b>RECENT TRADES</b>  ·  {ts}", "────────────────────────"]
        for t in closed:
            pnl = t.get("pnl_usd", 0)
            pct = t.get("pnl_percent", 0)
            result = "PROFIT" if pnl >= 0 else "LOSS"
            lines.append(
                f"<b>#{t.get('id')}  {t.get('position_type')} {t.get('asset')}</b>  "
                f"${pnl:+.2f}  ({pct:+.2f}%)  {result}\n"
                f"Exit: {t.get('exit_reason')}  ·  "
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
                f"<b>ACCOUNT BALANCE</b>  ·  {ts}\n"
                "────────────────────────\n"
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
        mode = "Demo" if balance_data.get("is_demo") else "Live"

        msg = (
            f"<b>ACCOUNT BALANCE</b>  ·  {ts}\n"
            "────────────────────────\n"
            f"Exchange    {exchange}  ({mode})\n"
            f"Equity      ${equity:,.2f}\n"
            f"Available   ${available:,.2f}\n"
            f"Used        ${margin_used:,.2f}\n"
            f"Margin      {margin_ratio:.1f}%  [{health}]\n"
            f"Unrealized  ${upnl:+.2f}"
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

        msg = (
            f"<b>P&amp;L SUMMARY</b>  ·  {ts}\n"
            "────────────────────────\n"
            f"Closed Trades  {len(closed)}\n"
            f"Win Rate       {win_rate:.1f}%  "
            f"({len(winners)}W / {len(losers)}L)\n"
            f"Avg Win        ${avg_win:+.2f}\n"
            f"Avg Loss       ${avg_loss:+.2f}\n"
            "────────────────────────\n"
            f"Today          ${today_pnl:+.2f}  ({len(today_trades)} trades)\n"
            f"All-time       ${total_pnl:+.2f}\n"
            f"Unrealized     ${upnl:+.2f}"
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

        msg_lines = [
            f"<b>END OF DAY  ·  {ts}</b>",
            "────────────────────────",
            f"Trades      {len(today_closed)}  ({today_wins} wins)",
            f"P&amp;L        ${today_pnl:+.2f}",
            "────────────────────────",
            f"Equity      ${equity:,.2f}",
            f"Unrealized  ${upnl:+.2f}",
            "────────────────────────",
            f"Position    {position}  ({asset})",
            f"Z-score     {zscore:+.4f}",
            f"Regime      {regime}",
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
