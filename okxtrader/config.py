"""Configuration: accounts, pairs and settings in one JSON file.

Credentials never live in that file. Each account names an environment
variable (`password_env`) holding its password, and the value lives in
`.env`, gitignored, written by the UI. Never in code, never in config,
never in chat, never in a log line.

Two things in here are load-bearing beyond "read some settings":

- **`save_raw` writes through a tmp file and `os.replace`.** A plain
  `open(path, 'w')` truncates, and a reader in that window sees half a
  config — which, in front of a read-modify-write save, wrote an EMPTY
  config back and deleted every account.
- **The clash refusals** (`endpoint_clash`, `login_clash`,
  `terminal_clash`) are checked at SAVE time, not only at startup. A
  refusal at save is a corrected field; a refusal at startup is five
  restart attempts with the reason scrolling past.
"""

import inspect
import json
import logging
import os
import re

from . import atomicfile
from .models import OrderType, OvernightMode, TimeInForce

#: Pair types where the two legs are the SAME underlying, so carry ties
#: them together and the contract has a date it converges on. Two
#: different instruments (RELATED) have no fair value and must not be
#: prompted for an expiry they do not have.
BASIS_PAIR_TYPES = ('SPOT_FUTURE', 'FUTURE_FUTURE')

#: The three a pair can be, and the one name that was written by an
#: older Exchanges page. A pair saved as DIFFERENT behaved as RELATED
#: everywhere it MATTERED, but the ladder's Pair type box showed
#: nothing selected for it — and saving that box then wrote back
#: whatever it happened to be showing.
PAIR_TYPES = ('SPOT_FUTURE', 'FUTURE_FUTURE', 'RELATED')
PAIR_TYPE_ALIASES = {'DIFFERENT': 'RELATED'}


def pair_type_name(value):
    """One of PAIR_TYPES. Anything unrecognised is RELATED — the
    reading with NO fair value, which is the safe way to be wrong."""
    name = str(value or 'SPOT_FUTURE').strip().upper()
    name = PAIR_TYPE_ALIASES.get(name, name)
    return name if name in PAIR_TYPES else 'RELATED'


#: Warnings already said once, so a file re-read every few seconds
#: does not scroll the interesting line off the screen.
_WARNED = set()


def _warn_once(template, *args):
    line = template % args
    if line in _WARNED:
        return
    _WARNED.add(line)
    logging.warning('%s', line)


def _choice(enum, value, default, key, field):
    """One of an enum's values, taking the DEFAULT for a blank.

    A null in the file is how this repeatedly took the terminal down:
    `OrderType(None)` raises, that came out of `TraderConfig.from_file`
    inside the launcher, and the launcher died before it started
    anything. A field that has a default has one for exactly this
    case — a config written by an older UI, or a value cleared by
    hand — so blank means the default, and only a value that is
    genuinely not a choice is refused, naming the pair, the field and
    what the choices are.
    """
    if value is None or value == '':
        value = default
    try:
        return enum(value)
    except ValueError:
        raise ValueError(
            f"pair '{key}': {field} is {value!r}, which is not one of "
            f"{', '.join(member.value for member in enum)}") from None


def _blank_to_none(value):
    """A number from the UI, where blank means "unset" and 0 does not.

    `?? ''` and not `|| ''`: a swap of 0, a commission of 0 and an
    allowance of 0 are all real statements, and a loop that skips falsy
    values can only ever SET an override, never clear one.
    """
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

try:
    from dotenv import load_dotenv
except ImportError:            # dotenv optional; the shell can set them
    load_dotenv = None


#: Defaults for every tunable the engine reads. Each one is a visible
#: setting in the UI — the spec's rule is that guessed numbers get
#: corrected from measurement, which needs them on screen first.
DEFAULT_SETTINGS = {
    # --- the loop -----------------------------------------------------
    'POLL_INTERVAL_SEC': 0.3,
    #: How often the coordinator drains clicks, on its own thread. This
    #: is the click-to-order latency the trader actually feels, and it
    #: is deliberately far shorter than the poll: waiting for the next
    #: poll would put up to a whole interval between the click and the
    #: order, on a product whose promise is that one click is one order.
    'COMMAND_POLL_SEC': 0.02,
    #: How often the coordinator retries an account whose leg runner
    #: is not answering. A terminal started late joins by itself.
    'LEG_RETRY_SEC': 5.0,
    'ACCOUNT_INFO_CACHE_SEC': 5.0,     # an IPC round trip; do not poll it

    # --- execution ----------------------------------------------------
    #: The naked window's ceiling. The crossing order goes IMMEDIATELY
    #: on fill; this is a failure-ESCALATION window, not patience. A
    #: market order round-trips in ~24ms, so 2.0s is ~80x headroom and
    #: only fires on a real fault (spec §4, decision 2).
    'LEG_DEADLINE_SEC': 2.0,
    #: Keep a partially-matched pair only if it is at least this
    #: fraction of the clip; otherwise unwind all of it.
    'MIN_MATCHED_FRACTION': 0.4,
    #: How far through the clicked spread a MARKET click may fill before
    #: it is refused, in ladder increments. Default a few; measure the
    #: real distribution before fixing it (spec, open question 12).
    'MARKET_PROTECTION_TICKS': 3.0,
    #: ONE CLICK IS ONE ORDER. A market click crosses immediately —
    #: that is the product, and the arming is made unmistakable instead
    #: (the mode badge, the tinted click columns, the cursor). Turn this
    #: on and every market click asks first; it is a deliberate choice
    #: for a desk that wants the extra gesture, not the default.
    'CONFIRM_MARKET_CLICKS': False,
    #: A click AWAY from the touch, in MARKET mode, rests as a working
    #: order instead of being refused. A buy under the offer cannot
    #: cross at any price, and "rest it here" is what a trader means by
    #: clicking there — refusing it made the whole far side of the
    #: ladder dead. Turn this off to have such a click refused instead.
    'CLICK_AWAY_RESTS': True,
    #: Refuse to trade a pair whose two accounts turn out to be one MT5
    #: login. OFF: one account carrying both legs is an ordinary spread
    #: (spot and the future at one broker), and only the desk knows
    #: whether that is what was meant. It is always SAID — a banner
    #: names the login and both readings — but it is not refused unless
    #: this is turned on.
    'REFUSE_SHARED_ACCOUNT': False,
    #: How often the ladder re-centres itself on the mid, in seconds.
    #: 0 re-centres only when the market leaves the visible window; the
    #: Lock tick on the rail stops it entirely. A ladder that re-centres
    #: under a click is how a trader clicks the wrong price, so this is
    #: a comfort setting with a real edge to it.
    'RECENTRE_SEC': 5.0,
    #: Which COLUMN places a buy. 'TT' is the price-ladder convention
    #: every desk arrives with: clicking the BIDS column joins the bid,
    #: which is a resting BUY, and clicking ASKS joins the offer, which
    #: is a resting SELL. 'TOUCH' is the hit/lift reading: clicking the
    #: ASKS column lifts the offer and buys.
    #:
    #: It changes NOTHING but which column sends which side. The side
    #: itself, the price the row carries, the sizing and the execution
    #: are all downstream of the click and identical either way — the
    #: BUY and SELL buttons and the B/S keys name their side outright
    #: and are not affected at all.
    #:
    #: DEFAULT IS TT, and that was a decision taken separately from
    #: adding the switch, which is why this comment used to say the
    #: opposite. The desk this is built for trades TT and every PC was
    #: being changed by hand after every install.
    #:
    #: It also settles a disagreement between three files. The Settings
    #: pane has always drawn an unset value as TT
    #: (`settings.CLICK_CONVENTION || 'TT'`) and the snapshot has
    #: always defaulted to TT too, while this line said TOUCH. So a
    #: fresh machine SHOWED 'Bids buy — TT price ladder' on the screen
    #: and CROSSED the other way — the one shape of bug that costs
    #: money without looking wrong.
    #:
    #: A desk that wants hit-and-lift sets it deliberately, and a desk
    #: that has ever pressed Apply on the Trading pane already carries
    #: its own value here and is not moved by this line at all.
    'CLICK_CONVENTION': 'TT',

    #: Minutes of nobody touching the screen before it locks itself.
    #:
    #: The case it is for: a trader leaves for the day and the next
    #: person to sit down finds a live account one click from a trade.
    #: A manual lock only helps the trader who remembers, and the whole
    #: problem is the evening they did not.
    #:
    #: 15 is long enough that watching a ladder without clicking does
    #: not lock it, short enough to catch somebody who walked away. 0
    #: turns it off, for a desk that wants only the button.
    #:
    #: It does NOTHING until a PIN is set: a screen that locks with no
    #: way in is an outage, not a safety feature.
    'AUTO_LOCK_MINUTES': 15,

    #: Does a click the OPPOSITE way close what is open, or stack a
    #: second position beside it?
    #:
    #: These accounts are HEDGING, so MT5 never nets: an opposite order
    #: always opens a SECOND position, and the trader who clicked the
    #: offer to cover a short ends up with a short and a long, both
    #: live, both paying carry, and a ticket count nobody expected.
    #: Every price ladder on every exchange reduces first, so this
    #: does too — by closing tickets OLDEST FIRST, which is what the
    #: broker cannot do for us.
    #:
    #: Tickets are taken oldest first, and the LAST one is taken in
    #: PART — as far as the click reaches and no further. A reduce can
    #: therefore never over-close and flip the net the other way, and
    #: never under-close and open the rest the wrong way either. Only
    #: what the click cannot cover opens, and it opens after the close
    #: has actually gone through.
    #:
    #: Note what this means on a MIXED book: a click that ADDS to the
    #: net still clears an opposite ticket first. Short 107 across a
    #: 93 long and a 200 short, SELL 100 clicked, closes the 93 and
    #: opens 7. The net moves by the 100 asked for either way, and
    #: closing takes the margin and the financing off both sides
    #: instead of stacking a third ticket. What it costs is that the
    #: 93 CROSSES when the level prints rather than earning it.
    #:
    #: OFF makes every click purely an open — on a hedging account
    #: that stacks the opposite ticket, which is the trade-off and why
    #: it is on by default.
    'CLOSE_FIRST': True,
    #: Ladder row height in pixels. 17 is the reference screen's; a
    #: bigger target is a faster, safer click on a large monitor.
    'ROW_HEIGHT_PX': 17,
    #: What backs a WORKING ORDER (a LIMIT click, or a MARKET click
    #: away from the touch) until it fills.
    #:
    #: TRIGGER (the default): NOTHING at the broker. The level is held
    #: here and, when the EXECUTABLE spread reaches it - the Sell
    #: spread at or over a SELL, the Buy spread at or under a BUY -
    #: both legs cross at MARKET together, through the same path a
    #: market click takes. It fills when the SPREAD gets there.
    #:
    #: QUOTE: the old way - a real limit on one leg, the other crossed
    #: when it fills. It earns one leg's bid-ask, but on two legs that
    #: move together (spot gold against the gold future) the limit
    #: fills on a move in GOLD, not in the spread, and the leg crossed
    #: afterwards has moved the same way: live, a SELL at 28.30 went
    #: on at 26.84, every fill the same direction. Kept for a pair
    #: whose legs really are independent; not on the screen.
    'WORKING_ORDERS': 'TRIGGER',
    #: Before a held working order is sent, read BOTH legs' prices
    #: again, fresh, and send only if the spread is STILL at the level.
    #: The poll that saw the level can be up to a poll old; a spread
    #: that only flickered there has gone back by the time the orders
    #: land, and that fill is the bad one. Costs one tick read per leg,
    #: only at the moment of sending. Off sends on the poll's prices.
    'RECHECK_BEFORE_SEND': True,
    #: The same for the Algo's TAKE-PROFIT, and only that exit: both
    #: legs read again, and the close goes only if the fresh closing
    #: price still reaches the target. A stop, the cutoff, a kill and
    #: every manual close are never re-checked - nothing waits there.
    'RECHECK_TAKE_PROFIT': True,
    #: Re-peg dead band, in ladder increments. Every MODIFY loses queue
    #: position, so re-pricing three times a second guarantees you are
    #: never at the front of a queue — which defeats quoting entirely
    #: (spec, open question 11).
    'REPEG_DEAD_BAND_TICKS': 1.0,
    'SLIPPAGE_POINTS': 1.0,

    # --- guards on the price itself (spec §8) -------------------------
    #: How long a leg's quote may go UNCHANGED before the spread is
    #: called stale and orders are withheld. 5s was tuned on a futures
    #: feed that ticks constantly; on a CFD account — and on any demo —
    #: a perfectly healthy leg is quiet for longer than that between
    #: trades, and the guard then fires all day on a market that is
    #: simply slow. 15s still catches a leg that has actually STOPPED,
    #: which is what it is for. 0 = off.
    'MAX_QUOTE_AGE_SEC': 15.0,
    #: While a leg is stale, re-subscribe it this often (seconds; 0 =
    #: never, do it by hand with the Feed button). Some terminals drop
    #: a symbol's subscription silently — the ladder ticks for a few
    #: seconds after a refresh and then goes quiet again — and a
    #: re-subscribe is what brings it back. It is logged every time, so
    #: a feed being nursed along like this is visible rather than
    #: hidden by the nursing.
    'AUTO_REFRESH_STALE_SEC': 20.0,
    'MAX_SPREAD_JUMP_SIGMA': 5.0,      # 0 = off
    'JUMP_SETTLE_SEC': 2.0,
    'SIGMA_WINDOW_QUOTES': 600,

    # --- the session clock (spec §3.1, §3.2) --------------------------
    #: One cutoff, on the BROKER's clock, shared by the DAY cancel and
    #: the overnight rule, so the trader configures a single time — and
    #: configures it in the broker's terms, which is how a trading day
    #: is actually reckoned.
    #: How often the broker's clock offset is re-measured. It does not
    #: drift on the scale of a poll, and it is a round trip per account.
    'BROKER_CLOCK_TTL_SEC': 300.0,
    'OVERNIGHT_CLOSE_HOUR': 16,
    'OVERNIGHT_CLOSE_MINUTE': 55,
    'OVERNIGHT_DEFAULT': OvernightMode.ALLOW.value,

    #: The take-profit target, as a percentage of the MARGIN one spread
    #: ties up. The exit price on the ladder is break-even — the entry
    #: price plus commission, both legs, both ends — plus this. 0 shows
    #: break-even alone. Nothing is ever SENT to the broker from it: it
    #: is a price on the screen, and no leg carries a broker-side stop.
    'TP_TARGET_PCT_OF_MARGIN': 2.0,
    #: How often the margin for one spread is re-priced from the
    #: terminals. It moves with the price, but slowly.
    'MARGIN_TTL_SEC': 60.0,
    #: Margin level (%) below which the monitor calls an account tight.
    #: Margin is posted PER ACCOUNT with two brokers, so the WEAKEST
    #: account governs what the pair can carry — not the total.
    'MARGIN_WARN_LEVEL': 200.0,

    # --- costs --------------------------------------------------------
    'SPREAD_COST_FACTOR': 1.0,
    'COMMISSION_PER_LOT_A': 0.0,
    'COMMISSION_PER_LOT_B': 0.0,
    #: A BUDGET, not a measurement: how much slippage break-even should
    #: allow for, in money per spread per round turn. The MEASURED
    #: realised slippage is shown beside it (the slippage report), so
    #: this gets corrected from data rather than left at whatever was
    #: first guessed. Default 0 — a fabricated cost is charged against
    #: every trade and the operator cannot tell it was never theirs.
    'SLIPPAGE_ALLOWANCE': 0.0,
    #: Break-even is only defined GIVEN a holding period, because the
    #: swap is charged per night. 0 is intraday, where the term
    #: vanishes; type the nights you expect to hold to see it.
    'BREAK_EVEN_NIGHTS': 0.0,
    #: An annual carry rate (percent), used ONLY to price the basis a
    #: second time as a cross-check on the broker's swap. Unset means
    #: no cross-check — which is honest — but it is the cheapest check
    #: on the screen and one wrong sign in a swap field is all it takes
    #: to display a licence to print money.
    'CARRY_RATE_PCT': None,

    # --- housekeeping -------------------------------------------------
    'RECONCILE_INTERVAL_SEC': 20.0,
    'CLOSE_ATTEMPTS': 3,
    #: 'ask' / 'always' / 'never' — what shutdown does with open
    #: positions. An unanswered prompt means NO (spec §12).
    'SHUTDOWN_CLOSE_POSITIONS': 'ask',
}

#: Settings the launcher reads at STARTUP. Changing one needs a restart
#: and must say so; everything else hot-applies. Crying "restart" on
#: every save teaches the operator to ignore the line that matters.
STRUCTURAL_SETTINGS = ('POLL_INTERVAL_SEC',)


class AccountConfig:
    """One MT5 account = one login, one terminal, one port.

    Two accounts pointing at the same terminal folder are ONE account
    whatever the config says — a terminal holds a single login.
    """

    def __init__(self, name, terminal_path=None, login=None,
                 password_env="", server=None, endpoint=None):
        self.name = name
        self.terminal_path = terminal_path
        self.login = int(login) if login else None
        self.password_env = password_env or env_key_for(name)
        self.server = server
        #: host:port where this account's leg runner listens.
        self.endpoint = endpoint

    @property
    def password(self):
        return os.environ.get(self.password_env) if self.password_env else None

    def to_dict(self):
        return {'terminal_path': self.terminal_path, 'login': self.login,
                'password_env': self.password_env, 'server': self.server,
                'endpoint': self.endpoint}

    @classmethod
    def from_dict(cls, name, raw):
        raw = raw or {}
        # Cleaned on the way in: a path saved with the quotes Windows'
        # "Copy as path" adds is still a path, and every part of the
        # engine reads it from here.
        return cls(name, terminal_path=(
                       clean_terminal_path(raw.get('terminal_path')) or None),
                   login=raw.get('login'),
                   password_env=raw.get('password_env'),
                   server=raw.get('server'), endpoint=raw.get('endpoint'))


class PairConfig:
    """One ladder: two symbols on two accounts, and how they are traded.

    Contract sizes and volume steps are NOT typed in — they are read
    from MT5 and cached here so the UI can render before the runners
    answer. `hedge_ratio_for` stamps beta with the pair it was computed
    for, so a stale beta from the previous instrument cannot silently
    define the spread (spec §2).
    """

    def __init__(self, key, name=None, leg_a=None, leg_b=None,
                 hedge_ratio=1.0, hedge_ratio_for=None, pair_type='SPOT_FUTURE',
                 increment=None, clip_lots_a=1.0, clip_lots_b=1.0,
                 contract_size_a=None, contract_size_b=None,
                 max_quote_age_sec=None,
                 default_quantity=1.0, order_type=OrderType.LIMIT.value,
                 exit_type=OrderType.MARKET.value,
                 time_in_force=TimeInForce.DAY.value,
                 overnight=OvernightMode.ALLOW.value,
                 quoting_leg=None, enabled=True, rows=30,
                 expiry=None, expiry_a=None,
                 swap_a_long_per_lot=None, swap_a_short_per_lot=None,
                 swap_b_long_per_lot=None, swap_b_short_per_lot=None,
                 commission_per_lot_a=None,
                 commission_per_lot_b=None, slippage_allowance=None,
                 break_even_nights=None, tp_target_pct_of_margin=None,
                 carry_rate_pct=None,
                 show_fair_window=False, algo=None, algo_window=None,
                 algo_params=None, session_open=None, session_close=None,
                 break_start=None, break_end=None):
        self.key = key
        self.name = name or key
        self.leg_a = dict(leg_a or {})      # {'account': ..., 'symbol': ...}
        self.leg_b = dict(leg_b or {})
        self.hedge_ratio = float(hedge_ratio or 1.0)
        self.hedge_ratio_for = hedge_ratio_for
        self.pair_type = pair_type_name(pair_type)
        #: Spread ticks per ladder row. None = derive it (see
        #: `derived_increment`) rather than guess a readable-looking one.
        self.increment = increment
        #: What ONE unit of the Qty box means on each leg, in LOTS.
        #: BOTH are the trader's: Qty 100 at 1 and 1 is 100 lots of leg
        #: A against 100 of leg B; at 1 and 2 it is 100 against 200.
        #:
        #: Blank reads as 1. Nothing derives leg B any more — it was
        #: computed from the hedge arithmetic, or lot for lot, or by
        #: equal notional, and every one of those could round a leg to
        #: zero or size a pair the trader had not asked for. A number
        #: typed into a box can do neither.
        self.clip_lots_a = float(clip_lots_a or 1.0)
        self.clip_lots_b = float(clip_lots_b or 1.0)
        #: What ONE LOT is, in the instrument's own units — 100 oz of
        #: gold, 5,000 of silver, 1,000 barrels of oil.
        #:
        #: None, and normally None: it is READ FROM MT5
        #: (`trade_contract_size`) on every resolve, because a contract
        #: size somebody typed is a contract size that can be wrong,
        #: and every money figure on the screen runs through it.
        #:
        #: An override exists for the case MT5 gets it wrong or the
        #: desk's broker reports something the spec sheet contradicts.
        #: It is loud when it is set: the ladder says so and Diagnose
        #: names the disagreement.
        self.contract_size_a = _blank_to_none(contract_size_a)
        self.contract_size_b = _blank_to_none(contract_size_b)
        #: How long THIS pair's legs may go unchanged before the spread
        #: is called stale and orders are withheld. None = the global
        #: MAX_QUOTE_AGE_SEC.
        #:
        #: It has to be per pair. The global was raised from 5s to 15s
        #: once already for this exact reason, and 15s is still short
        #: for a dated oil future that trades a few times a minute
        #: while gold spot ticks several times a second. One number
        #: cannot serve both: too high and a leg that has genuinely
        #: STOPPED goes unnoticed on the fast pair; too low and the
        #: slow pair refuses every order all day on a market that is
        #: merely quiet.
        self.max_quote_age_sec = _blank_to_none(max_quote_age_sec)
        self.default_quantity = float(default_quantity or 1.0)
        # Blank is the DEFAULT, never an exception: see `_choice`.
        self.order_type = _choice(OrderType, order_type,
                                  OrderType.LIMIT.value, key, 'order_type')
        #: How this ladder GETS OUT by default — the instruction the
        #: close controls and the F key follow.
        #:
        #: MARKET crosses now. LIMIT rests one closing order per open
        #: position at a level and waits there. Either way the thing
        #: that eventually reaches the broker is the SAME: a market
        #: close by ticket on both legs. Nothing rests a closing
        #: pending, because MT5 ignores `position` on a pending and an
        #: opposite limit OPENS a second position on a hedging account
        #: — that happened live on 2026-09-02. So this selects WHEN,
        #: not what.
        #:
        #: Default MARKET, and CLOSE ALL crosses now whatever this
        #: says: the way out must not change meaning under the button
        #: pressed in a hurry.
        self.exit_type = _choice(OrderType, exit_type,
                                 OrderType.MARKET.value, key, 'exit_type')
        self.time_in_force = _choice(TimeInForce, time_in_force,
                                     TimeInForce.DAY.value, key,
                                     'time_in_force')
        self.overnight = _choice(OvernightMode, overnight,
                                 OvernightMode.ALLOW.value, key, 'overnight')
        #: Which leg rests the real pending in LIMIT mode. None = pick
        #: the wider bid-ask (that is the spread being earned), measured
        #: not assumed (spec §4, open question 10).
        self.quoting_leg = quoting_leg
        self.enabled = True if enabled is None else bool(enabled)
        self.rows = int(rows or 30)
        #: The futures leg's expiry, and what carrying one spread for
        #: one day is worth at THIS broker on THIS account, in spread
        #: points. Neither is derivable from the price feed, and both
        #: are what turn a basis into a fair value rather than a number
        #: that happens to oscillate. Unset = no fair value shown.
        self.expiry = expiry
        #: Leg A's expiry too: a calendar spread needs both, and a leg
        #: whose contract month is wrong is exactly what a fair value
        #: beside the market is there to catch. Blank = read MT5's.
        self.expiry_a = expiry_a
        #: The broker's own swap, per lot per night, per leg per SIDE —
        #: four numbers, because a pair is long one leg and short the
        #: other and the two are charged differently. Blank means "use
        #: what MT5 reports"; a typed value wins, and 0 is a real
        #: statement, which is why these are None and not 0.0.
        self.swap_a_long_per_lot = _blank_to_none(swap_a_long_per_lot)
        self.swap_a_short_per_lot = _blank_to_none(swap_a_short_per_lot)
        self.swap_b_long_per_lot = _blank_to_none(swap_b_long_per_lot)
        self.swap_b_short_per_lot = _blank_to_none(swap_b_short_per_lot)
        #: What a trade on THIS ladder costs, and where it therefore
        #: gets out. Per ladder, not per system: a gold basis and a
        #: WTI/Brent differential are charged different commissions,
        #: held for different lengths of time and targeted differently,
        #: and one set of numbers covering both is a set that is wrong
        #: for at least one of them. None means "the default"; a typed
        #: value wins, and 0 is a real statement.
        self.commission_per_lot_a = _blank_to_none(commission_per_lot_a)
        self.commission_per_lot_b = _blank_to_none(commission_per_lot_b)
        self.slippage_allowance = _blank_to_none(slippage_allowance)
        self.break_even_nights = _blank_to_none(break_even_nights)
        self.tp_target_pct_of_margin = _blank_to_none(tp_target_pct_of_margin)
        self.carry_rate_pct = _blank_to_none(carry_rate_pct)
        #: Show the Fair Spread window for this pair. Off by default:
        #: the ladder is for the price, and a panel of derived figures
        #: beside it is a panel between the trader and the market.
        #: `show_fair_window` is the name this had while there were two
        #: algos to choose between; a config written then still opens
        #: its window. `algo` is accepted and IGNORED for the same
        #: reason — see the `algo` property.
        self.algo_window = bool(show_fair_window if algo_window is None
                                else algo_window)
        #: The Algo's own numbers for THIS ladder — entry z, candle
        #: timeframe and length, the gates and the optional exits. Only
        #: what was typed is kept; a blank is the default, read through
        #: `algo.clean_params`. Whether the Algo is ON is not here: that
        #: is held by the running engine and is off after every restart.
        self.algo_params = _clean_algo_params(algo_params)
        #: THIS ladder's trading hours, 'HH:MM' on the BROKER's clock
        #: (see `session.PairSession`). Blank close is the desk-wide
        #: OVERNIGHT_CLOSE; blank open is "any time before the close";
        #: the break takes both ends or neither. An oil future and a
        #: gold CFD do not keep the same day.
        from .session import clean_hhmm
        self.session_open = clean_hhmm(session_open)
        self.session_close = clean_hhmm(session_close)
        self.break_start = clean_hhmm(break_start)
        self.break_end = clean_hhmm(break_end)
        #: Cached MT5 metadata per leg, refreshed by the coordinator.
        self.meta_a = {}
        self.meta_b = {}

    @property
    def algo(self):
        """Which READING this ladder's window shows from config: Fair
        spread when the window is ticked, NONE when it is not.

        The Algo itself is not decided here. It is switched on in the
        RUNNING engine, per ladder, and is off after every restart, so
        no config file can bring a signal back that nobody turned on
        today. The snapshot says ALGO for a ladder whose Algo is on.
        """
        from .algo import FAIR_SPREAD, NONE
        return FAIR_SPREAD if self.algo_window else NONE

    @algo.setter
    def algo(self, value):
        """NONE shuts the window, FAIR_SPREAD opens it. ALGO is the
        engine's switch, not a config value, and changes nothing here.

        This used to have no setter at all, so sending `algo` through
        the settings path raised AttributeError mid-save.
        """
        from .algo import FAIR_SPREAD, NONE
        chosen = str(value or NONE).upper()
        if chosen == NONE:
            self.algo_window = False
        elif chosen == FAIR_SPREAD:
            self.algo_window = True

    @property
    def symbol_a(self):
        return self.leg_a.get('symbol')

    @property
    def symbol_b(self):
        return self.leg_b.get('symbol')

    @property
    def account_a(self):
        return self.leg_a.get('account')

    @property
    def account_b(self):
        return self.leg_b.get('account')

    def derived_increment(self):
        """`max(tick_B, beta x tick_A)` — the smallest step the spread
        can actually move in (spec, decision 6).

        Returns None when the legs' tick sizes are not known yet, which
        the UI renders as "—" rather than as a number it made up.
        """
        tick_a = (self.meta_a or {}).get('tick_size')
        tick_b = (self.meta_b or {}).get('tick_size')
        if not tick_a or not tick_b:
            return None
        return max(float(tick_b), float(self.hedge_ratio or 1.0) * float(tick_a))

    def effective_increment(self):
        """What the ladder actually steps by: the override, or derived."""
        return self.increment or self.derived_increment()

    def expects_expiry(self):
        """Should this pair HAVE an expiry?

        A basis pair (spot vs the future) has one, and a blank field is
        an unset field — show the prompt. Two different instruments
        (WTI vs Brent) legitimately have none, and prompting there is
        an error message for a correct configuration. Without this
        flag an operator who has just typed an expiry cannot tell
        whether it was rejected, ignored, or waiting on a restart.
        """
        return (self.pair_type or 'SPOT_FUTURE').upper() in BASIS_PAIR_TYPES

    def effective_expiry(self, leg='b'):
        """The expiry actually in force for one leg: the typed one, or
        the contract's own from MT5 when nothing was typed."""
        typed = self.expiry_a if leg == 'a' else self.expiry
        if typed:
            return typed
        meta = (self.meta_a if leg == 'a' else self.meta_b) or {}
        stamp = meta.get('expiry')
        if not stamp:
            return None
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(int(stamp),
                                          timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    def swap_overrides(self):
        """The four typed rates, in the shape `carry` reads them."""
        return {'swap_a_long_per_lot': self.swap_a_long_per_lot,
                'swap_a_short_per_lot': self.swap_a_short_per_lot,
                'swap_b_long_per_lot': self.swap_b_long_per_lot,
                'swap_b_short_per_lot': self.swap_b_short_per_lot}

    #: What ONE ladder's exit is priced from. Each of these has a
    #: system-wide default; set here, the ladder's own value wins.
    EXIT_FIELDS = {
        'commission_per_lot_a': 'COMMISSION_PER_LOT_A',
        'commission_per_lot_b': 'COMMISSION_PER_LOT_B',
        'slippage_allowance': 'SLIPPAGE_ALLOWANCE',
        'break_even_nights': 'BREAK_EVEN_NIGHTS',
        'tp_target_pct_of_margin': 'TP_TARGET_PCT_OF_MARGIN',
        'carry_rate_pct': 'CARRY_RATE_PCT',
    }

    def exit_settings(self, settings):
        """The settings the exit panel reads, for THIS ladder.

        The system-wide defaults, with this ladder's own values laid
        over them. A blank field is not 0 and not a refusal: it means
        "whatever the default is", so a desk that prices every pair the
        same types nothing at all.
        """
        merged = dict(settings or {})
        for field, key in self.EXIT_FIELDS.items():
            value = getattr(self, field, None)
            if value is not None:
                merged[key] = value
        return merged

    #: Fields a save applies WITHOUT a restart. The reference-only ones
    #: feed a panel rather than the engine; the per-ladder trading ones
    #: are what the ladder's own controls already change live, so a save
    #: from the settings pane and a click on the rail do the same thing.
    #: Blocking these behind a restart is what put "an assets change
    #: requires a restart" ten lines above a live trade while the values
    #: sat saved and correct.
    HOT_FIELDS = (('expiry', 'expiry_a',
                   'swap_a_long_per_lot', 'swap_a_short_per_lot',
                   'swap_b_long_per_lot', 'swap_b_short_per_lot',
                   'order_type', 'exit_type',
                   'time_in_force', 'overnight', 'increment',
                   'default_quantity', 'quoting_leg', 'rows',
                   # What ONE spread means in leg A lots. Leg B is
                   # never typed: it is the hedge, and it is derived.
                   'clip_lots_a', 'clip_lots_b',
                   'contract_size_a', 'contract_size_b',
                   'max_quote_age_sec',
                   'algo_window', 'show_fair_window', 'pair_type',
                   'algo_params',
                   'session_open', 'session_close',
                   'break_start', 'break_end')
                  + tuple(EXIT_FIELDS))

    def apply_hot(self, raw):
        """Take the reference-only fields from a freshly read config.

        Returns the names that actually CHANGED, so a hot-apply can be
        logged on the change rather than on a clock.
        """
        changed = []
        for field in self.HOT_FIELDS:
            if field not in (raw or {}):
                continue
            value = raw[field]
            if field in self.EXIT_FIELDS or (
                    field.startswith('swap_') and field.endswith('_per_lot')):
                value = _blank_to_none(value)
            elif field in ('algo_window', 'show_fair_window'):
                field = 'algo_window' if field == 'show_fair_window' else field
                value = bool(value)
            elif field == 'pair_type':
                value = pair_type_name(value)
            elif field == 'algo_params':
                value = _clean_algo_params(value)
            elif field in ('session_open', 'session_close', 'break_start',
                           'break_end'):
                from .session import clean_hhmm
                value = clean_hhmm(value)
            elif field == 'order_type':
                value = _choice(OrderType, value, self.order_type.value,
                                self.key, field)
            elif field == 'exit_type':
                value = _choice(OrderType, value, self.exit_type.value,
                                self.key, field)
            elif field == 'time_in_force':
                value = _choice(TimeInForce, value, self.time_in_force.value,
                                self.key, field)
            elif field == 'overnight':
                value = _choice(OvernightMode, value, self.overnight.value,
                                self.key, field)
            elif field in ('increment', 'default_quantity'):
                value = _blank_to_none(value)
                if field == 'default_quantity' and value is None:
                    continue          # a ladder always has a size
            elif field == 'rows':
                value = int(value or 30)
            elif field == 'max_quote_age_sec':
                value = _blank_to_none(value)
            elif field in ('contract_size_a', 'contract_size_b'):
                # Blank is MT5's own, which is the right answer almost
                # always. Never 1: a contract size of 1 would price
                # every figure on the ladder at a hundredth of the
                # truth on gold, silently.
                value = _blank_to_none(value)
            elif field in ('clip_lots_a', 'clip_lots_b'):
                # Blank is 1, never None: with nothing left to derive,
                # an unset leg has no other honest reading.
                value = float(_blank_to_none(value) or 1.0)
            if getattr(self, field) != value:
                setattr(self, field, value)
                changed.append(field)
        return changed

    def to_dict(self):
        return {
            'name': self.name, 'leg_a': dict(self.leg_a),
            'leg_b': dict(self.leg_b), 'hedge_ratio': self.hedge_ratio,
            'hedge_ratio_for': self.hedge_ratio_for,
            'pair_type': self.pair_type, 'increment': self.increment,
            'clip_lots_a': self.clip_lots_a, 'clip_lots_b': self.clip_lots_b,
            'contract_size_a': self.contract_size_a,
            'contract_size_b': self.contract_size_b,
            'max_quote_age_sec': self.max_quote_age_sec,
            'default_quantity': self.default_quantity,
            'order_type': self.order_type.value,
            'exit_type': self.exit_type.value,
            'time_in_force': self.time_in_force.value,
            'overnight': self.overnight.value,
            'quoting_leg': self.quoting_leg,
            'enabled': self.enabled, 'rows': self.rows,
            'expiry': self.expiry,
            'expiry_a': self.expiry_a,
            'swap_a_long_per_lot': self.swap_a_long_per_lot,
            'swap_a_short_per_lot': self.swap_a_short_per_lot,
            'swap_b_long_per_lot': self.swap_b_long_per_lot,
            'swap_b_short_per_lot': self.swap_b_short_per_lot,
            'commission_per_lot_a': self.commission_per_lot_a,
            'commission_per_lot_b': self.commission_per_lot_b,
            'slippage_allowance': self.slippage_allowance,
            'break_even_nights': self.break_even_nights,
            'tp_target_pct_of_margin': self.tp_target_pct_of_margin,
            'carry_rate_pct': self.carry_rate_pct,
            'algo': self.algo, 'algo_window': self.algo_window,
            'algo_params': dict(self.algo_params),
            'session_open': self.session_open,
            'session_close': self.session_close,
            'break_start': self.break_start, 'break_end': self.break_end,
        }

    @classmethod
    def from_dict(cls, key, raw):
        """Build a pair, DROPPING any field this version no longer has.

        A retired field must never take the terminal down.
        `swap_per_day` lived in one release and was removed in the
        next; a config still carrying it made `TraderConfig.from_file`
        raise TypeError — which killed the LAUNCHER itself on start,
        while the web process went on serving the last status file it
        had. The screen showed the previous day's prices, ageing
        normally, with no engine behind them.

        So an unknown field is a logged line and a dropped value, not
        an exception.
        """
        raw = dict(raw or {})
        raw.pop('key', None)
        accepted = set(inspect.signature(cls.__init__).parameters)
        accepted -= {'self', 'key'}
        unknown = sorted(set(raw) - accepted)
        for field in unknown:
            raw.pop(field)
        if unknown:
            # Once per (pair, fields). The launcher re-reads the config
            # every few seconds; the same three lines scrolling past
            # forever is how the line that MATTERS gets missed.
            _warn_once(
                "pair '%s': ignoring %s — this version does not read %s. "
                "It will disappear from the file at the next save.",
                key, ', '.join(unknown),
                'them' if len(unknown) > 1 else 'it')
        return cls(key, **raw)


def _clean_algo_params(raw):
    """The typed Algo settings only, by name; blanks dropped."""
    from .algo import DEFAULT_PARAMS
    return {key: value for key, value in dict(raw or {}).items()
            if key in DEFAULT_PARAMS and value not in (None, '')}


class TraderConfig:
    """Everything the engine reads, and where it came from."""

    def __init__(self, accounts=None, pairs=None, settings=None, path=None):
        self.accounts = dict(accounts or {})
        self.pairs = dict(pairs or {})
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(settings or {})
        self.path = path

    def get(self, key, default=None):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key, default))

    def enabled_pairs(self):
        return {k: p for k, p in self.pairs.items() if p.enabled}

    @classmethod
    def from_raw(cls, raw, path=None):
        raw = raw or {}
        accounts = {name: AccountConfig.from_dict(name, acct)
                    for name, acct in (raw.get('accounts') or {}).items()}
        pairs = {key: PairConfig.from_dict(key, pair)
                 for key, pair in (raw.get('pairs') or {}).items()}
        return cls(accounts, pairs, raw.get('settings'), path)

    @classmethod
    def from_file(cls, path):
        if load_dotenv is not None:
            load_dotenv(os.path.join(os.path.dirname(os.path.abspath(path))
                                     or '.', '.env'))
        return cls.from_raw(load_raw(path), path)

    def to_raw(self):
        return {
            'accounts': {n: a.to_dict() for n, a in self.accounts.items()},
            'pairs': {k: p.to_dict() for k, p in self.pairs.items()},
            'settings': dict(self.settings),
        }

    def restart_required(self, fresh):
        """Which changes the launcher only reads at STARTUP.

        Compare only what actually needs a restart — accounts, symbols,
        beta, contract sizes and the poll interval. Display and comfort
        settings hot-apply.
        """
        changes = []
        if {n: a.to_dict() for n, a in self.accounts.items()} != \
                {n: a.to_dict() for n, a in fresh.accounts.items()}:
            changes.append('accounts')
        for key in set(self.pairs) | set(fresh.pairs):
            old, new = self.pairs.get(key), fresh.pairs.get(key)
            if (old is None) != (new is None):
                changes.append(f'pair {key}')
                continue
            structural = ('leg_a', 'leg_b', 'hedge_ratio', 'enabled')
            if any(getattr(old, f) != getattr(new, f) for f in structural):
                changes.append(f'pair {key}')
        for key in STRUCTURAL_SETTINGS:
            if self.get(key) != fresh.get(key):
                changes.append(key)
        return changes


# --- reading and writing, safely ---------------------------------------

def load_raw(path):
    """The config as a plain dict.

    MISSING is legitimately empty (first run). PRESENT-BUT-BROKEN falls
    back to the `.bak` beside it, and failing that RAISES — a tolerant
    reader is precisely wrong here, because returning {} in front of a
    read-modify-write save is how every account gets deleted.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        try:
            with open(path + '.bak', 'r', encoding='utf-8') as f:
                backup = json.load(f)
        except (OSError, ValueError):
            raise RuntimeError(
                f'{os.path.basename(path)} could not be read ({e}) and there '
                f'is no usable backup beside it. Refusing to continue, '
                f'because saving now would overwrite it with nothing.'
            ) from None
        logging.error('%s unreadable (%s) — using the .bak. The next save '
                      'will rewrite the good copy.', path, e)
        return backup


#: Top-level keys whose disappearance is a catastrophe rather than an
#: edit: they are what lets the engine start at all.
CRITICAL_KEYS = ('accounts', 'pairs')


def save_raw(path, raw, allow_shrink=False):
    """Write the config, keeping a backup and refusing to gut it.

    `allow_shrink` is for the endpoints that legitimately remove things
    (deleting an account or a pair). Everything else is a partial edit
    and must not be able to drop a section it never meant to touch.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            current = json.load(f)
    except (OSError, ValueError):
        current = None
    if current and not allow_shrink:
        lost = [key for key in CRITICAL_KEYS
                if current.get(key) and not raw.get(key)]
        if lost:
            raise RuntimeError(
                'refusing to save a config that would drop '
                + ', '.join(lost)
                + ' — this looks like a partial read, not an edit')
    if current is not None:
        tmp_bak = path + '.bak.tmp'
        with open(tmp_bak, 'w', encoding='utf-8') as f:
            json.dump(current, f, indent=2)
        atomicfile.replace(tmp_bak, path + '.bak')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(raw, f, indent=2)
    atomicfile.replace(tmp, path)


# --- the three clashes, refused at SAVE time ---------------------------

def endpoint_clash(raw, name, endpoint):
    """Is another account already on this endpoint? Message or None.

    Only one process can listen on a port. Two accounts sharing one
    means the second leg runner cannot start — or, if the first won the
    race, BOTH legs connect to it and trade the SAME MT5 account while
    every screen reports two.
    """
    if not endpoint:
        return None                # blank = no runner; any number may
    for other, acct in (raw.get('accounts') or {}).items():
        if other == name:
            continue
        if ((acct or {}).get('endpoint') or '').strip() == endpoint:
            return (f"Endpoint {endpoint} already belongs to account "
                    f"'{other}'. One port serves ONE leg runner — give this "
                    f"account its own (e.g. {next_free_port(raw)}), or both "
                    f"legs would end up on the same terminal.")
    return None


def login_clash(raw, name, login):
    """Two rows with one login is the same MT5 account twice: both legs
    would trade it and hedge against themselves."""
    if not login:
        return None
    for other, acct in (raw.get('accounts') or {}).items():
        if other == name:
            continue
        if str((acct or {}).get('login') or '') == str(login):
            return (f"Login {login} already belongs to account '{other}'. "
                    f"Two accounts on one login is the same MT5 account "
                    f"twice — both legs would trade it and hedge against "
                    f"themselves.")
    return None


#: The file an account's MetaTrader 5 is launched from.
TERMINAL_EXE = 'terminal64.exe'


def clean_terminal_path(value):
    """A terminal path as it was typed or pasted, made into a path.

    Windows' "Copy as path" wraps what it copies in double quotes, and
    a path pasted from it was saved with them: the file was exactly
    where the path said, and the desk still would not start, because
    Windows was asked for a file whose name begins with a quote. So the
    whitespace and any matching quotes around the path come off, here,
    where every reader of the path goes through.
    """
    text = str(value or '').strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in '"\'':
        text = text[1:-1].strip()
    return text


def terminal_file(value):
    """The terminal64.exe a typed path means.

    A FOLDER that holds terminal64.exe means that file: "the MetaTrader
    folder" is what a person picks, and asking them to know which file
    inside it to name is asking a trader to do a technician's job.
    """
    path = clean_terminal_path(value)
    if path and os.path.isdir(path):
        inside = os.path.join(path, TERMINAL_EXE)
        if os.path.isfile(inside):
            return inside
    return path


def terminal_missing(value):
    """Why this account's MetaTrader 5 cannot be launched, in words that
    say what to do - or None when it can.

    Blank is NOT missing: it means attach to whichever terminal is
    already open, which is a choice.
    """
    path = clean_terminal_path(value)
    if not path:
        return None
    if os.path.isfile(path):
        return None
    if os.path.isdir(path):
        return (f"{path} is a folder with no {TERMINAL_EXE} in it. Pick "
                f"this account's MetaTrader 5 from the list of the ones "
                f"installed on this PC.")
    return (f"There is no MetaTrader 5 at {path}. Pick this account's "
            f"MetaTrader 5 from the list of the ones installed on this PC, "
            f"or correct the path.")


def terminal_roots():
    """Where a MetaTrader 5 is looked for on THIS PC: the Program Files
    folders, and the top of the drive - where a second, portable copy
    usually sits (C:\\MT5-A). None of it off Windows."""
    if os.name != 'nt':
        return []
    roots = []
    for key in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)'):
        folder = os.environ.get(key)
        if folder and folder not in roots:
            roots.append(folder)
    drive = os.environ.get('SystemDrive', 'C:') + os.sep
    if drive not in roots:
        roots.append(drive)
    return roots


def find_terminals(raw=None, roots=None):
    """Every MetaTrader 5 installed on this PC, for a person to PICK
    rather than type: each folder one level under each root that holds
    terminal64.exe, with the account already using it, if any.

    One level and no deeper, on purpose: it is read every time the page
    opens, and a walk of a whole drive is not.
    """
    roots = terminal_roots() if roots is None else roots
    used = {}
    for name, acct in ((raw or {}).get('accounts') or {}).items():
        path = clean_terminal_path((acct or {}).get('terminal_path'))
        if path:
            used[os.path.normcase(os.path.normpath(path))] = name
    found, seen = [], set()
    for root in roots:
        try:
            children = sorted(os.listdir(root))
        except OSError:
            continue
        for child in children:
            path = os.path.join(root, child, TERMINAL_EXE)
            key = os.path.normcase(os.path.normpath(path))
            if key in seen or not os.path.isfile(path):
                continue
            seen.add(key)
            found.append({'path': path, 'folder': child,
                          'used_by': used.get(key)})
    return found


def terminal_clash(raw, name, path):
    """One account needs one PORT, one LOGIN and one TERMINAL.

    A terminal holds a single login, so two accounts sharing an
    installation are one account: both leg runners attach to it, both
    trade whatever it happens to be signed into, and the pair hedges
    against itself.
    """
    path = clean_terminal_path(path)
    if not path:
        return None                # blank = attach to whatever is open
    for other, acct in (raw.get('accounts') or {}).items():
        if other == name:
            continue
        # Cleaned on both sides: one copy quoted and one not is still
        # ONE installation.
        if clean_terminal_path((acct or {}).get('terminal_path')).lower() \
                == path.lower():
            return (f"Account '{other}' already uses this MT5 installation. "
                    f"One terminal serves ONE login, so both legs would end "
                    f"up on the same account. Install a second copy of "
                    f"MetaTrader 5 in its own folder (or use a portable "
                    f"copy) and point this account at that one.")
    return None


def engine_hold_reasons(raw):
    """Why the launcher will not run the ENGINE on this config, one line
    per problem in words, or [].

    ONE list, read by two processes: the launcher, which holds the
    engine on it (`start.check_config`), and the web screen, which says
    WHY on the status line - so a person looking at a screen that is
    not trading is told what to fix and where, instead of "ENGINE DOWN".
    """
    problems = []
    for name, account in (raw.get('accounts') or {}).items():
        account = account or {}
        endpoint = (account.get('endpoint') or '').strip()
        for problem in (endpoint_clash(raw, name, endpoint),
                        login_clash(raw, name, account.get('login')),
                        terminal_clash(raw, name,
                                       account.get('terminal_path')),
                        # A MetaTrader 5 that is not where the account
                        # says. Held with the screen UP - the Exchanges
                        # page is where it is picked - not refused
                        # before the screen exists.
                        terminal_missing(account.get('terminal_path'))):
            if problem:
                problems.append(f"account '{name}': {problem}")
    return problems


def next_free_port(raw, host='127.0.0.1', first=9101):
    used = {(acct or {}).get('endpoint', '') for acct
            in (raw.get('accounts') or {}).values()}
    port = first
    while f'{host}:{port}' in used:
        port += 1
    return f'{host}:{port}'


# --- secrets ------------------------------------------------------------

_ENV_SAFE = re.compile(r'[^A-Z0-9_]')


def env_key_for(account_name):
    """The .env key for an account's password.

    Sanitised, because an account named `Ut 2` produced
    `MT5_PASSWORD_UT 2` — a key with a space, which dotenv cannot parse,
    so the password silently never loaded.
    """
    slug = _ENV_SAFE.sub('_', (account_name or '').upper().strip())
    slug = re.sub(r'_+', '_', slug).strip('_') or 'ACCOUNT'
    return f'MT5_PASSWORD_{slug}'


def env_line(key, value):
    """One `.env` line, quoted so passwords with spaces or `#` survive."""
    escaped = str(value or '').replace('\\', '\\\\').replace('"', '\\"')
    return f'{key}="{escaped}"'


def write_env_value(path, key, value):
    """Set one key in `.env`, leaving the rest of the file alone.

    Written through a tmp file and `os.replace` for the same reason the
    config is: a truncated `.env` is every account's password gone.
    """
    lines = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    replaced = False
    out = []
    for line in lines:
        if line.split('=', 1)[0].strip() == key:
            if not replaced:
                out.append(env_line(key, value))
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(env_line(key, value))
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    atomicfile.replace(tmp, path)
    os.environ[key] = str(value or '')
    try:
        os.chmod(path, 0o600)
    except OSError:               # not every filesystem allows it
        pass
