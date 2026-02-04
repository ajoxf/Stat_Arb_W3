"""
OKX Exchange adapter implementation.
"""

import hmac
import hashlib
import base64
import json
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import aiohttp

from .base import ExchangeAdapter
from models import MarketTick, OrderResult, Position, AccountInfo

logger = logging.getLogger(__name__)


class OKXAdapter(ExchangeAdapter):
    """
    OKX exchange adapter supporting spot and perpetual swaps.

    Symbol format:
    - Spot: BTC-USDT, ETH-USDT
    - Perpetual: BTC-USDT-SWAP, ETH-USDT-SWAP
    """

    # API endpoints
    BASE_URL = "https://www.okx.com"
    DEMO_URL = "https://www.okx.com"  # Same URL, different header

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str = "",
        is_testnet: bool = True,
    ):
        super().__init__(api_key, secret_key, passphrase, is_testnet)
        self._session: Optional[aiohttp.ClientSession] = None
        self.base_url = self.BASE_URL

    async def connect(self) -> bool:
        """Establish connection to OKX."""
        try:
            self._session = aiohttp.ClientSession()

            # Test connection with account info
            result = await self._request("GET", "/api/v5/account/balance")

            if result and "data" in result:
                self._connected = True
                self._clear_error()
                logger.info("Connected to OKX (demo=%s)", self.is_testnet)
                return True
            else:
                error = result.get("msg", "Unknown error") if result else "No response"
                self._set_error(f"OKX connection failed: {error}")
                return False

        except Exception as e:
            self._set_error(f"OKX connection error: {str(e)}")
            logger.exception("OKX connection error")
            return False

    async def disconnect(self) -> None:
        """Disconnect from OKX."""
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False
        logger.info("Disconnected from OKX")

    def _get_timestamp(self) -> str:
        """Get ISO timestamp for signing."""
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") + \
               datetime.utcnow().strftime("%f")[:3] + "Z"

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """Generate signature for request."""
        message = timestamp + method + path + body
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    def _get_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate request headers."""
        timestamp = self._get_timestamp()
        signature = self._sign(timestamp, method, path, body)

        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

        # Demo trading header
        if self.is_testnet:
            headers["x-simulated-trading"] = "1"

        return headers

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Make API request."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        url = self.base_url + path
        body = json.dumps(data) if data else ""

        if params:
            path = path + "?" + "&".join(f"{k}={v}" for k, v in params.items())
            url = self.base_url + path

        headers = self._get_headers(method, path, body)

        try:
            async with self._session.request(
                method, url, headers=headers, data=body if data else None
            ) as response:
                result = await response.json()

                if result.get("code") != "0":
                    error = result.get("msg", "Unknown error")
                    logger.warning("OKX API error: %s", error)
                    self._set_error(error)

                return result

        except Exception as e:
            logger.exception("OKX request error: %s %s", method, path)
            self._set_error(str(e))
            return None

    async def get_tick(self, symbol: str) -> Optional[MarketTick]:
        """Get current market tick."""
        try:
            result = await self._request(
                "GET", "/api/v5/market/ticker", params={"instId": symbol}
            )

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                return MarketTick(
                    symbol=symbol,
                    bid=float(data.get("bidPx", 0)),
                    ask=float(data.get("askPx", 0)),
                    last=float(data.get("last", 0)),
                    volume_24h=float(data.get("vol24h", 0)),
                    timestamp=datetime.utcnow(),
                )

        except Exception as e:
            logger.error("Error fetching OKX tick: %s", e)

        return None

    async def get_orderbook(
        self, symbol: str, depth: int = 5
    ) -> Optional[Dict[str, Any]]:
        """Get order book."""
        try:
            result = await self._request(
                "GET",
                "/api/v5/market/books",
                params={"instId": symbol, "sz": str(depth)},
            )

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                return {
                    "bids": [[float(b[0]), float(b[1])] for b in data.get("bids", [])],
                    "asks": [[float(a[0]), float(a[1])] for a in data.get("asks", [])],
                    "timestamp": datetime.utcnow(),
                }

        except Exception as e:
            logger.error("Error fetching OKX orderbook: %s", e)

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
            # Determine instrument type and trade mode
            inst_type = "SWAP" if "-SWAP" in symbol else "SPOT"
            td_mode = "cross" if inst_type == "SWAP" else "cash"

            order_data = {
                "instId": symbol,
                "tdMode": td_mode,
                "side": side.lower(),
                "ordType": "market" if order_type == "MARKET" else "limit",
                "sz": str(quantity),
            }

            if order_type == "LIMIT" and price:
                order_data["px"] = str(price)

            if reduce_only and inst_type == "SWAP":
                order_data["reduceOnly"] = True

            result = await self._request("POST", "/api/v5/trade/order", data=order_data)

            if result and result.get("code") == "0" and result.get("data"):
                order_info = result["data"][0]
                return OrderResult(
                    success=True,
                    order_id=order_info.get("ordId", ""),
                    filled_qty=quantity,  # OKX returns fill info separately
                    filled_price=price or 0,
                )
            else:
                error = result.get("msg", "Unknown error") if result else "No response"
                return OrderResult(success=False, error=error)

        except Exception as e:
            logger.exception("Error placing OKX order")
            return OrderResult(success=False, error=str(e))

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order."""
        try:
            result = await self._request(
                "POST",
                "/api/v5/trade/cancel-order",
                data={"instId": symbol, "ordId": order_id},
            )

            return result and result.get("code") == "0"

        except Exception as e:
            logger.exception("Error canceling OKX order")
            return False

    async def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """Get open positions."""
        try:
            params = {}
            if symbol:
                params["instId"] = symbol

            result = await self._request("GET", "/api/v5/account/positions", params=params)

            positions = []
            if result and result.get("code") == "0" and result.get("data"):
                for p in result["data"]:
                    pos_qty = float(p.get("pos", 0))
                    if pos_qty != 0:
                        positions.append(Position(
                            symbol=p.get("instId", ""),
                            side="LONG" if pos_qty > 0 else "SHORT",
                            quantity=abs(pos_qty),
                            entry_price=float(p.get("avgPx", 0)),
                            unrealized_pnl=float(p.get("upl", 0)),
                            leverage=float(p.get("lever", 1)),
                        ))

            return positions

        except Exception as e:
            logger.exception("Error fetching OKX positions")
            return []

    async def close_position(self, symbol: str) -> OrderResult:
        """Close an open position."""
        try:
            # Get current position
            positions = await self.get_positions(symbol)
            if not positions:
                return OrderResult(success=True)  # No position to close

            pos = positions[0]
            close_side = "sell" if pos.side == "LONG" else "buy"

            result = await self._request(
                "POST",
                "/api/v5/trade/close-position",
                data={
                    "instId": symbol,
                    "mgnMode": "cross",
                },
            )

            if result and result.get("code") == "0":
                return OrderResult(success=True)
            else:
                error = result.get("msg", "Unknown error") if result else "No response"
                return OrderResult(success=False, error=error)

        except Exception as e:
            logger.exception("Error closing OKX position")
            return OrderResult(success=False, error=str(e))

    async def get_account_info(self) -> Optional[AccountInfo]:
        """Get account information."""
        try:
            result = await self._request("GET", "/api/v5/account/balance")

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                total_eq = float(data.get("totalEq", 0))

                # Get USDT balance specifically
                usdt_balance = 0
                for detail in data.get("details", []):
                    if detail.get("ccy") == "USDT":
                        usdt_balance = float(detail.get("availBal", 0))
                        break

                return AccountInfo(
                    exchange="OKX",
                    balance_usd=total_eq,
                    available_balance_usd=usdt_balance,
                    margin_used=float(data.get("imr") or 0),
                    unrealized_pnl=float(data.get("upl") or 0),
                )

        except Exception as e:
            logger.exception("Error fetching OKX account info")

        return None

    async def get_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get funding rate for perpetual."""
        try:
            result = await self._request(
                "GET",
                "/api/v5/public/funding-rate",
                params={"instId": symbol},
            )

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                return {
                    "symbol": symbol,
                    "funding_rate": float(data.get("fundingRate", 0)),
                    "next_funding_time": data.get("fundingTime"),
                }

        except Exception as e:
            logger.error("Error fetching OKX funding rate: %s", e)

        return None

    async def get_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get symbol trading information."""
        try:
            inst_type = "SWAP" if "-SWAP" in symbol else "SPOT"
            result = await self._request(
                "GET",
                "/api/v5/public/instruments",
                params={"instType": inst_type, "instId": symbol},
            )

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                return {
                    "symbol": symbol,
                    "min_qty": float(data.get("minSz", 0)),
                    "qty_precision": int(data.get("lotSz", "0").find("1") - 1) if "." in data.get("lotSz", "1") else 0,
                    "price_precision": int(data.get("tickSz", "0").find("1") - 1) if "." in data.get("tickSz", "1") else 0,
                    "contract_val": float(data.get("ctVal", 1)),
                }

        except Exception as e:
            logger.error("Error fetching OKX symbol info: %s", e)

        return None
