"""The spread ladder for spread = k × A − B, with sizes derived from both books.

A spread has no order book of its own, so every size on the ladder is
DERIVED from the two legs' depth (five levels a side). The merge computes
``near − beta × far``; with leg A's prices pre-scaled by the hedge ratio k
and beta = 1 it computes exactly this system's spread:

    BUY the spread  (buy A, sell B): k × ask_A − bid_B   → the ASKS column
    SELL the spread (sell A, buy B): k × bid_A − ask_B   → the BIDS column

so the best ask row is the BUY spread and the best bid row the SELL spread
shown on Signal & Position. A leg without a book gives NO size (None),
never a size borrowed from the other leg. UNMEASURED IS NOT ZERO: an
invented size is a size a trader clicks on, and a click that finds a tenth
of the quantity it was shown is a half-hedged spread.

One "clip" is one Qty on the ladder: ``units_a`` of leg A against
``units_b`` of leg B (for BTC spot vs perp: the configured BTC qty per
trade on each side).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

#: Sizes below this are noise from floating-point division, not depth.
_EPSILON = 1e-9


def synthetic_book(near, far, beta, units_near, units_far, sign=1):
    """Merge two legs' books into one side of the spread's book.

    ``near``/``far`` are lists of {'price', 'volume'} ordered best-first.
    Returns [(spread_price, clips), …] in fill order, or None where either
    book or either unit size is unknown. The pairing walks both books from
    their touches outward, always consuming the level that runs out first —
    which both fills the maximum quantity and produces it cheapest-first."""
    if not near or not far or not units_near or not units_far:
        # UNMEASURED IS NOT ZERO. No book, or no unit size, means the size
        # is unknown — never that there is none.
        return None
    capacity_near = [level["volume"] / float(units_near) for level in near]
    capacity_far = [level["volume"] / float(units_far) for level in far]
    out, i, j = [], 0, 0
    while i < len(near) and j < len(far):
        take = min(capacity_near[i], capacity_far[j])
        if take > _EPSILON:
            out.append((near[i]["price"] - beta * far[j]["price"], take))
        capacity_near[i] -= take
        capacity_far[j] -= take
        if capacity_near[i] <= _EPSILON:
            i += 1
        if j < len(far) and capacity_far[j] <= _EPSILON:
            j += 1
    del sign
    return out


def _grid(level, base, increment, side):
    """The ladder row a fillable price belongs on. A price you can BUY at
    rounds UP to the next row (the row says "at this price or better");
    selling rounds down, for the same reason in the other direction."""
    steps = (level - base) / increment
    return base + increment * (math.ceil(steps - 1e-9) if side == "ask"
                               else math.floor(steps + 1e-9))


def implied_sizes(entries, levels, increment, side):
    """{level: clips} for one side, from a merged book. Entries are
    cumulative by construction, bucketed onto the grid so each row carries
    what IT adds — the rows then sum to the total. Size at a price the
    ladder does not show is DROPPED, never folded onto the edge row."""
    if entries is None:
        return {}
    on_grid, base = {}, levels[0] if levels else 0.0
    for price, clips in entries:
        row = _grid(price, base, increment, side)
        on_grid[row] = on_grid.get(row, 0.0) + clips
    if levels:
        visible = set(levels)
        on_grid = {row: clips for row, clips in on_grid.items() if row in visible}
    return on_grid


def _clips(value):
    """Whole clips, floored — rounding a part of a clip up would advertise
    size that is not there. Fractional-qty markets (BTC) still get a whole
    number of configured clips."""
    if value is None:
        return None
    whole = int(value + _EPSILON)
    return whole or None


def _scaled(depth: Optional[List[Dict]], side: str, k: float) -> Optional[List[Dict]]:
    if not depth:
        return None
    rows = [dict(lv, price=lv["price"] * k) for lv in depth if lv.get("type") == side]
    return rows or None


def _only(depth: Optional[List[Dict]], side: str) -> Optional[List[Dict]]:
    if not depth:
        return None
    rows = [lv for lv in depth if lv.get("type") == side]
    return rows or None


def build(depth_a: Optional[List[Dict]], depth_b: Optional[List[Dict]], k: float,
          units_a: float, units_b: float, sell_spread: Optional[float],
          buy_spread: Optional[float], increment: float, count: int = 21,
          anchor: Optional[float] = None) -> List[Dict]:
    """Ladder rows, HIGHEST PRICE FIRST. Empty when the spread cannot be priced.

    ``units_a`` / ``units_b`` are units per clip (BTC per trade on each leg).
    ``anchor`` pins the grid (e.g. the entry spread) so rows do not renumber
    on every tick; by default it centres on the mid of the two executable
    spreads, snapped to a multiple of the increment."""
    if sell_spread is None or buy_spread is None or not increment or increment <= 0:
        return []
    mid = (sell_spread + buy_spread) / 2.0
    centre = anchor if anchor is not None else mid
    base = round(centre / increment) * increment
    half = count // 2
    levels = [round(base + increment * step, 10) for step in range(half, half - count, -1)]

    k = float(k or 1.0)
    # BUY: lift A's asks (×k), hit B's bids.  SELL: hit A's bids, lift B's asks.
    asks = implied_sizes(synthetic_book(_scaled(depth_a, "ask", k), _only(depth_b, "bid"),
                                        1.0, units_a, units_b, sign=1),
                         levels, increment, "ask")
    bids = implied_sizes(synthetic_book(_scaled(depth_a, "bid", k), _only(depth_b, "ask"),
                                        1.0, units_a, units_b, sign=-1),
                         levels, increment, "bid")
    priced = bool(asks) or bool(bids)

    def _row_of(price, side):
        steps = (price - levels[-1]) / increment
        r = levels[-1] + increment * (math.ceil(steps - 1e-9) if side == "ask"
                                      else math.floor(steps + 1e-9))
        return round(r, 10)

    best_ask = _row_of(buy_spread, "ask")
    best_bid = _row_of(sell_spread, "bid")
    mid_row = min(levels, key=lambda lv: abs(lv - mid))
    out = []
    for lv in levels:
        out.append({
            "level": lv,
            "is_mid": lv == mid_row,
            "is_best_ask": lv == best_ask,      # BUY the spread here
            "is_best_bid": lv == best_bid,      # SELL the spread here
            # None, NOT zero, where no book could be derived.
            "ask_size": _clips(asks.get(lv)) if priced else None,
            "bid_size": _clips(bids.get(lv)) if priced else None,
        })
    return out
