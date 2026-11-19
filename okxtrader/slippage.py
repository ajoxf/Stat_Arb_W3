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
    window lines up with the deal stamps in MT5's own History. With no
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
                f'the same clock MT5 stamps its deals with')
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
            **_leg_slips(position),
        })
    return rows


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
        'counts': {
            'positions': len(rows),
            'open': sum(1 for r in rows if r['open']),
            'closed': sum(1 for r in rows if not r['open']),
        },
    }
