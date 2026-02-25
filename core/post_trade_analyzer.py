"""
Post-trade analysis using the Anthropic API.

After each closed (non-paper) trade the analyzer fires in a background
thread, calls Claude with tool_use to get structured JSON, stores the
learning in the database, then optionally hands off to AutoTuner.
"""

import os
import json
import logging
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from models import Trade
    from database.manager import DatabaseManager

logger = logging.getLogger(__name__)

_ANALYSIS_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024

# Tool schema — forces Claude to return structured JSON via tool_use
_ANALYSIS_TOOL = {
    "name": "record_trade_analysis",
    "description": "Record a structured post-trade analysis with actionable recommendations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "root_cause": {
                "type": "string",
                "description": "1-2 sentences: why this trade won or lost."
            },
            "patterns": {
                "type": "string",
                "description": "1-2 sentences: repeating failure or success patterns visible in recent history."
            },
            "recommendations": {
                "type": "array",
                "description": "Up to 2 specific parameter changes.",
                "items": {
                    "type": "object",
                    "properties": {
                        "param":           {"type": "string",  "description": "Exact config field name, e.g. entry_threshold"},
                        "current_value":   {"type": "number",  "description": "Current value of the parameter"},
                        "suggested_value": {"type": "number",  "description": "Recommended new value"},
                        "confidence":      {"type": "number",  "description": "Confidence 0.0–1.0"},
                        "rationale":       {"type": "string",  "description": "One sentence explanation"}
                    },
                    "required": ["param", "current_value", "suggested_value", "confidence", "rationale"]
                }
            },
            "confidence_score": {
                "type": "integer",
                "description": "Overall strategy confidence 1–10.",
                "minimum": 1,
                "maximum": 10
            },
            "summary": {
                "type": "string",
                "description": "One sentence overall summary."
            }
        },
        "required": ["root_cause", "patterns", "recommendations", "confidence_score", "summary"]
    }
}


class PostTradeAnalyzer:
    """
    Calls the Anthropic API after each closed trade to diagnose outcomes,
    accumulate learnings, and (optionally) trigger the AutoTuner.

    Usage:
        analyzer = PostTradeAnalyzer(db, socketio, auto_tuner)
        analyzer.analyze_async(trade)
    """

    def __init__(self, db: "DatabaseManager", socketio=None, auto_tuner=None) -> None:
        self.db = db
        self.socketio = socketio
        self.auto_tuner = auto_tuner
        self._api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
        if not self._api_key:
            logger.warning("ANTHROPIC_API_KEY not set — post-trade analysis disabled")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def analyze_async(self, trade: "Trade") -> None:
        """Fire-and-forget analysis in a daemon thread."""
        if trade.is_open or trade.is_paper:
            return
        if not self._api_key:
            return
        threading.Thread(
            target=self._run_analysis,
            args=(trade,),
            daemon=True,
            name=f"post-trade-{trade.id}",
        ).start()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_analysis(self, trade: "Trade") -> None:
        try:
            import anthropic
        except ImportError:
            logger.error("anthropic package not installed. Run: pip install anthropic")
            return

        try:
            client = anthropic.Anthropic(api_key=self._api_key)

            # Pull recent closed trades + past learnings for context
            recent_trades: List["Trade"] = [
                t for t in self.db.get_trades(limit=25) if not t.is_open
            ][:15]
            past_learnings: List[Dict[str, Any]] = self.db.get_recent_learnings(limit=8)

            prompt = self._build_prompt(trade, recent_trades, past_learnings)

            message = client.messages.create(
                model=_ANALYSIS_MODEL,
                max_tokens=_MAX_TOKENS,
                tools=[_ANALYSIS_TOOL],
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract structured result from tool_use block
            analysis_data: Optional[Dict[str, Any]] = None
            for block in message.content:
                if block.type == "tool_use" and block.name == "record_trade_analysis":
                    analysis_data = block.input
                    break

            if not analysis_data:
                logger.warning("No tool_use block returned for trade %s", trade.id)
                return

            logger.info(
                "Post-trade analysis: trade=%d score=%d/10 recs=%d",
                trade.id,
                analysis_data.get("confidence_score", 0),
                len(analysis_data.get("recommendations", [])),
            )

            # Persist learning
            learning_id = self.db.save_learning(trade.id, analysis_data)

            # Also store raw text summary for backward-compat trade_analysis table
            self.db.save_trade_analysis(
                trade.id,
                json.dumps(analysis_data, indent=2),
                _ANALYSIS_MODEL,
            )

            # Push to dashboard
            if self.socketio:
                self.socketio.emit(
                    "trade_analysis",
                    {
                        "trade_id": trade.id,
                        "learning_id": learning_id,
                        "analysis": analysis_data,
                        "timestamp": datetime.utcnow().isoformat(),
                        "model": _ANALYSIS_MODEL,
                    },
                    namespace="/",
                )

            # Hand off to AutoTuner if enabled
            if self.auto_tuner:
                self.auto_tuner.check_and_apply(trade.id)

        except Exception:
            logger.exception("Post-trade analysis failed for trade %s", trade.id)

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        trade: "Trade",
        recent_trades: List["Trade"],
        past_learnings: List[Dict[str, Any]],
    ) -> str:
        duration_str = "unknown"
        if trade.entry_time and trade.exit_time:
            secs = (trade.exit_time - trade.entry_time).total_seconds()
            duration_str = f"{secs / 60:.1f} min"

        outcome = "WIN" if trade.pnl_usd > 0 else "LOSS"
        wins = sum(1 for t in recent_trades if t.pnl_usd > 0)
        losses = len(recent_trades) - wins

        history_lines = []
        for t in recent_trades:
            flag = "W" if t.pnl_usd > 0 else "L"
            history_lines.append(
                f"  [{flag}] {t.position_type:5s}  "
                f"entry_z={t.entry_zscore:+.2f}  exit_z={t.exit_zscore:+.2f}  "
                f"pnl=${t.pnl_usd:+.2f}  reason={t.exit_reason}"
            )

        learnings_lines = []
        for lrn in past_learnings:
            recs = json.loads(lrn.get("recommendations", "[]"))
            rec_str = "; ".join(
                f"{r['param']}→{r['suggested_value']} (conf={r['confidence']:.2f})"
                for r in recs
            ) or "none"
            learnings_lines.append(
                f"  [{lrn.get('timestamp', '')[:16]}] "
                f"{lrn.get('summary', '')}  |  recs: {rec_str}"
            )

        return f"""You are a quant analyst reviewing a crypto statistical-arbitrage (spot-futures basis) trade.
Use the record_trade_analysis tool to return your structured analysis.

═══ THIS TRADE · {outcome} ═══
Asset:          {trade.asset}
Direction:      {trade.position_type} spread
Entry Z-score:  {trade.entry_zscore:+.4f}
Exit  Z-score:  {trade.exit_zscore:+.4f}
Entry Spread:   {trade.entry_spread:.4f}
Exit  Spread:   {trade.exit_spread:.4f}
Spot @ entry:   ${trade.entry_spot_price:,.2f}
Fut  @ entry:   ${trade.entry_futures_price:,.2f}
Duration:       {duration_str}
P&L:            ${trade.pnl_usd:+.2f} ({trade.pnl_percent:+.2f}%)
Exit reason:    {trade.exit_reason}

═══ RECENT TRADE HISTORY ({len(recent_trades)} trades · {wins}W / {losses}L) ═══
{chr(10).join(history_lines) if history_lines else "  (no prior trades)"}

═══ ACCUMULATED LEARNINGS (from previous analyses) ═══
{chr(10).join(learnings_lines) if learnings_lines else "  (no prior learnings)"}

Tunable parameters and their safe ranges:
  entry_threshold:     1.8 – 3.5  (current typical: 2.0)
  exit_threshold:      0.3 – 1.0  (current typical: 0.5)
  stop_loss_threshold: 3.0 – 5.5  (current typical: 4.0)
  min_std_multiple:    1.0 – 2.5  (current typical: 1.2)
  slippage_bps:        1.0 – 10.0 (current typical: 3.0)

Call record_trade_analysis now."""
