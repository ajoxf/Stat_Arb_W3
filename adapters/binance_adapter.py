"""
Binance Exchange adapter implementation.
"""

import hmac
import hashlib
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
import aiohttp

from .base import ExchangeAdapter
from models import MarketTick, OrderResult, Position, AccountInfo

logger = logging.getLogger(__name__)


class BinanceAdapter(ExchangeAdapter):
    """
    Binance exchange adapter supporting spot and USDT-M futures.

    Symbol format:
    - Spot: BTCUSDT, ETHUSDT
    - Futures: BTCUSDT, ETHUSDT (same symbol, different endpoint)
    """

    # API endpoints
    SPOT_URL = "https://api.binance.com"
    FUTURES_URL = "https://fapi.binance.com"
    SPOT_TESTNET_URL = "https://testnet.binance.vision"
    FUTURES_TESTNET_URL = "https://testnet.binancefuture.com"

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

        # Set base URL based on mode
        if is_futures:
            self.base_url = self.FUTURES_TESTNET_URL if is_testnet else self.FUTURES_URL
        else:
            self.base_url = self.SPOT_TESTNET_URL if is_testnet else self.SPOT_URL

    async def connect(self) -> bool:
        """Establish connection to Binance."""
        try:
            self._session = aiohttp.ClientSession()

            # Test connection
            if self.is_futures:
                result = await self._request("GET", "/fapi/v2/balance", signed=True)
            else:
                result = await self._request("GET", "/api/v3/account", signed=True)

            if result is not None and "code" not in result:
                self._connected = True
                self._clear_error()
                logger.info("Connected to Binance (testnet=%s, futures=%s)",
                            self.is_testnet, self.is_futures)
                return True
            else:
                error = result.get("msg", "Unknown error") if result else "No response"
                self._set_error(f"Binance connection failed: {error}")
                return False

        except Exception as e:
            self._set_error(f"Binance connection error: {str(e)}")
            logger.exception("Binance connection error")
            return False

    async def disconnect(self) -> None:
        """Disconnect from Binance."""
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False
        logger.info("Disconnected from Binance")

    def _sign(self, params: Dict) -> str:
        """Generate signature for request."""
        query_string = urlencode(params)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _get_headers(self) -> Dict[str, str]:
        """Generate request headers."""
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        signed: bool = False,
    ) -> Optional[Dict]:
        """Make API request."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        url = self.base_url + path
        params = params or {}

        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)

        headers = self._get_headers()

        try:
            if method == "GET":
                async with self._session.get(url, params=params, headers=headers) as response:
                    result = await response.json()
            else:
                async with self._session.request(
                    method, url, params=params, headers=headers
                ) as response:
                    result = await response.json()

            if isinstance(result, dict) and "code" in result:
                error = result.get("msg", "Unknown error")
                logger.warning("Binance API error: %s", error)
                self._set_error(error)

            return result

        except Exception as e:
            logger.exception("Binance request error: %s %s", method, path)
            self._set_error(str(e))
            return None

    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        """Get current market tick."""
        try:
            if self.is_futures:
                path = "/fapi/v1/ticker/bookTicker"
            else:
                path = "/api/v3/ticker/bookTicker"

            result = await self._request("GET", path, params={"symbol": symbol})

            if result and "bidPrice" in result:
                # Get last price from 24hr ticker
                if self.is_futures:
                    price_result = await self._request(
                        "GET", "/fapi/v1/ticker/24hr", params={"symbol": symbol}
                    )
                else:
                    price_result = await self._request(
                        "GET", "/api/v3/ticker/24hr", params={"symbol": symbol}
                    )

                last_price = float(price_result.get("lastPrice", 0)) if price_result else 0
                volume = float(price_result.get("volume", 0)) if price_result else 0

                return MarketTick(
                    symbol=symbol,
                    bid=float(result.get("bidPrice", 0)),
                    ask=float(result.get("askPrice", 0)),
                    last=last_price,
                    volume_24h=volume,
                    timestamp=datetime.utcnow(),
                )

        except Exception as e:
            logger.error("Error fetching Binance tick: %s", e)

        return None

    async def get_orderbook(
        self, symbol: str, depth: int = 5
    ) -> Optional[Dict[str, Any]]:
        """Get order book."""
        try:
            if self.is_futures:
                path = "/fapi/v1/depth"
            else:
                path = "/api/v3/depth"

            result = await self._request(
                "GET", path, params={"symbol": symbol, "limit": depth}
            )

            if result and "bids" in result:
                return {
                    "bids": [[float(b[0]), float(b[1])] for b in result.get("bids", [])],
                    "asks": [[float(a[0]), float(a[1])] for a in result.get("asks", [])],
                    "timestamp": datetime.utcnow(),
                }

        except Exception as e:
            logger.error("Error fetching Binance orderbook: %s", e)

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
            if self.is_futures:
                path = "/fapi/v1/order"
            else:
                path = "/api/v3/order"

            params = {
                "symbol": symbol,
                "side": side.upper(),
                "type": order_type.upper(),
                "quantity": str(quantity),
            }

            if order_type == "LIMIT" and price:
                params["price"] = str(price)
                params["timeInForce"] = "GTC"

            if reduce_only and self.is_futures:
                params["reduceOnly"] = "true"

            result = await self._request("POST", path, params=params, signed=True)

            if result and "orderId" in result:
                return OrderResult(
                    success=True,
                    order_id=str(result.get("orderId", "")),
                    filled_qty=float(result.get("executedQty", 0)),
                    filled_price=float(result.get("avgPrice", 0)) if self.is_futures
                                 else float(result.get("price", 0)),
                )
            else:
                error = result.get("msg", "Unknown error") if result else "No response"
                return OrderResult(success=False, error=error)

        except Exception as e:
            logger.exception("Error placing Binance order")
            return OrderResult(success=False, error=str(e))

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order."""
        try:
            if self.is_futures:
                path = "/fapi/v1/order"
            else:
                path = "/api/v3/order"

            result = await self._request(
                "DELETE",
                path,
                params={"symbol": symbol, "orderId": order_id},
                signed=True,
            )

            return result and "orderId" in result

        except Exception as e:
            logger.exception("Error canceling Binance order")
            return False

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Get open positions (futures only)."""
        if not self.is_futures:
            return []

        try:
            result = await self._request("GET", "/fapi/v2/positionRisk", signed=True)

            positions = []
            if result:
                for p in result:
                    if symbol and p.get("symbol") != symbol:
                        continue

                    pos_amt = float(p.get("positionAmt", 0))
                    if pos_amt != 0:
                        positions.append(Position(
                            symbol=p.get("symbol", ""),
                            side="LONG" if pos_amt > 0 else "SHORT",
                            quantity=abs(pos_amt),
                            entry_price=float(p.get("entryPrice", 0)),
                            unrealized_pnl=float(p.get("unRealizedProfit", 0)),
                            leverage=float(p.get("leverage", 1)),
                        ))

            return positions

        except Exception as e:
            logger.exception("Error fetching Binance positions")
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
            close_side = "SELL" if pos.side == "LONG" else "BUY"

            return await self.place_order(
                symbol=symbol,
                side=close_side,
                order_type="MARKET",
                quantity=pos.quantity,
                reduce_only=True,
            )

        except Exception as e:
            logger.exception("Error closing Binance position")
            return OrderResult(success=False, error=str(e))

    async def get_account_info(self) -> Optional[AccountInfo]:
        """Get account information."""
        try:
            if self.is_futures:
                result = await self._request("GET", "/fapi/v2/account", signed=True)

                if result:
                    return AccountInfo(
                        exchange="Binance Futures",
                        balance_usd=float(result.get("totalWalletBalance", 0)),
                        available_balance_usd=float(result.get("availableBalance", 0)),
                        margin_used=float(result.get("totalInitialMargin", 0)),
                        unrealized_pnl=float(result.get("totalUnrealizedProfit", 0)),
                    )
            else:
                result = await self._request("GET", "/api/v3/account", signed=True)

                if result:
                    # Find USDT balance
                    usdt_balance = 0
                    for balance in result.get("balances", []):
                        if balance.get("asset") == "USDT":
                            usdt_balance = float(balance.get("free", 0))
                            break

                    return AccountInfo(
                        exchange="Binance Spot",
                        balance_usd=usdt_balance,
                        available_balance_usd=usdt_balance,
                        margin_used=0,
                        unrealized_pnl=0,
                    )

        except Exception as e:
            logger.exception("Error fetching Binance account info")

        return None

    async def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get funding rate for perpetual."""
        if not self.is_futures:
            return None

        try:
            result = await self._request(
                "GET",
                "/fapi/v1/premiumIndex",
                params={"symbol": symbol},
            )

            if result:
                return {
                    "symbol": symbol,
                    "funding_rate": float(result.get("lastFundingRate", 0)),
                    "next_funding_time": result.get("nextFundingTime"),
                }

        except Exception as e:
            logger.error("Error fetching Binance funding rate: %s", e)

        return None

    async def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get symbol trading information."""
        try:
            if self.is_futures:
                path = "/fapi/v1/exchangeInfo"
            else:
                path = "/api/v3/exchangeInfo"

            result = await self._request("GET", path)

            if result and "symbols" in result:
                for s in result["symbols"]:
                    if s.get("symbol") == symbol:
                        filters = {f["filterType"]: f for f in s.get("filters", [])}

                        lot_size = filters.get("LOT_SIZE", {})
                        price_filter = filters.get("PRICE_FILTER", {})

                        return {
                            "symbol": symbol,
                            "min_qty": float(lot_size.get("minQty", 0)),
                            "qty_precision": self._count_decimals(lot_size.get("stepSize", "1")),
                            "price_precision": self._count_decimals(price_filter.get("tickSize", "1")),
                            "contract_val": 1,
                        }

        except Exception as e:
            logger.error("Error fetching Binance symbol info: %s", e)

        return None

    def _count_decimals(self, value: str) -> int:
        """Count decimal places in a string number."""
        if "." in value:
            return len(value.split(".")[1].rstrip("0"))
        return 0
