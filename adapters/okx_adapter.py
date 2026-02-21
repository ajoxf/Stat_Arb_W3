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
        spot_leverage: int = 1,
    ):
        super().__init__(api_key, secret_key, passphrase, is_testnet)
        self._session: Optional[aiohttp.ClientSession] = None
        self.base_url = self.BASE_URL
        # cross mode required for leveraged spot accounts; cash for simple 1x spot
        self._spot_td_mode = "cross" if spot_leverage > 1 else "cash"

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
                    # Get detailed error from data array
                    data_arr = result.get("data", [])
                    if data_arr and isinstance(data_arr, list) and len(data_arr) > 0:
                        sub_code = data_arr[0].get("sCode", "")
                        sub_msg = data_arr[0].get("sMsg", "")
                        if sub_code or sub_msg:
                            error = f"{error} (sCode={sub_code}: {sub_msg})"
                    logger.warning("OKX API error: %s | Full response: %s", error, result)
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
            else:
                # Log API error if result exists but code is not "0"
                if result:
                    error_msg = result.get("msg", "Unknown")
                    error_code = result.get("code", "?")
                    logger.warning("OKX ticker API error for %s: code=%s, msg=%s",
                                  symbol, error_code, error_msg)
                else:
                    logger.warning("OKX ticker API returned None for %s", symbol)

        except Exception as e:
            logger.error("Error fetching OKX tick for %s: %s", symbol, e)

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
        pos_side: Optional[str] = None,
    ) -> OrderResult:
        """
        Place an order.

        Note: For SWAP contracts, quantity is in base currency (e.g., BTC).
        This method converts to contracts automatically.

        Args:
            pos_side: Position side for long/short mode accounts ("long" or "short").
                      If None, will auto-detect based on account mode.
        """
        try:
            # Determine instrument type and trade mode
            inst_type = "SWAP" if "-SWAP" in symbol else "SPOT"
            td_mode = "cross" if inst_type == "SWAP" else self._spot_td_mode

            # Get symbol info for size validation and formatting
            symbol_info = await self.get_symbol_info(symbol)
            sz = quantity
            sz_str = ""

            if inst_type == "SWAP":
                # For SWAP: convert quantity to contracts
                if symbol_info:
                    ct_val = symbol_info.get("contract_val", 0.01)
                    if ct_val <= 0:
                        return OrderResult(success=False, error=f"Invalid contract value {ct_val}")
                    # Convert BTC quantity to number of contracts
                    contracts = quantity / ct_val
                    sz = round(contracts)
                    # Validate minimum 1 contract - don't silently inflate small positions
                    if sz < 1:
                        logger.error("SWAP quantity %.6f = %.2f contracts (ctVal=%.4f), minimum is 1",
                                    quantity, contracts, ct_val)
                        return OrderResult(success=False, error=f"Quantity {quantity} too small, need at least {ct_val} for 1 contract")
                    logger.info("SWAP order: %.6f %s = %d contracts (ctVal=%.4f)",
                               quantity, symbol.split("-")[0], sz, ct_val)
                else:
                    # No symbol info - cannot safely place SWAP order
                    return OrderResult(success=False, error="Cannot place SWAP order without symbol info")
                sz_str = str(int(sz))
            else:
                # For SPOT: validate and format quantity properly
                if symbol_info:
                    min_sz = symbol_info.get("min_qty", 0)
                    lot_sz = symbol_info.get("lot_sz", 0.00000001)

                    # Use qty_precision from symbol_info (calculated from original API string)
                    # Don't recalculate from float - str(0.00000001) becomes "1e-08"
                    decimals = symbol_info.get("qty_precision", 8)

                    # Round to lot_sz precision
                    sz = round(quantity, decimals)

                    # Validate minimum
                    if sz < min_sz:
                        logger.error("SPOT order size %.8f below minimum %.8f for %s",
                                    sz, min_sz, symbol)
                        return OrderResult(success=False, error=f"Size {sz} below minimum {min_sz}")

                    logger.info("SPOT order: qty=%.8f (minSz=%.8f, lotSz=%.8f, decimals=%d)",
                               sz, min_sz, lot_sz, decimals)
                else:
                    # No symbol info - use safe defaults
                    decimals = 8
                    sz = round(quantity, decimals)
                    logger.warning("SPOT order without symbol info, using defaults: qty=%.8f, decimals=%d",
                                  sz, decimals)

                # Validate sz is positive after rounding
                if sz <= 0:
                    return OrderResult(success=False, error=f"Order size {sz} is not positive after rounding")

                # Format size string with correct precision (use decimals, not hardcoded 8)
                sz_str = f"{sz:.{decimals}f}".rstrip("0").rstrip(".")
                if not sz_str or sz_str == "0":
                    return OrderResult(success=False, error=f"Order size formatted to invalid value: {sz_str}")

            # OKX order types: market, limit, post_only, fok, ioc
            # post_only = limit order that's cancelled if it would fill immediately (ensures maker)
            if order_type == "MARKET":
                okx_ord_type = "market"
            elif order_type == "POST_ONLY":
                okx_ord_type = "post_only"  # Maker-only limit order
            else:
                okx_ord_type = "limit"

            order_data = {
                "instId": symbol,
                "tdMode": td_mode,
                "side": side.lower(),
                "ordType": okx_ord_type,
                "sz": sz_str,
            }

            if order_type in ("LIMIT", "POST_ONLY") and price:
                # Use price_precision from symbol_info (calculated from original API string)
                if symbol_info:
                    price_decimals = symbol_info.get("price_precision", 2)
                    rounded_price = round(price, price_decimals)
                    px_str = f"{rounded_price:.{price_decimals}f}"
                else:
                    px_str = str(round(price, 2))
                order_data["px"] = px_str

            if reduce_only and inst_type == "SWAP":
                order_data["reduceOnly"] = True

            # For spot market BUY, OKX defaults sz to quote currency (USDT).
            # tgtCcy=base_ccy tells OKX sz is in base currency (BTC) instead.
            # Per OKX docs this applies to SPOT Market Orders in both cash and margin mode.
            if inst_type == "SPOT" and okx_ord_type == "market" and side.upper() == "BUY":
                order_data["tgtCcy"] = "base_ccy"

            # Cross-margin SPOT orders require ccy = margin currency (quote currency).
            # OKX rejects cross-margin spot orders with "Parameter ccy can not be empty"
            # if this is omitted (applies to both BUY and SELL directions).
            if inst_type == "SPOT" and td_mode == "cross":
                symbol_parts = symbol.split("-")
                # BTC-USDT → quote = USDT; guard against malformed symbols
                if len(symbol_parts) >= 2:
                    order_data["ccy"] = symbol_parts[1]

            # Handle position side for long/short mode accounts (required for SWAP)
            if inst_type == "SWAP":
                if pos_side:
                    # Explicit pos_side provided - use it (important for closing positions!)
                    order_data["posSide"] = pos_side
                    logger.info("Using explicit posSide=%s", pos_side)
                elif not reduce_only:
                    # Only auto-detect for NEW positions (entries), not for exits
                    account_config = await self.get_account_config()
                    if account_config and account_config.get("position_mode") == "long_short_mode":
                        # In long/short mode: buy opens long, sell opens short
                        order_data["posSide"] = "long" if side.upper() == "BUY" else "short"
                        logger.info("Account in long_short_mode, auto-setting posSide=%s for entry", order_data["posSide"])

            logger.info("Placing order: %s", order_data)
            result = await self._request("POST", "/api/v5/trade/order", data=order_data)

            if result and result.get("code") == "0" and result.get("data"):
                order_info = result["data"][0]
                order_id = order_info.get("ordId", "")
                logger.info("Order placed successfully: %s %s %s qty=%s, order_id=%s",
                           side, order_type, symbol, sz, order_id)

                # For MARKET orders, assume immediate fill
                # For LIMIT/POST_ONLY orders, return 0 filled until confirmed via get_order_status
                if order_type == "MARKET":
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        filled_qty=quantity,  # Market orders fill immediately
                        filled_price=price or 0,
                    )
                else:
                    # Limit/post_only order - don't assume fill, let caller check status
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        filled_qty=0,  # Not filled yet - must check status
                        filled_price=0,
                    )
            else:
                # Log full error details
                error = result.get("msg", "Unknown error") if result else "No response"
                data_errors = result.get("data", []) if result else []
                if data_errors and isinstance(data_errors, list) and len(data_errors) > 0:
                    sub_error = data_errors[0].get("sMsg", "") or data_errors[0].get("sCode", "")
                    if sub_error:
                        error = f"{error}: {sub_error}"
                logger.error("Order failed: %s | Request: %s | Response: %s",
                            error, order_data, result)
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

    async def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the status of a specific order.

        Returns:
            Dict with order status info including:
            - state: 'live', 'partially_filled', 'filled', 'canceled'
            - filled_qty: Amount filled
            - filled_price: Average fill price
            - remaining_qty: Unfilled amount
        """
        try:
            result = await self._request(
                "GET",
                "/api/v5/trade/order",
                params={"instId": symbol, "ordId": order_id},
            )

            if result and result.get("code") == "0" and result.get("data"):
                o = result["data"][0]
                sz = float(o.get("sz", 0) or 0)
                fill_sz = float(o.get("fillSz", 0) or o.get("accFillSz", 0) or 0)
                fill_px = float(o.get("fillPx", 0) or o.get("avgPx", 0) or 0)
                state = o.get("state", "")

                return {
                    "order_id": order_id,
                    "symbol": symbol,
                    "state": state,  # live, partially_filled, filled, canceled
                    "quantity": sz,
                    "filled_qty": fill_sz,
                    "filled_price": fill_px,
                    "remaining_qty": sz - fill_sz,
                    "side": o.get("side", ""),
                    "order_type": o.get("ordType", ""),
                }
            else:
                # Order might not exist (already cancelled or never placed)
                logger.warning("Could not fetch order status for %s: %s",
                             order_id, result.get("msg") if result else "No response")
                return None

        except Exception as e:
            logger.error("Error fetching order status: %s", e)
            return None

    async def get_pending_orders(self, symbol: Optional[str] = None, inst_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all pending (open) orders.

        Args:
            symbol: Optional specific symbol to filter
            inst_type: Optional instrument type ('SPOT', 'SWAP')

        Returns:
            List of pending orders
        """
        try:
            params: Dict[str, Any] = {}
            if symbol:
                params["instId"] = symbol
            if inst_type:
                params["instType"] = inst_type

            result = await self._request("GET", "/api/v5/trade/orders-pending", params=params)

            orders = []
            if result and result.get("code") == "0" and result.get("data"):
                for o in result["data"]:
                    orders.append({
                        "order_id": o.get("ordId", ""),
                        "symbol": o.get("instId", ""),
                        "side": o.get("side", ""),
                        "order_type": o.get("ordType", ""),
                        "quantity": float(o.get("sz", 0) or 0),
                        "price": float(o.get("px", 0) or 0),
                        "filled_qty": float(o.get("fillSz", 0) or 0),
                        "state": o.get("state", ""),
                        "created_at": o.get("cTime", ""),
                    })
            return orders

        except Exception as e:
            logger.error("Error fetching pending orders: %s", e)
            return []

    async def cancel_all_orders(self, symbol: Optional[str] = None, inst_type: Optional[str] = None) -> int:
        """
        Cancel all pending orders for a symbol or instrument type.

        Returns:
            Number of orders cancelled
        """
        try:
            pending = await self.get_pending_orders(symbol, inst_type)
            cancelled = 0

            for order in pending:
                success = await self.cancel_order(order["symbol"], order["order_id"])
                if success:
                    cancelled += 1
                    logger.info("Cancelled orphan order: %s on %s", order["order_id"], order["symbol"])

            return cancelled

        except Exception as e:
            logger.error("Error cancelling all orders: %s", e)
            return 0

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

    async def get_leverage_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get leverage setting for a specific instrument."""
        try:
            # Determine margin mode from symbol
            mgn_mode = "cross"  # Default to cross margin

            params = {
                "instId": symbol,
                "mgnMode": mgn_mode,
            }

            result = await self._request("GET", "/api/v5/account/leverage-info", params=params)

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                return {
                    "symbol": data.get("instId", symbol),
                    "leverage": float(data.get("lever", 1)),
                    "margin_mode": data.get("mgnMode", "cross"),
                    "pos_side": data.get("posSide", ""),
                }

            return None

        except Exception as e:
            logger.warning("Error fetching leverage info for %s: %s", symbol, e)
            return None

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
        """Get detailed account information including margin requirements."""
        try:
            result = await self._request("GET", "/api/v5/account/balance")

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                total_eq = float(data.get("totalEq", 0))
                imr = float(data.get("imr") or 0)  # Initial margin requirement
                mmr = float(data.get("mmr") or 0)  # Maintenance margin requirement
                upl = float(data.get("upl") or 0)  # Unrealized P&L

                # Available equity from top-level (cross-margin available)
                # This is more accurate than just USDT availBal
                avail_eq = float(data.get("availEq") or 0)

                # Get USDT balance specifically as fallback
                usdt_avail = 0
                usdt_eq = 0
                total_cash_bal = 0
                for detail in data.get("details", []):
                    ccy = detail.get("ccy", "")
                    # Sum up all cash balances for reference
                    cash_bal = float(detail.get("cashBal") or 0)
                    eq = float(detail.get("eq") or 0)
                    total_cash_bal += cash_bal

                    if ccy == "USDT":
                        usdt_avail = float(detail.get("availBal") or 0)
                        usdt_eq = eq

                # Use availEq if available, otherwise fall back to USDT available
                available = avail_eq if avail_eq > 0 else usdt_avail

                # Calculate margin ratio (lower is riskier)
                margin_ratio = 0.0
                if mmr > 0:
                    margin_ratio = (total_eq / mmr) * 100  # As percentage

                logger.debug("Account: totalEq=%.2f, availEq=%.2f, usdt_avail=%.2f, imr=%.2f, upl=%.2f",
                            total_eq, avail_eq, usdt_avail, imr, upl)

                return AccountInfo(
                    exchange="OKX",
                    balance_usd=total_eq,
                    available_balance_usd=available,
                    margin_used=imr,
                    unrealized_pnl=upl,
                    total_equity=total_eq,
                    initial_margin=imr,
                    maintenance_margin=mmr,
                    margin_ratio=margin_ratio,
                    available_margin=available,
                    leverage_used=imr / total_eq if total_eq > 0 else 0,
                )

        except Exception as e:
            logger.exception("Error fetching OKX account info")

        return None

    async def get_position_margin_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get detailed margin info for a specific position."""
        try:
            positions = await self.get_positions(symbol)
            if not positions:
                return None

            pos = positions[0]

            # Get mark price
            mark_result = await self._request(
                "GET",
                "/api/v5/public/mark-price",
                params={"instId": symbol},
            )
            mark_price = None
            if mark_result and mark_result.get("code") == "0" and mark_result.get("data"):
                mark_price = float(mark_result["data"][0].get("markPx", 0))

            # Get position details with margin info
            pos_result = await self._request(
                "GET",
                "/api/v5/account/positions",
                params={"instId": symbol},
            )

            if pos_result and pos_result.get("code") == "0" and pos_result.get("data"):
                p = pos_result["data"][0]

                def safe_float(val, default=0.0):
                    """Convert to float safely, handling empty strings from OKX."""
                    try:
                        return float(val) if val not in (None, '', 'None') else default
                    except (ValueError, TypeError):
                        return default

                liq_px = p.get("liqPx")
                return {
                    "symbol": symbol,
                    "side": pos.side,
                    "quantity": pos.quantity,
                    "entry_price": pos.entry_price,
                    "mark_price": mark_price,
                    "liquidation_price": safe_float(liq_px) if liq_px not in (None, '', 'None') else None,
                    "margin": safe_float(p.get("margin")),
                    "margin_ratio": safe_float(p.get("mgnRatio")) * 100,
                    "unrealized_pnl": pos.unrealized_pnl,
                    "leverage": pos.leverage,
                    "imr": safe_float(p.get("imr")),
                    "mmr": safe_float(p.get("mmr")),
                }

        except Exception as e:
            logger.error("Error fetching position margin info: %s", e)

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
                lot_sz_str = data.get("lotSz", "0.00000001")
                tick_sz_str = data.get("tickSz", "0.01")

                # Calculate decimal precision from string representation
                # e.g., "0.00000001" -> 8 decimals, "0.001" -> 3 decimals
                qty_precision = len(lot_sz_str.split(".")[1]) if "." in lot_sz_str else 0
                price_precision = len(tick_sz_str.split(".")[1]) if "." in tick_sz_str else 0

                return {
                    "symbol": symbol,
                    "min_qty": float(data.get("minSz") or 0),
                    "lot_sz": float(lot_sz_str),  # Minimum order increment
                    "tick_sz": float(tick_sz_str),  # Price tick size
                    "qty_precision": qty_precision,
                    "price_precision": price_precision,
                    "contract_val": float(data.get("ctVal") or 1),
                }

        except Exception as e:
            logger.error("Error fetching OKX symbol info: %s", e)

        return None

    async def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "cross") -> bool:
        """
        Set leverage for a symbol.

        Args:
            symbol: Instrument ID (e.g., BTC-USDT-SWAP)
            leverage: Leverage value (1-125 depending on instrument)
            margin_mode: 'cross' or 'isolated'

        Returns:
            True if successful, False otherwise
        """
        try:
            # Leverage setting is only for derivatives (SWAP/FUTURES), not spot
            if "-SWAP" not in symbol and "-FUTURES" not in symbol:
                logger.debug("Leverage not applicable for spot symbol: %s", symbol)
                return True

            data = {
                "instId": symbol,
                "lever": str(leverage),
                "mgnMode": margin_mode,
            }

            result = await self._request("POST", "/api/v5/account/set-leverage", data=data)

            if result and result.get("code") == "0":
                logger.info("Leverage set to %dx for %s (mode=%s)", leverage, symbol, margin_mode)
                return True
            else:
                error = result.get("msg", "Unknown error") if result else "No response"
                logger.error("Failed to set leverage for %s: %s", symbol, error)
                return False

        except Exception as e:
            logger.exception("Error setting leverage for %s", symbol)
            return False

    async def get_leverage(self, symbol: str) -> Optional[int]:
        """
        Get current leverage setting for a symbol.

        Args:
            symbol: Instrument ID (e.g., BTC-USDT-SWAP)

        Returns:
            Current leverage value or None if error
        """
        try:
            if "-SWAP" not in symbol and "-FUTURES" not in symbol:
                return 1  # Spot doesn't have leverage

            result = await self._request(
                "GET",
                "/api/v5/account/leverage-info",
                params={"instId": symbol, "mgnMode": "cross"},
            )

            if result and result.get("code") == "0" and result.get("data"):
                return int(float(result["data"][0].get("lever", 1)))

        except Exception as e:
            logger.error("Error getting leverage for %s: %s", symbol, e)

        return None

    async def get_order_history(self, symbol: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Fetch recent order history from OKX.

        Returns filled and cancelled orders for both SPOT and SWAP.
        """
        orders = []

        inst_types = ["SPOT", "SWAP"]
        for inst_type in inst_types:
            params: Dict[str, Any] = {"instType": inst_type, "limit": str(limit)}
            if symbol:
                params["instId"] = symbol

            result = await self._request("GET", "/api/v5/trade/orders-history", params=params)

            if result and result.get("code") == "0" and result.get("data"):
                for o in result["data"]:
                    try:
                        fee = o.get("fee", "0") or "0"
                        fill_px = o.get("fillPx", "") or o.get("avgPx", "") or "0"
                        fill_sz = o.get("fillSz", "") or o.get("accFillSz", "") or "0"
                        orders.append({
                            "order_id":    o.get("ordId", ""),
                            "symbol":      o.get("instId", ""),
                            "inst_type":   inst_type,
                            "side":        o.get("side", ""),       # buy / sell
                            "pos_side":    o.get("posSide", ""),    # long / short / net
                            "order_type":  o.get("ordType", ""),    # market / limit
                            "state":       o.get("state", ""),      # filled / cancelled / live
                            "quantity":    float(o.get("sz", 0) or 0),
                            "fill_qty":    float(fill_sz),
                            "fill_price":  float(fill_px),
                            "fee":         float(fee),
                            "fee_ccy":     o.get("feeCcy", ""),
                            "leverage":    o.get("lever", ""),
                            "pnl":         float(o.get("pnl", 0) or 0),
                            "created_at":  o.get("cTime", ""),
                            "filled_at":   o.get("uTime", ""),
                            "td_mode":     o.get("tdMode", ""),     # cash / cross / isolated
                        })
                    except (ValueError, TypeError) as e:
                        logger.debug("Skipping order record due to parse error: %s", e)
                        continue

        # Sort by creation time descending
        orders.sort(key=lambda x: x["created_at"], reverse=True)
        return orders[:limit]

    async def get_account_config(self) -> Optional[Dict[str, Any]]:
        """
        Get account configuration including UID.

        Returns:
            Dict with uid, account_level, position_mode, etc.
        """
        try:
            result = await self._request("GET", "/api/v5/account/config")

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                uid = data.get("uid", "")
                level = data.get("level", "")
                logger.debug("Account config fetched: UID=%s, Level=%s", uid, level)
                return {
                    "uid": uid,
                    "account_level": data.get("acctLv", ""),  # 1=Simple, 2=Single-currency margin, etc.
                    "position_mode": data.get("posMode", ""),  # long_short_mode or net_mode
                    "auto_loan": data.get("autoLoan", False),
                    "greeks_type": data.get("greeksType", ""),
                    "level": level,  # User level (VIP tier)
                    "level_tmp": data.get("levelTmp", ""),  # Temporary VIP level
                }
            else:
                error_msg = result.get("msg", "Unknown error") if result else "No response"
                logger.warning("Failed to fetch account config: %s", error_msg)

        except Exception as e:
            logger.error("Error fetching account config: %s", e)

        return None

    async def get_spot_balances(self, include_frozen: bool = True) -> Dict[str, Dict[str, float]]:
        """
        Get all spot balances from trading account.

        Returns:
            Dict mapping currency to balance details:
            {'BTC': {'available': 0.1, 'frozen': 0.15, 'total': 0.25, 'equity': 0.25}}
        """
        balances = {}
        try:
            result = await self._request("GET", "/api/v5/account/balance")

            if result and result.get("code") == "0" and result.get("data"):
                data = result["data"][0]
                for detail in data.get("details", []):
                    ccy = detail.get("ccy", "")
                    avail = float(detail.get("availBal", 0) or 0)
                    frozen = float(detail.get("frozenBal", 0) or 0)
                    cash_bal = float(detail.get("cashBal", 0) or 0)
                    eq = float(detail.get("eq", 0) or 0)

                    # Include if there's any balance (available or frozen)
                    total = avail + frozen
                    if total > 0.00000001 or cash_bal > 0.00000001:  # Filter out true dust
                        balances[ccy] = {
                            'available': avail,
                            'frozen': frozen,
                            'total': cash_bal if cash_bal > 0 else total,
                            'equity': eq,
                        }
                        if ccy not in ('USDT', 'USDC'):
                            logger.debug("Balance %s: avail=%.8f, frozen=%.8f, total=%.8f, eq=%.8f",
                                        ccy, avail, frozen, cash_bal, eq)

        except Exception as e:
            logger.error("Error fetching spot balances: %s", e)

        return balances

    async def get_asset_balance(self, currency: str) -> Dict[str, float]:
        """
        Get balance info for a specific currency.

        Args:
            currency: Currency code (e.g., 'BTC', 'USDT')

        Returns:
            Dict with 'available', 'frozen', 'total', 'equity'
        """
        balances = await self.get_spot_balances()
        return balances.get(currency, {'available': 0, 'frozen': 0, 'total': 0, 'equity': 0})

    async def sell_spot_to_usdt(self, currency: str, quantity: float = None) -> OrderResult:
        """
        Sell a spot asset to USDT.

        Args:
            currency: Currency to sell (e.g., 'BTC')
            quantity: Amount to sell (if None, sells entire available balance)

        Returns:
            OrderResult with success/failure info
        """
        try:
            # Get current balance if quantity not specified
            bal_info = await self.get_asset_balance(currency)
            available = bal_info.get('available', 0)
            frozen = bal_info.get('frozen', 0)
            total = bal_info.get('total', 0)

            if quantity is None:
                quantity = available

            if quantity <= 0.00000001:  # Essentially zero
                if frozen > 0.00000001:
                    return OrderResult(
                        success=False,
                        error=f"No available {currency} to sell. {frozen:.6f} is frozen (used as margin). Close positions first."
                    )
                return OrderResult(success=True, error=f"No {currency} balance to sell")

            symbol = f"{currency}-USDT"

            # Get symbol info for precision
            symbol_info = await self.get_symbol_info(symbol)
            min_qty = symbol_info.get("min_qty", 0) if symbol_info else 0

            if quantity < min_qty:
                return OrderResult(success=False, error=f"Quantity {quantity} below minimum {min_qty}")

            # Place market sell order
            result = await self.place_order(
                symbol=symbol,
                side="SELL",
                order_type="MARKET",
                quantity=quantity,
            )

            if result.success:
                logger.info("Sold %.6f %s to USDT, order_id=%s", quantity, currency, result.order_id)
            else:
                logger.error("Failed to sell %s: %s", currency, result.error)

            return result

        except Exception as e:
            logger.exception("Error selling %s to USDT", currency)
            return OrderResult(success=False, error=str(e))
