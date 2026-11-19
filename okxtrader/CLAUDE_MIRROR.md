# Working on MT5-Trader

The build specification is held privately, outside this repository — get
it from the repository owner and read it first. Most of it is a rule that
was paid for on a live account in the stat-arb system this code is ported
from.

## Hard rules

- **`pytest tests/ -q` must pass before any commit**, and LIVE mode must
  never be run without it.
- **The spread is `Leg B - beta x Leg A`**, built from the MID OF THE
  BOOK, never `tick.last`. Levels and triggers read the EXECUTABLE side
  for their own direction; a position reads the OPPOSITE executable side
  to close.
- **`L_B = L_A x C_A / (beta x C_B)`**, and `k = L_B x C_B` is the one
  multiplier every spread-to-money conversion uses.
- **Closes target position TICKETS.** These accounts are hedging mode;
  an opposite market order opens a SECOND position.
- **Never attach broker-side stops to individual legs.** One leg
  stopping alone converts the hedge into a naked position.
- **Credentials live only in `.env`** — never in code, config, chat or a
  log line.
- **Sweep our pendings at shutdown AND at startup**, magic-scoped.
- **The book is persisted and recovered.** An empty book at startup
  makes every live position look like an orphan; the reconciler
  auto-closes nothing until recovery says the book is complete, and
  never touches a position it cannot explain.
- **Automatic orders come ONLY from a ladder's Algo in LIVE.** LIVE is
  per ladder, confirmed every time it is switched on, and OFF after
  every restart; Signals (DRY_RUN) signals and sends nothing - the trader trades. While a ladder is
  LIVE it takes NO new manual orders (closes still work), and LIVE is
  refused on a ladder holding a manual position or a working order.
  The Algo trades through the same executor a click uses: MARKET both
  legs in, closes by TICKET. It never enters while it is collecting
  candles, or past a filter it has switched on (edge, regime,
  probability, half-life) — and a filter it cannot price BLOCKS. `algo.py`, `bands.py`, `algofilters.py`,
  `algodesk.py`, `backtest.py`, `analysis.py` and `shadow.py` (the Algo's own code, run for the signal journal) decide and must not reach an order; `algoexec.py` is the one module
  that sends, and only via the coordinator — `tests/test_algo.py`
  fails the build otherwise. Nothing else places, modifies or cancels
  an order by itself.

- **Telegram is a remote desk, not a trader.** `telegram.py` goes
  through the web app's own routes; only ids in
  `TELEGRAM_ALLOWED_USERS` get an answer; every action is confirmed
  (LIVE twice); it may send `set_algo`, `set_pair`, `flatten_pair` and
  `kill` and nothing else — it never opens a position. The token lives
  in `.env` and no URL carrying it is ever logged.

## Conventions that are easy to lose in a refactor

- `positions()` and `pending_orders()` return **None for "unknown"**
  (the leg could not be read), which is NOT "flat"/"no orders". Code
  that treats None as empty will sweep a live account clean in its own
  report while the money sits at the broker.
- **Unmeasured is not zero.** Return None and render "—".
- Guards may withhold an ORDER. **A guard must never prevent a close.**
  The one agreed exception: the Algo's TAKE-PROFIT is confirmed on
  prices read again before it is sent (`RECHECK_TAKE_PROFIT`) - a
  target is an opportunity, not a risk, and the stop stands meanwhile.
  Stops, the cutoff, kills and manual closes are never re-checked.
- A refusal carries the broker's own words (`10027 AutoTrading disabled
  by client`), never "check the log".
- `okxtrader/broker.py` is the only module allowed to import
  MetaTrader5.
- Every test that asserts a guard withholds something needs a
  **control** that turns the guard off and asserts the opposite.
