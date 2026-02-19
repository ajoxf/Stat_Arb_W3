"""
Order execution module for crypto statistical arbitrage.

Supports two modes:
1. MARKET: Immediate execution with market orders (higher cost, guaranteed fill)
2. LIMIT: Pegged limit orders that track best bid/ask (lower cost, may not fill)

For spread trades, both legs must be managed simultaneously to avoid leg risk.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass
from enum import Enum

from models import TradingConfig, OrderResult, MarketTick
from adapters.base import ExchangeAdapter

logger = logging.getLogger(__name__)


class ExecutionMode(Enum):
    """Order execution mode."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class LegStatus(Enum):
    """Status of a single leg in a spread trade."""
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class LegOrder:
    """Represents one leg of a spread trade."""
    symbol: str
    side: str  # BUY or SELL
    quantity: float
    target_price: float = 0.0
    order_id: str = ""
    status: LegStatus = LegStatus.PENDING
    filled_qty: float = 0.0
    filled_price: float = 0.0
    last_update: Optional[datetime] = None
    pos_side: Optional[str] = None  # For OKX long_short_mode: "long" or "short"


@dataclass
class SpreadOrder:
    """Represents a complete spread trade (both legs)."""
    spot_leg: LegOrder
    futures_leg: LegOrder
    created_at: datetime = None
    timeout_at: datetime = None
    is_entry: bool = True  # True for entry, False for exit
    position_type: str = ""  # LONG or SHORT

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()

    @property
    def is_complete(self) -> bool:
        """Check if both legs are filled."""
        return (self.spot_leg.status == LegStatus.FILLED and
                self.futures_leg.status == LegStatus.FILLED)

    @property
    def is_failed(self) -> bool:
        """Check if either leg failed."""
        return (self.spot_leg.status == LegStatus.FAILED or
                self.futures_leg.status == LegStatus.FAILED)

    @property
    def has_partial_fill(self) -> bool:
        """Check if we have a partial fill (leg risk situation)."""
        spot_filled = self.spot_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)
        futures_filled = self.futures_leg.status in (LegStatus.FILLED, LegStatus.PARTIAL)
        return spot_filled != futures_filled


class OrderExecutor:
    """
    Handles order execution for spread trades.

    Supports both market orders (immediate) and pegged limit orders
    (track best bid/ask for better fills).
    """

    # How often to update limit order prices (ms)
    PRICE_UPDATE_INTERVAL_MS = 200

    def __init__(
        self,
        config: TradingConfig,
        spot_adapter: ExchangeAdapter,
        futures_adapter: ExchangeAdapter,
    ):
        self.config = config
        self.spot_adapter = spot_adapter
        self.futures_adapter = futures_adapter

        # Current spread order being executed
        self.active_order: Optional[SpreadOrder] = None

        # Callbacks
        self.on_fill: Optional[Callable[[SpreadOrder], None]] = None
        self.on_partial_fill: Optional[Callable[[SpreadOrder], None]] = None
        self.on_timeout: Optional[Callable[[SpreadOrder], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None

        # Execution state
        self._executing = False
        self._execution_task: Optional[asyncio.Task] = None

    def update_config(self, config: TradingConfig) -> None:
        """Update configuration."""
        self.config = config

    async def execute_entry(
        self,
        position_type: str,  # LONG or SHORT
        spot_tick: MarketTick,
        futures_tick: MarketTick,
        quantity: float,
    ) -> Optional[SpreadOrder]:
        """
        Execute entry trade for a spread position.

        LONG spread: Buy spot, Sell futures
        SHORT spread: Sell spot, Buy futures

        Returns SpreadOrder with execution results.
        """
        if self._executing:
            logger.warning("Already executing an order")
            return None

        # Determine leg sides and futures pos_side for OKX long_short_mode
        # LONG spread: Buy spot, Sell futures (short position)
        # SHORT spread: Sell spot, Buy futures (long position)
        if position_type == "LONG":
            spot_side = "BUY"
            futures_side = "SELL"
            futures_pos_side = "short"  # Selling futures = opening short
        else:
            spot_side = "SELL"
            futures_side = "BUY"
            futures_pos_side = "long"  # Buying futures = opening long

        # Create spread order
        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol=self.config.spot_symbol,
                side=spot_side,
                quantity=quantity,
            ),
            futures_leg=LegOrder(
                symbol=self.config.futures_symbol,
                side=futures_side,
                quantity=quantity,
                pos_side=futures_pos_side,  # For OKX long_short_mode
            ),
            is_entry=True,
            position_type=position_type,
            timeout_at=datetime.utcnow(),
        )

        # Set timeout
        from datetime import timedelta
        spread_order.timeout_at = datetime.utcnow() + timedelta(
            seconds=self.config.limit_order_timeout_sec
        )

        return await self._execute_spread(spread_order, spot_tick, futures_tick)

    async def execute_exit(
        self,
        position_type: str,  # Current position: LONG or SHORT
        spot_tick: MarketTick,
        futures_tick: MarketTick,
        quantity: float,
    ) -> Optional[SpreadOrder]:
        """
        Execute exit trade to close a spread position.

        Close LONG spread: Sell spot, Buy futures
        Close SHORT spread: Buy spot, Sell futures
        """
        if self._executing:
            logger.warning("Already executing an order")
            return None

        # Opposite of entry, but SAME pos_side (closing the same position)
        # Close LONG spread: Sell spot, Buy futures (to close short = pos_side stays "short")
        # Close SHORT spread: Buy spot, Sell futures (to close long = pos_side stays "long")
        if position_type == "LONG":
            spot_side = "SELL"
            futures_side = "BUY"
            futures_pos_side = "short"  # Closing the short position
        else:
            spot_side = "BUY"
            futures_side = "SELL"
            futures_pos_side = "long"  # Closing the long position

        spread_order = SpreadOrder(
            spot_leg=LegOrder(
                symbol=self.config.spot_symbol,
                side=spot_side,
                quantity=quantity,
            ),
            futures_leg=LegOrder(
                symbol=self.config.futures_symbol,
                side=futures_side,
                quantity=quantity,
                pos_side=futures_pos_side,  # CRITICAL: same as entry pos_side!
            ),
            is_entry=False,
            position_type=position_type,
        )

        from datetime import timedelta
        spread_order.timeout_at = datetime.utcnow() + timedelta(
            seconds=self.config.limit_order_timeout_sec
        )

        return await self._execute_spread(spread_order, spot_tick, futures_tick)

    async def _execute_spread(
        self,
        spread_order: SpreadOrder,
        spot_tick: MarketTick,
        futures_tick: MarketTick,
    ) -> SpreadOrder:
        """Execute a spread order using configured mode."""
        self._executing = True
        self.active_order = spread_order

        try:
            if self.config.order_execution_mode == "MARKET":
                return await self._execute_market(spread_order)
            else:
                return await self._execute_limit(spread_order, spot_tick, futures_tick)
        finally:
            self._executing = False
            self.active_order = None

    async def _execute_market(self, spread_order: SpreadOrder) -> SpreadOrder:
        """Execute spread using market orders (immediate fill)."""
        logger.info("Executing spread with MARKET orders: %s %s",
                    spread_order.position_type,
                    "ENTRY" if spread_order.is_entry else "EXIT")

        # Execute both legs simultaneously
        spot_task = self._place_market_order(
            self.spot_adapter,
            spread_order.spot_leg,
        )
        futures_task = self._place_market_order(
            self.futures_adapter,
            spread_order.futures_leg,
        )

        spot_result, futures_result = await asyncio.gather(
            spot_task, futures_task, return_exceptions=True
        )

        # Process results
        if isinstance(spot_result, Exception):
            spread_order.spot_leg.status = LegStatus.FAILED
            logger.error("Spot leg failed: %s", spot_result)
        else:
            self._update_leg_from_result(spread_order.spot_leg, spot_result)

        if isinstance(futures_result, Exception):
            spread_order.futures_leg.status = LegStatus.FAILED
            logger.error("Futures leg failed: %s", futures_result)
        else:
            self._update_leg_from_result(spread_order.futures_leg, futures_result)

        # Handle partial fills (leg risk)
        if spread_order.has_partial_fill:
            logger.warning("PARTIAL FILL - Leg risk detected!")
            if self.on_partial_fill:
                self.on_partial_fill(spread_order)
            # Try to recover by market-closing the filled leg
            await self._handle_leg_risk(spread_order)

        if spread_order.is_complete and self.on_fill:
            self.on_fill(spread_order)

        return spread_order

    async def _execute_limit(
        self,
        spread_order: SpreadOrder,
        spot_tick: MarketTick,
        futures_tick: MarketTick,
    ) -> SpreadOrder:
        """Execute spread using pegged limit orders."""
        logger.info("Executing spread with LIMIT orders: %s %s",
                    spread_order.position_type,
                    "ENTRY" if spread_order.is_entry else "EXIT")

        # Calculate initial prices
        self._update_target_prices(spread_order, spot_tick, futures_tick)

        # Place initial limit orders
        await self._place_limit_orders(spread_order)

        # Monitor and adjust until filled or timeout
        while not spread_order.is_complete and not spread_order.is_failed:
            # Check timeout
            if datetime.utcnow() >= spread_order.timeout_at:
                logger.warning("Limit order timeout reached")
                await self._handle_timeout(spread_order)
                if self.on_timeout:
                    self.on_timeout(spread_order)
                break

            # Wait before next update
            await asyncio.sleep(self.PRICE_UPDATE_INTERVAL_MS / 1000)

            # Get fresh ticks
            new_spot_tick = await self.spot_adapter.get_tick(self.config.spot_symbol)
            new_futures_tick = await self.futures_adapter.get_tick(self.config.futures_symbol)

            if not new_spot_tick or not new_futures_tick:
                continue

            # Check order status
            await self._check_order_status(spread_order)

            # If not filled, update prices
            if not spread_order.is_complete:
                old_spot_price = spread_order.spot_leg.target_price
                old_futures_price = spread_order.futures_leg.target_price

                self._update_target_prices(spread_order, new_spot_tick, new_futures_tick)

                # Amend orders if prices changed significantly
                if (abs(spread_order.spot_leg.target_price - old_spot_price) > 0.01 or
                    abs(spread_order.futures_leg.target_price - old_futures_price) > 0.01):
                    await self._amend_limit_orders(spread_order)

        # Handle partial fills
        if spread_order.has_partial_fill:
            logger.warning("PARTIAL FILL after limit execution - Leg risk!")
            await self._handle_leg_risk(spread_order)

        if spread_order.is_complete and self.on_fill:
            self.on_fill(spread_order)

        return spread_order

    def _update_target_prices(
        self,
        spread_order: SpreadOrder,
        spot_tick: MarketTick,
        futures_tick: MarketTick,
    ) -> None:
        """Calculate target prices for limit orders based on current orderbook."""
        offset_bps = self.config.limit_order_price_offset_bps / 10000

        # For BUY: place slightly above best bid to be near top of book
        # For SELL: place slightly below best ask to be near top of book

        if spread_order.spot_leg.side == "BUY":
            # Want to buy: place at bid + small offset (maker, won't cross)
            spread_order.spot_leg.target_price = spot_tick.bid * (1 + offset_bps)
        else:
            # Want to sell: place at ask - small offset (maker, won't cross)
            spread_order.spot_leg.target_price = spot_tick.ask * (1 - offset_bps)

        if spread_order.futures_leg.side == "BUY":
            spread_order.futures_leg.target_price = futures_tick.bid * (1 + offset_bps)
        else:
            spread_order.futures_leg.target_price = futures_tick.ask * (1 - offset_bps)

    async def _place_market_order(
        self,
        adapter: ExchangeAdapter,
        leg: LegOrder,
    ) -> OrderResult:
        """Place a market order for a single leg."""
        return await adapter.place_order(
            symbol=leg.symbol,
            side=leg.side,
            order_type="MARKET",
            quantity=leg.quantity,
            pos_side=leg.pos_side,  # For OKX long_short_mode
        )

    async def _place_limit_orders(self, spread_order: SpreadOrder) -> None:
        """Place initial limit orders for both legs."""
        # Place spot limit order
        spot_result = await self.spot_adapter.place_order(
            symbol=spread_order.spot_leg.symbol,
            side=spread_order.spot_leg.side,
            order_type="LIMIT",
            quantity=spread_order.spot_leg.quantity,
            price=spread_order.spot_leg.target_price,
            pos_side=spread_order.spot_leg.pos_side,
        )

        if spot_result.success:
            spread_order.spot_leg.order_id = spot_result.order_id
            spread_order.spot_leg.status = LegStatus.OPEN
        else:
            spread_order.spot_leg.status = LegStatus.FAILED
            logger.error("Failed to place spot limit order: %s", spot_result.error)

        # Place futures limit order
        futures_result = await self.futures_adapter.place_order(
            symbol=spread_order.futures_leg.symbol,
            side=spread_order.futures_leg.side,
            order_type="LIMIT",
            quantity=spread_order.futures_leg.quantity,
            price=spread_order.futures_leg.target_price,
            pos_side=spread_order.futures_leg.pos_side,  # CRITICAL for OKX long_short_mode
        )

        if futures_result.success:
            spread_order.futures_leg.order_id = futures_result.order_id
            spread_order.futures_leg.status = LegStatus.OPEN
        else:
            spread_order.futures_leg.status = LegStatus.FAILED
            logger.error("Failed to place futures limit order: %s", futures_result.error)

    async def _amend_limit_orders(self, spread_order: SpreadOrder) -> None:
        """Amend (update price of) existing limit orders."""
        # Amend spot order if still open
        if spread_order.spot_leg.status == LegStatus.OPEN:
            try:
                # Cancel and replace (some exchanges support amend, but cancel/replace is universal)
                await self.spot_adapter.cancel_order(
                    spread_order.spot_leg.symbol,
                    spread_order.spot_leg.order_id,
                )
                result = await self.spot_adapter.place_order(
                    symbol=spread_order.spot_leg.symbol,
                    side=spread_order.spot_leg.side,
                    order_type="LIMIT",
                    quantity=spread_order.spot_leg.quantity - spread_order.spot_leg.filled_qty,
                    price=spread_order.spot_leg.target_price,
                    pos_side=spread_order.spot_leg.pos_side,
                )
                if result.success:
                    spread_order.spot_leg.order_id = result.order_id
            except Exception as e:
                logger.error("Failed to amend spot order: %s", e)

        # Amend futures order if still open
        if spread_order.futures_leg.status == LegStatus.OPEN:
            try:
                await self.futures_adapter.cancel_order(
                    spread_order.futures_leg.symbol,
                    spread_order.futures_leg.order_id,
                )
                result = await self.futures_adapter.place_order(
                    symbol=spread_order.futures_leg.symbol,
                    side=spread_order.futures_leg.side,
                    order_type="LIMIT",
                    quantity=spread_order.futures_leg.quantity - spread_order.futures_leg.filled_qty,
                    price=spread_order.futures_leg.target_price,
                    pos_side=spread_order.futures_leg.pos_side,  # Keep same pos_side
                )
                if result.success:
                    spread_order.futures_leg.order_id = result.order_id
            except Exception as e:
                logger.error("Failed to amend futures order: %s", e)

    async def _check_order_status(self, spread_order: SpreadOrder) -> None:
        """Check the fill status of both legs."""
        # For now, we rely on the order result from place_order
        # In a production system, you'd query order status from the exchange
        # This is a simplified implementation

        # Check if orders were filled by attempting to get positions
        # (This is a workaround - proper implementation would use order status API)
        pass

    async def _handle_timeout(self, spread_order: SpreadOrder) -> None:
        """Handle timeout - cancel unfilled orders and close any partial fills."""
        logger.debug("Handling limit order timeout")

        # Cancel any open orders
        if spread_order.spot_leg.status == LegStatus.OPEN:
            try:
                await self.spot_adapter.cancel_order(
                    spread_order.spot_leg.symbol,
                    spread_order.spot_leg.order_id,
                )
                spread_order.spot_leg.status = LegStatus.CANCELLED
            except Exception as e:
                logger.error("Failed to cancel spot order: %s", e)

        if spread_order.futures_leg.status == LegStatus.OPEN:
            try:
                await self.futures_adapter.cancel_order(
                    spread_order.futures_leg.symbol,
                    spread_order.futures_leg.order_id,
                )
                spread_order.futures_leg.status = LegStatus.CANCELLED
            except Exception as e:
                logger.error("Failed to cancel futures order: %s", e)

        # Handle partial fills
        if spread_order.has_partial_fill:
            await self._handle_leg_risk(spread_order)

    async def _handle_leg_risk(self, spread_order: SpreadOrder) -> None:
        """
        Handle leg risk when one leg is filled but the other isn't.

        Strategy: Market-close the filled leg to eliminate directional exposure.
        """
        logger.warning("Handling leg risk - closing partial position with market order")

        spot_filled = spread_order.spot_leg.status == LegStatus.FILLED
        futures_filled = spread_order.futures_leg.status == LegStatus.FILLED

        if spot_filled and not futures_filled:
            # Spot filled, futures didn't - close spot position
            close_side = "SELL" if spread_order.spot_leg.side == "BUY" else "BUY"
            result = await self.spot_adapter.place_order(
                symbol=spread_order.spot_leg.symbol,
                side=close_side,
                order_type="MARKET",
                quantity=spread_order.spot_leg.filled_qty,
            )
            logger.info("Closed spot leg to handle leg risk: %s", result)

        elif futures_filled and not spot_filled:
            # Futures filled, spot didn't - close futures position
            # IMPORTANT: Use SAME pos_side as entry to close the position
            close_side = "SELL" if spread_order.futures_leg.side == "BUY" else "BUY"
            result = await self.futures_adapter.place_order(
                symbol=spread_order.futures_leg.symbol,
                side=close_side,
                order_type="MARKET",
                quantity=spread_order.futures_leg.filled_qty,
                pos_side=spread_order.futures_leg.pos_side,  # Same pos_side to close!
                reduce_only=True,
            )
            logger.info("Closed futures leg to handle leg risk: %s", result)

        if self.on_error:
            self.on_error("Leg risk occurred - partial position closed with market order")

    def _update_leg_from_result(self, leg: LegOrder, result: OrderResult) -> None:
        """Update leg status from order result."""
        if result.success:
            leg.order_id = result.order_id
            leg.filled_qty = result.filled_qty
            leg.filled_price = result.filled_price
            leg.status = LegStatus.FILLED if result.filled_qty >= leg.quantity else LegStatus.PARTIAL
        else:
            leg.status = LegStatus.FAILED

        leg.last_update = datetime.utcnow()

    async def cancel_active_order(self) -> None:
        """Cancel any active order."""
        if self.active_order:
            await self._handle_timeout(self.active_order)
