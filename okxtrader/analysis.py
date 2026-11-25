"""The Analysis tab: what the closed trades say, in numbers.

Ported from arrow-statarb's Analysis page and fitted to this desk —
money in the account currency, positions by ticket, a spread of
`Leg B - beta x Leg A`. Seven readings:

- **Summary**: trades, closed, net P&L, win rate.
- **Drawdown**: the equity curve's worst fall, and each trade's MAE
  (worst point against it) and MFE (best point for it).
- **Take / hold calibration**: where trades PEAKED, so the take-profit
  is set from measured peaks rather than opinion.
- **What-if-held shadow**: after an exit that was not a clean target,
  the position is marked on its own entry for an hour more — did the
  spread come back to break-even, or to the target? Often it did, and
  the exit was premature; or it did not, and the exit was right.
- **Z-score excursions**: how often the spread touches +-2 and +-3
  sigma on the Algo's own band, and how often a 2-sigma stretch comes
  all the way back to the mean.
- **Trade journal**: every round trip, entry to exit.
- (The backtest is the Algo's own, run from here.)

NOTHING HERE CAN REACH AN ORDER. It takes numbers and returns numbers;
a test keeps its imports to the standard library.

Unmeasured is not zero: a trade whose P&L or peak was never measured is
left out of the figure it would distort, and an empty set reads as a
dash, never 0.
"""

import math

#: How long a shadow keeps marking a closed position.
SHADOW_WINDOW_SEC = 3600.0


# -- while a position is open: its best and worst point ----------------------

def observe_extremes(position, net_pnl, now):
    """Fold this poll's net P&L into the position's peak and trough.

    Returns True when either moved. Minutes are from the position's own
    open. A poll with no measurable P&L changes nothing.
    """
    if net_pnl is None:
        return False
    opened = getattr(position, 'opened_at', None)
    if opened is None:
        opened = now
    minutes = round(max(0.0, now - opened) / 60.0, 1)
    changed = False
    peak = getattr(position, 'peak_pnl', None)
    if peak is None or net_pnl > peak:
        position.peak_pnl = round(float(net_pnl), 2)
        position.peak_min = minutes
        changed = True
    trough = getattr(position, 'trough_pnl', None)
    if trough is None or net_pnl < trough:
        position.trough_pnl = round(float(net_pnl), 2)
        position.trough_min = minutes
        changed = True
    return changed


# -- after a position closes: the what-if-held shadow ------------------------

def is_clean_target(close_reason):
    """A take-profit that banked: nothing for a shadow to learn."""
    return 'profit target' in str(close_reason or '').lower()


def arm_shadow(position, fees, target_net, now, window_sec=SHADOW_WINDOW_SEC):
    """A watch on a just-closed position, or None when there is nothing
    to watch: a clean target, or a position that cannot be marked."""
    if is_clean_target(getattr(position, 'close_reason', None)):
        return None
    if position.entry_spread is None or not position.spread_units:
        return None
    return {
        'position_id': position.position_id,
        'pair_key': position.pair_key,
        'side': position.side.value,
        'entry_spread': float(position.entry_spread),
        # WHOLE-position k: dollars per 1.00 of spread for all of it.
        'units': float(position.spread_units),
        'fees': float(fees or 0.0),
        'target_net': (None if not target_net else float(target_net)),
        'exit_reason': position.close_reason,
        'exit_pnl': position.realized_pnl,
        'armed_at': float(now),
        'window_sec': float(window_sec),
        'peak_net': None, 'peak_min': None,
        'reverted_be': False, 'be_min': None,
        'reverted_target': False, 'target_min': None,
        'done': False,
    }


def shadow_net(watch, closing_spread):
    """What the closed position WOULD be making now, net of commission."""
    move = float(closing_spread) - watch['entry_spread']
    if watch['side'] == 'SELL':
        move = -move
    return move * watch['units'] - watch['fees']


def update_shadow(watch, closing_spread, now):
    """Mark one watch against the price it would close at. Returns True
    when it changed. Finished at the end of its window — downtime
    included, so a restart does not leave one lingering."""
    if watch.get('done'):
        return False
    changed = False
    minutes = round(max(0.0, now - watch['armed_at']) / 60.0, 1)
    if closing_spread is not None:
        net = shadow_net(watch, closing_spread)
        if watch['peak_net'] is None or net > watch['peak_net']:
            watch['peak_net'] = round(net, 2)
            watch['peak_min'] = minutes
            changed = True
        if net >= 0 and not watch['reverted_be']:
            watch['reverted_be'] = True
            watch['be_min'] = minutes
            changed = True
        if watch['target_net'] and net >= watch['target_net'] \
                and not watch['reverted_target']:
            watch['reverted_target'] = True
            watch['target_min'] = minutes
            changed = True
    if now - watch['armed_at'] >= watch['window_sec']:
        watch['done'] = True
        changed = True
    return changed


def shadow_summary(watches):
    watches = list(watches or ())
    active = [w for w in watches if not w.get('done')]
    done = [w for w in watches if w.get('done')]
    be = [w for w in done if w.get('reverted_be')]
    target = [w for w in done if w.get('reverted_target')]
    be_mins = [w['be_min'] for w in be if w.get('be_min') is not None]
    return {
        'active': len(active), 'completed': len(done),
        'reverted_be': len(be), 'reverted_target': len(target),
        'revert_be_rate': _rate(len(be), len(done)),
        'revert_target_rate': _rate(len(target), len(done)),
        'avg_revert_min': (round(sum(be_mins) / len(be_mins), 1)
                           if be_mins else None),
        'watches': sorted(watches, key=lambda w: -w.get('armed_at', 0))[:50],
    }


# -- the Algo's band: z-score excursions -------------------------------------

class ZExcursions:
    """Touches of +-2 and +-3 sigma, and returns to the mean.

    A touch is counted on the OUTWARD crossing of its band, then not
    again until z is back inside +-1 (so a spread sitting on the band
    is one touch, not a hundred). A reversion is a 2-sigma stretch
    that crosses back through 0.
    """

    MAX_EVENTS = 200

    def __init__(self, data=None, now=0.0):
        data = dict(data or {})
        self.counts = dict({'touch_2_up': 0, 'touch_2_down': 0,
                            'touch_3_up': 0, 'touch_3_down': 0,
                            'reversions': 0},
                           **(data.get('counts') or {}))
        self.max_z = data.get('max_z')
        self.min_z = data.get('min_z')
        self.since = data.get('since') or now
        self.events = list(data.get('events') or [])
        #: WHERE IT HAD GOT TO, restored - not reset.
        #:
        #: These four used to live only in memory. Every restart
        #: therefore re-armed all four bands, and a spread parked
        #: beyond +2 sigma logged a fresh touch on the first poll
        #: after it: six restarts, six touches, one stretch. The
        #: counts were saved and the reason they were safe to add to
        #: was not.
        self._prev = data.get('prev')
        self._arm = dict({'2u': True, '3u': True, '2d': True, '3d': True},
                         **(data.get('arm') or {}))
        self._open_up = bool(data.get('open_up'))
        self._open_down = bool(data.get('open_down'))

    def to_dict(self):
        return {'counts': dict(self.counts), 'max_z': self.max_z,
                'min_z': self.min_z, 'since': self.since,
                'events': self.events[-self.MAX_EVENTS:],
                'prev': self._prev, 'arm': dict(self._arm),
                'open_up': self._open_up, 'open_down': self._open_down}

    def _event(self, kind, z, at):
        self.counts[kind] += 1
        self.events.append({'at': at, 'type': kind, 'z': round(z, 2)})
        del self.events[:-self.MAX_EVENTS]

    def tally(self, z, at):
        """Fold one z in. Returns 'event' when a touch or reversion was
        counted, 'extreme' when only the max/min moved, else None."""
        if z is None or not math.isfinite(z):
            return None
        what = None
        if self.max_z is None or z > self.max_z:
            self.max_z = round(z, 3)
            what = 'extreme'
        if self.min_z is None or z < self.min_z:
            self.min_z = round(z, 3)
            what = 'extreme'
        if z >= 2.0 and self._arm['2u']:
            self._event('touch_2_up', z, at)
            self._arm['2u'] = False
            self._open_up = True
            what = 'event'
        if z >= 3.0 and self._arm['3u']:
            self._event('touch_3_up', z, at)
            self._arm['3u'] = False
            what = 'event'
        if z < 1.0:
            self._arm['2u'] = self._arm['3u'] = True
        if z <= -2.0 and self._arm['2d']:
            self._event('touch_2_down', z, at)
            self._arm['2d'] = False
            self._open_down = True
            what = 'event'
        if z <= -3.0 and self._arm['3d']:
            self._event('touch_3_down', z, at)
            self._arm['3d'] = False
            what = 'event'
        if z > -1.0:
            self._arm['2d'] = self._arm['3d'] = True
        if self._prev is not None:
            if self._open_up and self._prev > 0 >= z:
                self._event('reversions', z, at)
                self._open_up = False
                what = 'event'
            elif self._open_down and self._prev < 0 <= z:
                self._event('reversions', z, at)
                self._open_down = False
                what = 'event'
        self._prev = z
        return what


def excursions_view(data, now):
    data = dict(data or {})
    counts = dict(data.get('counts') or {})
    for name in ('touch_2_up', 'touch_2_down', 'touch_3_up', 'touch_3_down',
                 'reversions'):
        counts.setdefault(name, 0)
    since = data.get('since')
    return dict(counts,
                touch_2_total=counts['touch_2_up'] + counts['touch_2_down'],
                touch_3_total=counts['touch_3_up'] + counts['touch_3_down'],
                max_z=data.get('max_z'), min_z=data.get('min_z'),
                since=since,
                since_sec=(None if since is None else max(0.0, now - since)),
                events=list(reversed(data.get('events') or []))[:100])


# -- over closed trades ------------------------------------------------------

def _rate(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def _pnl(row):
    value = row.get('realized_pnl')
    return None if value is None else float(value)


def _gross(row):
    """Closed: (exit - entry) x k, signed for the side. None if any part
    of it was not measured."""
    try:
        entry = float(row['entry_spread'])
        exit_ = float(row['exit_spread'])
        units = float(row['spread_units'])
    except (KeyError, TypeError, ValueError):
        return None
    sign = 1.0 if row.get('side') == 'BUY' else -1.0
    return (exit_ - entry) * sign * units


def returns(row, pnl):
    """Net P&L as a share of the margin BOTH legs tied up at entry -
    the base the TP % and SL % are set on - and of the notional (both
    legs' face value). Each None when its base was not measured."""
    margin = row.get('entry_margin')
    quantity = row.get('quantity')
    margin = (float(margin) * float(quantity or 1.0)) if margin else None
    notional = 0.0
    for leg in ('a', 'b'):
        fill = row.get('leg_' + leg) or {}
        try:
            notional += (float(fill['volume']) * float(fill['contract_size'])
                         * float(fill['price']))
        except (KeyError, TypeError, ValueError):
            notional = None
            break
    return {'margin': None if margin is None else round(margin, 2),
            'margin_estimated': row.get('entry_margin_source') == 'leverage',
            'pct_margin': (None if pnl is None or not margin
                           else round(pnl / margin * 100.0, 4)),
            'notional': None if not notional else round(notional, 2),
            'pct_notional': (None if pnl is None or not notional
                             else round(pnl / notional * 100.0, 6))}


def on_capital(pnl, margin, notional, capital):
    """One trade against the CAPITAL the trader manages: its net as a
    share of it (return on capital), the margin it tied up as a share of
    it (capital used), and its two legs' face value over it (exposure,
    x). Each None while the capital is not set or its base was not
    measured."""
    capital = float(capital or 0.0)
    if capital <= 0:
        return {'pct_capital': None, 'pct_capital_used': None,
                'exposure': None}
    return {'pct_capital': (None if pnl is None
                            else round(pnl / capital * 100.0, 4)),
            'pct_capital_used': (None if not margin
                                 else round(margin / capital * 100.0, 2)),
            'exposure': (None if not notional
                         else round(notional / capital, 3))}


def summary(positions, capital=0.0):
    """Trades opened in the window, the closed ones' net and win rate."""
    rows = list(positions or ())
    closed = [r for r in rows if r.get('closed_at') is not None
              and _pnl(r) is not None]
    wins = [r for r in closed if _pnl(r) > 0]
    # RETURN ON MARGIN over the trades whose margin WAS measured: their
    # net over the margin they used. A trade with no margin is left out
    # of both sides, never counted as using none.
    based = [(r, returns(r, _pnl(r))['margin']) for r in closed]
    based = [(r, m) for r, m in based if m]
    used = sum(m for _r, m in based)
    # ON CAPITAL: the window's net over the capital managed, and how
    # much of it the trades tied up - on average and at most, each trade
    # by its own margin.
    capital = float(capital or 0.0)
    shares = ([m / capital * 100.0 for _r, m in based] if capital > 0
              else [])
    net = sum(_pnl(r) for r in closed) if closed else None
    return {'capital': capital or None,
            'return_on_capital': (round(net / capital * 100.0, 4)
                                  if capital > 0 and net is not None
                                  else None),
            'capital_used_avg': (round(sum(shares) / len(shares), 2)
                                 if shares else None),
            'capital_used_max': round(max(shares), 2) if shares else None,
            'count': len(rows), 'closed': len(closed),
            'open': sum(1 for r in rows if r.get('closed_at') is None),
            'net_pnl': (round(sum(_pnl(r) for r in closed), 2)
                        if closed else None),
            'wins': len(wins), 'losses': len(closed) - len(wins),
            'win_rate': _rate(len(wins), len(closed)),
            'return_on_margin': (round(sum(_pnl(r) for r, _m in based)
                                       / used * 100.0, 4) if used else None),
            'margin_trades': len(based)}


def drawdown(positions, capital=0.0):
    """The equity curve over closed trades, oldest first: its peak, its
    worst fall from a peak, and how far below the peak it is now.

    And the DEEPEST OPEN DRAWDOWN: the same curve, but each trade taken
    at its worst point while it was still open (its MAE, `trough_pnl`)
    rather than where it closed - the low the account actually went
    through. A trade that dipped to -$233 and closed at -$91 was a $233
    fall, not a $91 one. Each trade is counted from the closed P&L
    before it; trades open at the same time are counted one at a time,
    so it is the deepest any ONE trade took the account. Against
    `capital` (the capital managed) as a %, where it is set."""
    closed = sorted((r for r in positions or ()
                     if r.get('closed_at') is not None
                     and _pnl(r) is not None),
                    key=lambda r: r['closed_at'])
    if not closed:
        return {'trades': 0, 'max': None, 'max_pct': None, 'current': None,
                'peak_equity': None, 'worst_trade': None, 'best_trade': None,
                'max_pct_capital': None, 'open_max': None,
                'open_max_pct_capital': None, 'open_max_trade': None}
    peak = running = worst = 0.0
    deepest, deepest_row = None, None
    for row in closed:
        trough = row.get('trough_pnl')
        if trough is not None and float(trough) < 0:
            fall = peak - (running + float(trough))
            if deepest is None or fall > deepest:
                deepest, deepest_row = fall, row
        running += _pnl(row)
        peak = max(peak, running)
        worst = max(worst, peak - running)
    current = max(0.0, peak - running)
    capital = float(capital or 0.0)

    def on_capital(money):
        return (round(100.0 * money / capital, 2)
                if capital > 0 and money is not None else None)
    deepest = None if deepest is None else round(max(deepest, worst), 2)
    return {'trades': len(closed), 'max': round(worst, 2),
            'max_pct_capital': on_capital(worst),
            'open_max': deepest,
            'open_max_pct_capital': on_capital(deepest),
            'open_max_trade': None if deepest_row is None else {
                'position_id': deepest_row.get('position_id'),
                'pair_key': deepest_row.get('pair_key'),
                'side': deepest_row.get('side'),
                'closed_at': deepest_row.get('closed_at'),
                'trough_pnl': round(float(deepest_row['trough_pnl']), 2),
                'pnl': round(_pnl(deepest_row), 2)},
            'max_pct': (round(100.0 * worst / peak, 1) if peak else None),
            'current': round(current, 2), 'peak_equity': round(peak, 2),
            'equity': round(running, 2),
            'worst_trade': round(min(_pnl(r) for r in closed), 2),
            'best_trade': round(max(_pnl(r) for r in closed), 2)}


def _num(value, places=4):
    """A spread price as the screen prints it, or None.

    Trailing zeros are trimmed back to two decimals: an oil spread is
    quoted in cents and `71.4000` reads as four digits of precision
    this system does not have, while an FX spread genuinely needs them.
    """
    if value is None:
        return None
    try:
        text = f'{float(value):,.{places}f}'
    except (TypeError, ValueError):
        return None
    if '.' not in text:
        return text
    whole, _, decimals = text.partition('.')
    decimals = decimals.rstrip('0')
    while len(decimals) < 2:
        decimals += '0'
    return f'{whole}.{decimals}'


def _lots(leg):
    """One leg's volume, or None. `leg` is the stored JSON dict."""
    if not isinstance(leg, dict):
        return None
    volume = leg.get('volume')
    return None if volume is None else float(volume)


#: The exit reasons the Algo itself signals, and which level each one
#: was measured against. Anything else - a cutoff, a kill, a close by
#: hand, a force-clear - is not a level being touched and is left in
#: its own words.
_LEVEL_OF_EXIT = (('stop loss', 'sl'), ('profit target', 'tp'))


def exit_columns(row):
    """WHY this trade ended, as COLUMNS rather than a sentence.

    'Algo: stop loss' on its own does not say what the stop was, how it
    was arrived at, or how far past it the close went - which is the
    difference between a stop that was hit and a stop that was set in
    the wrong place. These were written as one prose line under each
    row first; a journal is a table, and numbers that matter belong in
    columns where they line up down the page and can be compared at a
    glance.

    The levels are the ones the position was last judged against. A
    trade closed before they were kept has none and says so, rather
    than guessing a level from today's settings.
    """
    levels = row.get('exit_levels') or {}
    reason = (row.get('close_reason') or '').lower()
    side = row.get('side')
    sign = 1.0 if side == 'BUY' else -1.0
    which = None
    for words, name in _LEVEL_OF_EXIT:
        if words in reason:
            which = name
    out = {'break_even': levels.get('break_even'),
           'tp': levels.get('tp'), 'sl': levels.get('sl'),
           # Which level this exit was measured against, so the screen
           # can mark that one cell rather than all three.
           'on_level': which,
           'sized_by': _how_the_level_was_set(which, levels)
           if which else None,
           'level_money': (levels.get('sl_money') if which == 'sl' else
                           levels.get('tp_money') if which == 'tp' else None),
           'gap': None, 'gap_word': None, 'note': None}
    level = levels.get(which) if which else None
    exit_spread = row.get('exit_spread')
    if which and level is None:
        out['note'] = ('the level it was measured against was not '
                       'recorded \u2014 kept from this version on')
    elif which and exit_spread is not None:
        past = sign * (float(exit_spread) - float(level))
        past = past if which == 'tp' else -past
        out['gap'] = round(abs(past), 6)
        out['gap_word'] = 'past' if past >= 0 else 'short of'
    return out


def _how_the_level_was_set(which, levels):
    """The SETTING behind a stop or a target, in its own numbers."""
    mode = levels.get('stop_mode' if which == 'sl' else 'target_mode')
    if mode == 'ATR':
        mult = levels.get('atr_stop_mult' if which == 'sl'
                          else 'atr_target_mult')
        atr = levels.get('entry_atr')
        if mult is not None and atr is not None:
            return f'{mult:g} \u00d7 ATR {_num(atr)}'
        return 'ATR at entry, not measured'
    pct = levels.get('stop_loss_pct' if which == 'sl' else 'tp_pct')
    margin = levels.get('entry_margin')
    if pct is None:
        return None
    said = f'{float(pct):g}% of margin'
    if margin is not None:
        said += f' ({margin:,.2f}/spread)'
    return said


def excursion_row(row):
    """One closed trade: how bad it got, how good it got, and how much
    of its best it kept (utilisation)."""
    pnl = _pnl(row)
    peak = row.get('peak_pnl')
    trough = row.get('trough_pnl')
    mfe = None if peak is None else max(float(peak), 0.0)
    mae = None if trough is None else abs(min(float(trough), 0.0))
    return {'position_id': row.get('position_id'),
            'pair_key': row.get('pair_key'), 'side': row.get('side'),
            'source': row.get('source') or 'MANUAL',
            'closed_at': row.get('closed_at'),
            'exit_reason': row.get('close_reason'),
            # Entry, exit and the size are NOT repeated here: the
            # trade journal below carries them for the same trades, and
            # one page saying the same thing twice is one page nobody
            # reads twice. This table is about the EXTREMES.
            'entry_spread': row.get('entry_spread'),
            'exit_spread': row.get('exit_spread'),
            'lots_a': _lots(row.get('leg_a')),
            'lots_b': _lots(row.get('leg_b')),
            'mae': None if mae is None else round(mae, 2),
            'mfe': None if mfe is None else round(mfe, 2),
            'pnl': None if pnl is None else round(pnl, 2),
            # Only for a winner: "kept -266% of its best" says nothing.
            'utilisation_pct': (round(100.0 * pnl / mfe, 0)
                                if mfe and pnl is not None and pnl > 0
                                else None),
            'peak_min': row.get('peak_min'),
            'trough_min': row.get('trough_min'),
            # The margin BOTH legs tied up at entry: the worst and best
            # points read on it, as the TP % and SL % are set on it.
            'margin': returns(row, pnl)['margin']}


def excursion_rows(positions):
    """Closed trades that carry their extremes, newest first. Trades
    from before the extremes were tracked have none, and are left out
    rather than shown as 0."""
    rows = [excursion_row(r) for r in positions or ()
            if r.get('closed_at') is not None
            and (r.get('peak_pnl') is not None
                 or r.get('trough_pnl') is not None)]
    return sorted(rows, key=lambda r: -(r['closed_at'] or 0))


#: The stops the winners' worst points are tried against, in % of the
#: margin both legs tied up (the base the desk's SL % is set on).
STOP_STUDY_PCTS = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.5, 10.0)


def stop_study(positions, capital=0.0, levels=STOP_STUDY_PCTS):
    """WHERE A STOP WOULD HAVE COST LEAST, from the trades themselves.

    Every closed trade with its worst point (MAE) and its margin: how far
    it went against, as a % of its margin and in spread points. Then,
    for each stop level, what it would have done to THESE trades: a
    trade that went that far against would have been stopped there -
    net -(level x its margin) - instead of closing where it did. That
    cuts the winners that dipped that far and caps the losers.

    The stop is assumed filled AT its level and nothing after it is
    changed: a real stop in a spike fills worse, so the savings are a
    best case and the cost of cutting winners is exact.
    """
    capital = float(capital or 0.0)
    trades = []
    for row in positions or ():
        pnl = _pnl(row)
        trough = row.get('trough_pnl')
        margin = returns(row, pnl)['margin'] if pnl is not None else None
        if row.get('closed_at') is None or pnl is None or trough is None \
                or not margin:
            continue
        mae = max(0.0, -float(trough))
        units = row.get('spread_units')
        trades.append({'pnl': pnl, 'margin': float(margin), 'mae': mae,
                       'mae_pct': 100.0 * mae / float(margin),
                       'mae_points': (mae / float(units) if units else None)})
    winners = [t for t in trades if t['pnl'] > 0]
    losers = [t for t in trades if t['pnl'] <= 0]

    def spread_of(values):
        values = sorted(values)
        if not values:
            return None
        return {'median': round(percentile(values, 50), 2),
                'p75': round(percentile(values, 75), 2),
                'p90': round(percentile(values, 90), 2),
                'max': round(values[-1], 2)}
    rows = []
    for level in levels:
        hit = [t for t in trades if t['mae_pct'] >= level]
        change = sum(-level / 100.0 * t['margin'] - t['pnl'] for t in hit)
        rows.append({
            'pct_margin': level,
            'winners_cut': sum(1 for t in hit if t['pnl'] > 0),
            'losers_stopped': sum(1 for t in hit if t['pnl'] <= 0),
            'net_change': round(change, 2),
            'net_change_pct_capital': (round(100.0 * change / capital, 2)
                                       if capital > 0 else None)})
    return {'trades': len(trades), 'winners': len(winners),
            'losers': len(losers),
            'winners_mae_pct': spread_of([t['mae_pct'] for t in winners]),
            'winners_mae_points': spread_of(
                [t['mae_points'] for t in winners
                 if t['mae_points'] is not None]),
            'losers_mae_pct': spread_of([t['mae_pct'] for t in losers]),
            'levels': rows}


def percentile(values, pct):
    """Linear-interpolated percentile of a sorted list, or None."""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    rank = (len(values) - 1) * pct / 100.0
    low = int(math.floor(rank))
    high = min(low + 1, len(values) - 1)
    return float(values[low] + (values[high] - values[low]) * (rank - low))


def calibration(positions):
    """Where closed trades peaked: the distribution of their best net
    P&L, a take-profit near its 65th percentile ("about two in three
    trades got at least this far"), and a max-hold at the median minute
    the WINNERS peaked."""
    closed = [r for r in positions or () if r.get('closed_at') is not None
              and r.get('peak_pnl') is not None]
    peaks = sorted(float(r['peak_pnl']) for r in closed)
    winners = [r for r in closed if (_pnl(r) or 0) > 0]
    win_minutes = sorted(float(r['peak_min']) for r in winners
                         if r.get('peak_min') is not None)

    def at(pct):
        value = percentile(peaks, pct)
        return None if value is None else round(value, 2)
    hold = percentile(win_minutes, 50)
    return {'n': len(closed), 'winners': len(winners),
            'peak_pctile': {str(p): at(p) for p in (50, 70, 90)},
            'suggested_take': at(65),
            'suggested_max_hold_min': (None if hold is None
                                       else round(hold, 1))}


def journal(positions, entry_z=None, exit_z=None, capital=0.0):
    """Every closed round trip, newest first, with a running total in
    the order they closed. `entry_z` / `exit_z` map position id to the
    Algo's z at that end (manual trades have none)."""
    entry_z = entry_z or {}
    exit_z = exit_z or {}
    closed = sorted((r for r in positions or ()
                     if r.get('closed_at') is not None),
                    key=lambda r: r['closed_at'])
    # None, not 0.0, until something has actually been priced - the
    # same rule the rest of this file follows. A first trade whose P&L
    # could not be read used to print a running total of $0.00, which
    # reads as 'it broke even' rather than as 'nobody measured it'.
    running = None
    out = []
    for row in closed:
        pnl = _pnl(row)
        if pnl is not None:
            running = pnl + (running or 0.0)
        legs = {}
        for leg in ('a', 'b'):
            fill = row.get('leg_' + leg) or {}
            legs[leg] = {'side': fill.get('side'),
                         'volume': fill.get('volume'),
                         'entry': fill.get('price'),
                         'exit': fill.get('exit_price'),
                         'symbol': fill.get('symbol')}
        opened = row.get('opened_at')
        gross = _gross(row)
        based = returns(row, pnl)
        out.append({
            'position_id': row.get('position_id'),
            'pair_key': row.get('pair_key'), 'side': row.get('side'),
            'quantity': row.get('quantity'),
            'source': row.get('source') or 'MANUAL',
            'opened_at': opened, 'closed_at': row.get('closed_at'),
            'held_sec': (None if opened is None
                         else row['closed_at'] - opened),
            'entry_spread': row.get('entry_spread'),
            'exit_spread': row.get('exit_spread'),
            'entry_z': entry_z.get(row.get('position_id')),
            'exit_z': exit_z.get(row.get('position_id')),
            'leg_a': legs['a'], 'leg_b': legs['b'],
            'exit_reason': row.get('close_reason'),
            # ...and the NUMBERS behind that reason, as their own
            # columns: the three levels, which one it came off on, the
            # setting that put it there, and how far past it the close
            # went. 'Algo: stop loss' alone cannot tell a stop that was
            # hit from a stop that was in the wrong place.
            'exit': exit_columns(row),
            'pnl': None if pnl is None else round(pnl, 2),
            **based,
            **on_capital(pnl, based['margin'], based['notional'], capital),
            # The spread's move times k, before commission; the fees
            # are what lies between that and the net. Either is None
            # when a side of it was not measured.
            'gross_pnl': None if gross is None else round(gross, 2),
            'fees': (None if gross is None or pnl is None
                     else round(gross - pnl, 2)),
            'cum_pnl': None if running is None else round(running, 2)})
    out.reverse()
    return out


def trade_numbers(positions):
    """TRADE NO: every closed trade on the desk's record, numbered in the
    order it closed - #1 the first ever. Taken over the WHOLE record, so a
    trade keeps its number whatever window or ladder the tab is showing.
    {position id: number}."""
    closed = sorted((r for r in positions or ()
                     if r.get('closed_at') is not None),
                    key=lambda r: (r['closed_at'], str(r.get('position_id'))))
    return {r.get('position_id'): i for i, r in enumerate(closed, 1)}


def _liquidity(kinds):
    """'M' all maker, 'T' all taker, 'M+T' both, None unseen."""
    kinds = set(kinds)
    if not kinds:
        return None
    if kinds == {'maker'}:
        return 'M'
    if kinds == {'taker'}:
        return 'T'
    return 'M+T'


def trade_fees(positions, fills):
    """THE FEES OKX ACTUALLY CHARGED, per trade: {position id: {'fills'
    (maker / taker per leg and end), 'maker', 'taker' (their cost, a
    rebate negative), 'n_maker', 'n_taker', 'priced', 'complete' (fills
    seen at both ends of both legs)}}.

    Each fill names the position ticket it opened or closed; each leg of
    a position lists its tickets. A fill counts once, and only while its
    trade was alive."""
    by_ticket = {}
    for f in fills or ():
        ticket = f.get('position_ticket')
        if ticket not in (None, ''):
            by_ticket.setdefault(str(ticket), []).append(f)

    def in_life(f, position):
        # A quarter of an hour either side, for the broker's clock and a
        # close that settles late. Tickets are the broker ledger's own
        # numbers: a reset ledger starts them again, and an old fill
        # with the same number must not be charged to a new trade.
        at = f.get('broker_time_ms')
        opened = position.get('opened_at')
        if at is None or opened is None:
            return True
        at = at / 1000.0 - float(f.get('server_offset_s') or 0.0)
        closed = position.get('closed_at') or opened
        return opened - 900 <= at <= closed + 900
    seen = set()
    out = {}
    for position in sorted(positions or (), key=lambda p: (
            p.get('closed_at') or float('inf'), str(p.get('position_id')))):
        fills_of = {}
        found = []
        for leg in ('a', 'b'):
            tickets = ((position.get('leg_' + leg) or {})
                       .get('position_tickets') or ())
            mine = [f for t in tickets for f in by_ticket.get(str(t), ())
                    if in_life(f, position)]
            for end, label in (('open', 'entry'), ('close', 'exit')):
                fills_of[f'{label}_{leg}'] = _liquidity(
                    (f.get('order_type') or '').lower() for f in mine
                    if f.get('entry') == end
                    and (f.get('order_type') or '').lower()
                    in ('maker', 'taker'))
            found += [f for f in mine
                      if (f.get('account'), f.get('deal_id')) not in seen]
            seen.update((f.get('account'), f.get('deal_id')) for f in mine)
        maker = taker = 0.0
        n_maker = n_taker = 0
        priced = False
        for f in found:
            fee = f.get('fee') if f.get('fee') is not None \
                else f.get('commission')
            kind = (f.get('order_type') or '').lower()
            if kind == 'maker':
                n_maker += 1
            elif kind == 'taker':
                n_taker += 1
            if fee is None:
                continue
            priced = True
            cost = -float(fee)
            if kind == 'maker':
                maker += cost
            else:
                taker += cost
        out[position.get('position_id')] = {
            'fills': fills_of, 'maker': maker, 'taker': taker,
            'n_maker': n_maker, 'n_taker': n_taker, 'priced': priced,
            'complete': priced and all(fills_of.values())}
    return out


def with_okx_fees(positions, fills):
    """The positions with each closed trade's NET P&L worked out from the
    fees OKX ACTUALLY CHARGED - the gross move, from the fill prices,
    less those fees - wherever its fills are on record at both ends of
    both legs. Otherwise the desk's own (the fee estimated at the
    account's rates) stands. Copies; the record is not touched.

    Every figure built after this - the journal, the summary, the
    drawdown, both reports - is then on OKX's fees."""
    fees = trade_fees(positions, fills)
    out = []
    for position in positions or ():
        row = dict(position)
        got = fees.get(row.get('position_id')) or {}
        gross = _gross(row)
        if row.get('closed_at') is not None and got.get('complete') \
                and gross is not None:
            row['desk_pnl'] = row.get('realized_pnl')
            row['realized_pnl'] = gross - (got['maker'] + got['taker'])
            row['fees_from'] = 'OKX'
        out.append(row)
    return out


def attach_fills(journal, positions, fills):
    """MAKER OR TAKER, and THE FEES, per trade - from OKX's own fills
    (`trade_fees`): per leg and end, whether OKX filled it as maker
    ('M'), taker ('T') or both ('M+T'), and the fees it charged, split
    maker / taker. A trade whose fills are not in the journal keeps the
    desk's own estimate (`fees`), said as one (`fees_source`). Then the
    running tally of fees, in close order, and the totals. Returns the
    tally."""
    fees = trade_fees(positions, fills)
    tally = {'fees': 0.0, 'maker_fees': 0.0, 'taker_fees': 0.0,
             'maker_fills': 0, 'taker_fills': 0, 'measured': 0,
             'estimated': 0, 'trades': 0}
    for row in journal or ():
        got = fees.get(row.get('position_id')) or {
            'fills': {}, 'maker': 0.0, 'taker': 0.0, 'n_maker': 0,
            'n_taker': 0, 'priced': False}
        fills_of = got['fills']
        maker, taker = got['maker'], got['taker']
        n_maker, n_taker = got['n_maker'], got['n_taker']
        priced = got['priced']
        row['fills'] = fills_of
        notional = row.get('notional')
        row['maker_fills'], row['taker_fills'] = n_maker, n_taker
        if priced:
            row['okx_fees'] = round(maker + taker, 4)
            row['maker_fees'] = round(maker, 4)
            row['taker_fees'] = round(taker, 4)
            row['fee'] = row['okx_fees']
            row['fees_source'] = 'OKX'
        else:
            row['okx_fees'] = row['maker_fees'] = row['taker_fees'] = None
            row['fee'] = row.get('fees')
            row['fees_source'] = None if row['fee'] is None else 'estimate'
        # THE CHECK: fees in basis points of both legs' notional. Both
        # ends at maker on each leg is 2 x the maker rate; at taker,
        # 2 x the taker rate (4 and 10 bp at OKX's 2 / 5 bp).
        row['fees_bp_notional'] = (round(row['fee'] / notional * 1e4, 2)
                                   if row.get('fee') is not None and notional
                                   else None)
    # The running tally, in the order the trades closed (the journal is
    # newest first).
    running = 0.0
    gross_total = fees_on_gross = 0.0
    notional_total = fees_on_notional = 0.0
    for row in reversed(journal or ()):
        gross = row.get('gross_pnl')
        # FEES AS A SHARE OF THE GROSS: what the costs took out of what
        # the spread move made. Only for a move that made money - a share
        # of a loss says nothing.
        row['fees_pct_gross'] = (
            round(100.0 * row['fee'] / gross, 2)
            if row.get('fee') is not None and gross and gross > 0 else None)
        if row.get('fee') is not None and gross is not None:
            gross_total += gross
            fees_on_gross += row['fee']
        if row.get('fee') is not None and row.get('notional'):
            notional_total += row['notional']
            fees_on_notional += row['fee']
        if row.get('fee') is not None:
            running += row['fee']
            tally['trades'] += 1
            if row['fees_source'] == 'OKX':
                tally['measured'] += 1
                tally['maker_fees'] += row['maker_fees']
                tally['taker_fees'] += row['taker_fees']
            else:
                tally['estimated'] += 1
        tally['maker_fills'] += row.get('maker_fills') or 0
        tally['taker_fills'] += row.get('taker_fills') or 0
        row['cum_fees'] = round(running, 2) if tally['trades'] else None
    tally['fees'] = round(running, 2)
    tally['gross'] = round(gross_total, 2)
    tally['fees_bp_notional'] = (round(fees_on_notional / notional_total
                                       * 1e4, 2) if notional_total else None)
    tally['fees_pct_gross'] = (round(100.0 * fees_on_gross / gross_total, 2)
                               if gross_total > 0 else None)
    tally['maker_fees'] = round(tally['maker_fees'], 2)
    tally['taker_fees'] = round(tally['taker_fees'], 2)
    return tally


def number_trades(body, numbers):
    """The same Trade no on every table of trades in a report: the
    journal and the worst / best points."""
    for key in ('journal', 'excursion'):
        number_journal(body.get(key), numbers)
    return body


def number_journal(journal, numbers):
    """Each journal row's Trade no, from `trade_numbers`."""
    for row in journal or ():
        row['trade_no'] = numbers.get(row.get('position_id'))
    return journal


def algo_z(events):
    """(entry_z, exit_z) by position id, from the Algo's own audit trail.

    An EXIT signal names its position. An ENTER signal does not — the
    position has no id until it fills — so its z is matched to the
    position the next `algo_order` ENTER for the same ladder names,
    within a minute.
    """
    entry, exit_ = {}, {}
    ordered = sorted(events or (), key=lambda e: e.get('at') or 0)
    pending = {}
    for event in ordered:
        detail = event.get('detail') or {}
        pair = event.get('pair_key')
        if event.get('kind') == 'algo_signal':
            if detail.get('action') == 'ENTER':
                pending[pair] = (event.get('at') or 0, detail.get('z'))
            elif detail.get('action') == 'EXIT' and detail.get('position_id'):
                exit_.setdefault(detail['position_id'], detail.get('z'))
        elif event.get('kind') == 'algo_order' and \
                detail.get('action') == 'ENTER' and detail.get('ok') and \
                detail.get('position_id') and pair in pending:
            at, z = pending.pop(pair)
            if (event.get('at') or 0) - at <= 60.0:
                entry[detail['position_id']] = z
    return entry, exit_


def report(positions, events=(), shadows=(), excursions=None, now=0.0,
           capital=0.0):
    """Everything the tab shows, from what is recorded. `capital`: what
    the trader manages (CAPITAL_MANAGED), 0 when not set."""
    positions = list(positions or ())
    entry_z, exit_z = algo_z(events)
    return {'summary': summary(positions, capital),
            'drawdown': drawdown(positions, capital),
            'stop_study': stop_study(positions, capital),
            'excursion': excursion_rows(positions),
            'calibration': calibration(positions),
            'shadow': shadow_summary(shadows),
            'z_excursions': {key: excursions_view(data, now)
                             for key, data in (excursions or {}).items()},
            'journal': journal(positions, entry_z, exit_z, capital)}


#: The windows the volume is counted over: the broker's own TODAY, and
#: the last 7 and 30 days rolling.
NOTIONAL_PERIODS = (('today', 'Today'), ('7d', 'Last 7 days'),
                    ('30d', 'Last 30 days'))


def contract_sizes(pairs=None, positions=()):
    """{(account, symbol): contract size} - what one LOT is, per leg.

    The ladders' own sizes first (MT5's, or the trader's override, as
    the engine publishes them); then the size frozen on each recorded
    position's legs, so a ladder deleted since still prices its fills.
    """
    sizes = {}
    for row in positions or ():
        for leg in (row.get('leg_a'), row.get('leg_b')):
            leg = leg or {}
            size = leg.get('contract_size')
            if size and leg.get('account') and leg.get('symbol'):
                sizes[(leg['account'], leg['symbol'])] = float(size)
    for pair in (pairs or {}).values():
        for side in ('a', 'b'):
            size = pair.get(f'contract_size_{side}')
            account, symbol = pair.get(f'account_{side}'), \
                pair.get(f'symbol_{side}')
            if size and account and symbol:
                sizes[(account, symbol)] = float(size)
    return sizes


def notional_traded(fills, sizes, now, date=None, date_ms=None):
    """Notional volume traded - lots x contract size x price, per deal -
    today, over the last 7 days and over the last 30, and over a picked
    range of the broker's days (`date` its label): per ladder, per
    account, and in all, from the BROKER's own deals.

    Each deal is placed in time on the broker's stamp less that
    broker's offset from UTC; "today" and a picked date are the broker's
    own days, as its terminal shows them. A deal whose contract size is
    not known is COUNTED, not priced: unmeasured is not zero, and the
    screen says how many there were rather than leaving them out of a
    total.

    `date_ms` is the picked range, (start, end) with the end exclusive,
    on the broker's clock as its stamps are written (the caller reads it
    off the two picked days).
    """
    offset = None
    for fill in fills:
        if fill.get('server_offset_s') is not None:
            offset = float(fill['server_offset_s'])
            break
    offset = offset or 0.0
    broker_now = now + offset
    day_start = broker_now - broker_now % 86400.0 - offset
    since = {'today': day_start, '7d': now - 7 * 86400.0,
             '30d': now - 30 * 86400.0}
    periods = list(NOTIONAL_PERIODS)
    if date:
        periods.append(('date', date))

    def in_period(period, at, stamp):
        if period == 'date':
            return date_ms is not None and \
                date_ms[0] <= stamp < date_ms[1]
        return at >= since[period]

    def cell():
        return {'notional': 0.0, 'lots': 0.0, 'fills': 0, 'unpriced': 0,
                'a': 0.0, 'b': 0.0}

    def cells():
        return {k: cell() for k, _ in periods}

    by_pair, by_account, total = {}, {}, cells()
    for fill in fills:
        stamp = fill.get('broker_time_ms')
        if stamp is None:
            continue
        at = stamp / 1000.0 - float(fill.get('server_offset_s') or 0)
        volume = abs(float(fill.get('volume') or 0.0))
        price = fill.get('price')
        size = sizes.get((fill.get('account'), fill.get('symbol')))
        notional = (volume * size * abs(float(price))
                    if size and price is not None else None)
        pair = by_pair.setdefault(fill.get('pair_key') or '(no ladder)',
                                  cells())
        account = by_account.setdefault(fill.get('account') or '?', cells())
        leg = (fill.get('leg') or '').lower()
        for period, _label in periods:
            if not in_period(period, at, stamp):
                continue
            for target in (pair[period], account[period], total[period]):
                target['fills'] += 1
                target['lots'] += volume
                if notional is None:
                    target['unpriced'] += 1
                    continue
                target['notional'] += notional
                if leg in ('a', 'b'):
                    target[leg] += notional
    groups = list(by_pair.values()) + list(by_account.values()) + [total]
    for group in groups:
        for c in group.values():
            for name in ('notional', 'lots', 'a', 'b'):
                c[name] = round(c[name], 2)

    def listed(rows, key):
        return [dict(c, **{key: name}) for name, c in sorted(rows.items())
                if any(c[p]['fills'] for p, _ in periods)]

    return {'periods': [{'key': k, 'label': label} for k, label in periods],
            'date': date,
            'rows': listed(by_pair, 'pair_key'),
            'accounts': listed(by_account, 'account'),
            'total': total}
