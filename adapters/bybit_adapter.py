"""
Bybit Exchange adapter implementation.
"""

import hmac
import hashlib
import time
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import aiohttp

from .base import ExchangeAdapter
from models import MarketTick, OrderResult, Position, AccountInfo

logger = logging.getLogger(__name__)


class BybitAdapter(ExchangeAdapter):
    """
    Bybit exchange adapter supporting unified account API v5.

    Symbol format:
    - Spot: BTCUSDT, ETHUSDT
    - Linear perpetual: BTCUSDT, ETHUSDT
    """

    # API endpoints
    BASE_URL = "https://api.bybit.com"
    TESTNET_URL = "https://api-testnet.bybit.com"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str = "",
        is_testnet: bool = True,
        is_futures: bool = False,
    ):
        super().__init__(api_key, secret_key, passphrase, is_testnet)
        self.is_futures = is_futures
        self._session: Optional[aiohttp.ClientSession] = None
        self.base_url = self.TESTNET_URL if is_testnet else self.BASE_URL
        self.recv_window = 5000

    async def connect(self) -> bool:
        """Establish connection to Bybit."""
        try:
            self._session = aiohttp.ClientSession()

            # Test connection with wallet balance
            result = await self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})

            if result and result.get("retCode") == 0:
                self._connected = True
                self._clear_error()
                logger.info("Connected to Bybit (testnet=%s, futures=%s)",
                            self.is_testnet, self.is_futures)
                return True
            else:
                error = result.get("retMsg", "Unknown error") if result else "No response"
                self._set_error(f"Bybit connection failed: {error}")
                return False

        except Exception as e:
            self._set_error(f"Bybit connection error: {str(e)}")
            logger.exception("Bybit connection error")
            return False

    async def disconnect(self) -> None:
        """Disconnect from Bybit."""
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False
        logger.info("Disconnected from Bybit")

    def _sign(self, timestamp: str, params: str) -> str:
        """Generate signature for request."""
        param_str = f"{timestamp}{self.api_key}{self.recv_window}{params}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _get_headers(self, timestamp: str, params: str) -> Dict[str, str]:
        """Generate request headers."""
        signature = self._sign(timestamp, params)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": str(self.recv_window),
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Make API request."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        url = self.base_url + path
        timestamp = str(int(time.time() * 1000))
        params = params or {}

        if method == "GET":
            param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            if param_str:
                url = f"{url}?{param_str}"
        else:
            param_str = json.dumps(params) if params else ""

        headers = self._get_headers(timestamp, param_str)

        try:
            if method == "GET":
                async with self._session.get(url, headers=headers) as response:
                    result = await response.json()
            else:
                async with self._session.post(url, headers=headers, json=params) as response:
                    result = await response.json()

            if result.get("retCode") != 0:
                error = result.get("retMsg", "Unknown error")
                logger.warning("Bybit API error: %s", error)
                self._set_error(error)

            return result

        except Exception as e:
            logger.exception("Bybit request error: %s %s", method, path)
            self._set_error(str(e))
            return None

    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        """Get current market tick."""
        try:
            category = "linear" if self.is_futures else "spot"
            result = await self._request(
                "GET",
                "/v5/market/tickers",
                {"category": category, "symbol": symbol},
            )

            if result and result.get("retCode") == 0 and result.get("result", {}).get("list"):
                data = result["result"]["list"][0]
                return MarketTick(
                    symbol=symbol,
                    bid=float(data.get("bid1Price", 0)),
                    ask=float(data.get("ask1Price", 0)),
                    last=float(data.get("lastPrice", 0)),
                    volume_24h=float(data.get("volume24h", 0)),
                    timestamp=datetime.utcnow(),
                )

        except Exception as e:
            logger.error("Error fetching Bybit tick: %s", e)

        return None

    async def get_orderbook(
        self, symbol: str, depth: int = 5
    ) -> Optional[Dict[str, Any]]:
        """Get order book."""
        try:
            category = "linear" if self.is_futures else "spot"
            result = await self._request(
                "GET",
                "/v5/market/orderbook",
                {"category": category, "symbol": symbol, "limit": depth},
            )

            if result and result.get("retCode") == 0 and result.get("result"):
                data = result["result"]
                return {
                    "bids": [[float(b[0]), float(b[1])] for b in data.get("b", [])],
                    "asks": [[float(a[0]), float(a[1])] for a in data.get("a", [])],
                    "timestamp": datetime.utcnow(),
                }

        except Exception as e:
            logger.error("Error fetching Bybit orderbook: %s", e)

        return None

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        """Place an order."""
        try:
            category = "linear" if self.is_futures else "spot"

            params = {
                "category": category,
                "symbol": symbol,
                "side": side.capitalize(),
                "orderType": order_type.capitalize(),
                "qty": str(quantity),
            }

            if order_type == "LIMIT" and price:
                params["price"] = str(price)

            if reduce_only and self.is_futures:
                params["reduceOnly"] = True

            result = await self._request("POST", "/v5/order/create", params)

            if result and result.get("retCode") == 0:
                order_info = result.get("result", {})
                return OrderResult(
                    success=True,
                    order_id=order_info.get("orderId", ""),
                    filled_qty=quantity,
                    filled_price=price or 0,
                )
            else:
                error = result.get("retMsg", "Unknown error") if result else "No response"
                return OrderResult(success=False, error=error)

        except Exception as e:
            logger.exception("Error placing Bybit order")
            return OrderResult(success=False, error=str(e))

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order."""
        try:
            category = "linear" if self.is_futures else "spot"
            result = await self._request(
                "POST",
                "/v5/order/cancel",
                {"category": category, "symbol": symbol, "orderId": order_id},
            )

            return result and result.get("retCode") == 0

        except Exception as e:
            logger.exception("Error canceling Bybit order")
            return False

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Get open positions."""
        if not self.is_futures:
            return []

        try:
            params = {"category": "linear", "settleCoin": "USDT"}
            if symbol:
                params["symbol"] = symbol

            result = await self._request("GET", "/v5/position/list", params)

            positions = []
            if result and result.get("retCode") == 0:
                for p in result.get("result", {}).get("list", []):
                    size = float(p.get("size", 0))
                    if size != 0:
                        positions.append(Position(
                            symbol=p.get("symbol", ""),
                            side=p.get("side", "").upper(),
                            quantity=size,
                            entry_price=float(p.get("avgPrice", 0)),
                            unrealized_pnl=float(p.get("unrealisedPnl", 0)),
                            leverage=float(p.get("leverage", 1)),
                        ))

            return positions

        except Exception as e:
            logger.exception("Error fetching Bybit positions")
            return []

    async def close_position(self, symbol: str) -> OrderResult:
        """Close an open position."""
        if not self.is_futures:
            return OrderResult(success=False, error="Close position only for futures")

        try:
            positions = await self.get_positions(symbol)
            if not positions:
                return OrderResult(success=True)

            pos = positions[0]
            close_side = "Sell" if pos.side == "LONG" else "Buy"

            return await self.place_order(
                symbol=symbol,
                side=close_side,
                order_type="MARKET",
                quantity=pos.quantity,
                reduce_only=True,
            )

        except Exception as e:
            logger.exception("Error closing Bybit position")
            return OrderResult(success=False, error=str(e))

    async def get_account_info(self) -> Optional[AccountInfo]:
        """Get account information."""
        try:
            result = await self._request(
                "GET",
                "/v5/account/wallet-balance",
                {"accountType": "UNIFIED"},
            )

            if result and result.get("retCode") == 0:
                accounts = result.get("result", {}).get("list", [])
                if accounts:
                    account = accounts[0]
                    return AccountInfo(
                        exchange="Bybit",
                        balance_usd=float(account.get("totalEquity", 0)),
                        available_balance_usd=float(account.get("totalAvailableBalance", 0)),
                        margin_used=float(account.get("totalInitialMargin", 0)),
                        unrealized_pnl=float(account.get("totalPerpUPL", 0)),
                    )

        except Exception as e:
            logger.exception("Error fetching Bybit account info")

        return None

    async def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get funding rate for perpetual."""
        if not self.is_futures:
            return None

        try:
            result = await self._request(
                "GET",
                "/v5/market/tickers",
                {"category": "linear", "symbol": symbol},
            )

            if result and result.get("retCode") == 0:
                tickers = result.get("result", {}).get("list", [])
                if tickers:
                    data = tickers[0]
                    return {
                        "symbol": symbol,
                        "funding_rate": float(data.get("fundingRate", 0)),
                        "next_funding_time": data.get("nextFundingTime"),
                    }

        except Exception as e:
            logger.error("Error fetching Bybit funding rate: %s", e)

        return None

    async def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get symbol trading information."""
        try:
            category = "linear" if self.is_futures else "spot"
            result = await self._request(
                "GET",
                "/v5/market/instruments-info",
                {"category": category, "symbol": symbol},
            )

            if result and result.get("retCode") == 0:
                instruments = result.get("result", {}).get("list", [])
                if instruments:
                    inst = instruments[0]
                    lot_filter = inst.get("lotSizeFilter", {})
                    price_filter = inst.get("priceFilter", {})

                    return {
                        "symbol": symbol,
                        "min_qty": float(lot_filter.get("minOrderQty", 0)),
                        "qty_precision": self._count_decimals(lot_filter.get("qtyStep", "1")),
                        "price_precision": self._count_decimals(price_filter.get("tickSize", "1")),
                        "contract_val": 1,
                    }

        except Exception as e:
            logger.error("Error fetching Bybit symbol info: %s", e)

        return None

    def _count_decimals(self, value: str) -> int:
        """Count decimal places in a string number."""
        if "." in value:
            return len(value.split(".")[1].rstrip("0"))
        return 0
