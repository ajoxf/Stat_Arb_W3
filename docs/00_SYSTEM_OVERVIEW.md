# System Overview - Crypto Statistical Arbitrage Engine

## Purpose

This system implements a **statistical arbitrage strategy** between cryptocurrency spot and perpetual futures markets. It exploits temporary mispricings in the spread between these two markets, entering positions when the spread deviates significantly from its historical mean and exiting when it reverts.

## Core Strategy

```
Spread = Futures_Price - Spot_Price
Z-Score = (Spread - Rolling_Mean) / Rolling_Std

Entry Conditions:
  - Z-Score >= +2.0 → LONG (expect spread to decrease)
  - Z-Score <= -2.0 → SHORT (expect spread to increase)

Exit Conditions:
  - LONG position: Z-Score <= +0.5
  - SHORT position: Z-Score >= -0.5
  - Stop-Loss: |Z-Score| >= 4.0
```

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           FLASK WEB APPLICATION                             │
│                              (app.py - 1,900 lines)                         │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Web Routes: Dashboard, Settings, Setup, Analysis                   │   │
│  │  REST API: /api/config, /api/engine, /api/trades, /api/exchanges    │   │
│  │  WebSocket: Real-time tick, signal, trade, status events            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│   DATABASE       │       │  TRADING ENGINE  │       │   WEB UI         │
│   MANAGER        │       │                  │       │   TEMPLATES      │
│   (SQLite)       │       │  Orchestrator    │       │   + Static       │
└──────────────────┘       └──────────────────┘       └──────────────────┘
          │                           │
          │                           ├───────────────────┬───────────────────┐
          │                           ▼                   ▼                   ▼
          │               ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
          │               │ SIGNAL           │ │ ORDER            │ │ WEBSOCKET        │
          │               │ GENERATOR        │ │ EXECUTOR         │ │ MANAGER          │
          │               │                  │ │                  │ │                  │
          │               │ Z-Score, Hurst   │ │ Spread Orders    │ │ Real-time Ticks  │
          │               │ STD Filter       │ │ Leg Risk Mgmt    │ │ ~100ms Latency   │
          │               └──────────────────┘ └──────────────────┘ └──────────────────┘
          │                                             │                   │
          │                                             ▼                   ▼
          │                              ┌────────────────────────────────────────────┐
          │                              │         EXCHANGE ADAPTERS (REST)           │
          │                              │                                            │
          │                              │   ┌──────────┐ ┌──────────┐ ┌──────────┐  │
          │                              │   │   OKX    │ │ Binance  │ │  Bybit   │  │
          │                              │   │ (Primary)│ │          │ │          │  │
          │                              │   └──────────┘ └──────────┘ └──────────┘  │
          │                              └────────────────────────────────────────────┘
          │                                               │
          └───────────────────────────────────────────────┘
                            Data Persistence
```

## Component Summary

| Component | File(s) | Lines | Purpose |
|-----------|---------|-------|---------|
| **Web Application** | `app.py` | ~1,900 | Flask routes, API endpoints, WebSocket events |
| **Models** | `models.py` | ~544 | Data structures, enums, configuration |
| **Trading Engine** | `core/trading_engine.py` | ~900 | Main orchestrator, tick processing, state management |
| **Signal Generator** | `core/signals.py` | ~524 | Z-score, Hurst exponent, STD filter |
| **Order Executor** | `core/order_executor.py` | ~700 | Spread order execution, leg risk management |
| **Trade Logger** | `core/trade_logger.py` | ~238 | CSV logging for trades and alerts |
| **OKX Adapter** | `adapters/okx_adapter.py` | ~400 | OKX REST API integration |
| **OKX WebSocket** | `adapters/okx_websocket.py` | ~300 | Real-time tick streaming |
| **Binance Adapter** | `adapters/binance_adapter.py` | ~300 | Binance REST API integration |
| **Bybit Adapter** | `adapters/bybit_adapter.py` | ~300 | Bybit REST API integration |
| **Database Manager** | `database/manager.py` | ~600 | SQLite persistence |
| **Dashboard** | `templates/dashboard.html` | ~1,900 | Main trading interface |
| **Settings** | `templates/settings.html` | ~836 | Configuration editor |
| **Tests** | `tests/test_order_executor.py` | ~398 | Unit tests |

**Total: ~18,000+ lines of code**

## Data Flow

### 1. Price Acquisition
```
Exchange WebSocket (or REST Polling)
         │
         ▼
   MarketTick Objects
   (bid, ask, last, volume)
         │
         ▼
   TradingEngine._on_websocket_tick()
         │
         ▼
   Store spot_tick + futures_tick
```

### 2. Signal Generation
```
spot_tick + futures_tick
         │
         ▼
   SignalGenerator.add_tick()
         │
         ▼
   Calculate spread = futures - spot
         │
         ▼
   Update rolling statistics (every 300s)
         │
         ▼
   Calculate Z-score = (spread - mean) / std
         │
         ▼
   Apply filters (Hurst, STD)
         │
         ▼
   Generate Signal (NONE, LONG, SHORT, EXIT, STOP_LOSS)
```

### 3. Order Execution
```
Signal received
         │
         ▼
   TradingEngine._process_signal()
         │
         ├── LONG/SHORT → execute_entry()
         │                      │
         │                      ▼
         │              Calculate leg orders:
         │              - Spot: BUY/SELL
         │              - Futures: SELL/BUY (opposite)
         │                      │
         │                      ▼
         │              Execute simultaneously
         │              (asyncio.gather)
         │
         └── EXIT/STOP_LOSS → execute_exit()
                                 │
                                 ▼
                         Reverse entry orders
                         Calculate P&L
```

### 4. State Persistence
```
Trade executed
         │
         ├── Database: trades table
         ├── Database: spread_history table
         ├── CSV: trades_YYYYMMDD.csv
         └── WebSocket: emit('trade', ...)
```

## Startup Sequence

```python
# 1. Initialize Flask application
app = Flask(__name__)
socketio = SocketIO(app, async_mode='threading')

# 2. Load configuration
db = DatabaseManager(DATABASE_PATH)
config = db.get_config()

# 3. Create trading engine
engine = TradingEngine(config)

# 4. Start async event loop in background thread
loop = asyncio.new_event_loop()
threading.Thread(target=_run_async_loop, daemon=True).start()

# 5. Configure exchange adapters
spot_adapter = OKXAdapter(exchange, is_futures=False)
futures_adapter = OKXAdapter(exchange, is_futures=True)
engine.set_adapters(spot_adapter, futures_adapter)

# 6. Setup WebSocket manager (optional, for ~100ms latency)
ws_manager = OKXWebSocketManager(...)
engine.set_websocket_manager(ws_manager)

# 7. Load spread history for signal generator recovery
spreads = db.get_spread_history(asset, lookback * 2)
for spread in reversed(spreads):
    signal_generator.add_tick_from_spread(spread)

# 8. Recover any open position from database
open_trade = db.get_open_trade(asset)
if open_trade:
    engine.open_trade = open_trade
    engine.state.current_position = open_trade.position_type

# 9. Schedule engine start
asyncio.run_coroutine_threadsafe(engine.start(), loop)

# 10. Start Flask server
socketio.run(app, host='0.0.0.0', port=5000)
```

## Position Types Explained

### LONG Position
**Market View**: Spread is too high, expect it to decrease (futures overpriced vs spot)

```
Entry:
  - BUY spot (own the asset)
  - SELL futures (short the contract, pos_side="short")

Exit:
  - SELL spot
  - BUY futures (close short position, pos_side="short")

Profit when: Spread decreases (Z-score moves toward 0)
```

### SHORT Position
**Market View**: Spread is too low, expect it to increase (spot overpriced vs futures)

```
Entry:
  - SELL spot (short the asset, requires margin)
  - BUY futures (long the contract, pos_side="long")

Exit:
  - BUY spot (close short)
  - SELL futures (close long, pos_side="long")

Profit when: Spread increases (Z-score moves toward 0)
```

## Key Design Decisions

### 1. Dual-Leg Execution
Both spot and futures orders are executed simultaneously using `asyncio.gather()`. This minimizes the time gap between legs and reduces "leg risk" (one leg filled, other not).

### 2. WebSocket vs REST Polling
- **WebSocket**: ~100ms latency, preferred for live trading
- **REST Polling**: ~500ms interval, fallback when WebSocket unavailable

### 3. Statistics Update Interval
Mean and standard deviation are recalculated every 300 seconds (configurable). Z-score is calculated on every tick using the current spread and cached statistics. This provides stable bands while remaining responsive to price changes.

### 4. Paper Trading Mode
When enabled, the engine simulates trades without sending real orders. Useful for testing strategies without financial risk.

### 5. Position Verification
Every 60 seconds, the engine queries the exchange for actual positions and compares with internal state. Mismatches trigger alerts in the UI.

## File Structure

```
Stat_Arb_W3/
├── app.py                      # Flask application entry point
├── models.py                   # Data structures and configuration
├── requirements.txt            # Python dependencies
├── .env                        # Environment variables (secrets)
├── trading.db                  # SQLite database
│
├── adapters/                   # Exchange integrations
│   ├── __init__.py
│   ├── base.py                 # Abstract adapter interface
│   ├── okx_adapter.py          # OKX REST API
│   ├── okx_websocket.py        # OKX WebSocket streaming
│   ├── binance_adapter.py      # Binance REST API
│   └── bybit_adapter.py        # Bybit REST API
│
├── core/                       # Trading logic
│   ├── __init__.py
│   ├── signals.py              # Signal generation algorithms
│   ├── trading_engine.py       # Main orchestrator
│   ├── order_executor.py       # Order execution & leg risk
│   └── trade_logger.py         # CSV event logging
│
├── database/                   # Persistence layer
│   ├── __init__.py
│   └── manager.py              # SQLite operations
│
├── templates/                  # Jinja2 HTML templates
│   ├── base.html               # Base layout
│   ├── dashboard.html          # Main trading interface
│   ├── settings.html           # Configuration page
│   ├── setup.html              # Exchange setup page
│   └── analysis.html           # Analytics page
│
├── static/                     # Static assets
│   └── favicon.svg
│
├── logs/                       # CSV log files (auto-created)
│   ├── trades_YYYYMMDD.csv
│   └── alerts_YYYYMMDD.csv
│
├── tests/                      # Unit tests
│   ├── __init__.py
│   └── test_order_executor.py
│
└── docs/                       # Documentation
    ├── 00_SYSTEM_OVERVIEW.md   # This file
    └── ...
```

## Dependencies

### Core Framework
- Flask 2.3+ - Web application framework
- Flask-SocketIO 5.3+ - WebSocket support
- python-socketio 5.8+ - SocketIO protocol

### Async/Networking
- aiohttp 3.8+ - Async HTTP client for exchange APIs
- requests 2.31+ - Sync HTTP (fallback)

### Data Processing
- numpy 1.24+ - Numerical computations (Hurst calculation)

### Configuration
- python-dotenv 1.0+ - Environment variable loading

### Standard Library (No Install Required)
- asyncio - Async event loop
- sqlite3 - Database
- threading - Background tasks
- hmac, hashlib, base64 - API signing
- csv, json, logging - Utilities

## Environment Variables

```bash
# Flask
FLASK_SECRET_KEY=your-secret-key-here
FLASK_DEBUG=false

# Trading
USE_WEBSOCKET=true

# Database
DATABASE_PATH=trading.db

# Exchange credentials loaded from database (Setup page)
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy environment template
cp .env.example .env

# 3. Configure secret key in .env
FLASK_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")

# 4. Run the application
python app.py

# 5. Open browser to http://localhost:5000

# 6. Configure exchange credentials in Setup page

# 7. Adjust trading parameters in Settings page

# 8. Enable trading from Dashboard
```

## Recreating the System

To recreate this system from documentation:

1. **Read this document** for overall architecture understanding
2. **Read `01_MODELS.md`** to understand data structures
3. **Read `02_CONFIGURATION.md`** to understand parameters
4. **Read `03_CORE_ENGINE.md`** to implement the main orchestrator
5. **Read `04_SIGNAL_GENERATION.md`** to implement trading signals
6. **Read `05_ORDER_MANAGEMENT.md`** to implement order execution
7. **Read `06_ORDER_ROUTING.md`** to implement exchange adapters
8. **Read `07_WEBSOCKET.md`** to implement real-time data
9. **Read `08_RISK_MANAGEMENT.md`** to implement safety mechanisms
10. **Read `09_UI_DASHBOARD.md`** to implement the web interface
11. **Read `10_ERROR_LOGGING.md`** to implement logging
12. **Read `11_DATA_STORAGE.md`** to implement persistence
13. **Read `12_TESTING.md`** to implement tests

Each document contains complete code examples and implementation details sufficient to recreate the component from scratch.
