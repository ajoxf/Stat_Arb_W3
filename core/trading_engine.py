"""
Trading engine for crypto statistical arbitrage.
Manages the main trading loop, position management, and order execution.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable, Dict, Any, List
from dataclasses import dataclass

from models import (
    TradingConfig, Trade, MarketTick, Signal, Position,
    OrderResult, CRYPTO_ASSETS, get_symbols_for_asset
)
from core.signals import SignalGenerator
from core.order_executor import OrderExecutor
from core.trade_logger import get_trade_logger
from adapters.base import ExchangeAdapter
from adapters.okx_websocket import OKXWebSocketManager

logger = logging.getLogger(__name__)


@dataclass
class EngineState:
    """Current engine state."""
    is_running: bool = False
    algo_enabled: bool = False
    paper_trading: bool = True
    current_position: str = "NONE"  # NONE, LONG, SHORT
    last_tick_time: Optional[datetime] = None
    last_signal: Optional[Signal] = None
    current_trade: Optional[Trade] = None
    error: str = ""


class TradingEngine:
    """
    Main trading engine that coordinates price feeds, signal generation,
    and order execution for crypto statistical arbitrage.
    """

    def __init__(self, config: TradingConfig):
        self.config = config
        self.signal_generator = SignalGenerator(config)
        self.state = EngineState(paper_trading=config.paper_trading)

        # Exchange adapters (REST)
        self.spot_adapter: Optional[ExchangeAdapter] = None
        self.futures_adapter: Optional[ExchangeAdapter] = None

        # Order executor for spread trades
        self.order_executor: Optional[OrderExecutor] = None

        # WebSocket manager (optional, for real-time streaming)
        self.ws_manager: Optional[OKXWebSocketManager] = None
        self._use_websocket: bool = False
        self._pending_leverage_setup: bool = False

        # Current market data
        self.spot_tick: Optional[MarketTick] = None
        self.futures_tick: Optional[MarketTick] = None

        # Current open trade
        self.open_trade: Optional[Trade] = None

        # Callbacks for UI updates
        self.on_tick: Optional[Callable[[MarketTick, MarketTick], None]] = None
        self.on_signal: Optional[Callable[[Signal], None]] = None
        self.on_trade: Optional[Callable[[Trade], None]] = None
        self.on_status: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # Control flags
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Post-stop-loss cooldown: prevent re-entry for this many seconds after a stop-loss
        self._stop_loss_cooldown_sec = 60
        self._stop_loss_cooldown_until: Optional[datetime] = None

        # General entry cooldown: prevent rapid re-entry after any trade
        self._entry_cooldown_until: Optional[datetime] = None

        # Execution lock to prevent new trades while one is being executed
        self._executing_trade = False

        # Tick processing lock: prevents concurrent _process_tick_pair tasks
        # Critical for WebSocket mode where ticks arrive faster than processing
        self._processing_tick = False

        # Position reconciliation tracking
        self._last_position_verify: Optional[datetime] = None
        self._position_verify_interval = 60  # seconds between checks
        self._position_mismatch: Optional[Dict[str, Any]] = None

        # Order execution tracking for pattern detection
        self._spot_order_attempts = 0
        self._spot_order_failures = 0
        self._futures_order_attempts = 0
        self._futures_order_failures = 0
        self._last_order_stats_log: Optional[datetime] = None
        self._order_stats_log_interval = 300  # Log stats every 5 minutes

        # Tick interval in seconds
        self.tick_interval = 0.5  # 500ms

    def update_config(self, config: TradingConfig) -> None:
        """Update trading configuration."""
        self.config = config
        self.signal_generator.update_config(config)
        self.state.paper_trading = config.paper_trading
        self.state.algo_enabled = config.algo_enabled
        if self.order_executor:
            self.order_executor.update_config(config)

        # Apply leverage settings if adapters are configured
        # Note: This may be called from Flask thread without an event loop
        if self.futures_adapter and not config.paper_trading:
            try:
                loop = asyncio.get_running_loop()
                asyncio.create_task(self._apply_leverage_settings())
            except RuntimeError:
                # No running loop - leverage will be applied on next trade or engine restart
                logger.debug("Skipping leverage update (no event loop) - will apply on next trade")

        logger.debug("Trading config updated: asset=%s, paper=%s, algo=%s, exec_mode=%s",
                     config.asset, config.paper_trading, config.algo_enabled,
                     config.order_execution_mode)

    async def _cleanup_orphan_orders(self) -> None:
        """
        Cancel any pending orders from previous sessions.

        This prevents orphan orders from accumulating if the app crashes or restarts.
        """
        if self.state.paper_trading:
            return  # No cleanup needed for paper trading

        try:
            cancelled_count = 0

            # Clean up spot orders
            if self.spot_adapter and hasattr(self.spot_adapter, 'cancel_all_orders'):
                count = await self.spot_adapter.cancel_all_orders(
                    symbol=self.config.spot_symbol,
                    inst_type="SPOT"
                )
                cancelled_count += count

            # Clean up futures orders
            if self.futures_adapter and hasattr(self.futures_adapter, 'cancel_all_orders'):
                count = await self.futures_adapter.cancel_all_orders(
                    symbol=self.config.futures_symbol,
                    inst_type="SWAP"
                )
                cancelled_count += count

            if cancelled_count > 0:
                logger.info("Cleaned up %d orphan orders from previous session", cancelled_count)

        except Exception as e:
            logger.error("Error cleaning up orphan orders: %s", e)

    async def _apply_leverage_settings(self) -> None:
        """Apply leverage settings to exchange."""
        try:
            # Set futures leverage
            if self.futures_adapter and hasattr(self.futures_adapter, 'set_leverage'):
                success = await self.futures_adapter.set_leverage(
                    self.config.futures_symbol,
                    self.config.futures_leverage,
                )
                if success:
                    logger.info("Futures leverage set to %dx", self.config.futures_leverage)
                else:
                    logger.warning("Failed to set futures leverage")

            # Note: Spot margin leverage may require different API calls
            # depending on exchange implementation

        except Exception as e:
            logger.error("Error applying leverage settings: %s", e)

    def set_adapters(self, spot: Optional[ExchangeAdapter], futures: Optional[ExchangeAdapter]) -> None:
        """Set exchange adapters (REST mode)."""
        self.spot_adapter = spot
        self.futures_adapter = futures
        self._use_websocket = False

        # Initialize order executor if we have both adapters
        if spot and futures:
            self.order_executor = OrderExecutor(self.config, spot, futures)
            logger.debug("Order executor initialized (mode=%s)", self.config.order_execution_mode)

            # Mark that we need to apply leverage settings when engine starts
            self._pending_leverage_setup = True

        logger.debug("Adapters set (REST mode): spot=%s, futures=%s",
                     type(spot).__name__ if spot else None,
                     type(futures).__name__ if futures else None)

    def set_websocket_manager(self, ws_manager: OKXWebSocketManager) -> None:
        """Set WebSocket manager for real-time streaming."""
        self.ws_manager = ws_manager
        self._use_websocket = True

        # Set up tick callback
        ws_manager.add_tick_callback(self._on_websocket_tick)

        logger.debug("WebSocket manager set (streaming mode)")

    def _on_websocket_tick(self, symbol: str, tick: MarketTick) -> None:
        """Handle incoming WebSocket tick."""
        # Update the appropriate tick based on symbol
        if symbol == self.config.spot_symbol:
            self.spot_tick = tick
        elif symbol == self.config.futures_symbol:
            self.futures_tick = tick

        # Process tick if we have both AND no tick is currently being processed
        # Without this guard, rapid WebSocket ticks spawn concurrent tasks that
        # all see current_position=NONE and place duplicate orders simultaneously
        if self.spot_tick and self.futures_tick and not self._processing_tick:
            asyncio.create_task(self._run_tick_guarded())

    async def _run_tick_guarded(self) -> None:
        """Process a tick with a guard to prevent concurrent execution."""
        if self._processing_tick:
            return  # Already processing, skip this tick
        self._processing_tick = True
        try:
            await self._process_tick_pair()
        finally:
            self._processing_tick = False

    def toggle_algo(self, enabled: bool) -> None:
        """Enable or disable algorithmic trading."""
        self.state.algo_enabled = enabled
        self.config.algo_enabled = enabled
        logger.info("Algo trading %s", "enabled" if enabled else "disabled")

    async def start(self) -> None:
        """Start the trading engine."""
        if self._running:
            logger.warning("Engine already running")
            return

        self._running = True
        self.state.is_running = True
        self.state.error = ""

        logger.info("Starting trading engine for %s (websocket=%s)",
                    self.config.asset, self._use_websocket)

        # Log comprehensive startup summary for monitoring
        self._log_startup_summary()

        # Clean up any orphan orders from previous sessions
        await self._cleanup_orphan_orders()

        # Apply pending leverage settings (deferred from set_adapters)
        if self._pending_leverage_setup:
            self._pending_leverage_setup = False
            await self._apply_leverage_settings()

        # Start WebSocket if configured
        if self._use_websocket and self.ws_manager:
            success = await self.ws_manager.start(
                self.config.spot_symbol,
                self.config.futures_symbol
            )
            if success:
                logger.debug("WebSocket streaming started for %s, %s",
                             self.config.spot_symbol, self.config.futures_symbol)
            else:
                logger.warning("WebSocket start failed, falling back to REST polling")
                self._use_websocket = False

        # Start main loop (for REST polling or as a fallback)
        if not self._use_websocket:
            self._task = asyncio.create_task(self._main_loop())

    async def stop(self) -> None:
        """Stop the trading engine."""
        self._running = False
        self.state.is_running = False

        # Stop WebSocket if running
        if self.ws_manager:
            await self.ws_manager.stop()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        logger.info("Trading engine stopped")

    async def _main_loop(self) -> None:
        """Main trading loop."""
        logger.debug("Main loop started")

        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(self.tick_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                error_msg = f"Error in main loop: {str(e)}"
                logger.exception(error_msg)
                self.state.error = error_msg
                if self.on_error:
                    self.on_error(error_msg)
                await asyncio.sleep(1)  # Wait before retrying

        logger.debug("Main loop ended")

    async def _tick(self) -> None:
        """Process one tick (REST polling mode)."""
        # Fetch current prices
        spot_tick = await self._get_spot_tick()
        futures_tick = await self._get_futures_tick()

        if not spot_tick or not futures_tick:
            return

        self.spot_tick = spot_tick
        self.futures_tick = futures_tick

        await self._process_tick_pair()

    async def _process_tick_pair(self) -> None:
        """Process a pair of spot/futures ticks (shared by REST and WebSocket modes)."""
        if not self.spot_tick or not self.futures_tick:
            return

        self.state.last_tick_time = datetime.utcnow()

        # Periodic position reconciliation (every 60 seconds)
        if not self.state.paper_trading:
            await self._periodic_position_check()

        # Update signal generator with position
        self.signal_generator.set_position(self.state.current_position)

        # Add tick to signal generator
        self.signal_generator.add_tick(self.spot_tick, self.futures_tick)

        # Notify tick callback
        if self.on_tick:
            self.on_tick(self.spot_tick, self.futures_tick)

        # Generate signal
        signal = self.signal_generator.generate_signal()
        self.state.last_signal = signal

        # Notify signal callback
        if self.on_signal:
            self.on_signal(signal)

        # Execute trading logic if algo enabled
        if self.state.algo_enabled and signal.signal_type != "NONE":
            await self._process_signal(signal)

    async def _get_spot_tick(self) -> Optional[MarketTick]:
        """Get current spot price."""
        if self.spot_adapter:
            try:
                return await self.spot_adapter.get_tick(self.config.spot_symbol)
            except Exception as e:
                logger.error("Error fetching spot tick: %s", e)

        # Paper trading fallback - simulate price
        if self.state.paper_trading:
            return self._simulate_tick(self.config.spot_symbol, is_spot=True)

        return None

    async def _get_futures_tick(self) -> Optional[MarketTick]:
        """Get current futures price."""
        if self.futures_adapter:
            try:
                return await self.futures_adapter.get_tick(self.config.futures_symbol)
            except Exception as e:
                logger.error("Error fetching futures tick: %s", e)

        # Paper trading fallback - simulate price
        if self.state.paper_trading:
            return self._simulate_tick(self.config.futures_symbol, is_spot=False)

        return None

    def _simulate_tick(self, symbol: str, is_spot: bool) -> MarketTick:
        """Simulate a market tick for paper trading."""
        import random

        # Base prices for different assets
        base_prices = {
            'BTC': 65000.0,
            'ETH': 3500.0,
            'SOL': 150.0,
            'XRP': 0.55,
            'DOGE': 0.12,
            'AVAX': 35.0,
            'LINK': 15.0,
        }

        asset = self.config.asset
        base = base_prices.get(asset, 100.0)

        # Add some randomness
        noise = random.gauss(0, base * 0.0001)
        price = base + noise

        # Futures typically trade at slight premium/discount
        if not is_spot:
            # Random basis between -0.1% and +0.3%
            basis = random.uniform(-0.001, 0.003)
            price = price * (1 + basis)

        spread_bps = random.uniform(1, 5)
        half_spread = (spread_bps / 10000) * price / 2

        return MarketTick(
            symbol=symbol,
            bid=price - half_spread,
            ask=price + half_spread,
            last=price,
            volume_24h=random.uniform(1000000, 10000000),
            timestamp=datetime.utcnow(),
        )

    async def _process_signal(self, signal: Signal) -> None:
        """Process a trading signal."""
        logger.debug("Processing signal: %s (zscore=%.4f, position=%s)",
                     signal.signal_type, signal.zscore, self.state.current_position)

        if signal.signal_type in ("LONG", "SHORT"):
            await self._open_position(signal)
        elif signal.signal_type in ("EXIT", "STOP_LOSS"):
            await self._close_position(signal)

    async def _open_position(self, signal: Signal) -> None:
        """Open a new position."""
        if self.state.current_position != "NONE":
            logger.warning("Already in position, ignoring entry signal")
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': f"Already in {self.state.current_position} position",
            }
            return

        # Check if already executing a trade (prevents duplicate orders)
        if self._executing_trade:
            logger.debug("Trade execution in progress, ignoring signal")
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': "Trade execution already in progress",
            }
            return

        # Check post-stop-loss cooldown
        if self._stop_loss_cooldown_until and datetime.utcnow() < self._stop_loss_cooldown_until:
            remaining = (self._stop_loss_cooldown_until - datetime.utcnow()).total_seconds()
            logger.debug("Stop-loss cooldown active, %.0fs remaining", remaining)
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': f"Stop-loss cooldown ({int(remaining)}s remaining)",
            }
            return

        # Check general entry cooldown (prevents rapid re-entry after any trade)
        if self._entry_cooldown_until and datetime.utcnow() < self._entry_cooldown_until:
            remaining = (self._entry_cooldown_until - datetime.utcnow()).total_seconds()
            logger.debug("Entry cooldown active, %.0fs remaining", remaining)
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': f"Entry cooldown ({int(remaining)}s remaining)",
            }
            return

        # SAFETY: Verify no existing position on exchange before entering
        if self.config.verify_exchange_position and not self.state.paper_trading:
            existing_position = await self._check_exchange_position()
            if existing_position:
                logger.warning("Exchange has existing position! Blocking entry. Position: %s", existing_position)
                self.signal_generator.last_blocked_signal = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'would_be_signal': signal.signal_type,
                    'zscore': round(signal.zscore, 4),
                    'reason': f"Exchange already has position: {existing_position}",
                }
                return

        # SAFETY: Check for existing open orders before placing new ones
        # This prevents placing duplicate orders when previous ones are still pending
        if not self.state.paper_trading:
            open_order_count = await self._count_open_orders()
            if open_order_count > 0:
                logger.warning("Exchange already has %d open order(s) - blocking new entry to prevent duplicates",
                               open_order_count)
                # Apply a short cooldown to give time for existing orders to resolve
                self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=30)
                self.signal_generator.last_blocked_signal = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'would_be_signal': signal.signal_type,
                    'zscore': round(signal.zscore, 4),
                    'reason': f"Exchange has {open_order_count} open order(s) already pending",
                }
                return

        if not self.spot_tick or not self.futures_tick:
            logger.warning("No tick data available")
            self.signal_generator.last_blocked_signal = {
                'timestamp': datetime.utcnow().isoformat(),
                'would_be_signal': signal.signal_type,
                'zscore': round(signal.zscore, 4),
                'reason': "No price data available",
            }
            return

        position_type = signal.signal_type  # LONG or SHORT
        spot_price = self.spot_tick.mid
        futures_price = self.futures_tick.mid

        # Calculate quantity
        quantity = self.config.position_size_usd / spot_price

        # Create trade record
        trade = Trade(
            asset=self.config.asset,
            position_type=position_type,
            entry_time=datetime.utcnow(),
            entry_spot_price=spot_price,
            entry_futures_price=futures_price,
            entry_spread=signal.spread,
            entry_zscore=signal.zscore,
            quantity=quantity,
            notional_usd=self.config.position_size_usd,
            is_open=True,
            is_paper=self.state.paper_trading,
        )

        # Execute orders if not paper trading
        if not self.state.paper_trading:
            self._executing_trade = True
            try:
                success = await self._execute_entry_orders(trade, signal)
                if not success:
                    # Apply cooldown after any failed order to prevent rapid retry
                    # This is critical: without this, the engine retries on every tick
                    cooldown_sec = max(30, getattr(self.config, 'entry_cooldown_seconds', 60))
                    self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=cooldown_sec)
                    logger.warning("Entry orders failed - applying %ds cooldown to prevent rapid retry",
                                   cooldown_sec)
                    return
            finally:
                self._executing_trade = False

        self.open_trade = trade
        self.state.current_position = position_type
        self.signal_generator.set_position(position_type)

        logger.info("Opened %s position: qty=%.6f, spot=%.2f, futures=%.2f, spread=%.6f, zscore=%.4f",
                    position_type, quantity, spot_price, futures_price, signal.spread, signal.zscore)

        if self.on_trade:
            self.on_trade(trade)

    async def _close_position(self, signal: Signal) -> None:
        """Close current position."""
        if self.state.current_position == "NONE" or not self.open_trade:
            logger.warning("No position to close")
            return

        if not self.spot_tick or not self.futures_tick:
            logger.warning("No tick data available")
            return

        trade = self.open_trade
        spot_price = self.spot_tick.mid
        futures_price = self.futures_tick.mid

        # Calculate P&L
        if trade.position_type == "LONG":
            # Long spread: bought spot, sold futures
            # P&L = (exit_spread - entry_spread) * quantity
            spread_change = signal.spread - trade.entry_spread
            pnl = spread_change * trade.quantity
        else:
            # Short spread: sold spot, bought futures
            # P&L = (entry_spread - exit_spread) * quantity
            spread_change = trade.entry_spread - signal.spread
            pnl = spread_change * trade.quantity

        pnl_percent = (pnl / trade.notional_usd) * 100 if trade.notional_usd > 0 else 0

        # Update trade record
        trade.exit_time = datetime.utcnow()
        trade.exit_spot_price = spot_price
        trade.exit_futures_price = futures_price
        trade.exit_spread = signal.spread
        trade.exit_zscore = signal.zscore
        trade.exit_reason = signal.signal_type
        trade.pnl_usd = pnl
        trade.pnl_percent = pnl_percent
        trade.is_open = False

        # Execute orders if not paper trading
        if not self.state.paper_trading:
            self._executing_trade = True
            try:
                await self._execute_exit_orders(trade, signal)
            finally:
                self._executing_trade = False

        logger.info("Closed %s position: pnl=$%.2f (%.2f%%), reason=%s, zscore=%.4f",
                    trade.position_type, pnl, pnl_percent, signal.signal_type, signal.zscore)

        # Reset state
        self.state.current_position = "NONE"
        self.signal_generator.set_position("NONE")
        self.open_trade = None

        # Apply post-stop-loss cooldown to prevent immediate re-entry
        if signal.signal_type == "STOP_LOSS":
            from datetime import timedelta
            self._stop_loss_cooldown_until = datetime.utcnow() + timedelta(seconds=self._stop_loss_cooldown_sec)
            logger.info("Stop-loss cooldown active for %ds", self._stop_loss_cooldown_sec)

        # Apply general entry cooldown after any trade
        from datetime import timedelta
        cooldown_sec = getattr(self.config, 'entry_cooldown_seconds', 60)
        if cooldown_sec > 0:
            self._entry_cooldown_until = datetime.utcnow() + timedelta(seconds=cooldown_sec)
            logger.info("Entry cooldown active for %ds", cooldown_sec)

        if self.on_trade:
            self.on_trade(trade)

    async def _check_exchange_position(self) -> Optional[str]:
        """
        Check if there's an existing position on the exchange.

        Returns a description of the position if one exists, None otherwise.
        This prevents duplicate entries when engine state is out of sync.
        """
        try:
            existing_positions = []

            # Check futures positions
            if self.futures_adapter and hasattr(self.futures_adapter, 'get_positions'):
                positions = await self.futures_adapter.get_positions(self.config.futures_symbol)
                for pos in positions:
                    if pos.quantity > 0:
                        existing_positions.append(f"Futures {pos.side} {pos.quantity:.6f}")

            # Check spot balance (simplified - just check if we have the asset)
            # Note: For spot, we'd need to track what was bought for arbitrage vs held
            # For now, we focus on futures positions which are clearer indicators

            if existing_positions:
                return ", ".join(existing_positions)

            return None

        except Exception as e:
            logger.warning("Error checking exchange positions: %s", e)
            # On error, allow the trade but log warning
            return None

    async def _count_open_orders(self) -> int:
        """
        Count open/pending orders on the exchange for the current symbols.

        Used to prevent placing new orders when previous ones are still pending.
        Returns the total count across spot and futures.
        On error, returns 0 (fail open - allow trading rather than blocking indefinitely).
        """
        count = 0
        try:
            # Use get_pending_orders which queries /api/v5/trade/orders-pending
            if self.futures_adapter and hasattr(self.futures_adapter, 'get_pending_orders'):
                futures_orders = await self.futures_adapter.get_pending_orders(
                    symbol=self.config.futures_symbol
                )
                count += len(futures_orders) if futures_orders else 0

            if self.spot_adapter and hasattr(self.spot_adapter, 'get_pending_orders'):
                spot_orders = await self.spot_adapter.get_pending_orders(
                    symbol=self.config.spot_symbol
                )
                count += len(spot_orders) if spot_orders else 0

            if count > 0:
                logger.warning("Found %d open/pending order(s) on exchange - blocking new entry", count)

        except Exception as e:
            logger.warning("Error counting open orders: %s", e)

        return count

    async def verify_position_sync(self) -> Dict[str, Any]:
        """
        Verify that engine position state matches exchange positions.

        This is called periodically to detect orphaned/lost positions.
        Returns a dict with mismatch info if any discrepancy is found.
        """
        result = {
            'checked': True,
            'mismatch': False,
            'engine_position': self.state.current_position,
            'exchange_positions': [],
            'mismatch_reason': None,
        }

        try:
            # Get actual exchange positions
            if self.futures_adapter and hasattr(self.futures_adapter, 'get_positions'):
                positions = await self.futures_adapter.get_positions()

                for pos in positions:
                    if pos.quantity > 0:
                        result['exchange_positions'].append({
                            'symbol': pos.symbol,
                            'side': pos.side,
                            'quantity': pos.quantity,
                            'entry_price': pos.entry_price,
                            'unrealized_pnl': pos.unrealized_pnl,
                        })

            engine_has_position = self.state.current_position != "NONE"
            exchange_has_position = len(result['exchange_positions']) > 0

            # Check for mismatches
            if engine_has_position and not exchange_has_position:
                result['mismatch'] = True
                result['mismatch_reason'] = "Engine shows position but exchange has none (manually closed?)"
                logger.warning("Position mismatch: Engine=%s but exchange has no positions",
                             self.state.current_position)

            elif not engine_has_position and exchange_has_position:
                result['mismatch'] = True
                total_size = sum(p['quantity'] for p in result['exchange_positions'])
                total_pnl = sum(p['unrealized_pnl'] for p in result['exchange_positions'])
                result['mismatch_reason'] = f"Exchange has {len(result['exchange_positions'])} position(s) but engine shows FLAT"
                logger.warning("Position mismatch: Engine=FLAT but exchange has %d positions (size=%.2f, PnL=%.2f)",
                             len(result['exchange_positions']), total_size, total_pnl)

            # Store mismatch state for status reporting
            self._position_mismatch = result if result['mismatch'] else None
            self._last_position_verify = datetime.utcnow()

            return result

        except Exception as e:
            logger.warning("Error verifying position sync: %s", e)
            result['error'] = str(e)
            return result

    async def _periodic_position_check(self) -> None:
        """Run position verification if enough time has passed."""
        now = datetime.utcnow()

        if self._last_position_verify is None:
            # First check - do it
            await self.verify_position_sync()
        elif (now - self._last_position_verify).total_seconds() >= self._position_verify_interval:
            # Time for another check
            await self.verify_position_sync()

    async def _verify_leverage_settings(self) -> bool:
        """
        Verify that leverage settings on the exchange match our config.

        Returns True if leverage is correct or was successfully corrected.
        """
        if not self.futures_adapter:
            return True

        try:
            # Check current leverage on futures
            if hasattr(self.futures_adapter, 'get_leverage'):
                current_leverage = await self.futures_adapter.get_leverage(self.config.futures_symbol)
                if current_leverage is not None and current_leverage != self.config.futures_leverage:
                    logger.warning("Leverage mismatch: exchange=%dx, config=%dx. Attempting to correct...",
                                 current_leverage, self.config.futures_leverage)
                    # Try to set correct leverage
                    success = await self.futures_adapter.set_leverage(
                        self.config.futures_symbol,
                        self.config.futures_leverage
                    )
                    if success:
                        logger.info("Leverage corrected to %dx", self.config.futures_leverage)
                    else:
                        logger.error("Failed to correct leverage - trading with %dx instead of %dx",
                                   current_leverage, self.config.futures_leverage)
                        # Return False to block trading if leverage mismatch is critical
                        # For now, we'll warn but continue
                        return True
                else:
                    logger.info("✅ Leverage verified on exchange: %dx matches config", self.config.futures_leverage)
            return True
        except Exception as e:
            logger.error("Error verifying leverage: %s", e)
            return True  # Don't block trading on verification error

    async def _execute_entry_orders(self, trade: Trade, signal: Signal) -> bool:
        """Execute entry orders on exchanges using the order executor."""
        if not self.order_executor:
            logger.error("Order executor not configured for live trading")
            return False

        if not self.spot_tick or not self.futures_tick:
            logger.error("No tick data available for order execution")
            return False

        # Verify leverage settings before trading
        await self._verify_leverage_settings()

        # Track order attempts
        self._spot_order_attempts += 1
        self._futures_order_attempts += 1

        try:
            spread_order = await self.order_executor.execute_entry(
                position_type=signal.signal_type,
                spot_tick=self.spot_tick,
                futures_tick=self.futures_tick,
                quantity=trade.quantity,
            )

            if spread_order and spread_order.is_complete:
                trade.spot_order_id = spread_order.spot_leg.order_id
                trade.futures_order_id = spread_order.futures_leg.order_id
                # Update actual fill prices
                trade.entry_spot_price = spread_order.spot_leg.filled_price
                trade.entry_futures_price = spread_order.futures_leg.filled_price
                logger.info("ENTRY SUCCESS: mode=%s, spot_id=%s @ $%.2f, futures_id=%s @ $%.2f",
                            self.config.order_execution_mode,
                            trade.spot_order_id, trade.entry_spot_price,
                            trade.futures_order_id, trade.entry_futures_price)

                # Log to CSV for post-analysis
                csv_logger = get_trade_logger()
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

                # Log periodic stats
                self._log_order_stats()
                return True
            else:
                # Track which leg failed for pattern detection
                if spread_order:
                    from core.order_executor import LegStatus
                    failed_states = (LegStatus.FAILED, LegStatus.CANCELLED)
                    spot_failed = spread_order.spot_leg.status in failed_states
                    futures_failed = spread_order.futures_leg.status in failed_states

                    if spot_failed:
                        self._spot_order_failures += 1
                        logger.error("SPOT LEG FAILED: status=%s, error=%s",
                                    spread_order.spot_leg.status.name,
                                    getattr(spread_order.spot_leg, 'error', 'unknown'))
                    if futures_failed:
                        self._futures_order_failures += 1
                        logger.error("FUTURES LEG FAILED: status=%s",
                                    spread_order.futures_leg.status.name)

                    # CRITICAL: Detect spot-only failure pattern
                    self._check_spot_failure_pattern()

                error = "Spread order failed or incomplete"
                if spread_order and spread_order.has_partial_fill:
                    error = "Spread order had partial fill - leg risk handled"
                logger.error(error)
                self.state.error = error
                return False

        except Exception as e:
            error = f"Error executing entry orders: {str(e)}"
            logger.exception(error)
            self.state.error = error
            self._spot_order_failures += 1
            self._futures_order_failures += 1
            return False

    def _check_spot_failure_pattern(self) -> None:
        """
        Detect if spot orders are failing repeatedly while futures succeed.
        This is a CRITICAL pattern that indicates a systematic issue.
        """
        if self._spot_order_attempts < 3:
            return  # Need at least 3 attempts to detect pattern

        spot_fail_rate = self._spot_order_failures / self._spot_order_attempts
        futures_fail_rate = self._futures_order_failures / self._futures_order_attempts if self._futures_order_attempts > 0 else 0

        # Pattern: Spot failing >50% while futures failing <20%
        if spot_fail_rate > 0.5 and futures_fail_rate < 0.2:
            logger.critical(
                "🚨 SPOT-ONLY FAILURE PATTERN DETECTED! "
                "Spot: %d/%d failed (%.0f%%), Futures: %d/%d failed (%.0f%%). "
                "Check spot adapter, symbol config, or exchange permissions.",
                self._spot_order_failures, self._spot_order_attempts, spot_fail_rate * 100,
                self._futures_order_failures, self._futures_order_attempts, futures_fail_rate * 100
            )
            self.state.error = f"CRITICAL: Spot orders failing {spot_fail_rate*100:.0f}% of the time"

            # Log to CSV for post-analysis
            csv_logger = get_trade_logger()
            csv_logger.log_spot_failure_pattern(
                self._spot_order_attempts, self._spot_order_failures,
                self._futures_order_attempts, self._futures_order_failures
            )

    def _log_order_stats(self) -> None:
        """Log order execution statistics periodically for monitoring."""
        now = datetime.utcnow()
        if self._last_order_stats_log and (now - self._last_order_stats_log).total_seconds() < self._order_stats_log_interval:
            return

        self._last_order_stats_log = now
        logger.info(
            "📊 ORDER STATS: Spot %d/%d (%.0f%% success), Futures %d/%d (%.0f%% success)",
            self._spot_order_attempts - self._spot_order_failures,
            self._spot_order_attempts,
            (1 - self._spot_order_failures / self._spot_order_attempts) * 100 if self._spot_order_attempts > 0 else 100,
            self._futures_order_attempts - self._futures_order_failures,
            self._futures_order_attempts,
            (1 - self._futures_order_failures / self._futures_order_attempts) * 100 if self._futures_order_attempts > 0 else 100
        )

        # Log to CSV for post-analysis
        csv_logger = get_trade_logger()
        csv_logger.log_order_stats(
            self._spot_order_attempts, self._spot_order_failures,
            self._futures_order_attempts, self._futures_order_failures
        )

    def _log_startup_summary(self) -> None:
        """Log comprehensive startup summary for monitoring and debugging."""
        cfg = self.config
        logger.info("=" * 60)
        logger.info("🚀 TRADING ENGINE STARTUP SUMMARY")
        logger.info("=" * 60)
        logger.info("SYMBOLS: spot=%s, futures=%s", cfg.spot_symbol, cfg.futures_symbol)
        logger.info("MODE: paper=%s, algo=%s", cfg.paper_trading, cfg.algo_enabled)
        logger.info("POSITION SIZE: $%s (max: $%s)", cfg.position_size_usd, cfg.max_position_size_usd)
        logger.info("LEVERAGE: spot=%dx, futures=%dx", cfg.spot_leverage, cfg.futures_leverage)
        logger.info("FEES (bps): spot_maker=%.1f, spot_taker=%.1f, fut_maker=%.1f, fut_taker=%.1f",
                   getattr(cfg, 'spot_maker_fee_bps', 8),
                   getattr(cfg, 'spot_taker_fee_bps', 10),
                   getattr(cfg, 'futures_maker_fee_bps', 2),
                   getattr(cfg, 'futures_taker_fee_bps', 5))
        logger.info("EXECUTION: entry=%s, exit=%s",
                   getattr(cfg, 'entry_execution_mode', 'LIMIT'),
                   getattr(cfg, 'exit_execution_mode', 'MARKET'))
        logger.info("TIMEOUTS: limit_order=%ds, orphan_recovery=%ds, entry_cooldown=%ds",
                   cfg.limit_order_timeout_sec,
                   getattr(cfg, 'orphan_recovery_timeout_sec', 60),
                   getattr(cfg, 'entry_cooldown_seconds', 60))
        logger.info("SIGNALS: z_entry=%.2f, z_exit=%.2f, stop_loss=%.2f",
                   cfg.z_score_entry_threshold,
                   cfg.z_score_exit_threshold,
                   cfg.stop_loss_z_score)
        logger.info("FILTERS: hurst=%s (threshold=%.2f), std=%s (min=%.1fx)",
                   cfg.hurst_enabled, cfg.hurst_threshold,
                   cfg.std_filter_enabled, cfg.min_std_multiple)
        logger.info("=" * 60)

        # Also log to CSV for easy reference
        csv_logger = get_trade_logger()
        csv_logger.log_startup(cfg.to_dict())

    async def _execute_exit_orders(self, trade: Trade, signal: Signal) -> bool:
        """Execute exit orders on exchanges using the order executor."""
        if not self.order_executor:
            logger.error("Order executor not configured for live trading")
            return False

        if not self.spot_tick or not self.futures_tick:
            logger.error("No tick data available for order execution")
            return False

        try:
            spread_order = await self.order_executor.execute_exit(
                position_type=trade.position_type,
                spot_tick=self.spot_tick,
                futures_tick=self.futures_tick,
                quantity=trade.quantity,
            )

            if spread_order and spread_order.is_complete:
                # Update actual exit prices from fills
                trade.exit_spot_price = spread_order.spot_leg.filled_price
                trade.exit_futures_price = spread_order.futures_leg.filled_price
                logger.info("Exit orders executed: mode=%s", self.config.order_execution_mode)
                return True
            else:
                error = "Exit spread order failed or incomplete"
                if spread_order and spread_order.has_partial_fill:
                    error = "Exit order had partial fill - leg risk handled"
                logger.error(error)
                # Still return True since leg risk is handled
                return True

        except Exception as e:
            logger.exception("Error executing exit orders: %s", e)
            return False

    def get_status(self) -> Dict[str, Any]:
        """Get current engine status."""
        signal_state = self.signal_generator.get_state()

        # Calculate stop-loss cooldown remaining
        sl_cooldown_remaining = 0
        if self._stop_loss_cooldown_until and datetime.utcnow() < self._stop_loss_cooldown_until:
            sl_cooldown_remaining = round((self._stop_loss_cooldown_until - datetime.utcnow()).total_seconds())

        return {
            'is_running': self.state.is_running,
            'algo_enabled': self.state.algo_enabled,
            'paper_trading': self.state.paper_trading,
            'asset': self.config.asset,
            'position': self.state.current_position,
            'last_tick_time': self.state.last_tick_time.isoformat() if self.state.last_tick_time else None,
            'error': self.state.error,
            'spot_connected': self.spot_adapter is not None,
            'futures_connected': self.futures_adapter is not None,
            'signal': signal_state,
            'spot_tick': self.spot_tick.to_dict() if self.spot_tick else None,
            'futures_tick': self.futures_tick.to_dict() if self.futures_tick else None,
            'open_trade': self.open_trade.to_dict() if self.open_trade else None,
            'sl_cooldown_remaining': sl_cooldown_remaining,
            'sl_cooldown_sec': self._stop_loss_cooldown_sec,
            'executing_trade': self._executing_trade,
            'position_mismatch': self._position_mismatch,
        }

    def get_spread_history(self, n: int = 100) -> List[float]:
        """Get spread history for charting."""
        return self.signal_generator.get_spread_history(n)

    def get_zscore_history(self, n: int = 100) -> List[float]:
        """Get Z-score history for charting."""
        return self.signal_generator.get_zscore_history(n)

    def reset(self) -> None:
        """Reset engine state."""
        self.signal_generator.reset()
        self.state = EngineState(paper_trading=self.config.paper_trading)
        self.open_trade = None
        self.spot_tick = None
        self.futures_tick = None
        self._stop_loss_cooldown_until = None
        self._executing_trade = False
        logger.debug("Engine reset")
