"""
Per-user Trading Engine Manager for the SaaS platform.

Each authenticated user gets an isolated TradingEngine instance running in
its own asyncio event loop on a dedicated background thread.

Socket.IO events are emitted to a per-user room (f"user_{user_id}") so that
each browser session only receives its own data.
"""

import asyncio
import logging
from datetime import datetime, timezone
from threading import Thread
from typing import Optional, Dict, Callable, Any

from core.trading_engine import TradingEngine
from models import TradingConfig, MarketTick, Signal, Trade, SDTouchEvent

logger = logging.getLogger(__name__)


class UserEngineContext:
    """
    Holds all runtime state for a single user's trading engine.
    """
    def __init__(self, user_id: int, config: TradingConfig):
        self.user_id = user_id
        self.config = config
        self.engine: TradingEngine = TradingEngine(config)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[Thread] = None
        self.started = False

    def start(
        self,
        on_tick: Callable,
        on_signal: Callable,
        on_trade: Callable,
        on_error: Callable,
        on_sd_touch: Optional[Callable] = None,
        spread_history: Optional[list] = None,
        open_trade: Optional[Trade] = None,
        spot_adapter=None,
        futures_adapter=None,
        ws_manager=None,
    ) -> None:
        """Start the engine in a background thread."""
        if self.started:
            return

        self.loop = asyncio.new_event_loop()
        self.thread = Thread(
            target=self._run_loop,
            daemon=True,
            name=f"engine-user-{self.user_id}",
        )
        self.thread.start()

        # Wire callbacks
        self.engine.on_tick = on_tick
        self.engine.on_signal = on_signal
        self.engine.on_trade = on_trade
        self.engine.on_error = on_error
        if on_sd_touch:
            self.engine.signal_generator.on_sd_touch = on_sd_touch

        # Restore spread history for warm start
        if spread_history:
            spreads = [h['spread'] for h in spread_history]
            self.engine.signal_generator.load_spread_history(spreads)

        # Recover open position
        if open_trade and open_trade.asset == self.config.asset:
            self.engine.open_trade = open_trade
            self.engine.state.current_position = open_trade.position_type
            self.engine.signal_generator.set_position(open_trade.position_type)

        # Set adapters
        if spot_adapter or futures_adapter:
            self.engine.set_adapters(spot_adapter, futures_adapter)

        # Set WebSocket manager
        if ws_manager:
            self.engine.set_websocket_manager(ws_manager)

        # Launch engine coroutine
        asyncio.run_coroutine_threadsafe(self.engine.start(), self.loop)
        self.started = True
        logger.info("Engine started for user_id=%d", self.user_id)

    def stop(self) -> None:
        """Stop the engine gracefully."""
        if not self.started or not self.loop:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self.engine.stop(), self.loop)
            future.result(timeout=5)
        except Exception as exc:
            logger.warning("Error stopping engine for user_id=%d: %s", self.user_id, exc)
        finally:
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass
            self.started = False
            logger.info("Engine stopped for user_id=%d", self.user_id)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()


class EngineManager:
    """
    Singleton manager that owns one UserEngineContext per user.

    Usage in app.py:
        engine_mgr = EngineManager()
        ctx = engine_mgr.get_or_create(user_id, config)
        ctx.start(...)
    """

    def __init__(self):
        self._contexts: Dict[int, UserEngineContext] = {}

    def get(self, user_id: int) -> Optional[UserEngineContext]:
        return self._contexts.get(user_id)

    def get_or_create(self, user_id: int, config: TradingConfig) -> UserEngineContext:
        if user_id not in self._contexts:
            self._contexts[user_id] = UserEngineContext(user_id, config)
        return self._contexts[user_id]

    def stop(self, user_id: int) -> None:
        ctx = self._contexts.pop(user_id, None)
        if ctx:
            ctx.stop()

    def stop_all(self) -> None:
        for user_id in list(self._contexts.keys()):
            self.stop(user_id)
