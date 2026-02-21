# WebSocket Management

## Overview

The WebSocket Manager (`adapters/okx_websocket.py`, ~300 lines) provides real-time price streaming with ~100ms latency, significantly faster than REST polling (~500ms).

## File Location
```
adapters/okx_websocket.py
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    OKX WEBSOCKET SERVER                     │
│                                                             │
│   Production: wss://ws.okx.com:8443/ws/v5/public           │
│   Demo: wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999│
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                  OKXWebSocketManager                        │
│                                                             │
│   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐     │
│   │ Connection  │   │ Heartbeat   │   │ Message     │     │
│   │ Management  │   │ (25s ping)  │   │ Processing  │     │
│   └─────────────┘   └─────────────┘   └─────────────┘     │
│                              │                             │
│   ┌──────────────────────────┼──────────────────────────┐ │
│   │              Tick Callbacks                          │ │
│   │                                                      │ │
│   │   on_tick(symbol, MarketTick) ──► TradingEngine     │ │
│   └──────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## OKX WebSocket Protocol

### Endpoints

| Mode | URL |
|------|-----|
| Production | `wss://ws.okx.com:8443/ws/v5/public` |
| Demo | `wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999` |

### Update Frequency

- **On price change**: Every ~100ms
- **No price change**: Every 1 second
- **Heartbeat**: Ping required every <30 seconds

### Message Format

**Subscribe Request:**
```json
{
    "op": "subscribe",
    "args": [
        {"channel": "tickers", "instId": "BTC-USDT"},
        {"channel": "tickers", "instId": "BTC-USDT-SWAP"}
    ]
}
```

**Ticker Update:**
```json
{
    "arg": {"channel": "tickers", "instId": "BTC-USDT"},
    "data": [{
        "instId": "BTC-USDT",
        "last": "65432.10",
        "bidPx": "65431.00",
        "askPx": "65433.00",
        "vol24h": "1234567.89",
        "ts": "1640000000000"
    }]
}
```

---

## OKXWebSocketManager Class

### Constructor

```python
class OKXWebSocketManager:
    def __init__(self, exchange: Exchange):
        self.exchange = exchange
        self.is_demo = exchange.is_testnet

        # WebSocket connection
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False

        # Subscribed instruments
        self._subscribed_symbols: Set[str] = set()

        # Tick callbacks
        self._tick_callbacks: List[Callable[[str, MarketTick], None]] = []

        # Latest ticks
        self._latest_ticks: Dict[str, MarketTick] = {}

        # Tasks
        self._receive_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Reconnection settings
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._reconnect_delay = 1.0
```

### Start

```python
async def start(self, spot_symbol: str, futures_symbol: str) -> bool:
    """Start WebSocket connection and subscribe to symbols."""
    if self._running:
        return True

    try:
        # Build WebSocket URL
        if self.is_demo:
            url = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
        else:
            url = "wss://ws.okx.com:8443/ws/v5/public"

        # Connect
        self._ws = await websockets.connect(
            url,
            ping_interval=None,  # We handle heartbeat manually
            ping_timeout=None,
        )

        self._running = True

        # Start background tasks
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Subscribe to tickers
        await self._subscribe([spot_symbol, futures_symbol])

        logger.info("WebSocket started: %s, %s", spot_symbol, futures_symbol)
        return True

    except Exception as e:
        logger.error("WebSocket start failed: %s", e)
        return False
```

### Stop

```python
async def stop(self) -> None:
    """Stop WebSocket connection."""
    self._running = False

    # Cancel tasks
    if self._receive_task:
        self._receive_task.cancel()
        try:
            await self._receive_task
        except asyncio.CancelledError:
            pass

    if self._heartbeat_task:
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass

    # Close connection
    if self._ws:
        await self._ws.close()
        self._ws = None

    logger.info("WebSocket stopped")
```

### Subscribe

```python
async def _subscribe(self, symbols: List[str]) -> None:
    """Subscribe to ticker channels."""
    if not self._ws:
        return

    args = [{"channel": "tickers", "instId": symbol} for symbol in symbols]

    subscribe_msg = {
        "op": "subscribe",
        "args": args,
    }

    await self._ws.send(json.dumps(subscribe_msg))
    self._subscribed_symbols.update(symbols)

    logger.debug("Subscribed to: %s", symbols)
```

### Receive Loop

```python
async def _receive_loop(self) -> None:
    """Main loop to receive and process messages."""
    while self._running:
        try:
            if not self._ws:
                await asyncio.sleep(1)
                continue

            message = await asyncio.wait_for(
                self._ws.recv(),
                timeout=35  # Longer than heartbeat interval
            )

            await self._handle_message(message)

        except asyncio.TimeoutError:
            logger.warning("WebSocket receive timeout")
            await self._reconnect()

        except websockets.ConnectionClosed:
            logger.warning("WebSocket connection closed")
            if self._running:
                await self._reconnect()

        except asyncio.CancelledError:
            break

        except Exception as e:
            logger.error("WebSocket error: %s", e)
            await asyncio.sleep(1)
```

### Message Handling

```python
async def _handle_message(self, raw_message: str) -> None:
    """Parse and process incoming message."""
    try:
        message = json.loads(raw_message)

        # Ping/pong response
        if message == "pong":
            return

        # Subscription confirmation
        if message.get("event") == "subscribe":
            logger.debug("Subscription confirmed: %s", message.get("arg"))
            return

        # Ticker update
        if "data" in message and message.get("arg", {}).get("channel") == "tickers":
            await self._process_ticker(message)

    except json.JSONDecodeError:
        logger.warning("Invalid JSON: %s", raw_message[:100])

async def _process_ticker(self, message: Dict) -> None:
    """Process ticker message and invoke callbacks."""
    try:
        data = message["data"][0]
        symbol = data["instId"]

        tick = MarketTick(
            symbol=symbol,
            bid=float(data["bidPx"]),
            ask=float(data["askPx"]),
            last=float(data["last"]),
            volume_24h=float(data.get("vol24h", 0)),
            timestamp=datetime.utcnow(),
        )

        # Store latest tick
        self._latest_ticks[symbol] = tick

        # Invoke callbacks
        for callback in self._tick_callbacks:
            try:
                callback(symbol, tick)
            except Exception as e:
                logger.error("Tick callback error: %s", e)

    except Exception as e:
        logger.error("Error processing ticker: %s", e)
```

### Heartbeat

```python
async def _heartbeat_loop(self) -> None:
    """Send periodic ping to keep connection alive."""
    while self._running:
        try:
            await asyncio.sleep(25)  # OKX requires ping < 30s

            if self._ws and self._ws.open:
                await self._ws.send("ping")
                logger.debug("Sent heartbeat ping")

        except asyncio.CancelledError:
            break

        except Exception as e:
            logger.error("Heartbeat error: %s", e)
```

### Reconnection

```python
async def _reconnect(self) -> None:
    """Attempt to reconnect with exponential backoff."""
    if not self._running:
        return

    self._reconnect_attempts += 1

    if self._reconnect_attempts > self._max_reconnect_attempts:
        logger.error("Max reconnection attempts reached")
        self._running = False
        return

    # Exponential backoff
    delay = self._reconnect_delay * (2 ** (self._reconnect_attempts - 1))
    delay = min(delay, 60)  # Cap at 60 seconds

    logger.info("Reconnecting in %.1fs (attempt %d/%d)",
                delay, self._reconnect_attempts, self._max_reconnect_attempts)

    await asyncio.sleep(delay)

    try:
        # Close existing connection
        if self._ws:
            await self._ws.close()

        # Reconnect
        if self.is_demo:
            url = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
        else:
            url = "wss://ws.okx.com:8443/ws/v5/public"

        self._ws = await websockets.connect(
            url,
            ping_interval=None,
            ping_timeout=None,
        )

        # Resubscribe
        if self._subscribed_symbols:
            await self._subscribe(list(self._subscribed_symbols))

        self._reconnect_attempts = 0
        logger.info("Reconnected successfully")

    except Exception as e:
        logger.error("Reconnection failed: %s", e)
```

### Callbacks

```python
def add_tick_callback(self, callback: Callable[[str, MarketTick], None]) -> None:
    """Register a callback for tick updates."""
    self._tick_callbacks.append(callback)

def remove_tick_callback(self, callback: Callable) -> None:
    """Remove a tick callback."""
    if callback in self._tick_callbacks:
        self._tick_callbacks.remove(callback)

def get_latest_tick(self, symbol: str) -> Optional[MarketTick]:
    """Get the most recent tick for a symbol."""
    return self._latest_ticks.get(symbol)
```

---

## Integration with Trading Engine

```python
# In TradingEngine

def set_websocket_manager(self, ws_manager: OKXWebSocketManager) -> None:
    """Configure WebSocket for real-time streaming."""
    self.ws_manager = ws_manager
    self._use_websocket = True

    # Register tick callback
    ws_manager.add_tick_callback(self._on_websocket_tick)

def _on_websocket_tick(self, symbol: str, tick: MarketTick) -> None:
    """Handle incoming WebSocket tick."""
    # Update appropriate tick
    if symbol == self.config.spot_symbol:
        self.spot_tick = tick
    elif symbol == self.config.futures_symbol:
        self.futures_tick = tick

    # Process if we have both AND not already processing
    if self.spot_tick and self.futures_tick and not self._processing_tick:
        asyncio.create_task(self._run_tick_guarded())
```

---

## WebSocket vs REST Comparison

| Aspect | WebSocket | REST Polling |
|--------|-----------|--------------|
| Latency | ~100ms | ~500ms |
| Connection | Persistent | Per-request |
| Data freshness | Real-time | Poll interval |
| Server load | Lower | Higher |
| Reconnection | Required | N/A |
| Implementation | Complex | Simple |

---

## Error Handling

### Connection Loss

```python
# Automatic reconnection with exponential backoff
attempt 1: wait 1s
attempt 2: wait 2s
attempt 3: wait 4s
attempt 4: wait 8s
...
attempt N: wait min(2^(N-1), 60)s
```

### Message Parsing Errors

```python
try:
    message = json.loads(raw_message)
    # Process message
except json.JSONDecodeError:
    logger.warning("Invalid JSON received")
    # Continue - don't disconnect
```

### Callback Errors

```python
for callback in self._tick_callbacks:
    try:
        callback(symbol, tick)
    except Exception as e:
        logger.error("Callback error: %s", e)
        # Continue to next callback - don't stop processing
```

---

## Startup Flow

```python
# app.py

# 1. Check if WebSocket enabled
USE_WEBSOCKET = os.getenv('USE_WEBSOCKET', 'true').lower() == 'true'

if USE_WEBSOCKET:
    # 2. Create manager
    ws_manager = OKXWebSocketManager(active_exchange)

    # 3. Attach to engine
    engine.set_websocket_manager(ws_manager)

# 4. Engine starts WebSocket in start()
await engine.start()
# Inside start():
#   - await ws_manager.start(spot_symbol, futures_symbol)
#   - WebSocket connects and subscribes
#   - Ticks start flowing to _on_websocket_tick()
```

---

## Monitoring

### Check Connection Status

```python
def is_connected(self) -> bool:
    """Check if WebSocket is connected."""
    return self._ws is not None and self._ws.open

def get_status(self) -> Dict[str, Any]:
    """Get WebSocket status."""
    return {
        'connected': self.is_connected(),
        'running': self._running,
        'subscribed_symbols': list(self._subscribed_symbols),
        'reconnect_attempts': self._reconnect_attempts,
        'latest_ticks': {
            symbol: tick.timestamp.isoformat()
            for symbol, tick in self._latest_ticks.items()
        },
    }
```

### Latency Monitoring

```python
# Track time between ticks
last_tick_time = {}

def _process_ticker(self, message):
    symbol = message["data"][0]["instId"]
    now = datetime.utcnow()

    if symbol in last_tick_time:
        latency_ms = (now - last_tick_time[symbol]).total_seconds() * 1000
        if latency_ms > 200:
            logger.warning("High tick latency: %s = %.0fms", symbol, latency_ms)

    last_tick_time[symbol] = now
```
