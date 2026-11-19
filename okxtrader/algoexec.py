"""The LIVE sink: the one place an Algo intent becomes an order.

Everything upstream of this decides and records (`algo`, `bands`,
`algodesk`); a test reads those as code and fails the build if any of
them can reach an order. This module is the exception, and it is small
on purpose: it does not size, price or guard anything itself. It hands
an intent to the coordinator, which sends it down the SAME path a
manual click takes —

- **enter**: MARKET on both legs through `PairExecutor.market_entry`,
  with its 2.0s escalation and its unwind of a leg that went on alone;
  the executable price the decision was made at is the slippage guard,
  exactly as a clicked price is for a trader;
- **exit**: `PairExecutor.close_position` — both legs, by TICKET. Never
  an opposite order: these are hedging accounts, and an opposite order
  opens a second position.

It only ever runs while a ladder's Algo is LIVE, which is confirmed
every time it is switched on and is off after every restart.
"""


class LiveSink:
    """Sends the Algo's intents through the coordinator."""

    def __init__(self, coordinator):
        self.coordinator = coordinator

    def enter(self, pair, side, quantity):
        """Open `quantity` spreads on `side`. Returns {'ok', 'reason',
        'position_id'} — the broker's own words when it refuses."""
        return self.coordinator.algo_enter(pair, side, quantity)

    def leave(self, pair, position_id, reason):
        """Close one position, both legs, by ticket."""
        return self.coordinator.algo_exit(pair, position_id, reason)
