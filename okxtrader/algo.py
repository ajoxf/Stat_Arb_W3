"""The algos, and the switch that says which one is running.

This system is a MANUAL ladder with an Algo beside it. The rule this
module is where it would be lost:

    Nothing places an order by itself — except a ladder's Algo in LIVE,
    which a person switched on and confirmed, today, and which then has
    that ladder to itself.

What follows DECIDES and says what it would do. **It does not place,
modify or cancel an order itself** — a test reads this file as code and
fails the build if it can reach a broker. What it decides leaves as an
INTENT — "enter, selling the spread", "exit position POS-12, profit
target" — handed to `algodesk`, which records it, and in LIVE hands it
to `algoexec`, the one module that sends.

One algo per ladder, and NONE by default:

**FAIR_SPREAD** — what the basis SHOULD be, on financing alone. It
first asks what kind of pair this is, because the answer changes the
arithmetic:

- *spot vs a future*: the future converges to the spot on ITS expiry,
  so the carry runs to that date;
- *future vs future* (a calendar): the near leg is the one that
  expires first and the spread is decided then, so the carry runs to
  the NEAR expiry;
- *two different instruments*: nothing forces them together and there
  is no fair value to quote. Saying so is the honest answer.

**ALGO** — Bollinger bands on the spread (`B - beta x A`, from the mid):

- *the band*: EMA(N) of the spread's candles, plus and minus
  `entry_z` x sigma (population, last N closes, the forming candle
  included). See `bands`.
- *entry*: SELL the spread when the z of the BID-side spread (what a
  sell receives) is at or above `+entry_z`; BUY when the z of the
  ASK-side spread (what a buy pays) is at or below `-entry_z`. It must
  hold for `confirm_ticks` fresh quotes in a row, and only on a ladder
  with no position open.
- *gates* hold an ENTRY back and say why: no price or a stale/jumping
  one, not enough candles, the cooldown after an exit, the last
  `cutoff_buffer_min` of the session, and a |z| already past
  `max_entry_z` (a blow-out, not a stretch). **A gate never holds back
  an exit.**
- *exit*, for each REAL position on the ladder, measured from its own
  fill: the closing side reaching break-even after every cost plus the
  take-profit (% of margin) — the same TP the Exit panel shows — or
  falling to the STOP LOSS, break-even minus `stop_loss_pct` of margin
  (on by default). Three more, each OFF unless the ladder turns it on:
  a z-stop, a z mean reversion taken only in profit, and a time stop in
  candles.

The switch is per ladder, held in memory, and OFF after every restart:
an algo nobody turned on today is an algo nobody is watching.
"""

import math

#: What can be selected, per ladder. Exactly one, and NONE is the
#: default — an algo nobody asked for is an algo nobody is watching.
NONE = 'NONE'
FAIR_SPREAD = 'FAIR_SPREAD'
ALGO = 'ALGO'
ALGOS = (NONE, FAIR_SPREAD, ALGO)

#: How the Algo's intents are handled. DRY_RUN records and shows them
#: and sends nothing. LIVE also sends them: MARKET both legs to enter,
#: closes by TICKET to exit. LIVE is confirmed every time it is switched
#: on, and while it is on the ladder takes no new MANUAL orders.
DRY_RUN = 'DRY_RUN'
LIVE = 'LIVE'
MODES = (DRY_RUN, LIVE)

#: What kind of pair this is, which decides the fair-value arithmetic.
SPOT_FUTURE = 'SPOT_FUTURE'
FUTURE_FUTURE = 'FUTURE_FUTURE'
RELATED = 'RELATED'


def pair_kind(pair, expiry_a=None, expiry_b=None):
    """Spot vs future, calendar, or two different instruments.

    The OPERATOR declares it. This used to be inferred — both legs
    carry an expiry, so call it a calendar — and that is wrong in the
    one case it matters: UKOILV6 against USOILV6 is two futures with
    two dates and NO carry between them, because Brent and WTI are
    different oil. A calendar is the same underlying in two months,
    and nothing in a symbol's expiry says whether two contracts share
    an underlying.

    So the declaration is authoritative and the dates never change it.
    A pair declared RELATED has no fair spread however many expiries
    its legs report.
    """
    said = (getattr(pair, 'pair_type', None) or SPOT_FUTURE).upper()
    return said if said in (SPOT_FUTURE, FUTURE_FUTURE, RELATED) else RELATED


def carry_nights(kind, days_a, days_b):
    """How many nights the carry runs for, and why.

    A spread is decided on the day the FIRST of its legs converges: for
    spot vs a future that is the future's own expiry; for a calendar it
    is the near leg's. Running a calendar's carry to the far expiry
    prices a trade that is already over.
    """
    if kind == RELATED:
        return None, 'two different instruments — no date decides this pair'
    if kind == FUTURE_FUTURE:
        if days_a is None or days_b is None:
            return None, 'a calendar spread needs BOTH legs’ expiries'
        near = min(days_a, days_b)
        return near, (f'calendar spread — {near:g} night(s) to the NEAR '
                      f'expiry, which is when it is decided')
    if days_b is None:
        return None, "set the future’s expiry to price its carry"
    return days_b, f'spot vs a future — {days_b:g} night(s) to expiry'


# -- ALGO: Bollinger bands on the spread ------------------------------------

#: The timeframes a candle can be, in minutes — the ones MT5 can backfill.
TIMEFRAMES = (1, 5, 15, 30, 60, 240)

#: Every number the Algo reads, and what it is when nothing was typed.
#: Per ladder: a gold basis and an oil differential do not stretch the
#: same way. The take-profit is NOT here — it is the ladder's existing
#: `tp_target_pct_of_margin`, so the exit signal and the TP on the Exit
#: panel are one number, not two that can disagree.
DEFAULT_PARAMS = {
    'entry_z': 2.5,
    #: Which way the Algo may ENTER: BOTH, H_TO_L (sell the spread
    #: only) or L_TO_H (buy it only). Exits are never restricted — a
    #: position on either side always gets out.
    'direction': 'BOTH',
    'timeframe_min': 15,
    'length': 20,
    'confirm_ticks': 3,
    #: No entry signal past this |z|: that is a blow-out, not a stretch.
    #: 0 = no cap.
    'max_entry_z': 3.5,
    #: RE-ENTRY: do not sell the touch of the band — wait for the spread
    #: to come BACK inside it. A side is ARMED when its stretch reaches
    #: the entry z, and enters when it falls back by `reentry_back` (2.0
    #: armed, 1.5 enters). A trend rides the band and never comes back,
    #: so it gives no entry; a range does, a little later. Armed is lost
    #: if the spread reaches the mean first.
    'reentry_on': True,
    'reentry_back': 0.5,
    #: The re-entry WINDOW: how much of the way from the entry level back
    #: to the mean still counts as an entry, in %. 50: entry z 2.0 and
    #: back 0.5 enter between 1.50 and 0.75 - past 0.75 the side
    #: disarms. Without it every z from 1.50 down to 0 was an entry: a
    #: spread armed hours ago signalled at +0.05, with nothing left to
    #: revert. 100 is that old, open window.
    'reentry_window_pct': 50.0,
    #: TREND DIRECTION: when the band's middle (the EMA) has moved more
    #: than `trend_sigma` sigma over the last `trend_lookback_min`, no
    #: entry AGAINST it — no H to L while it rises, no L to H while it
    #: falls. The other side stays open.
    'trend_on': True,
    'trend_sigma': 1.0,
    'trend_lookback_min': 120,
    #: No entry signal in the last this-many minutes before the session
    #: cutoff, or after it. 0 = off.
    'cutoff_buffer_min': 20,
    #: No entry signal for this long after an exit signal or a close.
    'cooldown_min': 5,
    #: WARM-UP: no entry until the Algo has watched this many minutes of
    #: LIVE prices since it was turned on. The band can be full at once
    #: from MT5's history, but history is not this session's tape: a
    #: feed that has not been watched has not been checked. Time with no
    #: price (a leg down, the market shut) does not count. 0 = off.
    'warmup_min': 90,
    #: The STOP LOSS, as a percentage of the margin one spread ties up —
    #: the mirror of the take-profit, measured from the same break-even.
    #: ON by default at the take-profit's own 2%: a signal that says
    #: where to get out in profit and never where to get out in a loss
    #: is half an exit.
    'stop_loss_on': True,
    'stop_loss_pct': 2.0,
    #: How the stop and the target are SIZED: MARGIN (a % of the margin
    #: the trade ties up - the stop above, the ladder's TP % for the
    #: target) or ATR (a multiple of the spread's average true range,
    #: close to close, on the Algo's own candles). ATR sizes itself to
    #: the market: wider when the spread is busy, tighter when quiet.
    #: Either way it is measured from break-even and frozen at entry.
    'stop_mode': 'MARGIN',
    'target_mode': 'MARGIN',
    'atr_period': 14,
    'atr_stop_mult': 2.0,
    'atr_target_mult': 1.5,
    #: Show the SL <- entry -> TP bar while a position is on.
    'progress_bar': True,
    #: What ONE Algo trade is, in spreads (one spread = the ladder's
    #: Leg A lots / Leg B lots). Its own number, so a trader changing the
    #: keypad cannot resize the Algo by accident.
    'algo_qty': 1.0,
    # The day's limits. Any one of them stops ENTRIES for the rest of
    # the broker's day, and says which; exits always carry on. 0 = off.
    'max_trades_day': 10,
    #: In money, the session's Algo P&L — closed plus open — at or below
    #: minus this. Off until a desk sets its own number.
    'daily_loss_limit': 0.0,
    'max_losses_row': 3,
    # The entry FILTERS (see `algofilters`). Each must clear before an
    # entry, in dry run and LIVE alike; collecting candles always blocks.
    #: Edge: expected capture (capture_frac x |z| x sigma, in money) must
    #: be at least edge_multiple x the round-trip cost.
    'edge_on': True,
    'edge_multiple': 1.5,
    'edge_capture_frac': 0.5,
    #: Regime: no entry while the spread is TRENDING.
    'regime_on': True,
    'regime_er_max': 0.6,
    'regime_min_crossings': 4,
    #: Probability / EV — WITHDRAWN for now. It priced the chance of
    #: reaching the mean before the z-stop (4.0), not the Algo's real TP
    #: before its real SL, so it read ~99% on every entry and gated
    #: nothing. Forced off in `clean_params`; the arithmetic stays in
    #: `algofilters` for when it is rebuilt on the real exits.
    'prob_on': False,
    'min_win_prob': 0.60,
    'min_ev': 0.0,
    #: Half-life band in MINUTES: no entry when the spread reverts faster
    #: than the floor (noise) or slower than the ceiling. 0 = that end off.
    'half_life_min_min': 0.0,
    'half_life_max_min': 0.0,
    # The three optional exits — every one OFF until a ladder asks.
    'stop_z_on': False,
    'stop_z': 4.0,
    'reversion_on': False,
    'time_stop_on': False,
    'time_stop_candles': 20,
}

#: The exit settings a SIDE can have its own value of. Blank (None) is
#: the ladder's own value - which, for `tp_pct`, is the ladder's TP % of
#: margin. `stop_loss_on` stays one switch for both sides: a stop is a
#: protection, not a per-side preference.
SIDE_KEYS = ('stop_mode', 'target_mode', 'stop_loss_pct', 'atr_stop_mult',
             'atr_target_mult', 'tp_pct')
SIDES = {'SELL': 'sell', 'BUY': 'buy'}
for _key in SIDE_KEYS:
    for _suffix in SIDES.values():
        DEFAULT_PARAMS[f'{_key}_{_suffix}'] = None


def _side_base(key):
    """`stop_mode_sell` -> `stop_mode`; any other key -> None."""
    for suffix in SIDES.values():
        if key.endswith('_' + suffix) and key[:-len(suffix) - 1] in SIDE_KEYS:
            return key[:-len(suffix) - 1]
    return None


def for_side(params, side):
    """The settings ONE side trades on: the ladder's, with that side's
    own exit values laid over them where it has any. `tp_pct` is the
    side's TP % of margin, or None for the ladder's."""
    suffix = SIDES.get(getattr(side, 'value', side))
    out = dict(params)
    out['tp_pct'] = None
    if suffix is None:
        return out
    for key in SIDE_KEYS:
        value = params.get(f'{key}_{suffix}')
        if value is not None:
            out[key] = value
    return out


_BOOLS = ('stop_z_on', 'reversion_on', 'time_stop_on', 'stop_loss_on',
          'progress_bar', 'edge_on', 'regime_on', 'prob_on', 'reentry_on',
          'trend_on')
_INTS = ('timeframe_min', 'length', 'confirm_ticks', 'time_stop_candles',
         'max_trades_day', 'max_losses_row', 'regime_min_crossings',
         'atr_period')

#: How a stop or a target is sized.
LEVEL_MODES = ('MARGIN', 'ATR')


#: The directions an entry may take, and the side each one is.
DIRECTIONS = {'BOTH': ('SELL', 'BUY'), 'H_TO_L': ('SELL',),
              'L_TO_H': ('BUY',)}


def clean_params(raw):
    """The Algo's settings, every one present and of the right type.

    Blank means the default, never zero: a blank entry z read as 0
    would signal on every tick. A value that will not read is the
    default too — and the save that sent it is told by `check_params`,
    which the settings path calls first.
    """
    out = dict(DEFAULT_PARAMS)
    for key, value in (raw or {}).items():
        if key not in DEFAULT_PARAMS or value in (None, ''):
            continue
        try:
            if key == 'direction':
                chosen = str(value).strip().upper()
                if chosen in DIRECTIONS:
                    out[key] = chosen
                continue
            if key in ('stop_mode', 'target_mode') or \
                    _side_base(key) in ('stop_mode', 'target_mode'):
                chosen = str(value).strip().upper()
                if chosen in LEVEL_MODES:
                    out[key] = chosen
                continue
            if key in _BOOLS:
                out[key] = (value if isinstance(value, bool) else
                            str(value).strip().lower()
                            in ('1', 'true', 'yes', 'on'))
            elif key in _INTS:
                out[key] = int(float(value))
            else:
                out[key] = float(value)
        except (TypeError, ValueError):
            continue
    if out['timeframe_min'] not in TIMEFRAMES:
        out['timeframe_min'] = DEFAULT_PARAMS['timeframe_min']
    out['length'] = max(2, out['length'])
    out['confirm_ticks'] = max(1, out['confirm_ticks'])
    out['time_stop_candles'] = max(1, out['time_stop_candles'])
    # Withdrawn: a config saved while it was offered must not keep it
    # filtering trades from behind a screen that no longer shows it.
    out['prob_on'] = False
    if out['entry_z'] <= 0:
        out['entry_z'] = DEFAULT_PARAMS['entry_z']
    # Re-entry happens INSIDE the band and above the mean: never at or
    # past the mean, where there is nothing left to revert.
    out['reentry_back'] = min(max(out['reentry_back'], 0.05),
                              out['entry_z'] * 0.9)
    out['reentry_window_pct'] = min(max(out['reentry_window_pct'], 5.0),
                                    100.0)
    if out['trend_lookback_min'] <= 0:
        out['trend_lookback_min'] = DEFAULT_PARAMS['trend_lookback_min']
    if out['algo_qty'] <= 0:
        out['algo_qty'] = DEFAULT_PARAMS['algo_qty']
    out['atr_period'] = max(2, out['atr_period'])
    for key in ('atr_stop_mult', 'atr_target_mult'):
        if out[key] <= 0:
            out[key] = DEFAULT_PARAMS[key]
    # A side's own number that is not above 0 is no override at all -
    # never a stop or target of zero.
    for key in list(out):
        if _side_base(key) not in (None, 'stop_mode', 'target_mode') and \
                out[key] is not None and out[key] <= 0:
            out[key] = None
    return out


def check_params(raw):
    """What is WRONG with typed Algo settings, in words, or []."""
    problems = []
    for key, value in (raw or {}).items():
        if key not in DEFAULT_PARAMS:
            problems.append(f'{key} is not an Algo setting')
            continue
        if value in (None, '') or key in _BOOLS:
            continue
        if key == 'direction':
            if str(value).strip().upper() not in DIRECTIONS:
                problems.append(f'direction {value!r} — choose BOTH, '
                                f'H_TO_L or L_TO_H')
            continue
        if key in ('stop_mode', 'target_mode') or \
                _side_base(key) in ('stop_mode', 'target_mode'):
            if str(value).strip().upper() not in LEVEL_MODES:
                problems.append(f'{key} {value!r} — choose MARGIN or ATR')
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            problems.append(f'{key}: {value!r} is not a number')
            continue
        if key == 'timeframe_min' and int(number) not in TIMEFRAMES:
            problems.append(f'timeframe {value} min — choose one of '
                            + ', '.join(str(t) for t in TIMEFRAMES))
        elif key == 'entry_z' and number <= 0:
            problems.append('entry z must be above 0')
        elif key == 'algo_qty' and number <= 0:
            problems.append('Algo qty must be above 0')
        elif key == 'reentry_back' and number <= 0:
            problems.append('re-entry must be above 0 — how far back '
                            'inside the band, in z')
        elif key == 'reentry_window_pct' and not 0 < number <= 100:
            problems.append('re-entry window must be above 0 and at most '
                            '100 % of the way back to the mean')
        elif key == 'trend_sigma' and number <= 0:
            problems.append('trend filter sigma must be above 0')
        elif key == 'atr_period' and number < 2:
            problems.append('ATR period must be 2 candles or more')
        elif _side_base(key) is not None and number <= 0:
            problems.append(f'{key.replace("_", " ")} must be above 0 - '
                            f'leave it blank for the ladder\'s own')
        elif key in ('atr_stop_mult', 'atr_target_mult') and number <= 0:
            problems.append(f'{key.replace("_", " ")} must be above 0')
        elif number < 0:
            problems.append(f'{key} cannot be negative')
    return problems


def zscore(value, mean, sigma):
    """(value - mean) / sigma, or None when any of them is unmeasured."""
    if value is None or mean is None or not sigma:
        return None
    return (float(value) - float(mean)) / float(sigma)


_POSITION_DISPLAY = ('quantity', 'opened_at', 'age_sec', 'source',
                     'entry_atr', 'stop_mode', 'target_mode',
                     'tp_money', 'sl_money', 'atr_stop_mult',
                     'atr_target_mult',
                     'leg_a_side', 'leg_a_entry', 'leg_a_now',
                     'leg_b_side', 'leg_b_entry', 'leg_b_now')


def reentry_window(p):
    """(enters at, window ends at), as |z|, for re-entry: 2.0 armed,
    back 0.5, window 50 % -> (1.50, 0.75)."""
    back_at = p['entry_z'] - p['reentry_back']
    return back_at, back_at * (1.0 - p['reentry_window_pct'] / 100.0)


class AlgoSignal:
    """One ladder's Algo: it watches, decides, and says so.

    `evaluate` is called on every poll with what the coordinator already
    has — the market, the band, the ladder's open positions and the
    gates — and returns what the Algo says NOW, plus the INTENTS that
    became true on this call. An intent is reported once, on the edge
    where it becomes true, so the record is one line per signal and not
    one per poll.
    """

    def __init__(self, params=None):
        self.params = clean_params(params)
        self._streak = {'BUY': 0, 'SELL': 0}
        self._last_quote = None
        self._entry_live = None          # the side whose signal is showing
        self._exits_live = {}            # position id -> reason showing
        self._known = set()              # position ids seen last call
        self._cooldown_until = None
        #: Re-entry: which sides have stretched to the entry z and are
        #: waiting for the spread to come back inside.
        self._armed = {'BUY': False, 'SELL': False}

    def evaluate(self, now, md, stats, positions=(), gates=None):
        """What the Algo says, and what it decided on this call.

        - `now`: seconds, on the clock the cooldown and the time stop
          are measured on.
        - `md`: the pair's market — `short_spread` (what a sell
          receives), `long_spread` (what a buy pays), `mid_spread`,
          `quote_id`. None when a leg has no price.
        - `stats`: `bands.SpreadCandles.stats()`.
        - `positions`: this ladder's OPEN positions, each {position_id,
          side, entry_spread, opened_at, break_even, tp, sl, net_pnl}.
          `tp` or `sl` None means that level is not priced (or the stop
          is off), and no exit is signalled on a number that does not
          exist.
        - `gates`: {'health': why the price cannot be trusted, or None;
          'warmup': {sec, need_sec, done} — live time watched since the
          Algo was turned on; no entry until it is done;
          'cutoff_min': minutes to the session cutoff, negative past
          it, None unmeasured; 'session': why this ladder's own hours
          hold an entry (closed, or its daily break), or None; 'halt': a day's limit that has been hit,
          in words, or None; 'entry_check': f(side, z) -> why a
          stretch that HAS confirmed may still not be entered (the
          filters), or None}.
        """
        p = self.params
        gates = gates or {}
        stats = stats or {}
        positions = list(positions or ())
        ready = bool(stats.get('ready'))
        mean = stats.get('mean') if ready else None
        sigma = stats.get('sigma') if ready else None
        band = None if not ready else p['entry_z'] * sigma
        body = {
            'algo': ALGO, 'params': dict(p),
            'ready': ready, 'count': stats.get('count'),
            'needed': stats.get('needed'), 'note': stats.get('note'),
            'mean': mean, 'sigma': sigma,
            'upper': None if band is None else mean + band,
            'lower': None if band is None else mean - band,
            'z_buy': None, 'z_sell': None, 'z_mid': None,
            'state': 'WATCHING', 'signal': None, 'blocked': None,
            'health': gates.get('health'),
            'cooldown_sec': None, 'positions': [], 'intents': [],
            'warmup': gates.get('warmup')}
        short = (md or {}).get('short_spread')
        long_ = (md or {}).get('long_spread')
        if md and ready:
            body['z_sell'] = zscore(short, mean, sigma)
            body['z_buy'] = zscore(long_, mean, sigma)
            body['z_mid'] = zscore(md.get('mid_spread'), mean, sigma)

        # A position that has gone since the last call starts the
        # cooldown: whatever closed it, the next entry waits.
        current = {pos['position_id'] for pos in positions}
        if self._known - current:
            self._start_cooldown(now)
        self._known = current
        for gone in set(self._exits_live) - current:
            del self._exits_live[gone]

        fresh = self._fresh_quote(md)
        # A price the feed guards hold back - a stale leg, a spread that
        # jumped - is not one the Algo may ARM on either. Arming is half
        # the entry: a glitch that put one leg's old price against the
        # other's new one read as a 7-sigma stretch, armed re-entry, and
        # the first real price back inside the window would have been
        # taken as the stretch coming back. It only ever withheld the
        # ENTRY, so the arming survived the glitch.
        trusted = bool(md) and not gates.get('health') and \
            not md.get('jump_reason')
        self._count(body, fresh, trusted)

        for pos in positions:
            body['positions'].append(self._judge_exit(now, md, pos, body))

        if self._cooldown_until is not None and now < self._cooldown_until:
            body['cooldown_sec'] = self._cooldown_until - now

        if positions:
            body['state'] = 'IN_POSITION'
            self._entry_live = None
            if any(row['exit'] for row in body['positions']):
                body['state'] = 'EXIT'
            return body

        self._judge_entry(body, md, gates)
        return body

    # -- entry ------------------------------------------------------------

    def _fresh_quote(self, md):
        """Is this a NEW price, or the same one polled again?

        "Three ticks in a row" means three prices. A poll that finds the
        same quote is not a second confirmation of it.
        """
        if not md:
            self._last_quote = None
            return False
        quote = md.get('quote_id')
        if quote is None:
            return True
        if quote == self._last_quote:
            return False
        self._last_quote = quote
        return True

    def _count(self, body, fresh, trusted=True):
        p = self.params
        entry = p['entry_z']
        z_sell, z_buy = body['z_sell'], body['z_buy']
        if p['reentry_on']:
            back_at, floor = reentry_window(p)
            # Armed by the stretch; disarmed once the spread is through
            # the window's far edge - it came back too far, too fast, to
            # leave anything worth trading (at 100 % that edge is the
            # mean, as it always was).
            # Only on a price the desk would trade on: a guarded one
            # neither arms nor disarms. It can still confirm an arming
            # it did not make, and the entry it confirms is then held
            # back by the same guard, in its words.
            if z_sell is not None and trusted:
                if z_sell >= entry:
                    self._armed['SELL'] = True
                elif z_sell <= floor:
                    self._armed['SELL'] = False
            if z_buy is not None and trusted:
                if z_buy <= -entry:
                    self._armed['BUY'] = True
                elif z_buy >= -floor:
                    self._armed['BUY'] = False
            # The entry is the way back IN: inside the band, inside the
            # window - never near the mean.
            hits = {'SELL': (self._armed['SELL'] and z_sell is not None
                             and floor < z_sell <= back_at),
                    'BUY': (self._armed['BUY'] and z_buy is not None
                            and -back_at <= z_buy < -floor)}
        else:
            self._armed = {'BUY': False, 'SELL': False}
            hits = {'SELL': z_sell is not None and z_sell >= entry,
                    'BUY': z_buy is not None and z_buy <= -entry}
        body['armed'] = dict(self._armed)
        for side, hit in hits.items():
            if not hit:
                self._streak[side] = 0
            elif fresh:
                self._streak[side] += 1
        body['streak'] = dict(self._streak)

    def _judge_entry(self, body, md, gates):
        p = self.params
        side = None
        allowed = DIRECTIONS.get(p['direction'], DIRECTIONS['BOTH'])
        for candidate in ('SELL', 'BUY'):
            if candidate not in allowed:
                # This ladder only enters the other way. The stretch is
                # still shown; it is just not an entry here.
                continue
            if self._streak[candidate] >= p['confirm_ticks']:
                side = candidate
        blocked = self._entry_gate(body, md, gates, side)
        if side is None:
            self._entry_live = None
            if blocked:
                body['state'] = 'BLOCKED'
                body['blocked'] = blocked
            elif any(self._streak.values()):
                body['state'] = 'CONFIRMING'
            return
        if blocked:
            body['state'] = 'BLOCKED'
            body['blocked'] = blocked
            # A signal that WOULD have entered: the side and z it was
            # held back at, for "Last signal blocked".
            body['blocked_side'] = side
            body['blocked_z'] = (body['z_sell'] if side == 'SELL'
                                 else body['z_buy'])
            self._entry_live = None
            return
        body['state'] = 'SIGNAL'
        body['signal'] = side
        if self._entry_live != side:
            self._entry_live = side
            # An arming is spent by the entry it gave.
            self._armed[side] = False
            body['armed'] = dict(self._armed)
            z = body['z_sell'] if side == 'SELL' else body['z_buy']
            body['intents'].append({
                'action': 'ENTER', 'side': side, 'z': z,
                'spread': md.get('short_spread' if side == 'SELL'
                                 else 'long_spread'),
                'mid_spread': md.get('mid_spread'),
                'mean': body['mean'], 'sigma': body['sigma'],
                'upper': body['upper'], 'lower': body['lower'],
                'entry_z': p['entry_z']})

    def _entry_gate(self, body, md, gates, side):
        """Why an entry is held back now, in words, or None."""
        p = self.params
        if not md:
            return 'no price on one leg'
        if gates.get('health'):
            return gates['health']
        if gates.get('size'):
            # The Algo qty cannot be traded at all: say so all the time,
            # not only at the moment an order would have been refused.
            return 'Algo qty: ' + gates['size']
        if gates.get('halt'):
            # A day's limit: the trades, the loss, the losing run.
            return gates['halt']
        if not body['ready']:
            return (body.get('note') or
                    f"collecting candles {body.get('count') or 0}"
                    f"/{body.get('needed')}")
        warmup = gates.get('warmup')
        if warmup and not warmup.get('done'):
            # Whole minutes DONE, as the panel's bar counts them: a
            # half-minute is not "1 of 90" here and "0/90" beside it.
            return (f"warming up: {int(warmup['sec'] // 60)} of "
                    f"{round(warmup['need_sec'] / 60.0)} min of live "
                    f"prices watched")
        if body.get('cooldown_sec'):
            return f"cooldown {_mmss(body['cooldown_sec'])}"
        if gates.get('session'):
            return gates['session']
        levels = gates.get('levels')
        if isinstance(levels, dict):
            # Each side has its own exits, so its own answer. With no
            # signal yet, only a block on BOTH sides holds the ladder.
            levels = (levels.get(side) if side is not None else
                      (levels.get('SELL') if levels.get('SELL') and
                       levels.get('BUY') else None))
        if levels:
            return levels
        buffer_min = p['cutoff_buffer_min']
        cutoff = gates.get('cutoff_min')
        if buffer_min and cutoff is not None and cutoff <= buffer_min:
            return ('past the session cutoff' if cutoff <= 0 else
                    f'{cutoff:.0f} min to the session cutoff')
        if side is not None and p['max_entry_z']:
            z = body['z_sell'] if side == 'SELL' else body['z_buy']
            if z is not None and abs(z) > p['max_entry_z']:
                return (f'z {z:+.2f} is past the {p["max_entry_z"]:g} cap — '
                        f'a blow-out, not a stretch')
        check = gates.get('entry_check')
        if side is not None and check is not None:
            # The filters — edge, regime, probability, half-life — judged
            # at the z this entry would actually be taken at.
            z = body['z_sell'] if side == 'SELL' else body['z_buy']
            return check(side, z)
        return None

    def entry_failed(self, now):
        """An entry that was SENT and did not go on. The cooldown starts,
        and the signal may fire again after it — not on the next poll,
        which would send the same refused order three times a second."""
        self._entry_live = None
        self._start_cooldown(now)

    def exit_failed(self, position_id):
        """An exit that was sent and did not close: report it again, so
        it is retried. The desk paces the retries."""
        self._exits_live.pop(position_id, None)

    def _start_cooldown(self, now):
        minutes = self.params['cooldown_min']
        if minutes:
            self._cooldown_until = now + 60.0 * minutes

    # -- exit -------------------------------------------------------------

    def _judge_exit(self, now, md, pos, body):
        """Should THIS position come off, and why. Never gated."""
        p = self.params
        side = pos.get('side')
        closing = (md or {}).get('short_spread' if side == 'BUY'
                                 else 'long_spread')
        z_close = zscore(closing, body['mean'], body['sigma'])
        tp = pos.get('tp')
        sl = pos.get('sl')
        be = pos.get('break_even')
        entry = pos.get('entry_spread')
        row = {'position_id': pos['position_id'], 'side': side,
               'entry_spread': entry, 'closing_spread': closing,
               'z_close': z_close, 'tp': tp, 'sl': sl, 'break_even': be,
               'net_pnl': pos.get('net_pnl'), 'exit': None,
               'progress': progress(side, entry, closing, tp, sl)}
        # What the panel shows about the position itself — its size, its
        # age and each leg's fill — passes through untouched. Dropping it
        # made the panel fall back to "1.00 spread(s)" for a 0.5 position.
        for name in _POSITION_DISPLAY:
            if name in pos:
                row[name] = pos[name]
        reason = None
        if closing is not None and sl is not None and (
                closing <= sl if side == 'BUY' else closing >= sl):
            reason = 'STOP_LOSS'
        elif closing is not None and tp is not None and (
                closing >= tp if side == 'BUY' else closing <= tp):
            reason = 'PROFIT_TARGET'
        elif p['stop_z_on'] and z_close is not None and (
                z_close <= -p['stop_z'] if side == 'BUY'
                else z_close >= p['stop_z']):
            reason = 'Z_STOP'
        elif p['reversion_on'] and z_close is not None and be is not None \
                and closing is not None and (
                    (z_close >= 0 and closing >= be) if side == 'BUY'
                    else (z_close <= 0 and closing <= be)):
            reason = 'MEAN_REVERSION'
        elif p['time_stop_on'] and pos.get('opened_at') is not None and (
                now - float(pos['opened_at'])
                >= p['time_stop_candles'] * p['timeframe_min'] * 60.0):
            reason = 'TIME_STOP'
        row['exit'] = reason
        if reason is None:
            self._exits_live.pop(pos['position_id'], None)
            return row
        if self._exits_live.get(pos['position_id']) != reason:
            self._exits_live[pos['position_id']] = reason
            self._start_cooldown(now)
            body['intents'].append({
                'action': 'EXIT', 'position_id': pos['position_id'],
                'side': side, 'reason': reason, 'spread': closing,
                'z': z_close, 'entry_spread': entry,
                'break_even': be, 'tp': tp, 'sl': sl,
                'net_pnl': pos.get('net_pnl')})
        return row


def progress(side, entry, closing, tp, sl):
    """Where the closing price sits between the stop and the target.

    +1.0 is AT the take-profit, -1.0 is AT the stop loss, 0 is the
    entry; past either end it is clamped there. The two halves are
    scaled separately, because the stop and the target are rarely the
    same distance from the entry. None when the closing price or the
    entry is unknown — and a missing stop or target leaves only its own
    half unmeasured.
    """
    if closing is None or entry is None:
        return None
    sign = 1.0 if side == 'BUY' else -1.0
    gained = sign * (float(closing) - float(entry))
    if gained >= 0:
        if tp is None:
            return None
        room = sign * (float(tp) - float(entry))
        return 1.0 if room <= 0 else min(1.0, gained / room)
    if sl is None:
        return None
    room = sign * (float(entry) - float(sl))
    return -1.0 if room <= 0 else max(-1.0, gained / room)


#: The reasons an exit is signalled, in the trader's words.
EXIT_WORDS = {
    'STOP_LOSS': 'stop loss',
    'PROFIT_TARGET': 'profit target (after costs)',
    'Z_STOP': 'z-stop',
    'MEAN_REVERSION': 'back to the mean, in profit',
    'TIME_STOP': 'time stop',
}


def _mmss(seconds):
    seconds = max(0, int(math.ceil(seconds)))
    return f'{seconds // 60}:{seconds % 60:02d}'
