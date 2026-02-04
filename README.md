# Crypto Statistical Arbitrage Trading System

A complete crypto basis trading system implementing statistical arbitrage between spot and perpetual futures markets. The system trades crypto (BTC, ETH, SOL, etc.) using exchange APIs (OKX, Binance, Bybit).

## Overview

The system implements statistical arbitrage based on the spread between spot and perpetual futures prices:

- **Spread Calculation**: `Spread = Spot Price - Futures Price`
- **Z-Score**: `Z = (spread - rolling_mean) / rolling_std`
- **Entry Signals**:
  - Long Spread: `Z <= -2.0` (spread below mean, expect reversion up)
  - Short Spread: `Z >= +2.0` (spread above mean, expect reversion down)
- **Exit Signals**:
  - Long Position: Exit when `Z >= -0.5` (spread reverted toward mean)
  - Short Position: Exit when `Z <= +0.5` (spread reverted toward mean)

### Filters

- **Hurst Exponent Filter**: Blocks entries when `H >= 0.5` (trending market). Only mean-reverting regimes (`H < 0.5`) are tradeable.
- **STD Profitability Filter**: Ensures spread volatility is sufficient to cover trading costs.

## Features

- Real-time price feeds from OKX, Binance, and Bybit
- Z-score calculation with rolling mean/std (never locked at entry)
- Direction-aware exit signals
- Hurst exponent calculation for regime detection
- STD filter for profitability assessment
- Paper trading mode for testing
- Web-based dashboard with live charts
- Trade journal with P&L tracking
- SD touch event analysis

## Project Structure

```
Stat_Arb_W3/
├── app.py                  # Flask web application
├── models.py               # Data models and configurations
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variables template
├── adapters/
│   ├── __init__.py
│   ├── base.py             # Abstract exchange interface
│   ├── okx_adapter.py      # OKX REST implementation
│   ├── okx_websocket.py    # OKX WebSocket streaming
│   ├── binance_adapter.py  # Binance implementation
│   └── bybit_adapter.py    # Bybit implementation
├── core/
│   ├── __init__.py
│   ├── signals.py          # Z-score & signal generation
│   └── trading_engine.py   # Main trading loop
├── database/
│   ├── __init__.py
│   └── manager.py          # SQLite operations
└── templates/
    ├── base.html           # Base layout (dark theme)
    ├── dashboard.html      # Main trading dashboard
    ├── settings.html       # Configuration page
    ├── setup.html          # Exchange management
    └── analysis.html       # SD touch analysis
```

## Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd Stat_Arb_W3
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate  # Windows
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Create environment file:
```bash
cp .env.example .env
```

5. Edit `.env` with your API keys and configuration.

## Configuration

### Environment Variables

```
FLASK_SECRET_KEY=your-secret-key
FLASK_DEBUG=true

# WebSocket Streaming (real-time ~100ms updates)
USE_WEBSOCKET=true

# OKX
OKX_API_KEY=your-api-key
OKX_SECRET_KEY=your-secret-key
OKX_PASSPHRASE=your-passphrase
OKX_DEMO_MODE=true

# Binance
BINANCE_API_KEY=your-api-key
BINANCE_SECRET_KEY=your-secret-key
BINANCE_TESTNET=true

# Bybit
BYBIT_API_KEY=your-api-key
BYBIT_SECRET_KEY=your-secret-key
BYBIT_TESTNET=true
```

### Data Feed Modes

The system supports two modes for receiving price data:

| Mode | Latency | Setting |
|------|---------|---------|
| **WebSocket Streaming** | ~100ms | `USE_WEBSOCKET=true` (default) |
| **REST Polling** | 500ms | `USE_WEBSOCKET=false` |

**WebSocket Streaming** (recommended):
- Connects to OKX public WebSocket: `wss://ws.okx.com:8443/ws/v5/public`
- Receives ticker updates every 100ms on price change
- Automatic reconnection with exponential backoff
- Heartbeat ping/pong to maintain connection

### Trading Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| Entry Threshold | 2.0 | Z-score level for entry signals |
| Exit Threshold | 0.5 | Z-score level for exit signals |
| Stop Loss Threshold | 4.0 | Z-score level for stop loss |
| Lookback Period | 100 | Rolling window size for mean/std |
| Stats Update Interval | 300s | How often to recalculate mean/std (0 = every tick) |
| Hurst Threshold | 0.5 | Max Hurst exponent for entries |
| Min STD Multiple | 1.5 | Minimum profitability ratio |

### Stats Update Interval

The **Stats Update Interval** controls how often the rolling mean and standard deviation are recalculated:

- **0 (Every tick)**: Mean/std recalculated on every price update (~100ms). Bands shift constantly.
- **300 (5 minutes)**: Mean/std recalculated every 5 minutes. Bands stay stable, making entries/exits easier to track.
- **Higher values**: More stable bands, but slower adaptation to market changes.

The Z-score is always calculated in real-time using the current spread against the (potentially older) mean/std.

## Usage

1. Start the application:
```bash
python app.py
```

2. Open your browser and navigate to `http://localhost:5000`

3. Configure exchanges in the **Exchanges** tab

4. Adjust trading parameters in the **Settings** tab

5. Enable algorithmic trading using the toggle in the navigation bar

## Supported Assets

| Asset | Spot Symbol (OKX) | Futures Symbol (OKX) |
|-------|-------------------|----------------------|
| BTC | BTC-USDT | BTC-USDT-SWAP |
| ETH | ETH-USDT | ETH-USDT-SWAP |
| SOL | SOL-USDT | SOL-USDT-SWAP |
| XRP | XRP-USDT | XRP-USDT-SWAP |
| DOGE | DOGE-USDT | DOGE-USDT-SWAP |
| AVAX | AVAX-USDT | AVAX-USDT-SWAP |
| LINK | LINK-USDT | LINK-USDT-SWAP |

## Trading Logic

### Entry Signals
```python
if position == "NONE" and filters_pass:
    if zscore <= -entry_threshold:
        signal = "LONG"   # Buy spot, sell futures
    elif zscore >= entry_threshold:
        signal = "SHORT"  # Sell spot, buy futures
```

### Exit Signals (Direction-Aware)
```python
if position == "LONG":
    # Entered when z <= -entry, exit when z >= -exit
    if zscore >= -exit_threshold:
        signal = "EXIT"
    elif zscore <= -stop_loss_threshold:
        signal = "STOP_LOSS"

elif position == "SHORT":
    # Entered when z >= +entry, exit when z <= +exit
    if zscore <= exit_threshold:
        signal = "EXIT"
    elif zscore >= stop_loss_threshold:
        signal = "STOP_LOSS"
```

### Filters
- **Hurst Filter**: Applied to entries only, not exits
- **STD Filter**: Applied to entries only, not exits

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/config` | Get current configuration |
| POST | `/api/config` | Save configuration |
| POST | `/api/engine/toggle-algo` | Toggle algorithmic trading |
| GET | `/api/engine/status` | Get engine status |
| GET/POST | `/api/exchanges` | Get/Add exchanges |
| DELETE | `/api/exchanges/<id>` | Delete exchange |
| POST | `/api/exchanges/<id>/test` | Test exchange connection |
| POST | `/api/set-active-exchanges` | Set active trading exchanges |
| GET | `/api/trades` | Get recent trades |
| GET | `/api/trade-journal` | Get trade journal with stats |
| GET | `/api/spread-history` | Get spread/zscore history |
| GET | `/api/sd-touches` | Get SD touch events |

## WebSocket Events

| Event | Direction | Description |
|-------|-----------|-------------|
| `tick` | Server → Client | Price updates |
| `signal` | Server → Client | Signal updates |
| `trade` | Server → Client | Trade notifications |
| `status` | Server → Client | Engine status |
| `error` | Server → Client | Error messages |

## License

MIT License
