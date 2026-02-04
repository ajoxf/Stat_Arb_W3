"""
Flask web application for the Crypto Statistical Arbitrage Trading System.
"""

import os
import sys
import signal
import asyncio
import logging
import atexit
from threading import Thread
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

from models import TradingConfig, Exchange, Trade, MarketTick, Signal, CRYPTO_ASSETS
from core.signals import SignalGenerator
from core.trading_engine import TradingEngine
from database.manager import DatabaseManager
from adapters import OKXAdapter, BinanceAdapter, BybitAdapter, OKXWebSocketManager

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'crypto-arb-secret-key')

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Initialize database
db = DatabaseManager(os.getenv('DATABASE_PATH', 'trading.db'))

# Initialize trading engine
config = db.get_config()
engine = TradingEngine(config)

# Async event loop for trading engine
loop: Optional[asyncio.AbstractEventLoop] = None
engine_thread: Optional[Thread] = None
ws_manager: Optional[OKXWebSocketManager] = None
shutdown_in_progress = False


def run_async_loop(loop: asyncio.AbstractEventLoop):
    """Run the async event loop in a separate thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def start_engine_loop():
    """Start the trading engine in a background thread."""
    global loop, engine_thread, ws_manager

    if loop is None:
        loop = asyncio.new_event_loop()
        engine_thread = Thread(target=run_async_loop, args=(loop,), daemon=True)
        engine_thread.start()

    # Set up callbacks
    engine.on_tick = on_tick_callback
    engine.on_signal = on_signal_callback
    engine.on_trade = on_trade_callback
    engine.on_error = on_error_callback

    # Set up SD touch callback on signal generator
    engine.signal_generator.on_sd_touch = on_sd_touch_callback

    # Load spread history from database for recovery
    spread_history = db.get_spread_history(config.asset, limit=config.lookback_period)
    if spread_history:
        spreads = [h['spread'] for h in spread_history]
        engine.signal_generator.load_spread_history(spreads)
        logger.info("Loaded %d spread values from database", len(spreads))

    # Cleanup old spread history to prevent database bloat
    # Keep at least 2x lookback period to ensure sufficient data after restart
    keep_count = max(config.lookback_period * 2, 2000)
    db.cleanup_old_spread_history(config.asset, keep_count=keep_count)

    # Set up WebSocket streaming if enabled
    use_websocket = os.getenv('USE_WEBSOCKET', 'true').lower() == 'true'
    if use_websocket:
        is_demo = os.getenv('OKX_DEMO_MODE', 'true').lower() == 'true'
        ws_manager = OKXWebSocketManager(is_demo=is_demo)
        engine.set_websocket_manager(ws_manager)
        logger.info("WebSocket streaming enabled (demo=%s)", is_demo)

    # Start engine
    asyncio.run_coroutine_threadsafe(engine.start(), loop)
    logger.info("Trading engine started")


def stop_engine_loop():
    """Stop the trading engine gracefully."""
    global loop, shutdown_in_progress

    if shutdown_in_progress:
        return
    shutdown_in_progress = True

    logger.info("Shutting down trading engine...")

    if loop:
        try:
            # Stop the engine (which stops WebSocket)
            future = asyncio.run_coroutine_threadsafe(engine.stop(), loop)
            future.result(timeout=5)  # Wait up to 5 seconds
            logger.info("Trading engine stopped")
        except Exception as e:
            logger.warning("Error stopping engine: %s", e)

        try:
            # Stop the event loop
            loop.call_soon_threadsafe(loop.stop)
            logger.info("Event loop stopped")
        except Exception as e:
            logger.warning("Error stopping loop: %s", e)


def graceful_shutdown(signum=None, frame=None):
    """Handle graceful shutdown on SIGINT/SIGTERM."""
    logger.info("Received shutdown signal, cleaning up...")
    stop_engine_loop()
    logger.info("Shutdown complete")
    sys.exit(0)


# Register shutdown handlers
atexit.register(stop_engine_loop)
signal.signal(signal.SIGINT, graceful_shutdown)
# SIGTERM not available on Windows
if hasattr(signal, 'SIGTERM'):
    signal.signal(signal.SIGTERM, graceful_shutdown)


# Callback functions for engine events
def on_tick_callback(spot_tick: MarketTick, futures_tick: MarketTick):
    """Handle tick updates."""
    try:
        tick_data = {
            'spot': spot_tick.to_dict(),
            'futures': futures_tick.to_dict(),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        # Use socketio.emit with explicit namespace for background thread
        socketio.emit('tick', tick_data, namespace='/')
    except Exception as e:
        logger.error("Error emitting tick: %s", e)

    # Save spread to database for persistence/recovery
    spread = spot_tick.mid - futures_tick.mid
    db.save_spread(
        asset=config.asset,
        spot_price=spot_tick.mid,
        futures_price=futures_tick.mid,
        spread=spread,
    )


def on_signal_callback(signal: Signal):
    """Handle signal updates."""
    try:
        signal_data = signal.to_dict()
        signal_data['asset'] = config.asset
        socketio.emit('signal', signal_data, namespace='/')
    except Exception as e:
        logger.error("Error emitting signal: %s", e)

    # Log significant signals
    if signal.signal_type != "NONE":
        db.log_signal(signal_data)


def on_trade_callback(trade: Trade):
    """Handle trade updates."""
    try:
        # Save to database
        trade.id = db.save_trade(trade)
        socketio.emit('trade', trade.to_dict(), namespace='/')
    except Exception as e:
        logger.error("Error emitting trade: %s", e)


def on_error_callback(error: str):
    """Handle error updates."""
    try:
        socketio.emit('error', {'message': error}, namespace='/')
    except Exception as e:
        logger.error("Error emitting error event: %s", e)


def on_sd_touch_callback(event):
    """Handle SD touch events - log to database."""
    db.log_sd_touch(event)
    logger.info("SD touch: level=%s, direction=%s, zscore=%.4f",
                event.sd_level, event.direction, event.zscore)


# Routes
@app.route('/')
def index():
    """Redirect to dashboard."""
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    """Main trading dashboard."""
    config = db.get_config()
    exchanges = db.get_exchanges()
    return render_template('dashboard.html',
                           config=config,
                           exchanges=exchanges,
                           assets=CRYPTO_ASSETS)


@app.route('/settings')
def settings():
    """Configuration page."""
    config = db.get_config()
    return render_template('settings.html',
                           config=config,
                           assets=CRYPTO_ASSETS)


@app.route('/setup')
def setup():
    """Exchange management page."""
    exchanges = db.get_exchanges()
    return render_template('setup.html', exchanges=exchanges)


@app.route('/analysis')
def analysis():
    """SD touch analysis page."""
    config = db.get_config()
    sd_touches = db.get_sd_touches(asset=config.asset, limit=500)
    stats = db.get_trade_statistics()
    return render_template('analysis.html',
                           config=config,
                           sd_touches=[t.to_dict() for t in sd_touches],
                           stats=stats,
                           assets=CRYPTO_ASSETS)


# API Routes
@app.route('/api/config', methods=['GET'])
def get_config():
    """Get current configuration."""
    config = db.get_config()
    return jsonify(config.to_dict())


@app.route('/api/config', methods=['POST'])
def save_config():
    """Save configuration."""
    global config, engine

    data = request.json
    config = TradingConfig.from_dict(data)
    db.save_config(config)

    # Update engine
    engine.update_config(config)

    return jsonify({'success': True, 'config': config.to_dict()})


@app.route('/api/engine/toggle-algo', methods=['POST'])
def toggle_algo():
    """Toggle algorithmic trading."""
    data = request.json
    enabled = data.get('enabled', False)

    engine.toggle_algo(enabled)
    config.algo_enabled = enabled
    db.save_config(config)

    socketio.emit('status', engine.get_status())

    return jsonify({'success': True, 'algo_enabled': enabled})


@app.route('/api/engine/status', methods=['GET'])
def get_engine_status():
    """Get engine status."""
    return jsonify(engine.get_status())


@app.route('/api/engine/reset', methods=['POST'])
def reset_engine():
    """Reset engine state."""
    engine.reset()
    return jsonify({'success': True})


@app.route('/api/exchanges', methods=['GET'])
def get_exchanges():
    """Get all exchanges."""
    exchanges = db.get_exchanges()
    return jsonify([e.to_dict() for e in exchanges])


@app.route('/api/exchanges', methods=['POST'])
def add_exchange():
    """Add new exchange."""
    data = request.json

    exchange = Exchange(
        name=data.get('name', ''),
        exchange_type=data.get('exchange_type', ''),
        api_key=data.get('api_key', ''),
        secret_key=data.get('secret_key', ''),
        passphrase=data.get('passphrase', ''),
        is_testnet=data.get('is_testnet', True),
        role=data.get('role', 'BOTH'),
    )

    exchange_id = db.save_exchange(exchange)
    exchange.id = exchange_id

    return jsonify({'success': True, 'exchange': exchange.to_dict()})


@app.route('/api/exchanges/<int:exchange_id>', methods=['DELETE'])
def delete_exchange(exchange_id):
    """Delete exchange."""
    db.delete_exchange(exchange_id)
    return jsonify({'success': True})


@app.route('/api/exchanges/<int:exchange_id>/test', methods=['POST'])
def test_exchange(exchange_id):
    """Test exchange connection."""
    exchange = db.get_exchange(exchange_id)
    if not exchange:
        return jsonify({'success': False, 'error': 'Exchange not found'}), 404

    # Create adapter based on type
    adapter = create_adapter(exchange)
    if not adapter:
        return jsonify({'success': False, 'error': 'Unknown exchange type'}), 400

    # Test connection
    async def test_connection():
        try:
            connected = await adapter.connect()
            if connected:
                account = await adapter.get_account_info()
                await adapter.disconnect()
                return True, account.to_dict() if account else {}
            else:
                return False, adapter.last_error
        except Exception as e:
            return False, str(e)

    if loop:
        future = asyncio.run_coroutine_threadsafe(test_connection(), loop)
        success, result = future.result(timeout=30)
    else:
        success, result = False, "Engine not started"

    # Update status
    db.update_exchange_status(
        exchange_id,
        "CONNECTED" if success else "ERROR",
        "" if success else str(result)
    )

    return jsonify({
        'success': success,
        'account': result if success else None,
        'error': result if not success else None
    })


@app.route('/api/set-active-exchanges', methods=['POST'])
def set_active_exchanges():
    """Set active exchanges for trading."""
    data = request.json
    spot_id = data.get('spot_id')
    futures_id = data.get('futures_id')

    db.set_active_exchanges(spot_id, futures_id)

    # Update engine adapters
    spot_adapter = None
    futures_adapter = None

    if spot_id:
        exchange = db.get_exchange(spot_id)
        if exchange:
            spot_adapter = create_adapter(exchange, is_futures=False)

    if futures_id:
        exchange = db.get_exchange(futures_id)
        if exchange:
            futures_adapter = create_adapter(exchange, is_futures=True)

    engine.set_adapters(spot_adapter, futures_adapter)

    return jsonify({'success': True})


@app.route('/api/trades', methods=['GET'])
def get_trades():
    """Get recent trades."""
    limit = request.args.get('limit', 100, type=int)
    trades = db.get_trades(limit=limit)
    return jsonify([t.to_dict() for t in trades])


@app.route('/api/account-info', methods=['GET'])
def get_account_info():
    """Get account information from connected exchange."""
    # Determine exchange type and demo mode from environment or adapter
    is_demo = os.getenv('OKX_DEMO_MODE', 'true').lower() == 'true'
    exchange_type = os.getenv('EXCHANGE_TYPE', 'OKX').upper()

    account_data = {
        'connected': False,
        'exchange': exchange_type,
        'balance': 0,
        'available': 0,
        'margin_used': 0,
        'unrealized_pnl': 0,
        'daily_pnl': 0,
        'is_demo': is_demo,
    }

    # Check if we have adapters connected
    if engine.spot_adapter:
        # Get demo mode from adapter if available
        if hasattr(engine.spot_adapter, 'is_testnet'):
            account_data['is_demo'] = engine.spot_adapter.is_testnet

        # Get exchange type from adapter
        adapter_type = type(engine.spot_adapter).__name__.replace('Adapter', '').upper()
        account_data['exchange'] = adapter_type

        try:
            # Get account info from adapter
            async def fetch_account():
                if hasattr(engine.spot_adapter, 'get_account_info'):
                    return await engine.spot_adapter.get_account_info()
                return None

            if loop:
                future = asyncio.run_coroutine_threadsafe(fetch_account(), loop)
                account = future.result(timeout=10)
                if account:
                    account_data['connected'] = True
                    if account.exchange:
                        account_data['exchange'] = account.exchange
                    account_data['balance'] = account.balance_usd
                    account_data['available'] = account.available_balance_usd
                    account_data['margin_used'] = account.margin_used
                    account_data['unrealized_pnl'] = account.unrealized_pnl
        except Exception as e:
            logger.warning("Error fetching account info: %s", e)

    # Calculate daily P&L from trades
    stats = db.get_trade_statistics()
    if stats:
        account_data['daily_pnl'] = stats.get('daily_pnl', 0)

    return jsonify(account_data)


@app.route('/api/trade-journal', methods=['GET'])
def get_trade_journal():
    """Get trade journal with statistics."""
    trades = db.get_trades(limit=500)
    stats = db.get_trade_statistics()

    return jsonify({
        'trades': [t.to_dict() for t in trades],
        'statistics': stats
    })


@app.route('/api/spread-history', methods=['GET'])
def get_spread_history():
    """Get spread history for charting."""
    n = request.args.get('n', 100, type=int)
    spreads = engine.get_spread_history(n)
    zscores = engine.get_zscore_history(n)

    return jsonify({
        'spreads': spreads,
        'zscores': zscores
    })


@app.route('/api/sd-touches', methods=['GET'])
def get_sd_touches():
    """Get SD touch events."""
    asset = request.args.get('asset')
    limit = request.args.get('limit', 500, type=int)

    touches = db.get_sd_touches(asset=asset, limit=limit)
    return jsonify([t.to_dict() for t in touches])


# ============== Reset/Delete API Endpoints ==============

@app.route('/api/trades/clear', methods=['POST'])
def clear_trades():
    """Clear all trades (or for specific asset)."""
    data = request.json or {}
    asset = data.get('asset')  # Optional - if provided, clear only for this asset

    deleted = db.clear_trades(asset=asset)
    return jsonify({'success': True, 'deleted': deleted})


@app.route('/api/trades/<int:trade_id>', methods=['DELETE'])
def delete_trade(trade_id):
    """Delete a specific trade."""
    success = db.delete_trade(trade_id)
    if success:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Trade not found'}), 404


@app.route('/api/trades/<int:trade_id>/close', methods=['POST'])
def close_trade_manually(trade_id):
    """Manually close an open trade."""
    # Get current prices for the close
    if engine.spot_tick and engine.futures_tick:
        spot_price = engine.spot_tick.mid
        futures_price = engine.futures_tick.mid
        spread = spot_price - futures_price
        zscore = engine.signal_generator.current_zscore

        # Update the trade with exit details
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades SET
                    exit_time = ?,
                    exit_spot_price = ?,
                    exit_futures_price = ?,
                    exit_spread = ?,
                    exit_zscore = ?,
                    exit_reason = 'MANUAL',
                    is_open = 0
                WHERE id = ? AND is_open = 1
            """, (
                datetime.now(timezone.utc).isoformat(),
                spot_price, futures_price, spread, zscore, trade_id
            ))
            if cursor.rowcount > 0:
                # Reset engine position
                engine.state.current_position = "NONE"
                engine.signal_generator.set_position("NONE")
                engine.open_trade = None
                logger.info("Manually closed trade %d at spread=%.2f, zscore=%.4f", trade_id, spread, zscore)
                return jsonify({'success': True})

    # Fallback - just mark as closed without prices
    success = db.close_trade(trade_id, exit_reason="MANUAL")
    if success:
        engine.state.current_position = "NONE"
        engine.signal_generator.set_position("NONE")
        engine.open_trade = None
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Trade not found or already closed'}), 404


@app.route('/api/sd-touches/clear', methods=['POST'])
def clear_sd_touches():
    """Clear all SD touch events (or for specific asset)."""
    data = request.json or {}
    asset = data.get('asset')

    deleted = db.clear_sd_touches(asset=asset)
    # Also clear from signal generator memory
    engine.signal_generator.sd_touch_events.clear()
    engine.signal_generator.last_sd_level = 0.0

    return jsonify({'success': True, 'deleted': deleted})


@app.route('/api/spread-history/clear', methods=['POST'])
def clear_spread_history():
    """Clear spread history and reset signal generator."""
    data = request.json or {}
    asset = data.get('asset')

    deleted = db.clear_spread_history(asset=asset)
    # Reset signal generator
    engine.signal_generator.reset()

    return jsonify({'success': True, 'deleted': deleted})


@app.route('/api/engine/close-position', methods=['POST'])
def close_current_position():
    """Close the current open position manually."""
    if engine.state.current_position == "NONE" or not engine.open_trade:
        return jsonify({'success': False, 'error': 'No open position'}), 400

    # Create a manual exit signal
    if engine.spot_tick and engine.futures_tick:
        from models import Signal
        manual_signal = Signal(
            signal_type="EXIT",
            zscore=engine.signal_generator.current_zscore,
            spread=engine.signal_generator.current_spread,
            spread_mean=engine.signal_generator.current_mean,
            spread_std=engine.signal_generator.current_std,
            hurst=engine.signal_generator.current_hurst,
            regime="MANUAL_CLOSE",
            current_position=engine.state.current_position,
            timestamp=datetime.now(timezone.utc),
        )

        # Execute the close
        async def close_position():
            trade = engine.open_trade
            spot_price = engine.spot_tick.mid
            futures_price = engine.futures_tick.mid

            # Calculate P&L
            if trade.position_type == "LONG":
                spread_change = manual_signal.spread - trade.entry_spread
                pnl = spread_change * trade.quantity
            else:
                spread_change = trade.entry_spread - manual_signal.spread
                pnl = spread_change * trade.quantity

            pnl_percent = (pnl / trade.notional_usd) * 100 if trade.notional_usd > 0 else 0

            # Update trade
            trade.exit_time = datetime.now(timezone.utc)
            trade.exit_spot_price = spot_price
            trade.exit_futures_price = futures_price
            trade.exit_spread = manual_signal.spread
            trade.exit_zscore = manual_signal.zscore
            trade.exit_reason = "MANUAL"
            trade.pnl_usd = pnl
            trade.pnl_percent = pnl_percent
            trade.is_open = False

            # Execute exit orders if not paper trading
            if not engine.state.paper_trading and engine.order_executor:
                await engine._execute_exit_orders(trade, manual_signal)

            # Save to database
            db.save_trade(trade)

            # Reset engine state
            engine.state.current_position = "NONE"
            engine.signal_generator.set_position("NONE")
            engine.open_trade = None

            # Notify via socket
            socketio.emit('trade', trade.to_dict(), namespace='/')

            return trade

        if loop:
            future = asyncio.run_coroutine_threadsafe(close_position(), loop)
            trade = future.result(timeout=30)
            return jsonify({
                'success': True,
                'trade': trade.to_dict(),
                'message': f"Position closed. P&L: ${trade.pnl_usd:.2f} ({trade.pnl_percent:.2f}%)"
            })

    return jsonify({'success': False, 'error': 'No price data available'}), 400


@app.route('/api/active-orders', methods=['GET'])
def get_active_orders():
    """Get currently active/pending orders."""
    orders = []

    # Check if there's an active order in the order executor
    if engine.order_executor and engine.order_executor.active_order:
        spread_order = engine.order_executor.active_order
        orders.append({
            'type': 'ENTRY' if spread_order.is_entry else 'EXIT',
            'position_type': spread_order.position_type,
            'created_at': spread_order.created_at.isoformat() if spread_order.created_at else None,
            'timeout_at': spread_order.timeout_at.isoformat() if spread_order.timeout_at else None,
            'spot_leg': {
                'symbol': spread_order.spot_leg.symbol,
                'side': spread_order.spot_leg.side,
                'quantity': spread_order.spot_leg.quantity,
                'target_price': spread_order.spot_leg.target_price,
                'order_id': spread_order.spot_leg.order_id,
                'status': spread_order.spot_leg.status.value,
                'filled_qty': spread_order.spot_leg.filled_qty,
                'filled_price': spread_order.spot_leg.filled_price,
            },
            'futures_leg': {
                'symbol': spread_order.futures_leg.symbol,
                'side': spread_order.futures_leg.side,
                'quantity': spread_order.futures_leg.quantity,
                'target_price': spread_order.futures_leg.target_price,
                'order_id': spread_order.futures_leg.order_id,
                'status': spread_order.futures_leg.status.value,
                'filled_qty': spread_order.futures_leg.filled_qty,
                'filled_price': spread_order.futures_leg.filled_price,
            },
            'is_complete': spread_order.is_complete,
            'has_partial_fill': spread_order.has_partial_fill,
        })

    return jsonify({
        'orders': orders,
        'execution_mode': config.order_execution_mode,
        'is_executing': engine.order_executor._executing if engine.order_executor else False,
    })


@app.route('/api/reset-all', methods=['POST'])
def reset_all():
    """Reset everything - trades, SD touches, spread history, and engine state."""
    data = request.json or {}
    asset = data.get('asset')

    # Clear database
    trades_deleted = db.clear_trades(asset=asset)
    sd_deleted = db.clear_sd_touches(asset=asset)
    signals_deleted = db.clear_signal_log(asset=asset)
    spread_deleted = db.clear_spread_history(asset=asset)

    # Reset engine
    engine.reset()

    return jsonify({
        'success': True,
        'deleted': {
            'trades': trades_deleted,
            'sd_touches': sd_deleted,
            'signals': signals_deleted,
            'spread_history': spread_deleted,
        }
    })


def create_adapter(exchange: Exchange, is_futures: bool = False):
    """Create exchange adapter based on type."""
    if exchange.exchange_type.lower() == 'okx':
        return OKXAdapter(
            api_key=exchange.api_key,
            secret_key=exchange.secret_key,
            passphrase=exchange.passphrase,
            is_testnet=exchange.is_testnet,
        )
    elif exchange.exchange_type.lower() == 'binance':
        return BinanceAdapter(
            api_key=exchange.api_key,
            secret_key=exchange.secret_key,
            is_testnet=exchange.is_testnet,
            is_futures=is_futures,
        )
    elif exchange.exchange_type.lower() == 'bybit':
        return BybitAdapter(
            api_key=exchange.api_key,
            secret_key=exchange.secret_key,
            is_testnet=exchange.is_testnet,
            is_futures=is_futures,
        )
    return None


# SocketIO Events
@socketio.on('connect')
def handle_connect():
    """Handle client connection."""
    logger.info("Client connected")
    emit('status', engine.get_status())


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    logger.info("Client disconnected")


@socketio.on('get_status')
def handle_get_status():
    """Handle status request."""
    emit('status', engine.get_status())


@socketio.on('toggle_algo')
def handle_toggle_algo(data):
    """Handle algo toggle via WebSocket."""
    enabled = data.get('enabled', False)
    engine.toggle_algo(enabled)
    emit('status', engine.get_status(), broadcast=True)


# Start engine on app start
@app.before_request
def ensure_engine_started():
    """Ensure engine is started before handling requests."""
    global loop
    if loop is None:
        start_engine_loop()


if __name__ == '__main__':
    # Start the trading engine
    start_engine_loop()

    # Log the server address
    port = 5000
    logger.info("=" * 50)
    logger.info("Dashboard available at: http://localhost:%d", port)
    logger.info("=" * 50)

    # Run Flask app with SocketIO
    socketio.run(
        app,
        host='0.0.0.0',
        port=port,
        debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true'
    )
