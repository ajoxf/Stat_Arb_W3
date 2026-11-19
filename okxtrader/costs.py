"""What a round trip actually costs, per leg — and what is left to
charge once a position is marked at the price it would close at.

The split is the whole point of this module (spec §5):

- **crossing** is the bid-ask both legs pay, in at one side and out at
  the other. A position entered at its real fill and marked at the
  exit-side touch has ALREADY paid all of it — it is in the two prices.
  Subtracting it again from that mark charges the spread twice.
- **commission** is the broker's, per lot per leg, and is never in a
  price. It is what remains to subtract.

`spread_cost x k` (from spread.py) and `crossing_cost` here are two
views of ONE quantity. Use the executable spreads to decide whether a
level has been reached; never add them on top of the cost model.
"""


def cost_parts(md, lots_a, contract_a, lots_b, contract_b, costs_cfg):
    """(crossing, commission) in dollars, each leg in its OWN units.

    Pricing both legs' bid-asks off leg A's units is exact only when the
    legs match in lots and contract size. On XAGUSD/XAUUSD the stat-arb
    engine charged gold's 0.24 spread against SILVER's 5,000 units —
    $1,200 for a leg whose real cost is $28.
    """
    costs_cfg = costs_cfg or {}
    units_a = (lots_a or 0.0) * (contract_a or 0.0)
    units_b = (lots_b or 0.0) * (contract_b or 0.0)
    width_a = (md.get('leg_a_width') or 0.0)
    width_b = (md.get('leg_b_width') or 0.0)
    factor = costs_cfg.get('SPREAD_COST_FACTOR', 1.0)
    crossing = (width_a * units_a + width_b * units_b) * factor
    commission = (costs_cfg.get('COMMISSION_PER_LOT_A', 0.0) * (lots_a or 0.0)
                  + costs_cfg.get('COMMISSION_PER_LOT_B', 0.0)
                  * (lots_b or 0.0))
    return crossing, commission


def round_trip_cost(md, lots_a, contract_a, lots_b, contract_b, costs_cfg):
    """Dollars to open AND close both legs — crossing plus commission."""
    return sum(cost_parts(md, lots_a, contract_a, lots_b, contract_b,
                          costs_cfg))


def mark_fees(lots_a, lots_b, costs_cfg):
    """The only fee still outstanding on a position marked at the
    CLOSING touch: commission, both legs, both ends.

    The crossing is already in the two prices. This is the number
    subtracted from `SpreadPosition.mark()` to get net P&L, and it is
    what EXIT_IF_PROFIT reads (spec §3.2).
    """
    costs_cfg = costs_cfg or {}
    per_round_trip = 2.0
    return per_round_trip * (
        costs_cfg.get('COMMISSION_PER_LOT_A', 0.0) * (lots_a or 0.0)
        + costs_cfg.get('COMMISSION_PER_LOT_B', 0.0) * (lots_b or 0.0))
