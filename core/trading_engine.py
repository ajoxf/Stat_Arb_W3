"""
Trading engine for crypto statistical arbitrage.
Manages the main trading loop, position management, and order execution.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List
from dataclasses import dataclass

from models import (
    TradingConfig, Trade, MarketTick, Signal, Position,
    OrderResult, CRYPTO_ASSETS, get_symbols_for_asset
)
from core.signals import SignalGenerator
from core.order_executor import OrderExecutor
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
        if self.futures_adapter and not config.paper_trading:
            asyncio.create_task(self._apply_leverage_settings())

        logger.info("Trading config updated: asset=%s, paper=%s, algo=%s, exec_mode=%s",
                    config.asset, config.paper_trading, config.algo_enabled,
                    config.order_execution_mode)

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
            logger.info("Order executor initialized (mode=%s)", self.config.order_execution_mode)

            # Mark that we need to apply leverage settings when engine starts
            self._pending_leverage_setup = True

        logger.info("Adapters set (REST mode): spot=%s, futures=%s",
                    type(spot).__name__ if spot else None,
                    type(futures).__name__ if futures else None)

    def set_websocket_manager(self, ws_manager: OKXWebSocketManager) -> None:
        """Set WebSocket manager for real-time streaming."""
        self.ws_manager = ws_manager
        self._use_websocket = True

        # Set up tick callback
        ws_manager.add_tick_callback(self._on_websocket_tick)

        logger.info("WebSocket manager set (streaming mode)")

    def _on_websocket_tick(self, symbol: str, tick: MarketTick) -> None:
        """Handle incoming WebSocket tick."""
        # Update the appropriate tick based on symbol
        if symbol == self.config.spot_symbol:
            self.spot_tick = tick
        elif symbol == self.config.futures_symbol:
            self.futures_tick = tick

        # Process tick if we have both
        if self.spot_tick and self.futures_tick:
            # Use create_task to avoid blocking the WebSocket callback
            asyncio.create_task(self._process_tick_pair())

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
                logger.info("WebSocket streaming started for %s, %s",
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
        logger.info("Main loop started")

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

        logger.info("Main loop ended")

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
        logger.info("Processing signal: %s (zscore=%.4f, position=%s)",
                    signal.signal_type, signal.zscore, self.state.current_position)

        if signal.signal_type in ("LONG", "SHORT"):
            await self._open_position(signal)
        elif signal.signal_type in ("EXIT", "STOP_LOSS"):
            await self._close_position(signal)

    async def _open_position(self, signal: Signal) -> None:
        """Open a new position."""
        if self.state.current_position != "NONE":
            logger.warning("Already in position, ignoring entry signal")
            return

        if not self.spot_tick or not self.futures_tick:
            logger.warning("No tick data available")
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
            success = await self._execute_entry_orders(trade, signal)
            if not success:
                return

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
            await self._execute_exit_orders(trade, signal)

        logger.info("Closed %s position: pnl=$%.2f (%.2f%%), reason=%s, zscore=%.4f",
                    trade.position_type, pnl, pnl_percent, signal.signal_type, signal.zscore)

        # Reset state
        self.state.current_position = "NONE"
        self.signal_generator.set_position("NONE")
        self.open_trade = None

        if self.on_trade:
            self.on_trade(trade)

    async def _execute_entry_orders(self, trade: Trade, signal: Signal) -> bool:
        """Execute entry orders on exchanges using the order executor."""
        if not self.order_executor:
            logger.error("Order executor not configured for live trading")
            return False

        if not self.spot_tick or not self.futures_tick:
            logger.error("No tick data available for order execution")
            return False

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
                logger.info("Entry orders executed: mode=%s, spot_id=%s, futures_id=%s",
                            self.config.order_execution_mode,
                            trade.spot_order_id, trade.futures_order_id)
                return True
            else:
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
            return False

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
        logger.info("Engine reset")
