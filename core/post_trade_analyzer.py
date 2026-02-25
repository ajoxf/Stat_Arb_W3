"""
Post-trade analysis using the Anthropic API.

After each closed (non-paper) trade the analyzer fires in a background
thread, calls Claude, and stores the structured analysis in the database.
The result is also emitted via SocketIO so the dashboard can display it.
"""

import os
import logging
import threading
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from models import Trade
    from database.manager import DatabaseManager

logger = logging.getLogger(__name__)

_ANALYSIS_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024


class PostTradeAnalyzer:
    """
    Calls the Anthropic API after each closed trade to diagnose outcomes
    and surface actionable parameter suggestions.

    Usage:
        analyzer = PostTradeAnalyzer(db, socketio)
        # In on_trade_callback:
        analyzer.analyze_async(trade)
    """

    def __init__(self, db: "DatabaseManager", socketio=None) -> None:
        self.db = db
        self.socketio = socketio
        self._api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
        if not self._api_key:
            logger.warning(
                "ANTHROPIC_API_KEY not set — post-trade analysis disabled"
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def analyze_async(self, trade: "Trade") -> None:
        """
        Fire-and-forget: run analysis in a daemon thread so it never
        blocks the trading engine or Flask event loop.

        Only runs for real (non-paper) closed trades.
        """
        if trade.is_open or trade.is_paper:
            return
        if not self._api_key:
            return
        t = threading.Thread(
            target=self._run_analysis,
            args=(trade,),
            daemon=True,
            name=f"post-trade-analysis-{trade.id}",
        )
        t.start()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_analysis(self, trade: "Trade") -> None:
        try:
            import anthropic  # lazy import — only required when API key present
        except ImportError:
            logger.error(
                "anthropic package not installed. "
                "Run: pip install anthropic"
            )
            return

        try:
            client = anthropic.Anthropic(api_key=self._api_key)

            # Pull recent closed trades for pattern context
            recent_trades: List["Trade"] = [
                t for t in self.db.get_trades(limit=25) if not t.is_open
            ][:15]

            prompt = self._build_prompt(trade, recent_trades)

            message = client.messages.create(
                model=_ANALYSIS_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )

            analysis_text: str = message.content[0].text
            logger.info(
                "Post-trade analysis complete for trade %d (%d chars)",
                trade.id, len(analysis_text),
            )

            # Persist
            self.db.save_trade_analysis(trade.id, analysis_text, _ANALYSIS_MODEL)

            # Push to dashboard
            if self.socketio:
                self.socketio.emit(
                    "trade_analysis",
                    {
                        "trade_id": trade.id,
                        "analysis": analysis_text,
                        "timestamp": datetime.utcnow().isoformat(),
                        "model": _ANALYSIS_MODEL,
                    },
                    namespace="/",
                )

        except Exception:
            logger.exception("Post-trade analysis failed for trade %s", trade.id)

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(trade: "Trade", recent_trades: List["Trade"]) -> str:
        # Duration in minutes
        duration_str = "unknown"
        if trade.entry_time and trade.exit_time:
            secs = (trade.exit_time - trade.entry_time).total_seconds()
            duration_str = f"{secs / 60:.1f} min"

        # Outcome label
        outcome = "WIN" if trade.pnl_usd > 0 else "LOSS"

        # Recent history summary
        wins = sum(1 for t in recent_trades if t.pnl_usd > 0)
        losses = len(recent_trades) - wins
        history_lines = []
        for t in recent_trades:
            flag = "W" if t.pnl_usd > 0 else "L"
            history_lines.append(
                f"  [{flag}] {t.position_type:5s}  "
                f"entry_z={t.entry_zscore:+.2f}  "
                f"exit_z={t.exit_zscore:+.2f}  "
                f"pnl=${t.pnl_usd:+.2f}  "
                f"reason={t.exit_reason}"
            )
        history_block = "\n".join(history_lines) if history_lines else "  (no prior trades)"

        return f"""You are a quantitative analyst reviewing a crypto statistical-arbitrage
(spot-futures basis) trade. Be concise and data-driven.

═══════════════════════════════════════════════════════
COMPLETED TRADE  ·  {outcome}
═══════════════════════════════════════════════════════
Asset            : {trade.asset}
Direction        : {trade.position_type} spread
Entry Z-score    : {trade.entry_zscore:+.4f}
Exit  Z-score    : {trade.exit_zscore:+.4f}
Entry Spread     : {trade.entry_spread:.4f}
Exit  Spread     : {trade.exit_spread:.4f}
Spot  @ entry    : ${trade.entry_spot_price:,.2f}
Futures @ entry  : ${trade.entry_futures_price:,.2f}
Duration         : {duration_str}
P&L              : ${trade.pnl_usd:+.2f}  ({trade.pnl_percent:+.2f}%)
Exit reason      : {trade.exit_reason}

═══════════════════════════════════════════════════════
RECENT HISTORY  ({len(recent_trades)} closed trades · {wins}W / {losses}L)
═══════════════════════════════════════════════════════
{history_block}

═══════════════════════════════════════════════════════
ANALYSIS REQUIRED
═══════════════════════════════════════════════════════
Answer each section in 1-3 sentences. Be specific — use numbers.

1. ROOT CAUSE
   Why did this trade {outcome.lower()}? (spread behaviour, entry timing,
   z-score level, exit reason, duration?)

2. PATTERNS IN RECENT HISTORY
   Any repeating failure modes visible in the history above?
   (e.g., all LONGs losing, losses clustered at certain z-score ranges,
   stop-loss exits dominating, short hold-times?)

3. PARAMETER RECOMMENDATIONS
   Name 1-2 specific changes with suggested values, e.g.:
   - Raise entry_threshold from 2.0 → 2.5 (avoids low-conviction entries)
   - Lower stop_loss_threshold from 4.0 → 3.5 (cut losers earlier)

4. STRATEGY CONFIDENCE
   Score 1-10 and one-sentence rationale.
"""
