"""One position per ladder side, the way a desk reads it.

Hedging accounts keep every fill as its own position, with its own
tickets - and the closes still go by those tickets. But the TRADER reads
a ladder the way every institutional platform shows it: one net
position, one average entry, one break-even, one take-profit and one
stop. This turns the per-position rows the Algo prices into that.

Display and signals only. Nothing here places, sizes or closes an
order, and the LIVE Algo never sees a combined row: it holds one trade
at a time, and its closes go by the position it opened.

THE ARITHMETIC IS EXACT, NOT AN APPROXIMATION. Each fill's net P&L at a
closing price L is sign x (L - its break-even) x its money per point,
and every cost that fill paid is already inside its break-even. That is
linear in L, so:

- the combined P&L at L is the SUM over the fills;
- the combined break-even, where that sum is zero, is the average of
  the fills' break-evens weighted by money per point (`spread_units`);
- the combined take-profit and stop are the same weighted average of
  each fill's own. In ATR mode each fill's distance is its multiple of
  the ATR frozen at ITS entry, so this IS the weighted ATR; in % of
  margin mode it is the total money target over the total money per
  point.

Unmeasured is not zero: one fill whose level could not be priced makes
the combined level None, never an average of the ones that could.
"""

COMBINED_PREFIX = 'AVG:'


def _weights(rows):
    """Money per point when every fill has it; size when none does.
    A mix cannot be weighted honestly, so it is None."""
    units = [r.get('spread_units') for r in rows]
    if all(u for u in units):
        return [float(u) for u in units]
    if not any(units):
        qty = [r.get('quantity') for r in rows]
        if all(q for q in qty):
            return [float(q) for q in qty]
    return None


def _weighted(rows, weights, name):
    values = [r.get(name) for r in rows]
    if weights is None or any(v is None for v in values):
        return None
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


def _total(rows, name):
    values = [r.get(name) for r in rows]
    if any(v is None for v in values):
        return None
    return round(sum(values), 2)


def _leg(rows, leg):
    """The leg's side, total lots and size-weighted fill price."""
    side = rows[0].get(f'leg_{leg}_side')
    lots = [r.get(f'leg_{leg}_volume') for r in rows]
    prices = [r.get(f'leg_{leg}_entry') for r in rows]
    out = {f'leg_{leg}_side': side,
           f'leg_{leg}_now': rows[0].get(f'leg_{leg}_now')}
    if all(lots) and all(p is not None for p in prices):
        total = sum(lots)
        out[f'leg_{leg}_volume'] = round(total, 8)
        out[f'leg_{leg}_entry'] = sum(p * v for p, v in
                                      zip(prices, lots)) / total
    else:
        out[f'leg_{leg}_volume'] = None
        out[f'leg_{leg}_entry'] = None
    return out


def combine(rows, covered=None, now=None):
    """The Algo's per-position rows, with each side's two or more fills
    folded into ONE row. A side holding a single position is returned
    exactly as it came, so nothing changes until there is an add.

    `covered`: position id -> quantity already working to CLOSE it, so
    the screen can say "your close covers 1 of 2".
    """
    covered = covered or {}
    out = []
    by_side = {}
    for row in rows or ():
        by_side.setdefault(row.get('side'), []).append(row)
    for side, group in by_side.items():
        if len(group) < 2:
            out.extend(group)
            continue
        group = sorted(group, key=lambda r: (r.get('opened_at') or 0.0,
                                             str(r.get('position_id'))))
        weights = _weights(group)
        quantity = round(sum(float(r.get('quantity') or 0.0)
                             for r in group), 8)
        be = _weighted(group, weights, 'break_even')
        tp = _weighted(group, weights, 'tp')
        sl = _weighted(group, weights, 'sl')
        units = (sum(float(r['spread_units']) for r in group)
                 if all(r.get('spread_units') for r in group) else None)
        sign = 1.0 if side == 'BUY' else -1.0

        def worth(level):
            if level is None or be is None or not units:
                return None
            return round(sign * (level - be) * units, 2)

        first = group[0]
        ids = [r['position_id'] for r in group]
        row = {
            'position_id': COMBINED_PREFIX + '+'.join(str(i) for i in ids),
            'combined': True,
            'position_ids': ids,
            'side': side,
            'source': first.get('source'),
            'quantity': quantity,
            'entry_spread': _weighted(group, weights, 'entry_spread'),
            'opened_at': first.get('opened_at'),
            'age_sec': first.get('age_sec'),
            'break_even': be, 'tp': tp, 'sl': sl,
            'tp_money': worth(tp), 'sl_money': worth(sl),
            'net_pnl': _total(group, 'net_pnl'),
            'entry_atr': _weighted(group, weights, 'entry_atr'),
            'stop_mode': first.get('stop_mode'),
            'target_mode': first.get('target_mode'),
            'atr_stop_mult': first.get('atr_stop_mult'),
            'atr_target_mult': first.get('atr_target_mult'),
            'tp_pct': first.get('tp_pct'),
            'stop_loss_pct': first.get('stop_loss_pct'),
            'entry_margin': _weighted(group, weights, 'entry_margin'),
            'stop_points': (None if sl is None or be is None
                            else abs(sl - be)),
            'spread_units': units,
            'fills': [{'position_id': r['position_id'],
                       'quantity': r.get('quantity'),
                       'entry_spread': r.get('entry_spread'),
                       'opened_at': r.get('opened_at'),
                       'net_pnl': r.get('net_pnl')} for r in group],
            'close_covered': round(sum(float(covered.get(i) or 0.0)
                                       for i in ids), 8),
        }
        row.update(_leg(group, 'a'))
        row.update(_leg(group, 'b'))
        out.append(row)
    return out
