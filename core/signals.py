"""
Signal generation module for crypto statistical arbitrage.
Implements Z-score calculation, Hurst exponent, and STD filter.
"""

import numpy as np
from collections import deque
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any, Callable
import logging

from models import Signal, TradingConfig, MarketTick, SDTouchEvent

logger = logging.getLogger(__name__)


class SignalGenerator:
    """
    Generates trading signals based on spread Z-score with filters.

    The spread is calculated as: Futures Price - Spot Price
    Z-score = (spread - rolling_mean) / rolling_std

    Entry signals:
    - LONG: z >= +entry_threshold (spread above mean, futures premium high, expect reversion down)
    - SHORT: z <= -entry_threshold (spread below mean, futures discount, expect reversion up)

    Exit signals (direction-aware):
    - LONG position: exit when z <= +exit_threshold (spread reverted toward mean)
    - SHORT position: exit when z >= -exit_threshold (spread reverted toward mean)

    Filters (applied to ENTRY only):
    - Hurst exponent: H < 0.5 indicates mean-reverting regime
    - STD filter: Ensures volatility is sufficient to cover trading costs
    """

    def __init__(self, config: TradingConfig):
        self.config = config
        self.lookback = config.lookback_period
        self.stats_update_interval = config.stats_update_interval  # Seconds

        # Rolling data storage
        self.spread_history: deque = deque(maxlen=self.lookback)
        self.spot_prices: deque = deque(maxlen=self.lookback)
        self.futures_prices: deque = deque(maxlen=self.lookback)

        # Current state
        self.current_zscore: float = 0.0
        self.current_spread: float = 0.0
        self.current_mean: float = 0.0
        self.current_std: float = 0.0
        self.current_hurst: float = 0.5

        # Stats update timing
        self.last_stats_update: Optional[datetime] = None
        self._stats_initialized: bool = False

        # SD touch tracking
        self.last_sd_level: float = 0.0
        self.sd_touch_events: List[SDTouchEvent] = []

        # Callback for SD touch events (for database logging)
        self.on_sd_touch: Optional[Callable[[SDTouchEvent], None]] = None

        # Track current position for exit signals
        self.current_position: str = "NONE"

        # Track blocked signals for diagnostics
        self.last_blocked_signal: Optional[Dict[str, Any]] = None

    def update_config(self, config: TradingConfig) -> None:
        """Update configuration."""
        self.config = config
        self.stats_update_interval = config.stats_update_interval

        if config.lookback_period != self.lookback:
            self.lookback = config.lookback_period
            # Resize deques
            old_spreads = list(self.spread_history)
            old_spots = list(self.spot_prices)
            old_futures = list(self.futures_prices)
            self.spread_history = deque(old_spreads[-self.lookback:], maxlen=self.lookback)
            self.spot_prices = deque(old_spots[-self.lookback:], maxlen=self.lookback)
            self.futures_prices = deque(old_futures[-self.lookback:], maxlen=self.lookback)

    def set_position(self, position: str) -> None:
        """Set current position for exit signal calculation."""
        self.current_position = position

    def add_tick(self, spot_tick: MarketTick, futures_tick: MarketTick) -> None:
        """Add a new tick and update calculations."""
        spot_price = spot_tick.mid
        futures_price = futures_tick.mid

        if spot_price <= 0 or futures_price <= 0:
            logger.warning("Invalid tick prices: spot=%s, futures=%s", spot_price, futures_price)
            return

        spread = futures_price - spot_price

        self.spot_prices.append(spot_price)
        self.futures_prices.append(futures_price)
        self.spread_history.append(spread)

        self.current_spread = spread
        self._update_statistics()

    def _update_statistics(self) -> None:
        """
        Update rolling statistics.

        Mean and STD are only recalculated at the configured interval
        (stats_update_interval seconds). Z-score is always calculated
        using the current spread and the (potentially stale) mean/std.

        This provides stable bands for easier entry/exit tracking.
        """
        if len(self.spread_history) < 2:
            return

        now = datetime.utcnow()

        # Check if we need to recalculate mean/std
        should_update_stats = (
            not self._stats_initialized or
            self.last_stats_update is None or
            (now - self.last_stats_update).total_seconds() >= self.stats_update_interval
        )

        if should_update_stats:
            spreads = np.array(self.spread_history)
            self.current_mean = float(np.mean(spreads))
            self.current_std = float(np.std(spreads, ddof=1))

            # Update Hurst if we have enough data
            if len(self.spread_history) >= 20:
                self.current_hurst = self._calculate_hurst(spreads)

            self.last_stats_update = now
            self._stats_initialized = True

            logger.debug("Stats updated: mean=%.6f, std=%.6f, hurst=%.4f",
                        self.current_mean, self.current_std, self.current_hurst)

        # Always update z-score with current spread
        if self.current_std > 0:
            self.current_zscore = (self.current_spread - self.current_mean) / self.current_std
        else:
            self.current_zscore = 0.0

    def _calculate_hurst(self, series: np.ndarray) -> float:
        """
        Calculate Hurst exponent using R/S (Rescaled Range) analysis.

        H < 0.5: Mean-reverting (anti-persistent)
        H = 0.5: Random walk
        H > 0.5: Trending (persistent)
        """
        n = len(series)
        if n < 20:
            return 0.5

        # Use different sub-series lengths
        max_k = min(n // 2, 50)
        min_k = 10

        if max_k <= min_k:
            return 0.5

        rs_values = []
        n_values = []

        for k in range(min_k, max_k + 1, 5):
            rs_list = []

            for start in range(0, n - k + 1, k):
                subseries = series[start:start + k]
                if len(subseries) < k:
                    continue

                mean_val = np.mean(subseries)
                deviations = subseries - mean_val
                cumulative_deviations = np.cumsum(deviations)

                r = np.max(cumulative_deviations) - np.min(cumulative_deviations)
                s = np.std(subseries, ddof=1)

                if s > 0:
                    rs_list.append(r / s)

            if rs_list:
                rs_values.append(np.mean(rs_list))
                n_values.append(k)

        if len(rs_values) < 2:
            return 0.5

        # Linear regression in log-log space
        log_n = np.log(n_values)
        log_rs = np.log(rs_values)

        try:
            # H = slope of log(R/S) vs log(n)
            slope, _ = np.polyfit(log_n, log_rs, 1)
            hurst = float(np.clip(slope, 0.0, 1.0))
            return hurst
        except Exception:
            return 0.5

    def _check_std_filter(self) -> Tuple[bool, float]:
        """
        Check if STD is sufficient to cover trading costs.

        Returns (passed, profitability_ratio)
        """
        if not self.config.std_filter_enabled:
            return True, float('inf')

        if self.current_std <= 0:
            return False, 0.0

        # Use appropriate fee based on order execution mode
        # Market orders = taker fee, Limit orders = maker fee
        if self.config.order_execution_mode == "LIMIT":
            fee_bps = self.config.maker_fee_bps
        else:
            fee_bps = self.config.taker_fee_bps

        # Estimated round-trip cost in price terms
        # fee_bps is per side, so round-trip is 2x (entry + exit)
        # Also multiply by 2 for both legs (spot + futures)
        spot_price = self.spot_prices[-1] if self.spot_prices else 0
        if spot_price <= 0:
            return False, 0.0

        costs_price = (fee_bps / 10000) * spot_price * 4  # 2 sides * 2 legs

        # Profitability ratio: how many times STD covers the costs
        profitability_ratio = self.current_std / costs_price if costs_price > 0 else float('inf')

        passed = profitability_ratio >= self.config.min_std_multiple
        return passed, profitability_ratio

    def _track_sd_touch(self, zscore: float, spot_price: float, futures_price: float) -> Optional[SDTouchEvent]:
        """Track when Z-score crosses SD levels."""
        current_sd_level = 0.0

        # Determine current SD level
        for level in [-3, -2, -1, 1, 2, 3]:
            if level < 0:
                if zscore <= level and zscore > level - 1:
                    current_sd_level = level
                    break
            else:
                if zscore >= level and zscore < level + 1:
                    current_sd_level = level
                    break

        # Check for SD level crossing
        if current_sd_level != 0 and current_sd_level != self.last_sd_level:
            direction = "DOWN" if current_sd_level < self.last_sd_level else "UP"

            event = SDTouchEvent(
                asset=self.config.asset,
                timestamp=datetime.utcnow(),
                sd_level=current_sd_level,
                direction=direction,
                spread=self.current_spread,
                zscore=zscore,
                spot_price=spot_price,
                futures_price=futures_price,
            )

            self.sd_touch_events.append(event)
            self.last_sd_level = current_sd_level

            # Call callback for database logging
            if self.on_sd_touch:
                self.on_sd_touch(event)

            return event

        self.last_sd_level = current_sd_level
        return None

    def generate_signal(self) -> Signal:
        """
        Generate trading signal based on current state.

        Returns Signal object with type and all relevant metrics.
        """
        timestamp = datetime.utcnow()

        # Not enough data - must have FULL lookback period before trading
        if len(self.spread_history) < self.lookback:
            return Signal(
                signal_type="NONE",
                zscore=self.current_zscore,
                spread=self.current_spread,
                spread_mean=self.current_mean,
                spread_std=self.current_std,
                hurst=self.current_hurst,
                hurst_ok=None,  # Unknown until we have full data
                std_filter_ok=None,  # Unknown until we have full data
                regime="COLLECTING",
                current_position=self.current_position,
                timestamp=timestamp,
            )

        # Check filters
        hurst_ok = not self.config.hurst_enabled or self.current_hurst < self.config.hurst_threshold
        std_ok, _ = self._check_std_filter()

        # Determine regime
        if self.current_hurst < 0.4:
            regime = "MEAN_REVERTING"
        elif self.current_hurst > 0.6:
            regime = "TRENDING"
        else:
            regime = "NEUTRAL"

        # Track SD touches
        spot_price = self.spot_prices[-1] if self.spot_prices else 0
        futures_price = self.futures_prices[-1] if self.futures_prices else 0
        self._track_sd_touch(self.current_zscore, spot_price, futures_price)

        # Determine signal type
        signal_type = "NONE"
        blocked_reason = None

        if self.current_position == "NONE":
            # Check if Z-score crosses entry threshold
            z_triggers_long = self.current_zscore >= self.config.entry_threshold
            z_triggers_short = self.current_zscore <= -self.config.entry_threshold

            if z_triggers_long or z_triggers_short:
                # Z-score triggered - check why it might be blocked
                if not hurst_ok:
                    blocked_reason = "Hurst filter (H={:.3f} > {:.2f})".format(
                        self.current_hurst, self.config.hurst_threshold)
                elif not std_ok:
                    blocked_reason = "STD filter (volatility too low)"
                elif z_triggers_long and self.current_zscore >= self.config.stop_loss_threshold:
                    blocked_reason = "Z-score at stop-loss level ({:.2f} >= {:.2f})".format(
                        self.current_zscore, self.config.stop_loss_threshold)
                elif z_triggers_short and self.current_zscore <= -self.config.stop_loss_threshold:
                    blocked_reason = "Z-score at stop-loss level ({:.2f} <= -{:.2f})".format(
                        self.current_zscore, self.config.stop_loss_threshold)
                else:
                    # All filters pass - generate signal
                    if z_triggers_long:
                        signal_type = "LONG"
                    else:
                        signal_type = "SHORT"

                # Record blocked signal if applicable
                if blocked_reason:
                    self.last_blocked_signal = {
                        'timestamp': timestamp.isoformat(),
                        'would_be_signal': 'LONG' if z_triggers_long else 'SHORT',
                        'zscore': round(self.current_zscore, 4),
                        'reason': blocked_reason,
                    }
                    logger.debug("Signal blocked: %s (Z=%.4f) - %s",
                                'LONG' if z_triggers_long else 'SHORT',
                                self.current_zscore, blocked_reason)

        elif self.current_position == "LONG":
            # Exit signals for LONG position - filters do NOT apply
            # Entered when z >= +entry, exit when z <= +exit (returns toward 0)
            if self.current_zscore <= self.config.exit_threshold:
                signal_type = "EXIT"
            elif self.current_zscore >= self.config.stop_loss_threshold:
                signal_type = "STOP_LOSS"

        elif self.current_position == "SHORT":
            # Exit signals for SHORT position - filters do NOT apply
            # Entered when z <= -entry, exit when z >= -exit (returns toward 0)
            if self.current_zscore >= -self.config.exit_threshold:
                signal_type = "EXIT"
            elif self.current_zscore <= -self.config.stop_loss_threshold:
                signal_type = "STOP_LOSS"

        return Signal(
            signal_type=signal_type,
            zscore=self.current_zscore,
            spread=self.current_spread,
            spread_mean=self.current_mean,
            spread_std=self.current_std,
            hurst=self.current_hurst,
            hurst_ok=hurst_ok,
            std_filter_ok=std_ok,
            regime=regime,
            current_position=self.current_position,
            timestamp=timestamp,
        )

    def get_spread_history(self, n: int = 100) -> List[float]:
        """Get last n spread values."""
        return list(self.spread_history)[-n:]

    def get_zscore_history(self, n: int = 100) -> List[float]:
        """Calculate Z-score history for charting."""
        if len(self.spread_history) < 20:
            return []

        spreads = list(self.spread_history)
        zscores = []

        for i in range(19, len(spreads)):
            window = spreads[max(0, i - self.lookback + 1):i + 1]
            mean = np.mean(window)
            std = np.std(window, ddof=1)
            if std > 0:
                z = (spreads[i] - mean) / std
                zscores.append(float(z))
            else:
                zscores.append(0.0)

        return zscores[-n:]

    def get_state(self) -> Dict[str, Any]:
        """Get current state for dashboard."""
        # Calculate seconds until next stats update
        if self.last_stats_update:
            elapsed = (datetime.utcnow() - self.last_stats_update).total_seconds()
            next_update_in = max(0, self.stats_update_interval - elapsed)
        else:
            next_update_in = 0

        # Calculate filter status (same logic as generate_signal)
        hurst_ok = not self.config.hurst_enabled or self.current_hurst < self.config.hurst_threshold
        std_ok, std_ratio = self._check_std_filter()

        # Check if we have enough data (must have full lookback period)
        data_ready = len(self.spread_history) >= self.lookback

        # Determine regime
        if self.current_hurst < 0.4:
            regime = "MEAN_REVERTING"
        elif self.current_hurst > 0.6:
            regime = "TRENDING"
        else:
            regime = "NEUTRAL"

        return {
            'zscore': round(self.current_zscore, 4),
            'spread': round(self.current_spread, 6),
            'spread_mean': round(self.current_mean, 6),
            'spread_std': round(self.current_std, 6),
            'hurst': round(self.current_hurst, 4),
            'hurst_ok': hurst_ok if data_ready else None,
            'std_filter_ok': std_ok if data_ready else None,
            'std_ratio': round(std_ratio, 2) if std_ratio != float('inf') else None,
            'std_ratio_required': self.config.min_std_multiple,
            'std_filter_enabled': self.config.std_filter_enabled,
            'order_mode': self.config.order_execution_mode,
            'fee_bps_used': self.config.maker_fee_bps if self.config.order_execution_mode == "LIMIT" else self.config.taker_fee_bps,
            'regime': regime if data_ready else "COLLECTING",
            'data_points': len(self.spread_history),
            'lookback': self.lookback,
            'data_ready': data_ready,
            'current_position': self.current_position,
            'stats_update_interval': self.stats_update_interval,
            'last_stats_update': self.last_stats_update.isoformat() if self.last_stats_update else None,
            'next_stats_update_in': round(next_update_in),
            'last_blocked_signal': self.last_blocked_signal,
        }

    def load_spread_history(self, spreads: List[float]) -> None:
        """
        Load spread history from external source (e.g., database).
        Used for recovery after reconnection.
        """
        self.spread_history.clear()
        for spread in spreads[-self.lookback:]:
            self.spread_history.append(spread)

        if len(self.spread_history) >= 2:
            self._update_statistics()

        logger.info("Loaded %d spread values from history", len(self.spread_history))

    def reset(self) -> None:
        """Reset all state."""
        self.spread_history.clear()
        self.spot_prices.clear()
        self.futures_prices.clear()
        self.current_zscore = 0.0
        self.current_spread = 0.0
        self.current_mean = 0.0
        self.current_std = 0.0
        self.current_hurst = 0.5
        self.last_sd_level = 0.0
        self.sd_touch_events.clear()
        self.current_position = "NONE"
        self.last_stats_update = None
        self._stats_initialized = False
        self.last_blocked_signal = None
