"""One position per ladder side: every combined number is checked
against the separate fills it came from.

The arithmetic is exact (see `combined.py`): P&L at a price is linear
in that price, so the combined P&L is the SUM of the fills', and the
combined break-even, target and stop are money-weighted averages.
"""

import pytest

from okxtrader import combined
from okxtrader.coordinator import Coordinator
from okxtrader.models import OrderType

K = 1000.0          # money per 1.00 of spread, per spread (1 lot x 1,000)


def fill(pid, entry, opened, qty=1.0, atr=0.06, tp_mult=2.0, sl_mult=8.0,
         closing=9.90, side='SELL', units=None):
    """One position as `_algo_positions` publishes it, in ATR mode:
    break-even at the entry (costs aside), TP and SL multiples of the
    ATR frozen at ITS entry."""
    units = K * qty if units is None else units
    sign = 1.0 if side == 'BUY' else -1.0
    be = entry
    tp = be + sign * tp_mult * atr
    sl = be - sign * sl_mult * atr
    return {'position_id': pid, 'side': side, 'source': 'MANUAL',
            'quantity': qty, 'entry_spread': entry, 'opened_at': opened,
            'age_sec': 1000.0 - opened, 'break_even': be, 'tp': tp, 'sl': sl,
            'tp_money': round(sign * (tp - be) * units, 2),
            'sl_money': round(sign * (sl - be) * units, 2),
            'net_pnl': round(sign * (closing - be) * units, 2),
            'entry_atr': atr, 'stop_mode': 'ATR', 'target_mode': 'ATR',
            'atr_stop_mult': sl_mult, 'atr_target_mult': tp_mult,
            'spread_units': units, 'entry_margin': 500.0,
            'leg_a_side': 'BUY' if side == 'SELL' else 'SELL',
            'leg_a_entry': 82.40, 'leg_a_now': 82.23,
            'leg_a_volume': qty,
            'leg_b_side': side, 'leg_b_entry': 91.90 + entry - 9.48,
            'leg_b_now': 92.13, 'leg_b_volume': qty}


def worth(row, level):
    sign = 1.0 if row['side'] == 'BUY' else -1.0
    return sign * (level - row['break_even']) * row['spread_units']


def test_one_position_is_left_exactly_as_it_came():
    rows = [fill('P1', 9.48, 10.0)]
    assert combined.combine(rows) == rows


def test_two_fills_are_one_position_with_their_sum_and_average():
    a, b = fill('P1', 9.48, 10.0), fill('P2', 10.04, 900.0)
    [row] = combined.combine([b, a])
    assert row['combined'] and row['position_ids'] == ['P1', 'P2']
    assert row['position_id'] == 'AVG:P1+P2'
    assert row['quantity'] == 2.0 and row['spread_units'] == 2 * K
    assert row['entry_spread'] == pytest.approx(9.76)
    assert row['break_even'] == pytest.approx(9.76)
    # The SAME ATR on both: the levels keep their distance from BE.
    assert row['tp'] == pytest.approx(9.76 - 0.12)
    assert row['sl'] == pytest.approx(9.76 + 0.48)
    assert row['net_pnl'] == pytest.approx(a['net_pnl'] + b['net_pnl'])
    assert row['opened_at'] == 10.0                      # the oldest
    assert [f['position_id'] for f in row['fills']] == ['P1', 'P2']


@pytest.mark.parametrize('level', [9.30, 9.64, 9.76, 10.0, 10.24, 10.6])
def test_the_combined_money_at_any_price_is_the_sum_of_the_fills(level):
    fills = [fill('P1', 9.48, 1.0), fill('P2', 10.04, 2.0, qty=2.0, atr=0.09),
             fill('P3', 10.20, 3.0, qty=0.5, atr=0.05)]
    [row] = combined.combine(fills)
    total = sum(worth(f, level) for f in fills)
    assert worth(row, level) == pytest.approx(total)
    # ...and at its own target and stop, what the screen prints.
    assert row['tp_money'] == pytest.approx(
        sum(worth(f, row['tp']) for f in fills), abs=0.02)
    assert row['sl_money'] == pytest.approx(
        sum(worth(f, row['sl']) for f in fills), abs=0.02)
    # Break-even really is where the fills sum to nothing.
    assert sum(worth(f, row['break_even']) for f in fills) == \
        pytest.approx(0.0, abs=1e-6)


def test_ATR_mode_uses_the_size_weighted_ATR():
    a = fill('P1', 9.48, 1.0, qty=1.0, atr=0.06)
    b = fill('P2', 10.04, 2.0, qty=3.0, atr=0.10)
    [row] = combined.combine([a, b])
    weighted_atr = (1.0 * 0.06 + 3.0 * 0.10) / 4.0
    assert row['entry_atr'] == pytest.approx(weighted_atr)
    assert row['tp'] == pytest.approx(row['break_even'] - 2.0 * weighted_atr)
    assert row['sl'] == pytest.approx(row['break_even'] + 8.0 * weighted_atr)


def test_an_unpriced_level_on_one_fill_leaves_the_combined_level_unpriced():
    a, b = fill('P1', 9.48, 1.0), fill('P2', 10.04, 2.0)
    b['sl'] = None
    [row] = combined.combine([a, b])
    assert row['sl'] is None and row['sl_money'] is None
    assert row['tp'] is not None                # the priced one stays
    # Control: priced on both, it is a number.
    [row] = combined.combine([fill('P1', 9.48, 1.0), fill('P2', 10.04, 2.0)])
    assert row['sl'] is not None


def test_each_side_is_its_own_position():
    rows = combined.combine([fill('P1', 9.48, 1.0),
                             fill('P2', 9.10, 2.0, side='BUY')])
    assert len(rows) == 2 and not any(r.get('combined') for r in rows)


def test_each_leg_shows_its_total_lots_and_average_fill():
    a = fill('P1', 9.48, 1.0, qty=1.0)
    b = fill('P2', 10.04, 2.0, qty=3.0)
    [row] = combined.combine([a, b])
    assert row['leg_b_volume'] == 4.0
    assert row['leg_b_entry'] == pytest.approx(
        (a['leg_b_entry'] * 1 + b['leg_b_entry'] * 3) / 4)


def test_working_closes_are_counted_against_the_whole():
    [row] = combined.combine([fill('P1', 9.48, 1.0), fill('P2', 10.04, 2.0)],
                             covered={'P1': 1.0})
    assert row['close_covered'] == 1.0 and row['quantity'] == 2.0


# -- in the engine -------------------------------------------------------------

@pytest.fixture
def engine(config, pair, legs):
    co = Coordinator(config, legs, sleep=lambda s: None)
    co.start()
    co.poll_once()
    return co


def two_shorts(engine, pair):
    md = engine.market[pair.key]
    pair.order_type = OrderType.MARKET
    for _ in range(2):
        assert engine.click(pair.key, 'SELL', md['short_spread'], 1)['ok']
    pair.order_type = OrderType.LIMIT


def fed(engine, pair, mode):
    seen = {}
    engine.algos.is_on = lambda key: True
    engine.algos.mode = lambda key: mode
    engine.algos.observe = lambda pair, md, rows, gates: seen.setdefault(
        'rows', rows)
    engine._work_algo(pair, engine.market[pair.key])
    return seen['rows']


def test_signals_mode_reads_two_fills_as_one_position(engine, pair):
    two_shorts(engine, pair)
    rows = fed(engine, pair, 'DRY_RUN')
    assert len(rows) == 1 and rows[0]['combined']
    assert rows[0]['quantity'] == 2.0


def test_CONTROL_LIVE_is_never_combined(engine, pair):
    two_shorts(engine, pair)
    rows = fed(engine, pair, 'LIVE')
    assert len(rows) == 2 and not any(r.get('combined') for r in rows)


def test_the_exit_alert_names_the_whole_position():
    from okxtrader import algodesk
    [row] = combined.combine([fill('P1', 9.48, 1.0), fill('P2', 10.04, 2.0)])
    intent = {'action': 'EXIT', 'side': 'SELL', 'reason': 'STOP_LOSS',
              'position_id': row['position_id'], 'spread': 10.24,
              'net_pnl': row['sl_money']}
    alert = algodesk.signal_alert(intent, [row], {'algo_qty': 1}, 100.0)
    assert 'close your SELL 2 at' in alert['text'], alert['text']


def test_the_real_Algo_passes_the_combined_position_to_the_screen(engine,
                                                                   pair):
    """Not a stand-in: the Algo itself, in Signals, with two shorts on.
    What reaches the window is ONE position carrying its fills."""
    two_shorts(engine, pair)
    assert engine.algos.turn_on(pair)['ok']
    for _ in range(3):
        engine.poll_once()
    [row] = engine.algos.block(pair.key)['positions']
    assert row['combined'] is True and row['quantity'] == 2.0
    assert len(row['fills']) == 2 and row['close_covered'] == 0.0
    assert row['leg_a_volume'] and row['leg_b_volume']
    assert row['tp_money'] is not None and row['sl_money'] is not None
