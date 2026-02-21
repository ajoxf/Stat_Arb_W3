# Order Routing - Exchange Adapters

## Overview

The Exchange Adapters (`adapters/`) provide a unified interface to multiple cryptocurrency exchanges. They handle REST API communication, authentication, and order management.

## File Locations
```
adapters/
├── __init__.py
├── base.py              # Abstract base class
├── okx_adapter.py       # OKX implementation (~400 lines)
├── okx_websocket.py     # OKX WebSocket (~300 lines)
├── binance_adapter.py   # Binance implementation (~300 lines)
└── bybit_adapter.py     # Bybit implementation (~300 lines)
```

---

## Abstract Base Class

### ExchangeAdapter Interface

```python
# adapters/base.py

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from models import MarketTick, OrderResult, Position, AccountInfo

class ExchangeAdapter(ABC):
    """Abstract base class for exchange adapters."""

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to exchange."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from exchange."""
        pass

    @abstractmethod
    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        """Get current market tick (bid, ask, last, volume)."""
        pass

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 5) -> Optional[Dict]:
        """Get order book with specified depth."""
        pass

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,           # "BUY" or "SELL"
        order_type: str,     # "MARKET" or "LIMIT"
        quantity: float,
        price: float = None, # Required for LIMIT
        pos_side: str = None, # OKX long_short_mode
        reduce_only: bool = False,
    ) -> OrderResult:
        """Place an order."""
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order."""
        pass

    @abstractmethod
    async def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict]:
        """Get order status."""
        pass

    @abstractmethod
    async def get_positions(self, symbol: str = None) -> List[Position]:
        """Get open positions."""
        pass

    @abstractmethod
    async def close_position(self, symbol: str) -> OrderResult:
        """Close a position."""
        pass

    @abstractmethod
    async def get_account_info(self) -> Optional[AccountInfo]:
        """Get account balance and margin info."""
        pass

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> Optional[Dict]:
        """Get funding rate for perpetual."""
        pass

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set position leverage."""
        pass
```

---

## OKX Adapter

### Configuration

```python
# adapters/okx_adapter.py

class OKXAdapter(ExchangeAdapter):
    # API Endpoints
    BASE_URL = "https://www.okx.com"
    DEMO_BASE_URL = "https://www.okx.com"  # Same URL, different header

    def __init__(self, exchange: Exchange, is_futures: bool = False):
        self.exchange = exchange
        self.is_futures = is_futures
        self.api_key = exchange.api_key
        self.secret_key = exchange.secret_key
        self.passphrase = exchange.passphrase
        self.is_demo = exchange.is_testnet

        # Session for connection pooling
        self.session: Optional[aiohttp.ClientSession] = None
```

### Authentication

OKX uses HMAC-SHA256 signing:

```python
def _get_timestamp(self) -> str:
    """ISO timestamp with milliseconds."""
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.') + \
           f"{datetime.utcnow().microsecond // 1000:03d}Z"

def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
    """Generate HMAC-SHA256 signature."""
    message = timestamp + method.upper() + path + body
    signature = hmac.new(
        self.secret_key.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()
    return base64.b64encode(signature).decode()

def _get_headers(self, method: str, path: str, body: str = "") -> Dict:
    """Build authenticated headers."""
    timestamp = self._get_timestamp()
    sign = self._sign(timestamp, method, path, body)

    headers = {
        "OK-ACCESS-KEY": self.api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": self.passphrase,
        "Content-Type": "application/json",
    }

    # Demo mode header
    if self.is_demo:
        headers["x-simulated-trading"] = "1"

    return headers
```

### Get Market Tick

```python
async def get_tick(self, symbol: str) -> Optional[MarketTick]:
    """Fetch current ticker."""
    inst_type = "SWAP" if self.is_futures else "SPOT"
    path = f"/api/v5/market/ticker?instId={symbol}"

    try:
        response = await self._request("GET", path)
        if response and response.get("code") == "0":
            data = response["data"][0]
            return MarketTick(
                symbol=symbol,
                bid=float(data["bidPx"]),
                ask=float(data["askPx"]),
                last=float(data["last"]),
                volume_24h=float(data["vol24h"]),
                timestamp=datetime.utcnow(),
            )
    except Exception as e:
        logger.error("Error fetching tick: %s", e)
    return None
```

### Place Order

```python
async def place_order(
    self,
    symbol: str,
    side: str,
    order_type: str,
    quantity: float,
    price: float = None,
    pos_side: str = None,
    reduce_only: bool = False,
) -> OrderResult:
    """Place an order on OKX."""
    path = "/api/v5/trade/order"

    # Determine trade mode and side format
    td_mode = "cross"  # Cross-margin
    okx_side = side.lower()  # "buy" or "sell"

    body = {
        "instId": symbol,
        "tdMode": td_mode,
        "side": okx_side,
        "ordType": order_type.lower(),  # "market" or "limit"
        "sz": str(quantity),
    }

    # Add price for limit orders
    if order_type.upper() == "LIMIT" and price:
        body["px"] = str(price)

    # Add position side for futures
    if pos_side:
        body["posSide"] = pos_side  # "long" or "short"

    # Reduce only flag
    if reduce_only:
        body["reduceOnly"] = True

    try:
        response = await self._request("POST", path, body)
        if response and response.get("code") == "0":
            order_data = response["data"][0]
            return OrderResult(
                success=True,
                order_id=order_data["ordId"],
                filled_qty=float(order_data.get("fillSz", 0) or 0),
                filled_price=float(order_data.get("fillPx", 0) or 0),
            )
        else:
            error = response.get("msg", "Unknown error") if response else "No response"
            return OrderResult(success=False, error=error)

    except Exception as e:
        return OrderResult(success=False, error=str(e))
```

### Cancel Order

```python
async def cancel_order(self, symbol: str, order_id: str) -> bool:
    """Cancel an order."""
    path = "/api/v5/trade/cancel-order"
    body = {
        "instId": symbol,
        "ordId": order_id,
    }

    try:
        response = await self._request("POST", path, body)
        return response and response.get("code") == "0"
    except Exception as e:
        logger.error("Error cancelling order: %s", e)
        return False
```

### Get Order Status

```python
async def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict]:
    """Get order status."""
    path = f"/api/v5/trade/order?instId={symbol}&ordId={order_id}"

    try:
        response = await self._request("GET", path)
        if response and response.get("code") == "0" and response.get("data"):
            data = response["data"][0]
            return {
                "order_id": data["ordId"],
                "state": data["state"],  # live, canceled, partially_filled, filled
                "filled_qty": float(data.get("fillSz", 0) or 0),
                "filled_price": float(data.get("avgPx", 0) or 0),
            }
    except Exception as e:
        logger.error("Error getting order status: %s", e)
    return None
```

### Get Positions

```python
async def get_positions(self, symbol: str = None) -> List[Position]:
    """Get open positions."""
    path = "/api/v5/account/positions"
    if symbol:
        path += f"?instId={symbol}"

    positions = []
    try:
        response = await self._request("GET", path)
        if response and response.get("code") == "0":
            for pos in response.get("data", []):
                qty = float(pos.get("pos", 0) or 0)
                if abs(qty) > 0:
                    positions.append(Position(
                        symbol=pos["instId"],
                        side="LONG" if qty > 0 else "SHORT",
                        quantity=abs(qty),
                        entry_price=float(pos.get("avgPx", 0) or 0),
                        unrealized_pnl=float(pos.get("upl", 0) or 0),
                        leverage=float(pos.get("lever", 1) or 1),
                    ))
    except Exception as e:
        logger.error("Error getting positions: %s", e)
    return positions
```

### Set Leverage

```python
async def set_leverage(self, symbol: str, leverage: int) -> bool:
    """Set leverage for a symbol."""
    path = "/api/v5/account/set-leverage"
    body = {
        "instId": symbol,
        "lever": str(leverage),
        "mgnMode": "cross",
    }

    try:
        response = await self._request("POST", path, body)
        return response and response.get("code") == "0"
    except Exception as e:
        logger.error("Error setting leverage: %s", e)
        return False
```

### Get Account Info

```python
async def get_account_info(self) -> Optional[AccountInfo]:
    """Get account balance and margin details."""
    path = "/api/v5/account/balance"

    try:
        response = await self._request("GET", path)
        if response and response.get("code") == "0":
            data = response["data"][0]
            return AccountInfo(
                exchange="okx",
                total_equity=float(data.get("totalEq", 0) or 0),
                balance_usd=float(data.get("adjEq", 0) or 0),
                available_balance_usd=float(data.get("availBal", 0) or 0),
                margin_used=float(data.get("imr", 0) or 0),
                margin_ratio=float(data.get("mgnRatio", 0) or 0) * 100,
                unrealized_pnl=float(data.get("upl", 0) or 0),
            )
    except Exception as e:
        logger.error("Error getting account info: %s", e)
    return None
```

### Cancel All Orders

```python
async def cancel_all_orders(self, symbol: str = None, inst_type: str = None) -> int:
    """Cancel all pending orders. Returns count cancelled."""
    # First get pending orders
    path = "/api/v5/trade/orders-pending"
    if inst_type:
        path += f"?instType={inst_type}"
    if symbol:
        path += f"&instId={symbol}" if "?" in path else f"?instId={symbol}"

    try:
        response = await self._request("GET", path)
        if not response or response.get("code") != "0":
            return 0

        orders = response.get("data", [])
        if not orders:
            return 0

        # Cancel each order
        cancelled = 0
        for order in orders:
            if await self.cancel_order(order["instId"], order["ordId"]):
                cancelled += 1

        return cancelled
    except Exception as e:
        logger.error("Error cancelling all orders: %s", e)
        return 0
```

---

## Binance Adapter

### Configuration

```python
# adapters/binance_adapter.py

class BinanceAdapter(ExchangeAdapter):
    # Endpoints
    SPOT_BASE_URL = "https://api.binance.com"
    FUTURES_BASE_URL = "https://fapi.binance.com"
    SPOT_TESTNET_URL = "https://testnet.binance.vision"
    FUTURES_TESTNET_URL = "https://testnet.binancefuture.com"

    def __init__(self, exchange: Exchange, is_futures: bool = False):
        self.exchange = exchange
        self.is_futures = is_futures
        self.api_key = exchange.api_key
        self.secret_key = exchange.secret_key
        self.is_testnet = exchange.is_testnet

        # Select base URL
        if is_futures:
            self.base_url = self.FUTURES_TESTNET_URL if self.is_testnet else self.FUTURES_BASE_URL
        else:
            self.base_url = self.SPOT_TESTNET_URL if self.is_testnet else self.SPOT_BASE_URL
```

### Authentication

Binance uses HMAC-SHA256 with timestamp:

```python
def _sign(self, params: Dict) -> str:
    """Generate signature for params."""
    query_string = urlencode(params)
    signature = hmac.new(
        self.secret_key.encode(),
        query_string.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature

def _get_headers(self) -> Dict:
    """Build headers with API key."""
    return {
        "X-MBX-APIKEY": self.api_key,
        "Content-Type": "application/x-www-form-urlencoded",
    }

async def _request(self, method: str, endpoint: str, params: Dict = None, signed: bool = True):
    """Make authenticated request."""
    url = f"{self.base_url}{endpoint}"
    params = params or {}

    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)

    headers = self._get_headers()

    async with aiohttp.ClientSession() as session:
        if method == "GET":
            async with session.get(url, params=params, headers=headers) as resp:
                return await resp.json()
        else:
            async with session.post(url, data=params, headers=headers) as resp:
                return await resp.json()
```

### Key Differences from OKX

| Feature | OKX | Binance |
|---------|-----|---------|
| Symbol format | `BTC-USDT`, `BTC-USDT-SWAP` | `BTCUSDT` |
| Spot/Futures | Same endpoint, different instType | Different base URLs |
| Auth header | `OK-ACCESS-KEY` | `X-MBX-APIKEY` |
| Demo mode | Header `x-simulated-trading: 1` | Separate testnet URL |
| Position side | `posSide`: "long"/"short" | `positionSide`: "LONG"/"SHORT" |

---

## Bybit Adapter

### Configuration

```python
# adapters/bybit_adapter.py

class BybitAdapter(ExchangeAdapter):
    BASE_URL = "https://api.bybit.com"
    TESTNET_URL = "https://api-testnet.bybit.com"

    def __init__(self, exchange: Exchange, is_futures: bool = False):
        self.exchange = exchange
        self.is_futures = is_futures
        self.api_key = exchange.api_key
        self.secret_key = exchange.secret_key
        self.is_testnet = exchange.is_testnet
        self.base_url = self.TESTNET_URL if self.is_testnet else self.BASE_URL
```

### Authentication

Bybit uses timestamp + api_key + recv_window in signature:

```python
def _sign(self, timestamp: str, params: str) -> str:
    """Generate signature."""
    recv_window = "5000"
    sign_str = timestamp + self.api_key + recv_window + params
    signature = hmac.new(
        self.secret_key.encode(),
        sign_str.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature

def _get_headers(self, timestamp: str, params: str) -> Dict:
    """Build authenticated headers."""
    return {
        "X-BAPI-API-KEY": self.api_key,
        "X-BAPI-SIGN": self._sign(timestamp, params),
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json",
    }
```

---

## Symbol Mapping

```python
# From models.py

CRYPTO_ASSETS = {
    'BTC': {
        'name': 'Bitcoin',
        'okx_spot': 'BTC-USDT',
        'okx_futures': 'BTC-USDT-SWAP',
        'binance_spot': 'BTCUSDT',
        'binance_futures': 'BTCUSDT',
        'bybit_spot': 'BTCUSDT',
        'bybit_futures': 'BTCUSDT',
    },
    'ETH': {
        'name': 'Ethereum',
        'okx_spot': 'ETH-USDT',
        'okx_futures': 'ETH-USDT-SWAP',
        'binance_spot': 'ETHUSDT',
        'binance_futures': 'ETHUSDT',
        'bybit_spot': 'ETHUSDT',
        'bybit_futures': 'ETHUSDT',
    },
    # ... SOL, XRP, DOGE, AVAX, LINK
}

def get_symbols_for_asset(asset: str, exchange_type: str) -> Tuple[str, str]:
    """Get spot and futures symbols for an asset on an exchange."""
    config = CRYPTO_ASSETS[asset]
    spot_key = f"{exchange_type}_spot"
    futures_key = f"{exchange_type}_futures"
    return config[spot_key], config[futures_key]
```

---

## Error Handling

### OKX Error Codes

| Code | Meaning | Action |
|------|---------|--------|
| 0 | Success | Continue |
| 51000 | Parameter error | Check request |
| 51001 | System busy | Retry |
| 51006 | Order not found | Skip |
| 51008 | Insufficient balance | Stop trading |
| 51020 | Order already cancelled | Ignore |
| 51119 | POST_ONLY would taker | Retry as LIMIT |

### Retry Logic

```python
async def _request_with_retry(self, method: str, path: str, body: Dict = None, max_retries: int = 3):
    """Request with exponential backoff."""
    for attempt in range(max_retries):
        try:
            response = await self._request(method, path, body)
            if response and response.get("code") == "0":
                return response
            elif response and response.get("code") == "51001":  # System busy
                await asyncio.sleep(2 ** attempt)
                continue
            else:
                return response  # Return error response
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                raise
    return None
```

---

## Integration Example

```python
from adapters.okx_adapter import OKXAdapter
from models import Exchange

# Create exchange config
exchange = Exchange(
    name="OKX Demo",
    exchange_type="okx",
    api_key="your-api-key",
    secret_key="your-secret-key",
    passphrase="your-passphrase",
    is_testnet=True,  # Demo mode
    role="BOTH",
)

# Create adapters
spot_adapter = OKXAdapter(exchange, is_futures=False)
futures_adapter = OKXAdapter(exchange, is_futures=True)

# Connect
await spot_adapter.connect()
await futures_adapter.connect()

# Get ticks
spot_tick = await spot_adapter.get_tick("BTC-USDT")
futures_tick = await futures_adapter.get_tick("BTC-USDT-SWAP")

# Place order
result = await spot_adapter.place_order(
    symbol="BTC-USDT",
    side="BUY",
    order_type="LIMIT",
    quantity=0.001,
    price=50000.0,
)

if result.success:
    print(f"Order placed: {result.order_id}")

# Cleanup
await spot_adapter.disconnect()
await futures_adapter.disconnect()
```
