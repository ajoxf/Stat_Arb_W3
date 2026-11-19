"""Replay history through the Algo: what it WOULD have done.

The same decision code the live Algo runs — `AlgoSignal` for the entry
and the exits, `judge_filters` for the filters, the same band — fed one
15-minute candle at a time from MT5's own bars. Nothing here can reach
an order: it takes numbers and returns a list of what it would have
traded, and a test keeps it that way.

What it cannot see, and says so in its result:

- **Inside a candle.** Each candle is one look at the spread, at its
  close: a stop touched and recovered within fifteen minutes is not seen,
  and an exit is taken at the close it was seen at.
- **The book.** The bid-ask is the width measured now, held constant.
- **Confirmation in ticks.** Live, an entry needs N fresh quotes in a
  row; here one candle close is one confirmation.
- **The warm-up and the live feed checks**, which are about the live
  tape, not about history.
"""

import datetime
import re

from . import algo as algo_module
from . import algofilters
from . import bands
from .algodesk import judge_filters


def _cutoff_minutes(at, offset_sec, hour, minute):
    """Minutes from `at` (UTC seconds) to the session cutoff on the
    broker's clock; negative past it. None when the clock is unknown."""
    if offset_sec is None:
        return None
    broker = datetime.datetime.utcfromtimestamp(at + offset_sec)
    cutoff = broker.replace(hour=int(hour), minute=int(minute), second=0,
                            microsecond=0)
    return (cutoff - broker).total_seconds() / 60.0


def _broker_day(at, offset_sec):
    return datetime.datetime.utcfromtimestamp(
        at + (offset_sec or 0)).date().isoformat()


def _side_levels(p, closes, width, base):
    """(target, stop, block) for an entry on one side, on that side's
    own settings `p`: the margin levels `base` priced for it, or - in ATR
    mode - a multiple of the ATR on the candles CLOSED before this one,
    as live; no ATR yet, or a stop inside the bid-ask, blocks it."""
    target, stop = base.get('target_points'), base.get('stop_points')
    if 'ATR' in (p['stop_mode'], p['target_mode']):
        atr = algofilters.atr(closes[:-1], p['atr_period'])
        if not atr:
            return target, stop, 'levels: ATR not measured yet'
        if p['stop_mode'] == 'ATR':
            stop = p['atr_stop_mult'] * atr if p['stop_loss_on'] else None
        if p['target_mode'] == 'ATR':
            target = p['atr_target_mult'] * atr
    if stop is not None and stop <= (width or 0.0):
        return target, stop, 'levels: the stop is inside the bid-ask'
    return target, stop, None



def _edge_at(z, stats, filters, p):
    """`shadow.ShadowAlgo`'s edge reading, on a candle: what the move
    back is worth against its costs, and what the filter requires."""
    sigma = stats.get('sigma') if stats.get('ready') else None
    if z is None or sigma is None:
        return None
    cost = ((filters or {}).get('cost') or {}).get('total')
    return algofilters.edge(z, sigma, (filters or {}).get('k'),
                            p['algo_qty'], cost, p['edge_capture_frac'],
                            p['edge_multiple'])

def run(rows, params, width, cost_in, levels, offset_sec=None,
        cutoff=(16, 55)):
    """Replay `rows` [(bucket, mid close), ...] through the Algo.

    - `params`: the ladder's Algo settings (cleaned here).
    - `width`: the spread's bid-ask, long minus short, held constant.
    - `cost_in`: {'k', 'commission', 'slippage'} as the live filters get.
    - `levels`: {'fee_points', 'target_points', 'stop_points'} per spread,
      as the live Exit panel prices them; None for one that cannot be.
      With the Algo's stop or target in ATR mode, that one is instead a
      multiple of the ATR AT EACH ENTRY, as live - and an entry with no
      ATR yet is not taken, as live.
    - `offset_sec`: the broker's clock offset, for the session cutoff
      and the broker's day.

    Returns {'trades': [...], 'held': {reason: count}, 'summary': {...},
    'caveats': [...]}.
    """
    p = algo_module.clean_params(params)
    p['confirm_ticks'] = 1                 # one candle is one look
    signal = algo_module.AlgoSignal(p)
    tf = p['timeframe_min'] * 60.0
    rows = sorted((float(b), float(c)) for b, c in rows or ()
                  if c is not None)
    half = (width or 0.0) / 2.0
    k = (cost_in or {}).get('k')
    qty = p['algo_qty']
    commission = float((cost_in or {}).get('commission') or 0.0)
    fee = (levels or {}).get('fee_points') or 0.0
    target = (levels or {}).get('target_points')
    stop = (levels or {}).get('stop_points')

    trades, open_, held = [], {}, {}
    day = {'date': None, 'trades': 0, 'losses_row': 0}
    last_held = None
    counter = 0
    for i in range(len(rows)):
        bucket, close = rows[i]
        now = bucket + tf
        candles = bands.SpreadCandles(tf, p['length'])
        candles.seed(rows[:i + 1])
        # The candles the live Algo would hold — trimmed the same way.
        closes = candles.closes()
        stats = candles.stats()
        md = {'short_spread': close - half, 'long_spread': close + half,
              'mid_spread': close, 'quote_id': i}
        broker_day = _broker_day(bucket, offset_sec)
        if broker_day != day['date']:
            day = {'date': broker_day, 'trades': 0, 'losses_row': 0}
        halt = None
        if p['max_trades_day'] and day['trades'] >= p['max_trades_day']:
            halt = f"{day['trades']} trades today — the day's limit"
        elif p['max_losses_row'] and \
                day['losses_row'] >= p['max_losses_row']:
            halt = f"{day['losses_row']} losing trades in a row"
        positions = []
        for pid, pos in open_.items():
            closing = md['short_spread'] if pos['side'] == 'BUY' \
                else md['long_spread']
            sign = 1.0 if pos['side'] == 'BUY' else -1.0
            positions.append(dict(pos, position_id=pid, net_pnl=(
                None if not k else
                sign * (closing - pos['entry_spread']) * k * qty
                - commission)))
        filters, check = judge_filters(p, md, stats, closes, cost_in)
        # The stop and target THIS entry would get - from the ATR on the
        # candles CLOSED before this one, as live - and the same guard:
        # none until the ATR is measured, none with a stop inside the
        # bid-ask.
        sided = {}
        for side in ('SELL', 'BUY'):
            sided[side] = _side_levels(
                algo_module.for_side(p, side), closes, width,
                ((levels or {}).get('by_side') or {}).get(side)
                or {'target_points': target, 'stop_points': stop})
        levels_block = (sided['SELL'][2] if sided['SELL'][2] ==
                        sided['BUY'][2] else
                        {side: sided[side][2] for side in sided})
        gates = {'health': None, 'halt': halt, 'entry_check': check,
                 'levels': levels_block,
                 'cutoff_min': _cutoff_minutes(bucket, offset_sec, *cutoff)}
        body = signal.evaluate(now, md, stats, positions, gates)
        if body.get('blocked_side'):
            reason = _reason_kind(body.get('blocked'))
            if reason != last_held:
                held[reason] = held.get(reason, 0) + 1
            last_held = reason
        elif body.get('state') != 'BLOCKED':
            last_held = None
        for intent in body['intents']:
            if intent['action'] == 'ENTER':
                counter += 1
                pid = f'bt{counter}'
                side = intent['side']
                trade_target, trade_stop, _ = sided[side]
                entry = intent['spread']
                sign = 1.0 if side == 'BUY' else -1.0
                be = entry + sign * fee
                pos = {'side': side, 'entry_spread': entry,
                       'opened_at': now, 'break_even': be,
                       'tp': None if trade_target is None
                       else be + sign * trade_target,
                       'sl': None if trade_stop is None
                       else be - sign * trade_stop,
                       'entry_z': intent.get('z'),
                       # The edge filter's reading at this entry,
                       # priced exactly as the live journal prices it,
                       # switched on or not.
                       'edge': _edge_at(intent.get('z'), stats, filters,
                                        p)}
                open_[pid] = pos
                day['trades'] += 1
            elif intent['position_id'] in open_:
                pos = open_.pop(intent['position_id'])
                sign = 1.0 if pos['side'] == 'BUY' else -1.0
                exit_at = intent['spread']
                pnl = (None if not k else
                       sign * (exit_at - pos['entry_spread']) * k * qty
                       - commission)
                if pnl is not None:
                    day['losses_row'] = day['losses_row'] + 1 if pnl < 0 \
                        else 0
                trades.append({
                    'side': pos['side'], 'opened_at': pos['opened_at'],
                    'entry': pos['entry_spread'], 'entry_z': pos['entry_z'],
                    'edge': pos.get('edge'),
                    'closed_at': now, 'exit': exit_at,
                    'reason': intent.get('reason'), 'pnl': pnl})
    for pos in open_.values():
        trades.append({'side': pos['side'], 'opened_at': pos['opened_at'],
                       'entry': pos['entry_spread'],
                       'entry_z': pos['entry_z'], 'edge': pos.get('edge'),
                       'closed_at': None,
                       'exit': None, 'reason': 'still open', 'pnl': None})
    return {'trades': trades, 'held': held,
            'summary': summarise(trades, rows, tf),
            'caveats': CAVEATS}


CAVEATS = [
    'one look per candle, at its close: a stop touched inside a candle '
    'is not seen, and exits are taken at the close',
    'the bid-ask is today\'s width, held constant',
    'one candle close counts as one confirmation',
    'no warm-up and no live-feed checks — those are about the live tape',
]


def _reason_kind(reason):
    """A held-back reason without its changing numbers, for counting."""
    return re.sub(r'[-+]?\d[\d.,:]*', '#', reason or '?').strip()


def summarise(trades, rows, tf):
    closed = [t for t in trades if t['pnl'] is not None]
    wins = [t for t in closed if t['pnl'] > 0]
    net = sum(t['pnl'] for t in closed) if closed else 0.0
    peak, worst, running = 0.0, 0.0, 0.0
    for t in closed:
        running += t['pnl']
        peak = max(peak, running)
        worst = min(worst, running - peak)
    by_reason = {}
    for t in trades:
        by_reason[t['reason']] = by_reason.get(t['reason'], 0) + 1
    return {'candles': len(rows),
            'from': rows[0][0] if rows else None,
            'to': rows[-1][0] + tf if rows else None,
            'trades': len(trades), 'closed': len(closed),
            'wins': len(wins), 'losses': len(closed) - len(wins),
            'win_rate': (len(wins) / len(closed)) if closed else None,
            'net': net if closed else None,
            'max_drawdown': worst if closed else None,
            'exits': by_reason}
