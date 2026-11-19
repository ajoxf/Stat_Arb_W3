"""The hypothetical levels a signal would have been given.

What is left of a bigger module. A SIGMA ZONE TRACK lived here: a row
every time the spread's zone changed - Mean, +-1, +-2, +-3 sigma. It
was taken back out, and the number that decided it is worth keeping:
on one live desk it wrote **16,562 rows in a single day**, almost all
of them the spread flickering across the |z| = 1 boundary a second at
a time. Nobody read them, every one cost a write, and the page was
harder to use for having them on it.

What survives is the arithmetic the signals need: break-even is the
entry plus the costs to recover, and the target and the stop are
measured FROM break-even - never from the entry, and never from the
current touch. The same arithmetic `takeprofit.for_position` uses for
a position that is really on, with the fill it WOULD have had in place
of the one it did.

Nothing here reads a price, a broker or a clock, and nothing here can
reach an order.
"""

import math

def round_to(value, step):
    """`value` on the spread's own grid. No step means no rounding -
    never a price invented to a tidier number than the pair quotes."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not step:
        return value
    step = float(step)
    if step <= 0:
        return value
    return round(round(value / step) * step, 10)



def entry_side(z):
    """Which way the Algo would enter at this z.

    It sells a spread that is stretched high and buys one stretched
    low: `SpreadSide.SELL` above the mean, `BUY` below. None at the
    mean, where it would not enter at all.
    """
    if z is None:
        return None
    if z > 0:
        return 'SELL'
    if z < 0:
        return 'BUY'
    return None



def levels(side, entry_spread, fee_points, target_points, stop_points,
           step=None):
    """The hypothetical entry, take-profit and stop, on the spread grid.

    The same arithmetic `takeprofit.for_position` uses for a real
    position, with the fill it WOULD have had in place of the one it
    did: break-even is the entry plus the costs to recover, and the
    target and the stop are measured from break-even - never from the
    entry, and never from the current touch.

    Each is None where its input was not measured. A stop of None is a
    stop that could not be priced, which is not a stop at zero.
    """
    body = {'entry': None, 'break_even': None, 'tp': None, 'sl': None}
    if side not in ('BUY', 'SELL') or entry_spread is None:
        return body
    try:
        entry = float(entry_spread)
    except (TypeError, ValueError):
        return body
    if not math.isfinite(entry):
        return body
    body['entry'] = round_to(entry, step)
    if fee_points is None:
        return body
    sign = 1.0 if side == 'BUY' else -1.0
    break_even = entry + sign * float(fee_points)
    body['break_even'] = round_to(break_even, step)
    if target_points is not None:
        body['tp'] = round_to(break_even + sign * float(target_points), step)
    if stop_points is not None:
        body['sl'] = round_to(break_even - sign * float(stop_points), step)
    return body
