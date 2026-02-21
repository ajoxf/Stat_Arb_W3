"""
Flask web application for the Crypto Statistical Arbitrage Trading System.
"""

import os
import sys
import time
import signal
import asyncio
import logging
import atexit
from threading import Thread
from datetime import datetime, timezone
from typing import Optional, Dict, Any

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

# Suppress noisy HTTP request logs - use ERROR to hide all routine requests
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('engineio').setLevel(logging.ERROR)
logging.getLogger('socketio').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'crypto-arb-secret-key')

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

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
    logger.info("Async event loop thread starting...")
    asyncio.set_event_loop(loop)
    logger.info("Async event loop running")
    loop.run_forever()


def start_engine_loop():
    """Start the trading engine in a background thread."""
    global loop, engine_thread, ws_manager

    if loop is None:
        loop = asyncio.new_event_loop()
        engine_thread = Thread(target=run_async_loop, args=(loop,), daemon=True)
        engine_thread.start()
        # Wait for the event loop to actually start running
        time.sleep(0.1)
        logger.info("Event loop thread started, scheduling engine.start()")

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

    # Recover open position from database (if any)
    open_trades = db.get_trades(limit=1, open_only=True)
    if open_trades:
        open_trade = open_trades[0]
        if open_trade.asset == config.asset:
            engine.open_trade = open_trade
            engine.state.current_position = open_trade.position_type
            engine.signal_generator.set_position(open_trade.position_type)
            logger.info("Recovered open %s position from database (trade_id=%d, entry_zscore=%.2f)",
                       open_trade.position_type, open_trade.id, open_trade.entry_zscore)
        else:
            logger.warning("Open trade exists for different asset (%s vs %s), not recovering",
                          open_trade.asset, config.asset)

    # Set up WebSocket streaming if enabled
    use_websocket = os.getenv('USE_WEBSOCKET', 'true').lower() == 'true'
    if use_websocket:
        is_demo = os.getenv('OKX_DEMO_MODE', 'true').lower() == 'true'
        ws_manager = OKXWebSocketManager(is_demo=is_demo)
        engine.set_websocket_manager(ws_manager)
        logger.debug("WebSocket streaming enabled (demo=%s)", is_demo)

    # Initialize REST adapters for account info and order execution
    # This allows us to use WebSocket for fast price updates and REST for account data + orders
    api_key = os.getenv('OKX_API_KEY', '')
    secret_key = os.getenv('OKX_SECRET_KEY', '')
    passphrase = os.getenv('OKX_PASSPHRASE', '')
    is_demo = os.getenv('OKX_DEMO_MODE', 'true').lower() == 'true'

    if api_key and secret_key and passphrase:
        # Create adapter instances - used for account info always, order execution only if not paper trading
        spot_adapter = OKXAdapter(
            api_key=api_key,
            secret_key=secret_key,
            passphrase=passphrase,
            is_testnet=is_demo,
            spot_leverage=config.spot_leverage,
        )
        futures_adapter = OKXAdapter(
            api_key=api_key,
            secret_key=secret_key,
            passphrase=passphrase,
            is_testnet=is_demo,
        )
        engine.set_adapters(spot_adapter, futures_adapter)
        logger.info("REST adapters configured: demo=%s, paper=%s, symbols=(%s, %s)",
                   is_demo, config.paper_trading, config.spot_symbol, config.futures_symbol)
    else:
        logger.warning("API keys not configured - using paper trading simulation only")

    # Start engine - schedule the coroutine and give it time to start
    logger.info("Scheduling engine.start() coroutine...")
    future = asyncio.run_coroutine_threadsafe(engine.start(), loop)
    # Give the async task time to start running
    time.sleep(0.2)
    logger.info("Trading engine started (future done=%s)", future.done())


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
    spread = futures_tick.mid - spot_tick.mid
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
        # Add data_points and lookback from signal generator state
        sg_state = engine.signal_generator.get_state()
        signal_data['data_points'] = sg_state.get('data_points', 0)
        signal_data['lookback'] = sg_state.get('lookback', config.lookback_period)
        signal_data['data_ready'] = sg_state.get('data_ready', False)
        signal_data['std_ratio'] = sg_state.get('std_ratio')
        signal_data['std_ratio_required'] = sg_state.get('std_ratio_required')
        socketio.emit('signal', signal_data, namespace='/')
    except Exception as e:
        logger.error("Error emitting signal: %s", e)

    # Log significant signals
    if signal.signal_type != "NONE":
        db.log_signal(signal_data)


def on_trade_callback(trade: Trade):
    """Handle trade updates."""
    try:
        # Only save real (non-paper) trades to the journal database.
        # Paper trades are emitted to the socket for live dashboard view only.
        if not trade.is_paper:
            trade.id = db.save_trade(trade)
        # Always emit to socket so the dashboard shows real-time updates
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
    logger.debug("SD touch: level=%s, direction=%s, zscore=%.4f",
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

    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data received'}), 400

        config = TradingConfig.from_dict(data)
        db.save_config(config)

        # Update engine
        engine.update_config(config)

        return jsonify({'success': True, 'config': config.to_dict()})
    except Exception as e:
        logger.error("Error saving config: %s", e)
        return jsonify({'success': False, 'error': str(e)}), 500


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


@app.route('/api/engine/sync-position', methods=['POST'])
def sync_position():
    """Sync engine position state with database.

    This recovers the position if the engine lost track of it (e.g., after restart).
    Can also be used to force-clear the position if it's stuck.
    """
    data = request.json or {}
    action = data.get('action', 'recover')  # 'recover' or 'clear'

    if action == 'clear':
        # Force clear the position state (useful if position was manually closed on exchange)
        old_position = engine.state.current_position
        engine.state.current_position = "NONE"
        engine.signal_generator.set_position("NONE")
        engine.open_trade = None

        # Also mark any open trades in DB as closed
        open_trades = db.get_trades(limit=10, open_only=True)
        for trade in open_trades:
            db.close_trade(trade.id, exit_reason="MANUAL_SYNC")

        socketio.emit('status', engine.get_status())

        return jsonify({
            'success': True,
            'action': 'clear',
            'previous_position': old_position,
            'current_position': 'NONE',
            'trades_closed': len(open_trades),
        })

    elif action == 'recover':
        # Recover position from database
        open_trades = db.get_trades(limit=1, open_only=True)

        if not open_trades:
            return jsonify({
                'success': True,
                'action': 'recover',
                'message': 'No open trades in database',
                'current_position': engine.state.current_position,
            })

        open_trade = open_trades[0]

        # Check if it matches the current asset
        if open_trade.asset != config.asset:
            return jsonify({
                'success': False,
                'error': f'Open trade is for {open_trade.asset}, current asset is {config.asset}',
            })

        # Recover the position
        old_position = engine.state.current_position
        engine.open_trade = open_trade
        engine.state.current_position = open_trade.position_type
        engine.signal_generator.set_position(open_trade.position_type)

        socketio.emit('status', engine.get_status())

        return jsonify({
            'success': True,
            'action': 'recover',
            'previous_position': old_position,
            'recovered_position': open_trade.position_type,
            'trade_id': open_trade.id,
            'entry_zscore': open_trade.entry_zscore,
            'entry_time': open_trade.entry_time.isoformat() if open_trade.entry_time else None,
        })

    else:
        return jsonify({'success': False, 'error': f'Unknown action: {action}'}), 400


@app.route('/api/exchange-positions', methods=['GET'])
def get_exchange_positions():
    """
    Get actual positions from the exchange.

    This helps detect orphaned futures positions that the engine lost track of.
    """
    adapter = engine.futures_adapter
    if not adapter:
        return jsonify({'positions': [], 'error': 'No futures adapter available'})

    async def fetch_positions():
        return await adapter.get_positions()

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(fetch_positions(), loop)
            positions = future.result(timeout=10)

            # Format positions for response
            position_list = []
            for pos in positions:
                position_list.append({
                    'symbol': pos.symbol,
                    'side': pos.side,
                    'quantity': pos.quantity,
                    'entry_price': pos.entry_price,
                    'unrealized_pnl': pos.unrealized_pnl,
                    'leverage': pos.leverage,
                })

            # Compare with engine state
            engine_position = engine.state.current_position if engine.state else "NONE"
            engine_has_position = engine_position != "NONE"
            exchange_has_position = len(position_list) > 0

            # Detect mismatch
            mismatch = False
            mismatch_reason = None

            if engine_has_position and not exchange_has_position:
                mismatch = True
                mismatch_reason = "Engine thinks position is open but exchange has no position"
            elif not engine_has_position and exchange_has_position:
                mismatch = True
                mismatch_reason = "Exchange has position but engine shows FLAT"

            return jsonify({
                'success': True,
                'positions': position_list,
                'engine_position': engine_position,
                'engine_has_position': engine_has_position,
                'exchange_has_position': exchange_has_position,
                'mismatch': mismatch,
                'mismatch_reason': mismatch_reason,
            })

        except Exception as e:
            logger.error("Error fetching exchange positions: %s", e)
            return jsonify({'success': False, 'positions': [], 'error': str(e)})

    return jsonify({'positions': [], 'error': 'Event loop not running'})


@app.route('/api/close-exchange-position', methods=['POST'])
def close_exchange_position():
    """
    Close a position directly on the exchange.

    Use this to close orphaned positions that the engine lost track of.
    """
    data = request.json or {}
    symbol = data.get('symbol')

    if not symbol:
        return jsonify({'success': False, 'error': 'Symbol is required'})

    adapter = engine.futures_adapter
    if not adapter:
        return jsonify({'success': False, 'error': 'No futures adapter available'})

    async def close_position():
        return await adapter.close_position(symbol)

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(close_position(), loop)
            result = future.result(timeout=30)

            if result.success:
                logger.info("Closed exchange position for %s", symbol)
                return jsonify({
                    'success': True,
                    'message': f'Position closed for {symbol}',
                    'order_id': result.order_id,
                })
            else:
                return jsonify({'success': False, 'error': result.error})

        except Exception as e:
            logger.error("Error closing exchange position: %s", e)
            return jsonify({'success': False, 'error': str(e)})

    return jsonify({'success': False, 'error': 'Event loop not running'})


@app.route('/api/spot-holdings', methods=['GET'])
def get_spot_holdings():
    """
    Get current spot holdings (non-USDT assets).

    This helps detect orphaned spot positions from incomplete trades.
    """
    adapter = engine.spot_adapter or engine.futures_adapter
    if not adapter or not hasattr(adapter, 'get_spot_balances'):
        return jsonify({'holdings': [], 'error': 'No adapter available'})

    async def fetch_balances():
        return await adapter.get_spot_balances()

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(fetch_balances(), loop)
            balances = future.result(timeout=10)

            # Filter out stablecoins, keep only crypto assets
            stablecoins = {'USDT', 'USDC', 'BUSD', 'DAI', 'TUSD'}
            holdings = []

            # Minimum USD value to consider as orphan (ignore dust < $1)
            MIN_USD_ORPHAN_THRESHOLD = 1.0

            for currency, bal_info in balances.items():
                if currency not in stablecoins:
                    available = bal_info.get('available', 0)
                    frozen = bal_info.get('frozen', 0)
                    total = bal_info.get('total', 0) or (available + frozen)

                    if total > 0.00000001:  # Filter out zero
                        # Get USD value
                        usd_value = 0
                        if engine.spot_tick and currency == config.asset:
                            usd_value = total * engine.spot_tick.mid

                        # Only include if above minimum USD threshold (ignore dust)
                        if usd_value >= MIN_USD_ORPHAN_THRESHOLD:
                            holdings.append({
                                'currency': currency,
                                'available': available,
                                'frozen': frozen,
                                'total': total,
                                'usd_value': usd_value,
                                'is_trading_asset': currency == config.asset,
                                'can_sell': available > 0.00000001,
                            })

            # Check if there's an orphan (holding without active position)
            has_orphan = False
            for h in holdings:
                if h['is_trading_asset'] and engine.state.current_position == "NONE":
                    has_orphan = True
                    h['is_orphan'] = True

            return jsonify({
                'holdings': holdings,
                'has_orphan': has_orphan,
                'current_position': engine.state.current_position,
            })

        except Exception as e:
            logger.error("Error fetching spot holdings: %s", e)
            return jsonify({'holdings': [], 'error': str(e)})

    return jsonify({'holdings': [], 'error': 'Event loop not running'})


@app.route('/api/close-orphaned-spot', methods=['POST'])
def close_orphaned_spot():
    """
    Sell orphaned spot holdings back to USDT.

    Use this when a trade exit only closed the futures leg, leaving spot behind.
    """
    data = request.json or {}
    currency = data.get('currency', config.asset)  # Default to trading asset

    adapter = engine.spot_adapter
    if not adapter or not hasattr(adapter, 'sell_spot_to_usdt'):
        return jsonify({'success': False, 'error': 'No spot adapter available'})

    async def sell_to_usdt():
        # Get current balance (returns dict with 'available', 'total', etc.)
        balance_info = await adapter.get_asset_balance(currency)
        available = balance_info.get('available', 0) if isinstance(balance_info, dict) else 0

        if available <= 0:
            return None, f"No {currency} balance to sell (available: {available})"

        # Sell to USDT
        result = await adapter.sell_spot_to_usdt(currency)
        return result, available

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(sell_to_usdt(), loop)
            result, amount = future.result(timeout=30)

            if result is None:
                return jsonify({'success': True, 'message': amount})  # amount is error message here

            if result.success:
                return jsonify({
                    'success': True,
                    'currency': currency,
                    'amount_sold': amount,
                    'order_id': result.order_id,
                })
            else:
                return jsonify({'success': False, 'error': result.error})

        except Exception as e:
            logger.error("Error closing orphaned spot: %s", e)
            return jsonify({'success': False, 'error': str(e)})

    return jsonify({'success': False, 'error': 'Event loop not running'})


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
    """Get detailed account information including margin requirements."""
    # Determine exchange type and demo mode from environment or adapter
    is_demo = os.getenv('OKX_DEMO_MODE', 'true').lower() == 'true'
    exchange_type = os.getenv('EXCHANGE_TYPE', 'OKX').upper()

    # Check if API keys are configured
    api_key = os.getenv('OKX_API_KEY', '')
    has_api_keys = bool(api_key and os.getenv('OKX_SECRET_KEY', '') and os.getenv('OKX_PASSPHRASE', ''))

    account_data = {
        'connected': False,
        'exchange': exchange_type,
        'uid': '',
        'account_level': '',
        'balance': 0,
        'available': 0,
        'margin_used': 0,
        'unrealized_pnl': 0,
        'daily_pnl': 0,
        'is_demo': is_demo,
        # Enhanced margin details
        'total_equity': 0,
        'initial_margin': 0,
        'maintenance_margin': 0,
        'margin_ratio': 0,
        'available_margin': 0,
        'leverage_used': 0,
        # Position margin breakdown
        'spot_margin_used': 0,
        'futures_margin_used': 0,
        'spot_unrealized_pnl': 0,
        'futures_unrealized_pnl': 0,
        # Risk metrics
        'liquidation_price': None,
        'mark_price': None,
        'margin_health': 'N/A',  # SAFE, WARNING, DANGER
        # Debug info
        'has_api_keys': has_api_keys,
        'has_adapters': bool(engine.spot_adapter or engine.futures_adapter),
    }

    # Check if we have adapters connected
    if engine.spot_adapter or engine.futures_adapter:
        adapter = engine.spot_adapter or engine.futures_adapter

        # Get demo mode from adapter if available
        if hasattr(adapter, 'is_testnet'):
            account_data['is_demo'] = adapter.is_testnet

        # Get exchange type from adapter
        adapter_type = type(adapter).__name__.replace('Adapter', '').upper()
        account_data['exchange'] = adapter_type

        try:
            # Get account info from adapter
            async def fetch_account():
                if hasattr(adapter, 'get_account_info'):
                    return await adapter.get_account_info()
                return None

            async def fetch_position_margin():
                if hasattr(adapter, 'get_position_margin_info'):
                    return await adapter.get_position_margin_info(config.futures_symbol)
                return None

            async def fetch_account_config():
                if hasattr(adapter, 'get_account_config'):
                    return await adapter.get_account_config()
                return None

            if loop:
                # Fetch account info
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
                    account_data['total_equity'] = account.total_equity
                    account_data['initial_margin'] = account.initial_margin
                    account_data['maintenance_margin'] = account.maintenance_margin
                    account_data['margin_ratio'] = account.margin_ratio
                    account_data['available_margin'] = account.available_margin
                    account_data['leverage_used'] = account.leverage_used

                    # Determine margin health
                    if account.margin_ratio > 500:
                        account_data['margin_health'] = 'SAFE'
                    elif account.margin_ratio > 150:
                        account_data['margin_health'] = 'WARNING'
                    elif account.margin_ratio > 0:
                        account_data['margin_health'] = 'DANGER'

                # Fetch position margin info
                pos_future = asyncio.run_coroutine_threadsafe(fetch_position_margin(), loop)
                pos_margin = pos_future.result(timeout=10)

                if pos_margin:
                    account_data['liquidation_price'] = pos_margin.get('liquidation_price')
                    account_data['mark_price'] = pos_margin.get('mark_price')
                    account_data['futures_margin_used'] = pos_margin.get('imr', 0)
                    account_data['futures_unrealized_pnl'] = pos_margin.get('unrealized_pnl', 0)

                # Fetch account config for UID
                try:
                    config_future = asyncio.run_coroutine_threadsafe(fetch_account_config(), loop)
                    account_config = config_future.result(timeout=10)

                    if account_config:
                        account_data['uid'] = account_config.get('uid', '')
                        account_data['account_level'] = account_config.get('level', '')
                        logger.debug("UID fetched: %s, Level: %s", account_data['uid'], account_data['account_level'])
                    else:
                        logger.warning("Account config returned None")
                except Exception as config_err:
                    logger.warning("Error fetching account config: %s", config_err)

                # Fetch actual position leverage from exchange
                try:
                    async def fetch_positions():
                        if hasattr(adapter, 'get_positions'):
                            return await adapter.get_positions()
                        return []

                    pos_future = asyncio.run_coroutine_threadsafe(fetch_positions(), loop)
                    positions = pos_future.result(timeout=10)

                    # Add actual leverage info from positions
                    account_data['positions'] = []
                    for pos in positions:
                        pos_data = pos.to_dict()
                        account_data['positions'].append(pos_data)
                        # Track leverage from actual positions
                        if 'SWAP' in pos.symbol or 'PERP' in pos.symbol:
                            account_data['actual_futures_leverage'] = pos.leverage
                            account_data['futures_leverage_source'] = 'exchange'
                        elif pos.symbol and not any(x in pos.symbol for x in ['SWAP', 'PERP', 'FUTURE']):
                            # This is a spot/margin position - get its leverage
                            account_data['actual_spot_leverage'] = pos.leverage
                            account_data['spot_leverage_source'] = 'exchange'
                except Exception as pos_err:
                    logger.warning("Error fetching positions: %s", pos_err)

        except Exception as e:
            logger.warning("Error fetching account info: %s", e)

    # Add configured leverage for comparison
    account_data['configured_spot_leverage'] = config.spot_leverage
    account_data['configured_futures_leverage'] = config.futures_leverage

    # Set actual leverage - prefer exchange data, use sensible defaults
    # For spot: Default to 1x (cash trading) unless a margin position reports leverage
    if 'actual_spot_leverage' not in account_data:
        # No spot margin position found - use 1x for cash trading
        # VIP/margin accounts with active positions will get leverage from position data
        account_data['actual_spot_leverage'] = 1
        account_data['spot_leverage_source'] = 'cash'  # Cash spot = no leverage

    # For futures: Check if we need to fetch leverage from exchange settings
    if 'actual_futures_leverage' not in account_data:
        # Try to get leverage setting from exchange for the futures symbol
        try:
            async def fetch_futures_leverage():
                adapter = engine.futures_adapter or engine.spot_adapter
                if adapter and hasattr(adapter, 'get_leverage_info'):
                    return await adapter.get_leverage_info(config.futures_symbol)
                return None

            if loop:
                lev_future = asyncio.run_coroutine_threadsafe(fetch_futures_leverage(), loop)
                lev_info = lev_future.result(timeout=5)
                if lev_info and lev_info.get('leverage'):
                    account_data['actual_futures_leverage'] = lev_info['leverage']
                    account_data['futures_leverage_source'] = 'exchange'
                else:
                    account_data['actual_futures_leverage'] = config.futures_leverage
                    account_data['futures_leverage_source'] = 'configured'
        except Exception as lev_err:
            logger.debug("Could not fetch futures leverage: %s", lev_err)
            account_data['actual_futures_leverage'] = config.futures_leverage
            account_data['futures_leverage_source'] = 'configured'

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
        spread = futures_price - spot_price
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


@app.route('/api/exchange-orders', methods=['GET'])
def get_exchange_orders():
    """Fetch real order history from the exchange (OKX)."""
    limit = request.args.get('limit', 50, type=int)

    adapter = engine.futures_adapter or engine.spot_adapter
    if not adapter or not hasattr(adapter, 'get_order_history'):
        return jsonify({'orders': [], 'error': 'No adapter available'})

    async def fetch_orders():
        return await adapter.get_order_history(limit=limit)

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(fetch_orders(), loop)
            orders = future.result(timeout=15)
            return jsonify({'orders': orders})
        except Exception as e:
            logger.error("Error fetching exchange orders: %s", e)
            return jsonify({'orders': [], 'error': str(e)})

    return jsonify({'orders': [], 'error': 'Event loop not running'})


@app.route('/api/exchange-orders/csv', methods=['GET'])
def download_exchange_orders_csv():
    """Download exchange order history as CSV."""
    import csv
    import io
    from flask import Response

    limit = request.args.get('limit', 100, type=int)

    adapter = engine.futures_adapter or engine.spot_adapter
    if not adapter or not hasattr(adapter, 'get_order_history'):
        return Response("No adapter available", status=400)

    async def fetch_orders():
        return await adapter.get_order_history(limit=limit)

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(fetch_orders(), loop)
            orders = future.result(timeout=15)

            # Create CSV
            output = io.StringIO()
            if orders:
                fieldnames = ['created_at', 'symbol', 'inst_type', 'side', 'pos_side',
                              'order_type', 'quantity', 'fill_qty', 'fill_price',
                              'leverage', 'fee', 'fee_ccy', 'pnl', 'state', 'order_id']
                writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                for order in orders:
                    writer.writerow(order)

            output.seek(0)
            return Response(
                output.getvalue(),
                mimetype='text/csv',
                headers={'Content-Disposition': 'attachment; filename=exchange_orders.csv'}
            )
        except Exception as e:
            logger.error("Error exporting exchange orders: %s", e)
            return Response(f"Error: {e}", status=500)

    return Response("Event loop not running", status=500)


@app.route('/api/trades/csv', methods=['GET'])
def download_trades_csv():
    """Download trade journal as CSV."""
    import csv
    import io
    from flask import Response

    limit = request.args.get('limit', 500, type=int)
    trades = db.get_trades(limit=limit)

    output = io.StringIO()
    if trades:
        fieldnames = ['id', 'asset', 'position_type', 'entry_time', 'entry_spot_price',
                      'entry_futures_price', 'entry_spread', 'entry_zscore',
                      'exit_time', 'exit_spot_price', 'exit_futures_price',
                      'exit_spread', 'exit_zscore', 'exit_reason',
                      'quantity', 'notional_usd', 'pnl_usd', 'pnl_percent',
                      'spot_order_id', 'futures_order_id', 'is_open', 'is_paper']
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for trade in trades:
            writer.writerow(trade.to_dict())

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=trade_journal.csv'}
    )


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


# ============== Manual Order Testing ==============
# Store multiple test positions
import uuid
test_positions = {}  # id -> position data


@app.route('/api/test-order/open', methods=['POST'])
def open_test_order():
    """Open a manual test order - single leg or spread."""
    global test_positions

    if not engine.spot_adapter and not engine.futures_adapter:
        return jsonify({'success': False, 'error': 'No exchange adapters available. Check API keys.'}), 400

    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data available. Wait for connection.'}), 400

    data = request.json
    order_type = data.get('order_type', 'BUY_SPOT')
    size_usd = data.get('size_usd', 100)

    spot_price = engine.spot_tick.mid
    futures_price = engine.futures_tick.mid
    quantity = size_usd / spot_price

    logger.info("Executing test order: %s, size=$%.2f, qty=%.6f", order_type, size_usd, quantity)

    def calc_limit_price(side: str, tick) -> float:
        """
        Calculate a passive limit price matching the real order executor logic.
        BUY  → bid + offset_bps  (capped just below ask to stay maker)
        SELL → ask - offset_bps  (floored just above bid to stay maker)
        """
        offset_bps = config.limit_order_price_offset_bps / 10000
        SAFETY_BUFFER_BPS = 0.00005  # 0.5 bps safety buffer
        if side.upper() == "BUY":
            target = tick.bid * (1 + offset_bps)
            max_price = tick.ask * (1 - SAFETY_BUFFER_BPS)
            return round(min(target, max_price), 2)
        else:
            target = tick.ask * (1 - offset_bps)
            min_price = tick.bid * (1 + SAFETY_BUFFER_BPS)
            return round(max(target, min_price), 2)

    async def execute_single_leg(market_type: str, side: str, price: float):
        """Execute a single leg order."""
        adapter = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter
        if not adapter:
            return None, f"No {market_type} adapter available", None

        symbol = config.spot_symbol if market_type == "SPOT" else config.futures_symbol
        order_type_str = config.order_execution_mode  # MARKET or LIMIT

        # For LIMIT orders use proper bid+offset / ask-offset pricing (same as OrderExecutor)
        # instead of mid price, so orders behave as maker orders
        if order_type_str == "LIMIT":
            tick = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
            limit_price = calc_limit_price(side, tick)
            logger.info("LIMIT %s %s: bid=%.2f ask=%.2f offset=%.1f bps → price=%.2f",
                       side, market_type, tick.bid, tick.ask,
                       config.limit_order_price_offset_bps, limit_price)
        else:
            limit_price = None

        # For futures, determine pos_side for long_short_mode
        pos_side = None
        if market_type == "FUTURES":
            pos_side = "long" if side == "BUY" else "short"

        result = await adapter.place_order(
            symbol=symbol,
            side=side,
            order_type=order_type_str,
            quantity=quantity,
            price=limit_price,
            pos_side=pos_side,
        )

        if result.success:
            return result, None, pos_side
        return None, result.error, None

    async def execute_order():
        results = []

        if order_type == "BUY_SPOT":
            result, error, pos_side = await execute_single_leg("SPOT", "BUY", spot_price)
            if result:
                results.append(("SPOT", "BUY", spot_price, result, quantity, pos_side))
            else:
                return None, error

        elif order_type == "SELL_SPOT":
            result, error, pos_side = await execute_single_leg("SPOT", "SELL", spot_price)
            if result:
                results.append(("SPOT", "SELL", spot_price, result, quantity, pos_side))
            else:
                return None, error

        elif order_type == "BUY_FUTURES":
            result, error, pos_side = await execute_single_leg("FUTURES", "BUY", futures_price)
            if result:
                results.append(("FUTURES", "BUY", futures_price, result, quantity, pos_side))
            else:
                return None, error

        elif order_type == "SELL_FUTURES":
            result, error, pos_side = await execute_single_leg("FUTURES", "SELL", futures_price)
            if result:
                results.append(("FUTURES", "SELL", futures_price, result, quantity, pos_side))
            else:
                return None, error

        elif order_type == "LONG_SPREAD":
            # Buy Spot + Sell Futures
            spot_result, spot_error, _ = await execute_single_leg("SPOT", "BUY", spot_price)
            if not spot_result:
                return None, f"Spot order failed: {spot_error}"
            results.append(("SPOT", "BUY", spot_price, spot_result, quantity, None))

            futures_result, futures_error, pos_side = await execute_single_leg("FUTURES", "SELL", futures_price)
            if not futures_result:
                return None, f"Futures order failed: {futures_error}"
            results.append(("FUTURES", "SELL", futures_price, futures_result, quantity, pos_side))

        elif order_type == "SHORT_SPREAD":
            # Sell Spot + Buy Futures
            spot_result, spot_error, _ = await execute_single_leg("SPOT", "SELL", spot_price)
            if not spot_result:
                return None, f"Spot order failed: {spot_error}"
            results.append(("SPOT", "SELL", spot_price, spot_result, quantity, None))

            futures_result, futures_error, pos_side = await execute_single_leg("FUTURES", "BUY", futures_price)
            if not futures_result:
                return None, f"Futures order failed: {futures_error}"
            results.append(("FUTURES", "BUY", futures_price, futures_result, quantity, pos_side))

        return results, None

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(execute_order(), loop)
            results, error = future.result(timeout=60)

            if error:
                return jsonify({'success': False, 'error': error}), 400

            # Store positions
            for market_type, side, entry_price, result, qty, pos_side in results:
                pos_id = str(uuid.uuid4())[:8]
                test_positions[pos_id] = {
                    'id': pos_id,
                    'market_type': market_type,
                    'side': side,
                    'quantity': qty,
                    'entry_price': entry_price,
                    'order_id': result.order_id,
                    'entry_time': datetime.now(timezone.utc).isoformat(),
                    'pos_side': pos_side,  # Store for closing futures in long_short_mode
                }
                logger.info("Test position opened: %s %s %s @ $%.2f, order_id=%s, pos_side=%s",
                           pos_id, side, market_type, entry_price, result.order_id, pos_side)

            return jsonify({'success': True, 'positions_opened': len(results)})

        except Exception as e:
            logger.error("Error executing test order: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-order/close', methods=['POST'])
def close_test_order():
    """Close a specific test position."""
    global test_positions

    data = request.json
    position_id = data.get('position_id')

    if not position_id or position_id not in test_positions:
        return jsonify({'success': False, 'error': 'Position not found'}), 404

    pos = test_positions[position_id]

    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data available'}), 400

    # Determine closing side (opposite of entry)
    close_side = "SELL" if pos['side'] == "BUY" else "BUY"
    market_type = pos['market_type']
    quantity = pos['quantity']
    current_price = engine.spot_tick.mid if market_type == "SPOT" else engine.futures_tick.mid

    logger.info("Closing test position %s: %s %s @ $%.2f", position_id, close_side, market_type, current_price)

    # Get stored pos_side for futures (critical for long_short_mode!)
    stored_pos_side = pos.get('pos_side')
    original_order_id = pos.get('order_id')

    async def close_position():
        adapter = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter
        if not adapter:
            return None, f"No {market_type} adapter available"

        symbol = config.spot_symbol if market_type == "SPOT" else config.futures_symbol

        # First, check if the original order is still pending (not filled)
        # If pending, we should CANCEL it, not place an opposite order
        if original_order_id:
            order_status = await adapter.get_order_status(symbol, original_order_id)
            if order_status:
                state = order_status.get("state", "")
                filled_qty = order_status.get("filled_qty", 0)

                if state in ("live", "partially_filled") or filled_qty == 0:
                    # Order is still pending - cancel it instead of placing a close order
                    logger.info("Original order %s still pending (state=%s, filled=%.6f) - cancelling",
                               original_order_id, state, filled_qty)
                    cancelled = await adapter.cancel_order(symbol, original_order_id)
                    if cancelled:
                        # Return a "mock" successful result for cancelled order
                        from models import OrderResult
                        return OrderResult(success=True, order_id=original_order_id), "cancelled"
                    else:
                        return None, f"Failed to cancel pending order {original_order_id}"

                elif state == "filled":
                    # Order was filled - use actual filled qty for close order
                    if filled_qty > 0:
                        quantity = filled_qty
                    logger.info("Original order %s was filled (filled_qty=%.8f) - placing close order",
                               original_order_id, quantity)
                else:
                    # Order was already cancelled or in unknown state
                    logger.info("Original order %s already in state '%s' - removing position", original_order_id, state)
                    from models import OrderResult
                    return OrderResult(success=True, order_id=original_order_id), "already_closed"

        # Place closing order (only if original was filled)
        order_type_str = config.order_execution_mode

        # For LIMIT closes, use bid+offset / ask-offset (same as entry logic)
        # close_side is opposite of entry: BUY close uses ask-offset, SELL close uses bid+offset
        close_limit_price = None
        if order_type_str == "LIMIT":
            tick = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
            offset_bps = config.limit_order_price_offset_bps / 10000
            SAFETY_BUFFER_BPS = 0.00005
            if close_side.upper() == "BUY":
                target = tick.bid * (1 + offset_bps)
                max_price = tick.ask * (1 - SAFETY_BUFFER_BPS)
                close_limit_price = round(min(target, max_price), 2)
            else:
                target = tick.ask * (1 - offset_bps)
                min_price = tick.bid * (1 + SAFETY_BUFFER_BPS)
                close_limit_price = round(max(target, min_price), 2)
            logger.info("LIMIT close %s %s: bid=%.2f ask=%.2f offset=%.1f bps → price=%.2f",
                       close_side, market_type, tick.bid, tick.ask,
                       config.limit_order_price_offset_bps, close_limit_price)

        result = await adapter.place_order(
            symbol=symbol,
            side=close_side,
            order_type=order_type_str,
            quantity=quantity,
            price=close_limit_price,
            pos_side=stored_pos_side,  # Use original pos_side for closing!
            reduce_only=True if market_type == "FUTURES" else False,
        )

        return result, None if result.success else result.error

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(close_position(), loop)
            result, status_or_error = future.result(timeout=60)

            if not result or not result.success:
                return jsonify({'success': False, 'error': status_or_error or 'Close order failed'}), 400

            # Handle different close scenarios
            if status_or_error == "cancelled":
                # Order was pending and got cancelled - no P&L
                logger.info("Test position %s cancelled (was pending, never filled)", position_id)
                del test_positions[position_id]
                return jsonify({
                    'success': True,
                    'pnl_usd': 0,
                    'close_price': 0,
                    'order_id': result.order_id,
                    'message': 'Pending order cancelled (not filled)'
                })
            elif status_or_error == "already_closed":
                # Order was already cancelled/unknown state
                logger.info("Test position %s was already closed/cancelled", position_id)
                del test_positions[position_id]
                return jsonify({
                    'success': True,
                    'pnl_usd': 0,
                    'close_price': 0,
                    'order_id': result.order_id,
                    'message': 'Position was already closed'
                })

            # Normal close - calculate P&L
            entry_price = pos['entry_price']
            if pos['side'] == "BUY":
                pnl = (current_price - entry_price) * quantity
            else:
                pnl = (entry_price - current_price) * quantity

            logger.info("Test position %s closed: P&L=$%.2f", position_id, pnl)

            # Remove position
            del test_positions[position_id]

            return jsonify({
                'success': True,
                'pnl_usd': pnl,
                'close_price': current_price,
                'order_id': result.order_id,
            })

        except Exception as e:
            logger.error("Error closing test position: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-order/close-all', methods=['POST'])
def close_all_test_orders():
    """Close all test positions."""
    global test_positions

    if not test_positions:
        return jsonify({'success': True, 'total_pnl': 0, 'closed': 0})

    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data available'}), 400

    total_pnl = 0
    closed = 0
    errors = []

    async def close_all():
        nonlocal total_pnl, closed, errors

        for pos_id, pos in list(test_positions.items()):
            close_side = "SELL" if pos['side'] == "BUY" else "BUY"
            market_type = pos['market_type']
            quantity = pos['quantity']
            current_price = engine.spot_tick.mid if market_type == "SPOT" else engine.futures_tick.mid
            stored_pos_side = pos.get('pos_side')
            original_order_id = pos.get('order_id')

            adapter = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter
            if not adapter:
                errors.append(f"No {market_type} adapter for {pos_id}")
                continue

            symbol = config.spot_symbol if market_type == "SPOT" else config.futures_symbol

            # Check if original order is still pending - if so, cancel instead of close
            was_cancelled = False
            if original_order_id:
                order_status = await adapter.get_order_status(symbol, original_order_id)
                if order_status:
                    state = order_status.get("state", "")
                    filled_qty = order_status.get("filled_qty", 0)

                    if state in ("live", "partially_filled") or filled_qty == 0:
                        # Order still pending - cancel it
                        logger.info("Position %s order still pending - cancelling", pos_id)
                        cancelled = await adapter.cancel_order(symbol, original_order_id)
                        if cancelled:
                            was_cancelled = True
                            closed += 1
                            del test_positions[pos_id]
                            logger.info("Cancelled pending position %s", pos_id)
                            continue
                        else:
                            errors.append(f"{pos_id}: Failed to cancel pending order")
                            continue
                    elif state == "filled":
                        # Use actual filled quantity for close order
                        if filled_qty > 0:
                            quantity = filled_qty
                    elif state == "canceled":
                        # Already cancelled
                        closed += 1
                        del test_positions[pos_id]
                        logger.info("Position %s was already cancelled", pos_id)
                        continue

            # Place closing order (original was filled)
            order_type_str = config.order_execution_mode

            # For LIMIT closes, use bid+offset / ask-offset matching OrderExecutor logic
            close_limit_price = None
            if order_type_str == "LIMIT":
                tick = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
                offset_bps = config.limit_order_price_offset_bps / 10000
                SAFETY_BUFFER_BPS = 0.00005
                if close_side.upper() == "BUY":
                    target = tick.bid * (1 + offset_bps)
                    max_price = tick.ask * (1 - SAFETY_BUFFER_BPS)
                    close_limit_price = round(min(target, max_price), 2)
                else:
                    target = tick.ask * (1 - offset_bps)
                    min_price = tick.bid * (1 + SAFETY_BUFFER_BPS)
                    close_limit_price = round(max(target, min_price), 2)

            result = await adapter.place_order(
                symbol=symbol,
                side=close_side,
                order_type=order_type_str,
                quantity=quantity,
                price=close_limit_price,
                pos_side=stored_pos_side,  # Use original pos_side for closing!
                reduce_only=True if market_type == "FUTURES" else False,
            )

            if result.success:
                entry_price = pos['entry_price']
                if pos['side'] == "BUY":
                    pnl = (current_price - entry_price) * quantity
                else:
                    pnl = (entry_price - current_price) * quantity

                total_pnl += pnl
                closed += 1
                del test_positions[pos_id]
                logger.info("Closed position %s: P&L=$%.2f", pos_id, pnl)
            else:
                errors.append(f"{pos_id}: {result.error}")

    if loop:
        try:
            future = asyncio.run_coroutine_threadsafe(close_all(), loop)
            future.result(timeout=120)

            return jsonify({
                'success': len(errors) == 0,
                'total_pnl': total_pnl,
                'closed': closed,
                'errors': errors if errors else None,
            })

        except Exception as e:
            logger.error("Error closing all test positions: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-order/status', methods=['GET'])
def get_test_order_status():
    """Get all test positions with current prices and P&L."""
    positions = []

    for pos_id, pos in test_positions.items():
        market_type = pos['market_type']
        current_price = 0
        unrealized_pnl = 0

        if engine.spot_tick and engine.futures_tick:
            current_price = engine.spot_tick.mid if market_type == "SPOT" else engine.futures_tick.mid
            entry_price = pos['entry_price']
            quantity = pos['quantity']

            if pos['side'] == "BUY":
                unrealized_pnl = (current_price - entry_price) * quantity
            else:
                unrealized_pnl = (entry_price - current_price) * quantity

        positions.append({
            'id': pos_id,
            'market_type': market_type,
            'side': pos['side'],
            'quantity': pos['quantity'],
            'entry_price': pos['entry_price'],
            'current_price': current_price,
            'unrealized_pnl': unrealized_pnl,
            'order_id': pos['order_id'],
            'entry_time': pos['entry_time'],
        })

    return jsonify({'positions': positions})


# ─────────────────────────────────────────────────────────────────────────────
# Full Test Suite – runs 18 scenarios in the background, emits live WS events
# ─────────────────────────────────────────────────────────────────────────────

_test_suite_cancel: bool = False
_test_suite_running: bool = False
_single_running: bool = False          # True while a single-scenario run is in progress
_test_suite_state: Dict[str, Any] = {
    'running': False, 'current': 0, 'total': 36,
    'pass': 0, 'fail': 0, 'scenarios': [], 'start_time': None, 'order_mode': '',
    'single_running': False,
}

# 18 scenarios: 6 order-types × (fill-test, fill-test, cancel-test)
_SUITE_SCENARIOS = [
    {'id': '1a', 'label': 'BUY_SPOT #1',         'order_type': 'BUY_SPOT',      'cancel_test': False},
    {'id': '1b', 'label': 'BUY_SPOT #2',         'order_type': 'BUY_SPOT',      'cancel_test': False},
    {'id': '1c', 'label': 'BUY_SPOT #3 (cancel)','order_type': 'BUY_SPOT',      'cancel_test': True},
    {'id': '2a', 'label': 'SELL_FUTURES #1',      'order_type': 'SELL_FUTURES',  'cancel_test': False},
    {'id': '2b', 'label': 'SELL_FUTURES #2',      'order_type': 'SELL_FUTURES',  'cancel_test': False},
    {'id': '2c', 'label': 'SELL_FUTURES #3 (cancel)', 'order_type': 'SELL_FUTURES', 'cancel_test': True},
    {'id': '3a', 'label': 'BUY_FUTURES #1',       'order_type': 'BUY_FUTURES',   'cancel_test': False},
    {'id': '3b', 'label': 'BUY_FUTURES #2',       'order_type': 'BUY_FUTURES',   'cancel_test': False},
    {'id': '3c', 'label': 'BUY_FUTURES #3 (cancel)', 'order_type': 'BUY_FUTURES', 'cancel_test': True},
    {'id': '4a', 'label': 'SELL_SPOT #1',         'order_type': 'SELL_SPOT',     'cancel_test': False},
    {'id': '4b', 'label': 'SELL_SPOT #2',         'order_type': 'SELL_SPOT',     'cancel_test': False},
    {'id': '4c', 'label': 'SELL_SPOT #3 (cancel)','order_type': 'SELL_SPOT',     'cancel_test': True},
    {'id': '5a', 'label': 'LONG_SPREAD #1',       'order_type': 'LONG_SPREAD',   'cancel_test': False},
    {'id': '5b', 'label': 'LONG_SPREAD #2',       'order_type': 'LONG_SPREAD',   'cancel_test': False},
    {'id': '5c', 'label': 'LONG_SPREAD #3 (cancel)', 'order_type': 'LONG_SPREAD', 'cancel_test': True},
    {'id': '6a', 'label': 'SHORT_SPREAD #1',      'order_type': 'SHORT_SPREAD',  'cancel_test': False},
    {'id': '6b', 'label': 'SHORT_SPREAD #2',      'order_type': 'SHORT_SPREAD',  'cancel_test': False},
    {'id': '6c', 'label': 'SHORT_SPREAD #3 (cancel)', 'order_type': 'SHORT_SPREAD', 'cancel_test': True},
    # ── 18 MARKET-order scenarios (forced_mode overrides config) ──────────────
    {'id': 'm1a', 'label': 'MKT BUY_SPOT #1',              'order_type': 'BUY_SPOT',      'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm1b', 'label': 'MKT BUY_SPOT #2',              'order_type': 'BUY_SPOT',      'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm1c', 'label': 'MKT BUY_SPOT #3 (quick-close)','order_type': 'BUY_SPOT',      'cancel_test': True,  'forced_mode': 'MARKET'},
    {'id': 'm2a', 'label': 'MKT SELL_FUTURES #1',           'order_type': 'SELL_FUTURES',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm2b', 'label': 'MKT SELL_FUTURES #2',           'order_type': 'SELL_FUTURES',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm2c', 'label': 'MKT SELL_FUTURES #3 (quick-close)', 'order_type': 'SELL_FUTURES', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm3a', 'label': 'MKT BUY_FUTURES #1',            'order_type': 'BUY_FUTURES',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm3b', 'label': 'MKT BUY_FUTURES #2',            'order_type': 'BUY_FUTURES',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm3c', 'label': 'MKT BUY_FUTURES #3 (quick-close)', 'order_type': 'BUY_FUTURES', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm4a', 'label': 'MKT SELL_SPOT #1',              'order_type': 'SELL_SPOT',     'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm4b', 'label': 'MKT SELL_SPOT #2',              'order_type': 'SELL_SPOT',     'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm4c', 'label': 'MKT SELL_SPOT #3 (quick-close)','order_type': 'SELL_SPOT',     'cancel_test': True,  'forced_mode': 'MARKET'},
    {'id': 'm5a', 'label': 'MKT LONG_SPREAD #1',            'order_type': 'LONG_SPREAD',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm5b', 'label': 'MKT LONG_SPREAD #2',            'order_type': 'LONG_SPREAD',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm5c', 'label': 'MKT LONG_SPREAD #3 (quick-close)', 'order_type': 'LONG_SPREAD', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm6a', 'label': 'MKT SHORT_SPREAD #1',           'order_type': 'SHORT_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm6b', 'label': 'MKT SHORT_SPREAD #2',           'order_type': 'SHORT_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm6c', 'label': 'MKT SHORT_SPREAD #3 (quick-close)', 'order_type': 'SHORT_SPREAD', 'cancel_test': True, 'forced_mode': 'MARKET'},
]


async def _suite_open_order(order_type: str, quantity: float, forced_mode: str | None = None):
    """
    Place the opening leg(s) for a suite scenario.
    Returns (list_of_leg_tuples, error_str).  error_str is None on success.
    Each leg tuple: (market_type, side, entry_price, OrderResult, qty, pos_side)
    forced_mode overrides config.entry_execution_mode when set (e.g. 'MARKET').
    """
    order_mode = forced_mode or config.entry_execution_mode

    def calc_limit_price(side: str, tick) -> float:
        offset_bps = config.limit_order_price_offset_bps / 10000
        SAFETY = 0.00005
        if side.upper() == "BUY":
            return round(min(tick.bid * (1 + offset_bps), tick.ask * (1 - SAFETY)), 2)
        return round(max(tick.ask * (1 - offset_bps), tick.bid * (1 + SAFETY)), 2)

    async def single_leg(market_type: str, side: str):
        adapter = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter
        if not adapter:
            return None, f"No {market_type} adapter"
        symbol   = config.spot_symbol if market_type == "SPOT" else config.futures_symbol
        tick     = engine.spot_tick    if market_type == "SPOT" else engine.futures_tick
        pos_side = ("long" if side == "BUY" else "short") if market_type == "FUTURES" else None
        lp       = calc_limit_price(side, tick) if order_mode == "LIMIT" else None
        if order_mode == "LIMIT":
            logger.info("[SUITE] LIMIT %s %s: bid=%.2f ask=%.2f offset=%.1fbps → px=%.2f",
                        side, market_type, tick.bid, tick.ask,
                        config.limit_order_price_offset_bps, lp)
        result = await adapter.place_order(
            symbol=symbol, side=side, order_type=order_mode,
            quantity=quantity, price=lp, pos_side=pos_side,
        )
        if result.success:
            entry_price = tick.mid
            return (market_type, side, entry_price, result, quantity, pos_side), None
        return None, result.error

    legs: list = []
    if order_type in ("BUY_SPOT",):
        leg, err = await single_leg("SPOT", "BUY")
        if err: return None, err
        legs.append(leg)
    elif order_type == "SELL_SPOT":
        leg, err = await single_leg("SPOT", "SELL")
        if err: return None, err
        legs.append(leg)
    elif order_type == "BUY_FUTURES":
        leg, err = await single_leg("FUTURES", "BUY")
        if err: return None, err
        legs.append(leg)
    elif order_type == "SELL_FUTURES":
        leg, err = await single_leg("FUTURES", "SELL")
        if err: return None, err
        legs.append(leg)
    elif order_type == "LONG_SPREAD":
        spot_leg, err = await single_leg("SPOT", "BUY")
        if err: return None, f"Spot: {err}"
        legs.append(spot_leg)
        fut_leg, err = await single_leg("FUTURES", "SELL")
        if err: return legs, f"Futures: {err}"   # return spot leg so caller can clean up
        legs.append(fut_leg)
    elif order_type == "SHORT_SPREAD":
        spot_leg, err = await single_leg("SPOT", "SELL")
        if err: return None, f"Spot: {err}"
        legs.append(spot_leg)
        fut_leg, err = await single_leg("FUTURES", "BUY")
        if err: return legs, f"Futures: {err}"
        legs.append(fut_leg)
    else:
        return None, f"Unknown order_type: {order_type}"

    return legs, None


async def _suite_close_position(pos_id: str):
    """
    Close one test position (cancel if pending, close if filled).
    Returns (success: bool, detail_str: str).
    """
    global test_positions
    if pos_id not in test_positions:
        return False, "position not found"

    pos             = test_positions[pos_id]
    market_type     = pos['market_type']
    close_side      = "SELL" if pos['side'] == "BUY" else "BUY"
    quantity        = pos['quantity']
    symbol          = config.spot_symbol if market_type == "SPOT" else config.futures_symbol
    stored_pos_side = pos.get('pos_side')
    original_oid    = pos.get('order_id')
    adapter         = engine.spot_adapter if market_type == "SPOT" else engine.futures_adapter

    # Elapsed time from order placement to close/cancel
    try:
        entry_dt = datetime.fromisoformat(pos['entry_time'])
        elapsed  = (datetime.now(timezone.utc) - entry_dt).total_seconds()
        elapsed_str = f" ({elapsed:.1f}s)"
    except Exception:
        elapsed_str = ""

    if not adapter:
        return False, f"no {market_type} adapter"

    # Cancel if original order still pending
    if original_oid:
        status = await adapter.get_order_status(symbol, original_oid)
        if status:
            state      = status.get("state", "")
            filled_qty = status.get("filled_qty", 0)
            if state in ("live", "partially_filled") or filled_qty == 0:
                cancelled = await adapter.cancel_order(symbol, original_oid)
                del test_positions[pos_id]
                return True, f"cancelled (was pending){elapsed_str}" if cancelled else f"cancel-failed{elapsed_str}"
            elif state == "filled":
                if filled_qty > 0:
                    quantity = filled_qty
            elif state == "canceled":
                del test_positions[pos_id]
                return True, f"already cancelled{elapsed_str}"

    # Place closing order
    order_mode       = config.exit_execution_mode  # Use exit mode for closing legs
    close_lp: Optional[float] = None
    if order_mode == "LIMIT":
        tick        = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
        offset_bps  = config.limit_order_price_offset_bps / 10000
        SAFETY      = 0.00005
        if close_side.upper() == "BUY":
            close_lp = round(min(tick.bid * (1 + offset_bps), tick.ask * (1 - SAFETY)), 2)
        else:
            close_lp = round(max(tick.ask * (1 - offset_bps), tick.bid * (1 + SAFETY)), 2)

    result = await adapter.place_order(
        symbol=symbol, side=close_side, order_type=order_mode,
        quantity=quantity, price=close_lp,
        pos_side=stored_pos_side,
        reduce_only=(market_type == "FUTURES"),
    )
    if result.success:
        tick        = engine.spot_tick if market_type == "SPOT" else engine.futures_tick
        cur_price   = tick.mid if tick else pos['entry_price']
        pnl         = (cur_price - pos['entry_price']) * quantity if pos['side'] == "BUY" \
                      else (pos['entry_price'] - cur_price) * quantity
        del test_positions[pos_id]
        return True, f"closed pnl=${pnl:.2f}{elapsed_str}"
    return False, result.error


async def run_test_suite():
    """
    Full 36-scenario test suite (18 LIMIT + 18 MARKET).
    Runs entirely in the async event loop so it can await adapter calls without
    blocking Flask.  Emits 'test_suite_update' WebSocket events after every
    state change so the UI stays in sync.

    Timing per scenario:
      LIMIT  open: limit_timeout s wait + 20 s cooldown → ~13-15 min for 18 LIMIT
      MARKET open: 4 s wait          +  5 s cooldown → ~3 min for 18 MARKET
    Cancel/quick-close scenarios always close after 3 s.
    """
    global _test_suite_cancel, _test_suite_running, _test_suite_state, test_positions
    import copy

    _test_suite_running = True
    _test_suite_cancel  = False

    order_mode     = config.entry_execution_mode  # default; per-scenario forced_mode may override
    limit_timeout  = config.limit_order_timeout_sec

    scenarios = copy.deepcopy(_SUITE_SCENARIOS)
    for s in scenarios:
        s['status'] = 'pending'
        s['detail'] = ''
        s['mode']   = order_mode

    _test_suite_state = {
        'running':    True,
        'current':    0,
        'total':      len(scenarios),
        'pass':       0,
        'fail':       0,
        'scenarios':  scenarios,
        'start_time': datetime.now(timezone.utc).isoformat(),
        'order_mode': order_mode,
        'inter_pause': inter_pause,
    }
    socketio.emit('test_suite_update', _test_suite_state)

    spot_price = engine.spot_tick.mid if engine.spot_tick else 65000.0
    # Ensure quantity is large enough for at least 1 futures contract.
    # BTC-USDT-SWAP ctVal = 0.01 BTC → $100 notional at $98k is only 0.1 contracts (below min 1).
    futures_info = await engine.futures_adapter.get_symbol_info(config.futures_symbol) if engine.futures_adapter else None
    ct_val = futures_info.get("contract_val", 0.01) if futures_info else 0.01
    quantity = max(100.0 / spot_price, ct_val)  # at least 1 contract worth of BTC

    for idx, scenario in enumerate(scenarios):
        if _test_suite_cancel:
            scenario['status'] = 'cancelled'
            scenario['detail'] = 'suite stopped'
            break

        scenario['status'] = 'running'
        _test_suite_state['current'] = idx + 1
        socketio.emit('test_suite_update', _test_suite_state)
        logger.info("[TEST SUITE] %d/%d  %s  [%s]",
                    idx + 1, len(scenarios), scenario['label'], scen_mode)

        order_type   = scenario['order_type']
        cancel_test  = scenario['cancel_test']
        scen_mode    = scenario.get('forced_mode') or order_mode  # per-scenario override
        scenario['mode'] = scen_mode  # ensure Mode column is always populated
        inter_pause  = 5 if scen_mode == 'MARKET' else 20

        # ── OPEN ──────────────────────────────────────────────────────────
        try:
            legs, open_err = await _suite_open_order(order_type, quantity, forced_mode=scen_mode)
        except Exception as exc:
            open_err = str(exc)
            legs = None

        if open_err or not legs:
            scenario['status'] = 'fail'
            scenario['detail'] = f"open failed: {open_err}"
            _test_suite_state['fail'] += 1
            socketio.emit('test_suite_update', _test_suite_state)
            logger.warning("[TEST SUITE] %s FAIL open: %s", scenario['label'], open_err)
            await asyncio.sleep(inter_pause)
            continue

        # Register positions in global test_positions (same dict the UI reads)
        opened_ids = []
        for (mtype, side, entry_px, result, qty, ps) in legs:
            pos_id = str(uuid.uuid4())[:8]
            test_positions[pos_id] = {
                'id': pos_id, 'market_type': mtype, 'side': side,
                'quantity': qty, 'entry_price': entry_px,
                'order_id': result.order_id,
                'entry_time': datetime.now(timezone.utc).isoformat(),
                'pos_side': ps,
            }
            opened_ids.append(pos_id)

        oid_short = legs[0][3].order_id[:12] if legs else '?'
        scenario['detail'] = (
            f"{len(opened_ids)} leg(s) placed  order_id={oid_short}..."
        )
        socketio.emit('test_suite_update', _test_suite_state)

        # ── WAIT ──────────────────────────────────────────────────────────
        if cancel_test:
            # LIMIT cancel: cancels unfilled order.  MARKET quick-close: closes filled position.
            label = "cancel test" if scen_mode == "LIMIT" else "quick-close"
            scenario['detail'] += f"  |  {label} – closing in 3 s"
            socketio.emit('test_suite_update', _test_suite_state)
            await asyncio.sleep(3)
        elif scen_mode == "LIMIT":
            # Give the limit order a real chance to fill
            scenario['detail'] += f"  |  waiting {limit_timeout} s for fill…"
            socketio.emit('test_suite_update', _test_suite_state)
            await asyncio.sleep(limit_timeout)
        else:
            # Market order – small wait for exchange confirmation
            await asyncio.sleep(4)

        if _test_suite_cancel:
            scenario['status'] = 'cancelled'
            scenario['detail'] += '  |  suite stopped mid-scenario'
            break

        # ── CLOSE ─────────────────────────────────────────────────────────
        close_ok      = True
        close_details = []
        for pos_id in opened_ids:
            try:
                ok, detail = await _suite_close_position(pos_id)
                close_details.append(detail)
                if not ok:
                    close_ok = False
            except Exception as exc:
                close_details.append(str(exc))
                close_ok = False

        detail_str = "  |  ".join(close_details)
        if close_ok:
            scenario['status'] = 'pass'
            scenario['detail'] = detail_str
            _test_suite_state['pass'] += 1
            logger.info("[TEST SUITE] %s  PASS  %s", scenario['label'], detail_str)
        else:
            scenario['status'] = 'fail'
            scenario['detail'] = detail_str
            _test_suite_state['fail'] += 1
            logger.warning("[TEST SUITE] %s  FAIL  %s", scenario['label'], detail_str)

        socketio.emit('test_suite_update', _test_suite_state)

        # ── INTER-SCENARIO COOLDOWN ────────────────────────────────────────
        if idx < len(scenarios) - 1 and not _test_suite_cancel:
            scenario['detail'] += f"  |  cooling {inter_pause} s…"
            socketio.emit('test_suite_update', _test_suite_state)
            # Sleep in 1-second slices so we can react to cancellation quickly
            for _ in range(inter_pause):
                if _test_suite_cancel:
                    break
                await asyncio.sleep(1)

    _test_suite_state['running']  = False
    _test_suite_running           = False
    socketio.emit('test_suite_update', _test_suite_state)
    logger.info("[TEST SUITE] Done – pass=%d  fail=%d",
                _test_suite_state['pass'], _test_suite_state['fail'])


@app.route('/api/test-suite/start', methods=['POST'])
def start_test_suite():
    """Start the full 36-scenario test suite (18 LIMIT + 18 MARKET) in the background."""
    global _test_suite_running
    if _test_suite_running:
        return jsonify({'success': False, 'error': 'Suite already running'}), 400
    if not engine.spot_adapter:
        return jsonify({'success': False, 'error': 'No exchange connected'}), 400
    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data – wait for connection'}), 400
    if loop:
        asyncio.run_coroutine_threadsafe(run_test_suite(), loop)
        return jsonify({'success': True, 'message': 'Test suite started'})
    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-suite/stop', methods=['POST'])
def stop_test_suite():
    """Cancel the running test suite after the current scenario finishes."""
    global _test_suite_cancel
    _test_suite_cancel = True
    return jsonify({'success': True, 'message': 'Stop signal sent'})


@app.route('/api/test-suite/status', methods=['GET'])
def get_test_suite_status():
    """Return the current test suite state.
    If no suite has run yet, pre-populate scenarios so the UI can show Run buttons."""
    state = dict(_test_suite_state)
    if not state.get('scenarios'):
        state['scenarios'] = [
            {**s, 'status': 'pending', 'detail': '', 'mode': s.get('forced_mode', config.entry_execution_mode)}
            for s in _SUITE_SCENARIOS
        ]
    return jsonify(state)


# ─────────────────────────────────────────────────────────────────────────────
# Single-scenario runner
# ─────────────────────────────────────────────────────────────────────────────

async def run_single_scenario_task(scenario_id: str):
    """Run one scenario by ID, emitting live WebSocket updates like the full suite."""
    global _single_running, _test_suite_state, test_positions

    _single_running = True
    _test_suite_state['single_running'] = True

    try:
        scenario_def = next((s for s in _SUITE_SCENARIOS if s['id'] == scenario_id), None)
        if not scenario_def:
            logger.error("[SINGLE] Scenario %s not found", scenario_id)
            return

        # Ensure state has a scenarios list so the UI row can be updated
        if not _test_suite_state.get('scenarios'):
            _test_suite_state['scenarios'] = [
                {**s, 'status': 'pending', 'detail': '', 'mode': s.get('forced_mode', config.entry_execution_mode)}
                for s in _SUITE_SCENARIOS
            ]

        scen_idx = next(
            (i for i, s in enumerate(_test_suite_state['scenarios']) if s['id'] == scenario_id),
            None,
        )
        if scen_idx is None:
            logger.error("[SINGLE] Scenario %s missing from state list", scenario_id)
            return

        scenario = _test_suite_state['scenarios'][scen_idx]

        # Quantity: same logic as full suite
        spot_price = engine.spot_tick.mid if engine.spot_tick else 0
        if not spot_price:
            scenario['status'] = 'fail'
            scenario['detail'] = 'No spot price available'
            socketio.emit('test_suite_update', _test_suite_state)
            return

        symbol_info = None
        if engine.futures_adapter:
            symbol_info = await engine.futures_adapter.get_symbol_info(config.futures_symbol)
        ct_val   = float(symbol_info.get('ct_val', 0.01)) if symbol_info else 0.01
        quantity = max(100.0 / spot_price, ct_val)

        order_mode    = config.entry_execution_mode
        limit_timeout = config.limit_order_timeout_sec
        scen_mode     = scenario_def.get('forced_mode') or order_mode
        scenario['mode'] = scen_mode  # ensure Mode column is always populated
        cancel_test   = scenario_def.get('cancel_test', False)

        # Mark running
        scenario['status'] = 'running'
        scenario['detail'] = ''
        socketio.emit('test_suite_update', _test_suite_state)
        logger.info("[SINGLE] %s [%s]", scenario['label'], scen_mode)

        # Open
        try:
            legs, open_err = await _suite_open_order(
                scenario_def['order_type'], quantity, forced_mode=scen_mode,
            )
        except Exception as exc:
            open_err = str(exc)
            legs = None

        if open_err or not legs:
            scenario['status'] = 'fail'
            scenario['detail'] = f"open failed: {open_err}"
            socketio.emit('test_suite_update', _test_suite_state)
            return

        # Register positions
        opened_ids = []
        for (mtype, side, entry_px, result, qty, ps) in legs:
            pos_id = str(uuid.uuid4())[:8]
            test_positions[pos_id] = {
                'id': pos_id, 'market_type': mtype, 'side': side,
                'quantity': qty, 'entry_price': entry_px,
                'order_id': result.order_id,
                'entry_time': datetime.now(timezone.utc).isoformat(),
                'pos_side': ps,
            }
            opened_ids.append(pos_id)

        oid_short = legs[0][3].order_id[:12] if legs else '?'
        scenario['detail'] = f"{len(opened_ids)} leg(s) placed  order_id={oid_short}..."
        socketio.emit('test_suite_update', _test_suite_state)

        # Wait
        if cancel_test:
            label = "cancel test" if scen_mode == "LIMIT" else "quick-close"
            scenario['detail'] += f"  |  {label} – closing in 3 s"
            socketio.emit('test_suite_update', _test_suite_state)
            await asyncio.sleep(3)
        elif scen_mode == "LIMIT":
            scenario['detail'] += f"  |  waiting {limit_timeout} s for fill…"
            socketio.emit('test_suite_update', _test_suite_state)
            await asyncio.sleep(limit_timeout)
        else:
            await asyncio.sleep(4)

        # Close
        close_ok      = True
        close_details = []
        for pos_id in opened_ids:
            try:
                ok, detail = await _suite_close_position(pos_id)
                close_details.append(detail)
                if not ok:
                    close_ok = False
            except Exception as exc:
                close_details.append(str(exc))
                close_ok = False

        detail_str = "  |  ".join(close_details)
        if close_ok:
            scenario['status'] = 'pass'
            scenario['detail'] = detail_str
            logger.info("[SINGLE] %s  PASS  %s", scenario['label'], detail_str)
        else:
            scenario['status'] = 'fail'
            scenario['detail'] = detail_str
            logger.warning("[SINGLE] %s  FAIL  %s", scenario['label'], detail_str)

        socketio.emit('test_suite_update', _test_suite_state)

    finally:
        _single_running = False
        _test_suite_state['single_running'] = False
        socketio.emit('test_suite_update', _test_suite_state)


@app.route('/api/test-suite/run-scenario', methods=['POST'])
def api_run_single_scenario():
    """Run a single test scenario by ID without starting the full suite."""
    global _single_running
    if _test_suite_running:
        return jsonify({'success': False, 'error': 'Full suite is running'}), 400
    if _single_running:
        return jsonify({'success': False, 'error': 'A scenario is already running'}), 400
    if not engine.spot_adapter:
        return jsonify({'success': False, 'error': 'No exchange connected'}), 400
    if not engine.spot_tick or not engine.futures_tick:
        return jsonify({'success': False, 'error': 'No price data – wait for connection'}), 400

    data        = request.json or {}
    scenario_id = data.get('scenario_id')
    if not scenario_id:
        return jsonify({'success': False, 'error': 'scenario_id required'}), 400
    if not any(s['id'] == scenario_id for s in _SUITE_SCENARIOS):
        return jsonify({'success': False, 'error': f'Unknown scenario: {scenario_id}'}), 400

    if loop:
        asyncio.run_coroutine_threadsafe(run_single_scenario_task(scenario_id), loop)
        return jsonify({'success': True, 'scenario_id': scenario_id})
    return jsonify({'success': False, 'error': 'Event loop not running'}), 500


@app.route('/api/test-suite/download-csv', methods=['GET'])
def download_test_suite_csv():
    """Download the last test suite results as a CSV file."""
    import csv, io
    scenarios = _test_suite_state.get('scenarios', [])
    if not scenarios:
        return jsonify({'error': 'No test results available yet'}), 404

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['#', 'Scenario', 'Mode', 'Type', 'Cancel Test', 'Status', 'Detail'])
    for i, s in enumerate(scenarios, 1):
        writer.writerow([
            i,
            s.get('label', ''),
            s.get('mode', ''),
            s.get('order_type', ''),
            'yes' if s.get('cancel_test') else 'no',
            s.get('status', ''),
            s.get('detail', ''),
        ])

    from flask import Response
    ts = _test_suite_state.get('start_time', 'unknown')
    filename = f"test_suite_{ts[:10] if ts else 'results'}.csv"
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.route('/api/reset-trades', methods=['POST'])
def reset_trades_only():
    """Reset only trades and SD analysis - preserves spread data collection."""
    data = request.json or {}
    asset = data.get('asset')

    # Clear only trades and SD touches, keep spread history
    trades_deleted = db.clear_trades(asset=asset)
    sd_deleted = db.clear_sd_touches(asset=asset)
    signals_deleted = db.clear_signal_log(asset=asset)

    # Clear SD touch events from signal generator memory but keep spread data
    engine.signal_generator.sd_touch_events.clear()
    engine.signal_generator.last_sd_level = 0.0

    # Reset position state but keep spread history
    engine.state.current_position = "NONE"
    engine.signal_generator.set_position("NONE")
    engine.open_trade = None

    logger.info("Trades/SD reset: trades=%d, sd_touches=%d, signals=%d (spread preserved)",
               trades_deleted, sd_deleted, signals_deleted)

    return jsonify({
        'success': True,
        'deleted': {
            'trades': trades_deleted,
            'sd_touches': sd_deleted,
            'signals': signals_deleted,
        },
        'spread_preserved': True,
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
    logger.debug("Client connected")
    emit('status', engine.get_status())


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection."""
    logger.debug("Client disconnected")


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
    # ALWAYS start engine before Flask starts serving requests
    # This ensures spread history is loaded and engine is ready
    start_engine_loop()

    # Wait for engine to fully initialize (including API calls)
    logger.info("Waiting for engine initialization to complete...")
    time.sleep(1.0)

    # Log the server address
    port = 5000
    logger.info("=" * 50)
    logger.info("Dashboard available at: http://localhost:%d", port)
    logger.info("=" * 50)

    # Suppress HTTP request logs
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    logging.getLogger('engineio').setLevel(logging.ERROR)
    logging.getLogger('socketio').setLevel(logging.ERROR)
    app.logger.setLevel(logging.WARNING)

    # Run Flask app with SocketIO (threading mode)
    # use_reloader=False and debug=False for stable single-process operation
    socketio.run(
        app,
        host='0.0.0.0',
        port=port,
        debug=False,  # Disable debug mode for production stability
        use_reloader=False,
        allow_unsafe_werkzeug=True
    )
