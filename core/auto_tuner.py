"""
AutoTuner: applies Claude's parameter recommendations automatically.

Logic:
  - After each trade analysis, look at the last CONSENSUS_WINDOW learnings.
  - If CONSENSUS_MIN or more learnings recommend the same parameter in the
    same direction AND the average confidence >= MIN_CONFIDENCE:
      → clamp the suggested value to the safe corridor
      → apply the change to the DB config and the live engine config
      → log it to the learning_log table
      → emit a socket event so the dashboard shows what changed and why

Safe corridors prevent runaway adjustments:
  (min_value, max_value, max_step_per_adjustment)
"""

import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from database.manager import DatabaseManager

logger = logging.getLogger(__name__)

# How many recent learnings to look at
CONSENSUS_WINDOW = 5
# How many must agree on the same param + direction
CONSENSUS_MIN = 3
# Minimum average confidence to act
MIN_CONFIDENCE = 0.70

# param → (min_val, max_val, max_step)
SAFE_CORRIDORS: Dict[str, Tuple[float, float, float]] = {
    "entry_threshold":       (1.8, 3.5,  0.2),
    "exit_threshold":        (0.3, 1.0,  0.1),
    "stop_loss_threshold":   (3.0, 5.5,  0.3),
    "min_std_multiple":      (1.0, 2.5,  0.15),
    "slippage_bps":          (1.0, 10.0, 1.0),
}


class AutoTuner:
    """
    Reads recent learnings from DB and applies high-confidence consensus
    parameter changes to the live trading config.

    Usage:
        tuner = AutoTuner(db, engine, socketio)
        tuner.check_and_apply(trade_id)   # called after each analysis
    """

    def __init__(self, db: "DatabaseManager", engine=None, socketio=None) -> None:
        self.db = db
        self.engine = engine
        self.socketio = socketio

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def check_and_apply(self, trade_id: int) -> None:
        """
        Check if auto-tuning is enabled and if consensus exists for any
        parameter. Safe to call from any thread.
        """
        try:
            config = self.db.get_config()
            if not getattr(config, "auto_tune_enabled", False):
                return

            learnings = self.db.get_recent_learnings(limit=CONSENSUS_WINDOW)
            if len(learnings) < CONSENSUS_MIN:
                return

            changes = self._find_consensus(learnings)
            if not changes:
                return

            for param, suggested, avg_conf, rationale, supporting_ids in changes:
                self._apply_change(
                    param, suggested, avg_conf, rationale,
                    supporting_ids, trade_id,
                )

        except Exception:
            logger.exception("AutoTuner.check_and_apply error")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find_consensus(
        self,
        learnings: List[Dict[str, Any]],
    ) -> List[Tuple[str, float, float, str, List[int]]]:
        """
        Returns a list of (param, clamped_value, avg_confidence, rationale, learning_ids)
        for params that have reached consensus.
        """
        # Collect per-param votes
        votes: Dict[str, List[Dict[str, Any]]] = {}
        for lrn in learnings:
            recs = json.loads(lrn.get("recommendations", "[]"))
            for rec in recs:
                param = rec.get("param", "")
                if param not in SAFE_CORRIDORS:
                    continue
                current = rec.get("current_value", 0.0)
                suggested = rec.get("suggested_value", 0.0)
                direction = "up" if suggested > current else "down"
                key = f"{param}:{direction}"
                votes.setdefault(key, []).append({
                    "suggested": suggested,
                    "confidence": float(rec.get("confidence", 0.0)),
                    "rationale": rec.get("rationale", ""),
                    "learning_id": lrn.get("id"),
                })

        results = []
        for key, vote_list in votes.items():
            if len(vote_list) < CONSENSUS_MIN:
                continue
            avg_conf = sum(v["confidence"] for v in vote_list) / len(vote_list)
            if avg_conf < MIN_CONFIDENCE:
                continue

            param = key.split(":")[0]
            # Median of suggestions as the target
            suggestions = sorted(v["suggested"] for v in vote_list)
            median_suggestion = suggestions[len(suggestions) // 2]
            clamped = self._clamp(param, median_suggestion)
            rationale = vote_list[-1]["rationale"]  # Most recent
            ids = [v["learning_id"] for v in vote_list if v["learning_id"]]
            results.append((param, clamped, avg_conf, rationale, ids))

        return results

    def _clamp(self, param: str, value: float) -> float:
        """Clamp value to safe corridor, respecting max step from current."""
        min_v, max_v, max_step = SAFE_CORRIDORS[param]
        config = self.db.get_config()
        current = float(getattr(config, param, value))

        # Limit how far we move in one step
        if value > current:
            value = min(value, current + max_step)
        else:
            value = max(value, current - max_step)

        return round(max(min_v, min(max_v, value)), 4)

    def _apply_change(
        self,
        param: str,
        new_value: float,
        avg_conf: float,
        rationale: str,
        learning_ids: List[int],
        trigger_trade_id: int,
    ) -> None:
        config = self.db.get_config()
        old_value = float(getattr(config, param, new_value))

        if abs(new_value - old_value) < 1e-6:
            return  # No meaningful change

        # Apply to DB
        setattr(config, param, new_value)
        self.db.save_config(config)

        # Apply to live engine immediately
        if self.engine:
            setattr(self.engine.config, param, new_value)

        logger.info(
            "AutoTuner: %s  %.4f → %.4f  (conf=%.2f)  reason: %s",
            param, old_value, new_value, avg_conf, rationale,
        )

        # Log to learning_log table
        self.db.save_learning_log(
            param=param,
            old_value=old_value,
            new_value=new_value,
            avg_confidence=avg_conf,
            rationale=rationale,
            learning_ids=learning_ids,
            trigger_trade_id=trigger_trade_id,
        )

        # Emit to dashboard
        if self.socketio:
            self.socketio.emit(
                "auto_tune",
                {
                    "param": param,
                    "old_value": old_value,
                    "new_value": new_value,
                    "avg_confidence": round(avg_conf, 3),
                    "rationale": rationale,
                    "timestamp": datetime.utcnow().isoformat(),
                    "trigger_trade_id": trigger_trade_id,
                },
                namespace="/",
            )
