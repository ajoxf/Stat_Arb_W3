"""The slippage report, over a real session.

Every number here is MEASURED. Nothing in this module simulates a fill,
back-fills a missing one with zero, or models what the spread "should"
have done: it reads the positions the engine actually opened and closed
in one session and reports what each one cost against the price that was
clicked.

Three rules, and each of them has cost money somewhere:

- **Unmeasured is not zero.** A position whose decision spread or whose
  fill could not be read has `entry_slippage = None`, and it stays None
  all the way to the screen, counted in its own column. Averaging it in
  as 0.00 flatters every number in the table and hides the one case
  worth looking at — the fill nobody could price.
- **Positive is a COST, at both ends.** `executor.slippage()` already
  flips the sign for exits; this module only ever adds. A report that
  reads an exit's cost as a gain is worse than no report.
- **A session is the BROKER's session.** The window runs from the last
  session cutoff on the broker's clock, because that is the day the
  fills are stamped in. On a machine hours away from the broker, a
  window cut on local midnight puts half of one session's fills in the
  other's.

Money is `points x spread_units x quantity` — `k`, the one multiplier
every spread-to-money conversion in this system uses (spec §2). The
report never invents a second one.
"""

from datetime import datetime, timedelta

#: How many of the worst entries to list beside the summary. Enough to
#: see a pattern, few enough that the table stays readable.
WORST_N = 5


def session_window(now, offset_sec, close_hour=16, close_minute=55):
    """The session `now` is in, as (from, to) epoch seconds on OUR clock.

    The cut is taken on the BROKER's clock and converted back, so the
    window lines up with the deal stamps in OKX's own fill history. With no
    measured offset the report still runs — on this machine's clock, and
    saying so, because a report that refuses to open teaches nobody
    anything.
    """
    machine = datetime.fromtimestamp(now)
    measured = offset_sec is not None
    broker = machine + timedelta(seconds=offset_sec or 0)
    cutoff = broker.replace(hour=int(close_hour), minute=int(close_minute),
                            second=0, microsecond=0)
    if broker < cutoff:
        cutoff -= timedelta(days=1)       # still in yesterday's session
    start = now - (broker - cutoff).total_seconds()
    label = (f"since {cutoff.strftime('%a %d %b %H:%M')} "
             f"{'broker time' if measured else 'on this machine'}")
    if measured:
        note = (f'the session is cut on the broker clock, '
                f'{(offset_sec or 0) / 3600.0:+.1f}h from this machine — '
                f'the same clock OKX stamps its fills with')
    else:
        note = ('the broker clock has not been measured, so this window is '
                'cut on THIS machine\'s clock — it may not line up with the '
                'broker\'s trading day')
    return {'from': start, 'to': now, 'clock': 'broker' if measured else
            'machine', 'cutoff': cutoff.strftime('%H:%M'), 'label': label,
            'note': note, 'offset_sec': offset_sec}


def money(points, position):
    """`points` of spread, in dollars, for this position.

    `spread_units` is `k` — dollars per 1.00 of spread per spread — and
    `quantity` is how many spreads. Missing either one means the money
    is unmeasured, not zero.
    """
    if points is None:
        return None
    units = position.get('spread_units')
    quantity = position.get('quantity')
    if not units or quantity is None:
        return None
    return points * float(units) * float(quantity)


def _leg_slips(position):
    """Each leg's own slip, entry and exit: price points of THAT leg
    against the touch it was sent at (positive a cost), and the same in
    money. A leg whose send price was not kept is None, not zero."""
    out = {}
    for leg in ('a', 'b'):
        fill = position.get('leg_' + leg) or {}
        side = fill.get('side')
        closing = {'BUY': 'SELL', 'SELL': 'BUY'}.get(side)
        size = None
        try:
            size = float(fill['volume']) * float(fill['contract_size'])
        except (KeyError, TypeError, ValueError):
            pass
        for end, (s, sent, got) in (
                ('entry', (side, fill.get('sent_price'), fill.get('price'))),
                ('exit', (closing, fill.get('exit_sent_price'),
                          fill.get('exit_price')))):
            points = _slip(s, sent, got)
            out[f'{end}_leg_{leg}'] = points
            out[f'{end}_leg_{leg}_money'] = (
                None if points is None or size is None else points * size)
            # The same slip in BASIS POINTS of that leg's own price: a
            # point of BTC and a point of ETH are not the same size, a
            # basis point of either is.
            out[f'{end}_leg_{leg}_bps'] = (
                None if points is None or not sent
                else points / float(sent) * 1e4)
    return out


def _slip(side, sent, filled):
    if side is None or sent is None or filled is None:
        return None
    return (float(filled) - float(sent)) if side == 'BUY' \
        else (float(sent) - float(filled))


def _mean(values):
    values = [v for v in values if v is not None]
    return (sum(values) / len(values)) if values else None


def _leg_summary(rows):
    """Per leg, the mean slip at entry and at exit, with how many were
    measured - which LEG a ladder's slippage comes from."""
    out = {}
    for end in ('entry', 'exit'):
        for leg in ('a', 'b'):
            key = f'{end}_leg_{leg}'
            values = [r[key] for r in rows if r.get(key) is not None]
            out[key] = {'measured': len(values), 'points_mean': _mean(values),
                        'bps_mean': _mean([r.get(key + '_bps')
                                           for r in rows]),
                        'money_total': (sum(r[key + '_money'] for r in rows
                                            if r.get(key + '_money')
                                            is not None)
                                        if values else None)}
    return out


def _stats(samples):
    """Summarise one end (entries, or exits) of a set of positions.

    `samples` are `(points, money)` pairs for the positions that HAVE a
    measurement, plus a count of the ones that do not. With nothing
    measured every figure is None — which the UI renders as "—".
    """
    measured = [s for s in samples if s[0] is not None]
    unmeasured = len(samples) - len(measured)
    if not measured:
        return {'measured': 0, 'unmeasured': unmeasured,
                'points_mean': None, 'points_median': None,
                'points_worst': None, 'points_best': None,
                'money_total': None, 'money_mean': None,
                'paid': 0, 'earned': 0, 'flat': 0}
    points = sorted(s[0] for s in measured)
    cash = [s[1] for s in measured if s[1] is not None]
    middle = len(points) // 2
    median = (points[middle] if len(points) % 2
              else (points[middle - 1] + points[middle]) / 2.0)
    return {
        'measured': len(measured),
        'unmeasured': unmeasured,
        'points_mean': sum(points) / len(points),
        'points_median': median,
        # Worst is the biggest COST, best the biggest improvement — the
        # sign convention is the same at both ends.
        'points_worst': points[-1],
        'points_best': points[0],
        'money_total': sum(cash) if cash else None,
        'money_mean': (sum(cash) / len(cash)) if cash else None,
        'paid': sum(1 for p in points if p > 0),
        'earned': sum(1 for p in points if p < 0),
        'flat': sum(1 for p in points if p == 0),
    }


def _rows(positions):
    """One row per position: both ends, in points and in money."""
    rows = []
    for position in positions:
        entry = position.get('entry_slippage')
        exit_ = position.get('exit_slippage')
        entry_money = money(entry, position)
        exit_money = money(exit_, position)
        # A round turn only counts when BOTH ends were measured. One end
        # plus a zero is not a round turn, it is half of one.
        both = None if (entry is None or exit_ is None) else entry + exit_
        rows.append({
            'position_id': position.get('position_id'),
            'pair_key': position.get('pair_key'),
            'side': position.get('side'),
            'quantity': position.get('quantity'),
            'order_type': position.get('order_type'),
            'opened_at': position.get('opened_at'),
            'closed_at': position.get('closed_at'),
            'open': position.get('closed_at') is None,
            'entry_points': entry,
            'entry_money': entry_money,
            'exit_points': exit_,
            'exit_money': exit_money,
            'round_trip_points': both,
            'round_trip_money': (None if both is None
                                 else money(both, position)),
            'click_to_on_ms': position.get('click_to_on_ms'),
            'realized_pnl': position.get('realized_pnl'),
            'symbol_a': (position.get('leg_a') or {}).get('symbol'),
            'symbol_b': (position.get('leg_b') or {}).get('symbol'),
            'account_a': (position.get('leg_a') or {}).get('account'),
            'account_b': (position.get('leg_b') or {}).get('account'),
            **_leg_slips(position),
        })
        _split(rows[-1], position)
    return rows


def _split(row, position):
    """Where each end's slippage came from, in two parts that add up.

    BROKER FILL: each leg against the price it was SENT at - what the
    broker did, summed over both legs in money. MARKET MOVED WHILE
    SENDING: the rest - the spread changing between the decision and
    the moment the legs went (latency). Either part unmeasured makes
    both None: a split with one side guessed is not a split.
    """
    units = position.get('spread_units')
    quantity = position.get('quantity')
    per_point = (float(units) * float(quantity)) if units and quantity \
        else None
    for end in ('entry', 'exit'):
        total = row[f'{end}_money']
        legs = (row.get(f'{end}_leg_a_money'), row.get(f'{end}_leg_b_money'))
        broker = None if any(v is None for v in legs) else sum(legs)
        if total is None or broker is None:
            row[f'{end}_broker_money'] = row[f'{end}_moved_money'] = None
            row[f'{end}_broker_points'] = row[f'{end}_moved_points'] = None
            continue
        row[f'{end}_broker_money'] = broker
        row[f'{end}_moved_money'] = total - broker
        row[f'{end}_broker_points'] = (broker / per_point
                                       if per_point else None)
        row[f'{end}_moved_points'] = (
            None if row[f'{end}_broker_points'] is None
            else row[f'{end}_points'] - row[f'{end}_broker_points'])


def _split_summary(rows):
    out = {}
    for end in ('entry', 'exit'):
        pick = [r for r in rows if r.get(f'{end}_broker_money') is not None]
        if end == 'exit':
            pick = [r for r in pick if not r['open']]
        out[end] = {
            'measured': len(pick),
            'moved_money': sum(r[f'{end}_moved_money'] for r in pick)
            if pick else None,
            'broker_money': sum(r[f'{end}_broker_money'] for r in pick)
            if pick else None,
            'moved_points_mean': _mean([r[f'{end}_moved_points']
                                        for r in pick]),
            'broker_points_mean': _mean([r[f'{end}_broker_points']
                                         for r in pick]),
        }
    return out


def _by_account(rows):
    """Each leg's account and symbol: its fills, its own slip at entry
    and at exit, its worst, and what it cost - which BROKER the
    slippage comes from."""
    groups = {}
    for row in rows:
        for leg in ('a', 'b'):
            key = (row.get(f'account_{leg}'), row.get(f'symbol_{leg}'))
            if key == (None, None):
                continue
            g = groups.setdefault(key, {'entry': [], 'exit': [], 'money': [],
                                        'entry_bps': [], 'exit_bps': [],
                                        'legs': set()})
            g['legs'].add(leg.upper())
            for end in ('entry', 'exit'):
                points = row.get(f'{end}_leg_{leg}')
                if points is None:
                    continue
                g[end].append(points)
                g[f'{end}_bps'].append(row.get(f'{end}_leg_{leg}_bps'))
                money_ = row.get(f'{end}_leg_{leg}_money')
                if money_ is not None:
                    g['money'].append(money_)
    out = []
    for (account, symbol), g in sorted(groups.items(),
                                       key=lambda kv: (str(kv[0][0]),
                                                       str(kv[0][1]))):
        points = g['entry'] + g['exit']
        out.append({
            'account': account, 'symbol': symbol,
            'legs': ''.join(sorted(g['legs'])),
            'fills': len(points),
            'entry_points_mean': _mean(g['entry']),
            'exit_points_mean': _mean(g['exit']),
            # The same in basis points of the leg's own price: a BTC
            # point and an ETH point are not the same size.
            'entry_bps_mean': _mean(g['entry_bps']),
            'exit_bps_mean': _mean(g['exit_bps']),
            'points_worst': max(points) if points else None,
            'money_total': sum(g['money']) if g['money'] else None,
            'paid': sum(1 for p in points if p > 0),
            'earned': sum(1 for p in points if p < 0),
        })
    return out


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else \
        (values[middle - 1] + values[middle]) / 2.0


def _against_pnl(rows):
    """Round-turn slippage beside the closed trades' net P&L: the
    share of the P&L before slippage that slippage took. Only trades
    with BOTH measured count; a share of a loss is not a share, so it
    is None and the screen says so."""
    pick = [r for r in rows if not r['open']
            and r.get('round_trip_money') is not None
            and r.get('realized_pnl') is not None]
    if not pick:
        return {'trades': 0, 'slippage': None, 'net': None, 'before': None,
                'share': None}
    slip = sum(r['round_trip_money'] for r in pick)
    net = sum(float(r['realized_pnl']) for r in pick)
    before = net + slip
    return {'trades': len(pick), 'slippage': slip, 'net': net,
            'before': before,
            'share': (slip / before) if before > 0 else None}


def _summarise(rows):
    return {
        'entry': _stats([(r['entry_points'], r['entry_money'])
                         for r in rows]),
        # An open position has no exit yet; that is not an unmeasured
        # exit, so it is not counted as one.
        'exit': _stats([(r['exit_points'], r['exit_money'])
                        for r in rows if not r['open']]),
        'round_trip': _stats([(r['round_trip_points'], r['round_trip_money'])
                              for r in rows if not r['open']]),
        'positions': len(rows),
        'legs': _leg_summary(rows),
    }


def report(positions, window=None, names=None, worst_n=WORST_N):
    """The whole report for one session.

    Split by pair and by ORDER TYPE, because that split is the point: a
    LIMIT peg that is not beating a market click on the same ladder is
    costing time for nothing, and only the two columns side by side say
    so.
    """
    rows = _rows(positions or [])
    names = names or {}
    by_pair, by_type = {}, {}
    for row in rows:
        by_pair.setdefault(row['pair_key'], []).append(row)
        by_type.setdefault(row['order_type'] or 'MARKET', []).append(row)

    measured = [r for r in rows if r['entry_money'] is not None]
    worst = sorted(measured, key=lambda r: -r['entry_money'])[:worst_n]

    overall = _summarise(rows)
    return {
        'window': window,
        'overall': overall,
        'by_pair': {key: dict(_summarise(group),
                              name=names.get(key, key))
                    for key, group in sorted(by_pair.items())},
        'by_order_type': {key: _summarise(group)
                          for key, group in sorted(by_type.items())},
        'worst': worst,
        'rows': sorted(rows, key=lambda r: -(r['opened_at'] or 0)),
        'split': _split_summary(rows),
        'by_account': _by_account(rows),
        'click_to_on_median_ms': _median([r['click_to_on_ms']
                                          for r in rows]),
        'pnl': _against_pnl(rows),
        'counts': {
            'positions': len(rows),
            'open': sum(1 for r in rows if r['open']),
            'closed': sum(1 for r in rows if not r['open']),
        },
    }


# -- how each fill was made, read from OKX's own fills ---------------------

def _cost(fee):
    """OKX states a fee as a signed amount, negative when charged. Here a
    COST is positive, as everywhere on this page; a maker rebate comes
    out negative."""
    return None if fee is None else -float(fee)


def fill_quality(fills, positions, fee_bps=None):
    """Two things the price slip alone cannot say, from the journal of
    OKX's own fills over the same window.

    - **maker or taker**, per ladder, leg and end: OKX marks every fill
      (`execType`), and a "maker" fill that went in as taker is paid at
      the taker rate. `extra` is what those taker fills cost over the
      maker rate - None where this account's rates are not known.
    - **the gap between the legs**, per position: how long after the
      first leg filled the second one did. Under the maker fill that
      gap is where the slippage comes from - the first leg is on and
      the second is still waiting for the market.

    `fee_bps` is {pair_key: {'a': {'maker', 'taker'}, 'b': ...}}.
    """
    fee_bps = fee_bps or {}
    table = {}
    for f in fills or ():
        if not f.get('is_ours'):
            continue
        key = (f.get('pair_key') or '?', (f.get('leg') or '?').upper(),
               f.get('entry') or '?')
        row = table.setdefault(key, {
            'pair_key': key[0], 'leg': key[1], 'end': key[2],
            'symbol': f.get('symbol'), 'fills': 0, 'maker_fills': 0,
            'taker_fills': 0, 'maker_volume': 0.0, 'taker_volume': 0.0,
            'fees': 0.0, 'extra': 0.0, 'extra_known': True})
        kind = (f.get('order_type') or '').lower()
        volume = float(f.get('volume') or 0.0)
        cost = _cost(f.get('fee') if f.get('fee') is not None
                     else f.get('commission'))
        row['fills'] += 1
        if cost is not None:
            row['fees'] += cost
        if kind == 'maker':
            row['maker_fills'] += 1
            row['maker_volume'] += volume
        elif kind == 'taker':
            row['taker_fills'] += 1
            row['taker_volume'] += volume
            rates = (fee_bps.get(key[0]) or {}).get(key[1].lower()) or {}
            maker, taker = rates.get('maker'), rates.get('taker')
            if cost is None or maker is None or not taker:
                row['extra_known'] = False
            else:
                row['extra'] += cost * (1.0 - float(maker) / float(taker))
    liquidity = []
    for row in sorted(table.values(),
                      key=lambda r: (r['pair_key'], r['end'] != 'open',
                                     r['leg'])):
        total = row['maker_volume'] + row['taker_volume']
        row['taker_share'] = (row['taker_volume'] / total) if total else None
        if not row.pop('extra_known'):
            row['extra'] = None
        liquidity.append(row)

    # The gap: each leg's LAST fill of its open (and of its close), by
    # the position tickets the position holds on that leg.
    by_ticket = {}
    for f in fills or ():
        ticket = f.get('position_ticket')
        if ticket in (None, ''):
            continue
        by_ticket.setdefault(str(ticket), []).append(f)

    def last_fill(leg, end):
        times = [f.get('broker_time_ms')
                 for t in (leg or {}).get('position_tickets') or ()
                 for f in by_ticket.get(str(t), ())
                 if f.get('entry') == end and f.get('broker_time_ms')]
        return max(times) if times else None

    gaps = []
    for p in positions or ():
        legs = {name: p.get('leg_' + name) or {} for name in ('a', 'b')}
        row = {'position_id': p.get('position_id'),
               'pair_key': p.get('pair_key'), 'side': p.get('side'),
               'opened_at': p.get('opened_at')}
        for end, label in (('open', 'entry'), ('close', 'exit')):
            ta, tb = last_fill(legs['a'], end), last_fill(legs['b'], end)
            row[f'{label}_gap_ms'] = (None if ta is None or tb is None
                                      else abs(tb - ta))
            row[f'{label}_first'] = (None if ta is None or tb is None
                                     else ('A' if ta < tb else 'B'
                                           if tb < ta else 'both'))
        gaps.append(row)
    entry_gaps = [g['entry_gap_ms'] for g in gaps
                  if g['entry_gap_ms'] is not None]
    exit_gaps = [g['exit_gap_ms'] for g in gaps
                 if g['exit_gap_ms'] is not None]

    def gap_stats(values):
        if not values:
            return {'measured': 0, 'mean_ms': None, 'worst_ms': None}
        return {'measured': len(values),
                'mean_ms': sum(values) / len(values),
                'worst_ms': max(values)}

    return {'liquidity': liquidity,
            'gaps': sorted(gaps, key=lambda g: -(g['opened_at'] or 0)),
            'entry_gap': gap_stats(entry_gaps),
            'exit_gap': gap_stats(exit_gaps)}
