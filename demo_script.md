# Nexus Stat-Arb — Demo Walkthrough Script
**Target runtime: ~2.5 minutes  (~350 words at 140 wpm)**

---

## INTRO

This is Nexus — a live statistical arbitrage engine built for crypto.
It trades the price spread between spot and futures markets, going long when
the spread is unusually low and short when it's unusually high. Let me walk
you through it.

---

## DASHBOARD

We start on the Dashboard — the live command center.

At the very top you can see the connected exchange, your account equity,
available margin, and the overall health of the account at a glance.

Below that, the engine status tells you exactly where you are — whether the
system is still collecting spread data, or whether it's ready to trade.

On the left, spot and futures prices update in real time. The Z-score card
shows where the spread sits right now relative to its historical mean. A
Z-score beyond plus or minus two is what triggers a trade. You can also see
the current position, the entry Z-score, and live unrealized P&L.

The two charts underneath track Z-score and spread history so you can see the
rhythm of the market at a glance.

In the middle column, you get the full statistical picture — mean, standard
deviation, the Hurst exponent — which tells you whether the spread is
mean-reverting or trending. Below that, the filter status panel shows whether
the Hurst filter and the volatility filter are green. If a signal gets blocked,
you'll see exactly why.

There's also a live AI insights panel. After every closed trade, Claude
analyzes the result and surfaces actionable recommendations. If three
consecutive analyses agree on a change, the Auto-Tune engine applies it
automatically.

On the right, you have the full margin breakdown and a snapshot of the active
trading config — position size, entry and exit thresholds, leverage, all
visible in one place.

At the bottom, the full exchange order log gives you every leg of every order
with fill price, fees, and P&L.

---

## SETTINGS

Head over to Settings and this is where the strategy lives.

You choose the asset and trading pair, set your Z-score entry and exit
thresholds, and configure the lookback window — how many ticks of history
the engine uses to calculate the spread statistics.

The signal filters are powerful. The Hurst filter blocks entries when the
spread is trending rather than mean-reverting. The STD filter ensures the
spread is wide enough to cover fees before a trade is placed — you can dial
in your exact maker and taker fees so the math is always accurate.

Position sizing, leverage, order execution mode — market or limit — and safety
controls like entry cooldown are all here. And at the bottom, you can wire up
Telegram notifications so trade entries, exits, and system alerts go straight
to your phone.

---

## SETUP

The Setup page is where you connect your exchanges.

You can add OKX, Binance, or Bybit accounts, toggle between demo and live
mode, and test the connection before anything goes live. Once connected, you
assign which exchange handles spot and which handles futures — giving you full
flexibility to mix venues.

---

## ANALYSIS

Finally, the Analysis page gives you the historical view.

The top row shows total trades, win rate, and overall P&L at a glance. The SD
Touch Distribution chart shows how often price has hit each standard deviation
level — useful for calibrating your entry thresholds.

Below that, the full Trade Journal logs every trade — entry and exit time,
Z-scores, spot and futures prices, P&L, and exit reason — so you can review
exactly what happened and why.

---

## OUTRO

That's Nexus. A fully autonomous stat-arb engine with real-time risk
monitoring, AI-assisted self-tuning, and a clean interface to stay in control
at every level.
