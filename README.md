# Nexus — the OKX spread desk

A spread-trading desk for OKX: price ladders on the spread between two
OKX instruments, an Algo that signals or trades them, and a Trading
Monitor for what is on. It is built on the Mirror MT5 Trader desk (its
engine, ladders, windows and theme) with an OKX broker underneath, and
it fills the way Stat_Arb_W3 did.

## Start it

```
py -3.11 start.py            # Windows
python3 start.py             # macOS / Linux
```

or double-click `START-TRADING.bat` on Windows. That installs the
dependencies, runs the safety tests (and refuses to start if one fails)
and then runs `start.py`.

`start.py` is the only file to run. On the first run it creates
`config.json` and `.env` from the examples, opens the screen at
http://127.0.0.1:8000, and starts the engine. When you save an account
or a pair, it restarts the engine by itself.

## Set up

1. Create an API key on OKX: Profile > API > Create API key, with
   **Read** and **Trade** permission, never Withdraw. Start with a key
   made under **Demo trading**.
2. On the desk, press **Exchanges**. On the `okx` row choose Mode
   (Demo trading or LIVE), paste the key, secret and passphrase, and
   Save. They go to `.env` only, never to `config.json`. An existing
   Stat_Arb_W3 `.env` (`OKX_API_KEY`, `OKX_SECRET_KEY`,
   `OKX_PASSPHRASE`) is read as it is.
3. Press **Test**. It checks, in OKX's own error codes:
   - OKX answers, and this PC's clock is close to OKX's.
   - The key is accepted, and for the right environment (demo or live).
   - The key has Trade permission.
   - The account is in **Long/short** position mode.
   - The account mode can trade perps and short spot.

   It also shows your fee tier and equity.
4. Add pairs under **Pairs** > New pair. Search instruments across spot,
   perpetuals and futures. **Read both legs from OKX** takes contract
   sizes, ticks and minimums from OKX, sets the pair type and suggests β.

## Check it against your OKX demo account

Before trading, run the end-to-end check on a **Demo trading** key:

```
python tools/okx_demo_check.py
```

It uses the account saved on the Exchanges page, places the smallest
orders OKX accepts and takes them off again. It checks:

- the account itself;
- OKX's live WebSocket price feed;
- an order on and off by ticket, and that a second close sends nothing;
- post-only orders: refused when crossing, resting, amended, cancelled;
- a full W3 maker spread fill and maker close;
- a spot buy and close;
- the fills journal;
- that nothing of the check is left on OKX.

It refuses a LIVE account. Stop the desk while it runs. It writes
`okx_demo_check_report.txt` beside config.json, with no keys in it.

## What is on the screen

- **Ladders.** One per pair; the spread is `B − β × A`. H → L is where
  the spread can be sold (a short profits as it falls), and L → H is
  where it can be bought.
- **Algo window.** Signals or LIVE per ladder: z-score, bands,
  filters, TP/SL in price and dollars, and a backtest.
- **Fair spread and Exit.** Round trip (both legs' bid-ask),
  commission at your VIP maker/taker fees, funding over the days held,
  B/E and TP for each direction, and the carry-implied fair spread for
  a perp against a dated future.
- **Trading Monitor.** Positions, Working Orders (including maker fills
  in progress), Fills, Slippage, Accounts (OKX equity, margin ratio and
  each position's liquidation price), Reconciler and Analysis.
- **Market Sessions.** The equity centres that move crypto, and OKX
  funding at 00:00, 08:00 and 16:00 UTC, with a pop-up 10 minutes
  before each.
- **Telegram** (optional; `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_ALLOWED_USERS` in `.env`). Dashboard, balance with
  liquidation distances, Algo settings, and alerts for trades, failures,
  funding and liquidation risk.

## How orders fill

By default (`FILL_STYLE = maker`) a spread fills the Stat_Arb_W3 way:

1. Both legs rest as limit orders at the touch, 1 bp better than the
   bid or ask, never within 0.5 bp of the other side.
2. Every second, an order is re-priced (amended) if its price has moved
   more than 1 bp.
3. After 30 s with nothing filled, both are cancelled and nothing is
   opened.
4. If one leg filled and the other did not, the missing leg is
   recovered as maker for up to 60 s, then the filled leg is closed at
   market.

Your Close button and Algo exits on profit close the same way. Anything
still open after 30 s closes at market. CLOSE ALL, Flatten, the kill
switch and every stop close at market immediately. `FILL_STYLE = taker`
crosses both legs at market instead.

Crypto trades 24/7: there is no daily cutoff unless a ladder sets its
own hours (`MARKET_HOURS = SESSION` restores a single desk-wide
cutoff).

## Files

```
start.py              the launcher - the only thing to run
START-TRADING.bat     Windows: dependencies, safety tests, then start.py
config.example.json   copied to config.json on the first run
.env.example          copied to .env on the first run (secrets live here)
okxtrader/            the desk: engine, OKX broker, web UI, Telegram
  okx_api.py          OKX v5 REST: signing, demo header, OKX's errors
  okx_broker.py       the OKX broker the engine trades through
  okx_sim.py          a simulated OKX for tests and demos
  maker.py            the W3 maker fills and closes
tests/                the safety suite (pytest tests -q)
run_coordinator.py    the engine on its own (start.py runs it for you)
tools/                reports and maintenance scripts
```

## Try it without OKX

```
python3 -m okxtrader.okx_sim --port 8771
```

Then set the `okx` account's Base URL on the Exchanges page to
`http://127.0.0.1:8771`. The simulated exchange quotes BTC and ETH spot,
perpetuals and a December future, fills orders, charges VIP fees and
quotes funding rates.

## Tests

```
python3 -m pytest tests -q
```

The browser tests drive a real Chromium through Playwright, and skip if
no browser is installed.
