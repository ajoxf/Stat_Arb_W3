"""
core/reconcile.py

Reusable reconciliation logic — matches OKX-side orders against the bot's
trade table to surface gaps (orphan auto-closes, recovered entries that
didn't persist, manual closes).

Used by both:
- scripts/reconcile_okx.py (CLI, reads from OKX Order History CSV)
- /api/reconcile (web endpoint, reads from OKX REST adapter live)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


# ────────────────────────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────────────────────────

@dataclass
class OKXOrder:
    """One completed order on OKX, normalized across CSV / REST sources."""
    order_id: str
    order_time: datetime
    symbol: str             # ETH-USDT-26JUN26 etc.
    side: str               # "Open long" | "Open short" | "Close long" | "Close short"
    order_type: str         # "LIMIT" | "MARKET" | "POST_ONLY"
    filled_qty: float       # in CONTRACTS (OKX-native); convert via ct_val for base units
    avg_fill_price: float
    pnl: float
    fee: float
    status: str             # "COMPLETE" | "CANCELED" | ...
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.side.lower().startswith("open")

    @property
    def is_close(self) -> bool:
        return self.side.lower().startswith("close")

    @property
    def is_market(self) -> bool:
        return self.order_type.upper() == "MARKET"

    @property
    def base_asset(self) -> str:
        return self.symbol.split("-")[0]


@dataclass
class DBTrade:
    """One row from the bot's trades table."""
    id: int
    position_type: str       # LONG | SHORT
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]
    entry_spot_price: float
    entry_futures_price: float
    exit_spot_price: float
    exit_futures_price: float
    quantity: float
    pnl_usd: float
    is_open: bool


@dataclass
class MatchResult:
    db_trade: Optional[DBTrade]
    okx_open_eth: Optional[OKXOrder]
    okx_open_btc: Optional[OKXOrder]
    okx_close_eth: Optional[OKXOrder]
    okx_close_btc: Optional[OKXOrder]

    @property
    def is_full_match(self) -> bool:
        return all([
            self.db_trade, self.okx_open_eth, self.okx_open_btc,
            self.okx_close_eth, self.okx_close_btc,
        ])


# ────────────────────────────────────────────────────────────────────────
# Converters — bring foreign formats into OKXOrder
# ────────────────────────────────────────────────────────────────────────

def from_okx_rest_dict(d: Dict[str, Any]) -> Optional[OKXOrder]:
    """Convert one entry from OKXAdapter.get_order_history() into an
    OKXOrder. Returns None for rows we don't reconcile (cancelled, spot
    conversions, unfilled).
    """
    state = (d.get("state") or "").lower()
    if state != "filled":
        return None  # only consider filled orders

    side = (d.get("side") or "").lower()           # buy | sell
    pos_side = (d.get("pos_side") or "").lower()   # long | short | net
    order_type = (d.get("order_type") or "").upper()
    inst_type = (d.get("inst_type") or "").upper()
    symbol = d.get("symbol") or ""
    if "CONVERT" in symbol.upper():
        return None

    # Derive the human-readable side from buy/sell + posSide. For
    # net-mode SPOT orders (no posSide) we infer by Side alone: buy=Open,
    # sell=Close (best-effort; net-mode is rarely used for stat-arb here).
    if pos_side == "long":
        side_h = "Open long" if side == "buy" else "Close long"
    elif pos_side == "short":
        side_h = "Open short" if side == "sell" else "Close short"
    else:
        side_h = ("Open long" if side == "buy" else "Close long")

    # filled_at: prefer the REST 'filled_at' / 'updated_at' fields
    ts_ms = (d.get("filled_at") or d.get("updated_at")
             or d.get("created_at") or 0)
    try:
        ts = datetime.utcfromtimestamp(int(ts_ms) / 1000)
    except (TypeError, ValueError):
        ts = datetime.utcnow()

    ct_val = float(d.get("ct_val") or 1.0) or 1.0
    fill_contracts = float(d.get("fill_contracts") or d.get("fill_qty") or 0)
    fill_qty_base = fill_contracts * ct_val if inst_type in ("SWAP", "FUTURES") else float(d.get("fill_qty") or 0)

    return OKXOrder(
        order_id=str(d.get("order_id") or ""),
        order_time=ts,
        symbol=symbol,
        side=side_h,
        order_type=order_type,
        filled_qty=fill_qty_base,
        avg_fill_price=float(d.get("fill_price") or 0),
        pnl=float(d.get("pnl") or 0),
        fee=float(d.get("fee") or 0),
        status="COMPLETE",
        raw=d,
    )


# ────────────────────────────────────────────────────────────────────────
# Matching
# ────────────────────────────────────────────────────────────────────────

def _within_window(t1: datetime, t2: datetime, window_sec: int) -> bool:
    return abs((t1 - t2).total_seconds()) <= window_sec


def _find_okx_match(
    orders: List[OKXOrder],
    target_time: datetime,
    target_price: float,
    side_prefix: str,
    base_asset: str,
    window_sec: int,
    price_tol_bps: float,
    consumed: set,
) -> Optional[OKXOrder]:
    best: Optional[OKXOrder] = None
    best_score = float("inf")
    for o in orders:
        if id(o) in consumed:
            continue
        if not o.side.lower().startswith(side_prefix.lower()):
            continue
        if o.base_asset != base_asset:
            continue
        if not _within_window(o.order_time, target_time, window_sec):
            continue
        if target_price > 0 and o.avg_fill_price > 0:
            px_diff_bps = abs(o.avg_fill_price - target_price) / target_price * 10_000
            if px_diff_bps > price_tol_bps:
                continue
            score = px_diff_bps + abs((o.order_time - target_time).total_seconds()) / 60.0
        else:
            score = abs((o.order_time - target_time).total_seconds())
        if score < best_score:
            best_score = score
            best = o
    return best


def reconcile(
    okx_orders: List[OKXOrder],
    db_trades: List[DBTrade],
    window_sec: int = 600,
    price_tol_bps: float = 10.0,
) -> Tuple[List[MatchResult], List[OKXOrder]]:
    """Returns (matches, unmatched_okx_orders)."""
    matches: List[MatchResult] = []
    consumed: set = set()

    for trade in db_trades:
        if trade.is_open or not trade.entry_time:
            continue
        eth_open = _find_okx_match(
            okx_orders, trade.entry_time, trade.entry_spot_price,
            "Open", "ETH", window_sec, price_tol_bps, consumed,
        )
        btc_open = _find_okx_match(
            okx_orders, trade.entry_time, trade.entry_futures_price,
            "Open", "BTC", window_sec, price_tol_bps, consumed,
        )
        eth_close = _find_okx_match(
            okx_orders, trade.exit_time or trade.entry_time, trade.exit_spot_price,
            "Close", "ETH", window_sec, price_tol_bps, consumed,
        )
        btc_close = _find_okx_match(
            okx_orders, trade.exit_time or trade.entry_time, trade.exit_futures_price,
            "Close", "BTC", window_sec, price_tol_bps, consumed,
        )
        for o in (eth_open, btc_open, eth_close, btc_close):
            if o is not None:
                consumed.add(id(o))
        matches.append(MatchResult(
            db_trade=trade,
            okx_open_eth=eth_open,
            okx_open_btc=btc_open,
            okx_close_eth=eth_close,
            okx_close_btc=btc_close,
        ))

    unmatched = [o for o in okx_orders if id(o) not in consumed]
    return matches, unmatched


# ────────────────────────────────────────────────────────────────────────
# JSON-friendly summary (for the web endpoint)
# ────────────────────────────────────────────────────────────────────────

def _classify_unmatched(orders: List[OKXOrder]) -> Dict[str, List[OKXOrder]]:
    groups: Dict[str, List[OKXOrder]] = {
        "market_orphan_close": [],
        "limit_no_db_record": [],
        "market_no_db_record": [],
    }
    for o in orders:
        if o.is_market and o.is_close:
            groups["market_orphan_close"].append(o)
        elif o.is_market:
            groups["market_no_db_record"].append(o)
        else:
            groups["limit_no_db_record"].append(o)
    return groups


def _okx_to_dict(o: OKXOrder) -> Dict[str, Any]:
    return {
        "order_id": o.order_id,
        "order_time": o.order_time.isoformat(),
        "symbol": o.symbol,
        "side": o.side,
        "order_type": o.order_type,
        "filled_qty": o.filled_qty,
        "avg_fill_price": o.avg_fill_price,
        "pnl": o.pnl,
        "fee": o.fee,
        "status": o.status,
    }


def build_json_report(
    matches: List[MatchResult],
    unmatched: List[OKXOrder],
) -> Dict[str, Any]:
    """Build a JSON-serialisable summary for the web endpoint."""
    fully = [m for m in matches if m.is_full_match]
    partial = [m for m in matches if not m.is_full_match]

    partial_rows: List[Dict[str, Any]] = []
    for m in partial:
        t = m.db_trade
        missing = []
        if m.okx_open_eth is None:  missing.append("open-ETH")
        if m.okx_open_btc is None:  missing.append("open-BTC")
        if m.okx_close_eth is None: missing.append("close-ETH")
        if m.okx_close_btc is None: missing.append("close-BTC")
        partial_rows.append({
            "trade_id": t.id,
            "position_type": t.position_type,
            "entry_time": t.entry_time.isoformat() if t.entry_time else None,
            "exit_time": t.exit_time.isoformat() if t.exit_time else None,
            "pnl_usd": t.pnl_usd,
            "missing": missing,
        })

    price_mismatches: List[Dict[str, Any]] = []
    for m in fully:
        t = m.db_trade
        for label, db_p, okx in (
            ("entry-ETH", t.entry_spot_price, m.okx_open_eth),
            ("entry-BTC", t.entry_futures_price, m.okx_open_btc),
            ("exit-ETH",  t.exit_spot_price, m.okx_close_eth),
            ("exit-BTC",  t.exit_futures_price, m.okx_close_btc),
        ):
            if db_p == 0 or not okx or okx.avg_fill_price == 0:
                continue
            diff_bps = abs(okx.avg_fill_price - db_p) / db_p * 10_000
            if diff_bps > 1.0:
                price_mismatches.append({
                    "trade_id": t.id,
                    "leg": label,
                    "db_price": db_p,
                    "okx_price": okx.avg_fill_price,
                    "diff_bps": round(diff_bps, 2),
                })

    groups = _classify_unmatched(unmatched)
    unmatched_groups = {}
    for key, group in groups.items():
        if not group:
            continue
        unmatched_groups[key] = {
            "count": len(group),
            "total_pnl": round(sum(o.pnl for o in group), 4),
            "total_fees": round(sum(o.fee for o in group), 4),
            "orders": [_okx_to_dict(o) for o in group],
        }

    issues = len(partial_rows) + len(price_mismatches) + len(unmatched)

    return {
        "summary": {
            "db_trades_checked": len(matches),
            "fully_matched": len(fully),
            "partial_or_missing": len(partial_rows),
            "price_mismatches": len(price_mismatches),
            "unmatched_okx_orders": len(unmatched),
            "issues_total": issues,
        },
        "partial_db_trades": partial_rows,
        "price_mismatches": price_mismatches,
        "unmatched_okx": unmatched_groups,
    }
