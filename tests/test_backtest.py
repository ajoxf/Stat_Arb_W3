"""The backtest: history through the Algo's own decision code.

It has to show what the protections are for — a climbing spread sold
into by the plain Algo and left alone by the protected one — and that a
range still trades. And it can never reach an order.
"""

import ast
import random

import pytest

from okxtrader import backtest

TF = 900.0
COST = {'k': 10.0, 'commission': 0.0, 'slippage': 0.0}
LEVELS = {'fee_points': 0.0, 'target_points': 0.05, 'stop_points': 0.30}
BASE = {'entry_z': 2.0, 'reentry_back': 0.5, 'edge_on': False,
        'regime_on': False, 'cutoff_buffer_min': 0, 'cooldown_min': 0,
        'max_trades_day': 0, 'max_losses_row': 0, 'max_entry_z': 0,
        'trend_sigma': 1.0, 'trend_lookback_min': 120}
PROTECTED = dict(BASE, reentry_on=True, trend_on=True)
PLAIN = dict(BASE, reentry_on=False, trend_on=False)


def climbing(n=200, seed=7):
    rng = random.Random(seed)
    return [(i * TF, 10.0 + 0.02 * i + rng.uniform(-0.06, 0.06))
            for i in range(n)]


def ranging(n=200, seed=3):
    """Quiet around 10.00, with a stretch every 25 candles that comes
    back over the next three."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        value = 10.0 + rng.uniform(-0.02, 0.02)
        phase = i % 25
        if i > 30 and phase == 0:
            value += 0.20
        elif i > 30 and phase == 1:
            value += 0.06
        rows.append((i * TF, value))
    return rows


def sells(result):
    return [t for t in result['trades'] if t['side'] == 'SELL']


def test_a_climbing_spread_is_sold_into_without_the_protections():
    result = backtest.run(climbing(), PLAIN, 0.01, COST, LEVELS)
    assert len(sells(result)) >= 3


def test_and_left_alone_with_them():
    result = backtest.run(climbing(), PROTECTED, 0.01, COST, LEVELS)
    assert sells(result) == []
    assert any('trend' in reason for reason in result['held'])


def test_a_range_still_trades_with_the_protections_on():
    result = backtest.run(ranging(), PROTECTED, 0.01, COST, LEVELS)
    assert result['summary']['trades'] >= 3
    assert result['summary']['wins'] >= 1


def test_the_summary_adds_up():
    result = backtest.run(ranging(), PLAIN, 0.01, COST, LEVELS)
    s = result['summary']
    assert s['wins'] + s['losses'] == s['closed']
    closed = [t for t in result['trades'] if t['pnl'] is not None]
    assert s['net'] == pytest.approx(sum(t['pnl'] for t in closed))
    assert s['max_drawdown'] <= 0
    assert result['caveats']


def test_no_history_is_no_trades_not_an_error():
    result = backtest.run([], PROTECTED, 0.01, COST, LEVELS)
    assert result['trades'] == [] and result['summary']['net'] is None


def test_the_backtest_cannot_reach_an_order():
    tree = ast.parse(open(backtest.__file__, encoding='utf-8').read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add((node.module or '') + ':' + ','.join(
                a.name for a in node.names))
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert imported <= {'datetime', 're', ':algo', ':algofilters', ':bands',
                        'algodesk:judge_filters'}, imported


# -- through the engine ------------------------------------------------------------


def test_the_engine_backtests_a_ladder_from_mt5_history(config, pair, legs):
    from okxtrader.coordinator import Coordinator
    from test_algo_desk import Clock, give_history
    give_history(legs, count=80)
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=Clock())
    coordinator.start()
    coordinator.poll_once()
    answer = coordinator.algo_backtest(pair.key, days=1)
    assert answer['ok'], answer
    assert answer['summary']['candles'] > 20
    assert 'without_protections' in answer
    assert answer['width'] > 0
    # Nothing was sent: the brokers saw no order.
    assert legs['acct_a'].broker.sent == [] and legs['acct_b'].broker.sent == []


def test_the_engine_says_why_it_cannot_backtest(config, pair, legs):
    from okxtrader.coordinator import Coordinator
    from test_algo_desk import Clock
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=Clock())
    coordinator.start()
    coordinator.poll_once()
    answer = coordinator.algo_backtest(pair.key, days=1)    # no bars
    assert answer['ok'] is False and 'history' in answer['reason']


# -- derived history, written down ------------------------------------------

def give_reverting_history(legs, count=600):
    """A mean-reverting spread that actually STRETCHES.

    `give_history` alternates two prices, so z never leaves +-1 and the
    Algo never signals - fine for the tests it was written for, useless
    for one about what the Algo would have traded. This is an
    Ornstein-Uhlenbeck walk with a fixed seed: it pulls back towards its
    mean, wanders past the entry threshold often enough to signal, and
    is the same series on every run.

    It moves in WHOLE POINTS rather than cents, and that is deliberate.
    The fake symbols quote a 0.60 bid-ask against a k of 10, so a
    quieter spread is refused by the edge filter every time - capture
    never clears 1.5x the round trip - and the test would then be
    asserting that an untradeable market produces no trades, which is
    true and useless.
    """
    import math
    from test_algo_desk import T0, OFFSET
    start = T0 - (T0 % 900) - 900 * count
    bars_a, bars_b = [], []
    x, seed = 59.10, 12345
    for i in range(count):
        # A small deterministic PRNG, so the series never moves between
        # runs and a failure is always reproducible.
        seed = (1103515245 * seed + 12345) % (2 ** 31)
        noise = (seed / float(2 ** 31)) - 0.5
        x += 0.08 * (59.10 - x) + 0.9 * noise
        # A SHOCK every so often, alternating sides. Sigma is a rolling
        # window, so it adapts to a steady walk and |z| hardly ever
        # clears 2.5 - which is realistic for a quiet market and
        # useless for a test about what the Algo would have traded.
        if i % 45 == 0 and i:
            x += 4.5 if (i // 45) % 2 else -4.5
        stamp = start + 900 * i + OFFSET
        bars_a.append({'time': stamp, 'close': 4292.10, 'spread': 0,
                       'point': 0.01})
        bars_b.append({'time': stamp, 'close': 4292.10 + x,
                       'spread': 0, 'point': 0.01})
    legs['acct_a'].broker.bars['XAUUSD_'] = bars_a
    legs['acct_b'].broker.bars['GC1226'] = bars_b


def backfill_engine(config, legs, tmp_path, count=600):
    from okxtrader.coordinator import Coordinator
    from okxtrader.database import Store
    from test_algo_desk import Clock
    give_reverting_history(legs, count=count)
    store = Store(str(tmp_path / 'trader.db'))
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=Clock(), store=store)
    coordinator.start()
    coordinator.poll_once()
    return coordinator, store


def test_the_engine_derives_past_signals_from_history(config, pair, legs,
                                                      tmp_path):
    """The live track only measures forward, so a day that has already
    gone has nothing in it. This fills those in from MT5's own bars."""
    coordinator, store = backfill_engine(config, legs, tmp_path)
    assert store.signals() == []
    answer = coordinator.backfill_signals(pair.key, days=30)
    assert answer['ok'], answer
    rows = store.signals(pair.key)
    assert rows, answer
    assert answer['written'] == len(rows)
    # Every one of them says it was DERIVED, not measured.
    assert all(r['source'] == 'REPLAY' for r in rows)
    assert all(r['basis'] == 'ALGO' for r in rows)
    assert all(r['broker_day'] for r in rows)
    # Nothing was sent: a reading cannot reach an order.
    assert legs['acct_a'].broker.sent == [] and legs['acct_b'].broker.sent == []


def test_deriving_twice_replaces_rather_than_doubles(config, pair, legs,
                                                     tmp_path):
    """A replay is a function of the candles and the settings. Running
    it again must not leave two of every trade."""
    coordinator, store = backfill_engine(config, legs, tmp_path)
    first = coordinator.backfill_signals(pair.key, days=30)
    count = len(store.signals(pair.key))
    assert count
    again = coordinator.backfill_signals(pair.key, days=30)
    assert len(store.signals(pair.key)) == count
    assert again['pairs'][pair.key]['replaced'] == first['written']


def test_a_derived_row_never_overwrites_a_measured_one(config, pair, legs,
                                                       tmp_path):
    """Live rows were measured. Nothing re-derives a measurement."""
    coordinator, store = backfill_engine(config, legs, tmp_path)
    live_id = store.save_signal({
        'pair_key': pair.key, 'basis': 'ALGO', 'side': 'SELL',
        'state': 'CLOSED', 'outcome': 'TP', 'armed_at': 10.0,
        'armed_z': 2.6, 'source': 'LIVE', 'net_pnl': 12.0})
    coordinator.backfill_signals(pair.key, days=30)
    coordinator.backfill_signals(pair.key, days=30)
    kept = [r for r in store.signals(pair.key) if r['id'] == live_id]
    assert len(kept) == 1 and kept[0]['source'] == 'LIVE'


def test_the_derived_rows_carry_their_outcome_and_their_money(config, pair,
                                                              legs, tmp_path):
    coordinator, store = backfill_engine(config, legs, tmp_path)
    coordinator.backfill_signals(pair.key, days=30)
    closed = [r for r in store.signals(pair.key) if r['state'] == 'CLOSED']
    assert closed
    assert all(r['outcome'] in ('TP', 'SL', 'EXPIRED', 'EXIT') for r in closed)
    assert any(r['net_pnl'] is not None for r in closed)
    assert all(r['entered_at'] is not None for r in closed)


def test_a_derived_row_names_its_accounts_and_its_edge(config, pair, legs,
                                                      tmp_path):
    """A replayed row says what it was read off - each leg's account
    and broker - and the edge filter's reading at its entry, as a live
    row does."""
    coordinator, store = backfill_engine(config, legs, tmp_path)
    coordinator.backfill_signals(pair.key, days=30)
    rows = store.signals(pair.key)
    assert rows
    for row in rows:
        assert (row['leg_a_account'], row['leg_b_account']) == \
            (pair.account_a, pair.account_b)
        assert row['leg_a_broker'] == 'FakeServer'
        assert row['leg_b_broker'] == 'FakeServer'
        assert row['edge_ratio'] is not None and row['edge_ratio'] > 0
        assert row['edge_required'] is not None
        assert row['edge_on'] is not None


def test_a_ladder_with_no_history_says_so_and_writes_nothing(config, pair,
                                                             legs, tmp_path):
    from okxtrader.coordinator import Coordinator
    from okxtrader.database import Store
    from test_algo_desk import Clock
    store = Store(str(tmp_path / 'trader.db'))
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=Clock(), store=store)
    coordinator.start()
    coordinator.poll_once()
    answer = coordinator.backfill_signals(pair.key, days=30)
    assert answer['ok'] and answer['written'] == 0
    assert answer['pairs'][pair.key]['ok'] is False
    assert store.signals() == []


def test_a_derive_that_finds_nothing_says_what_held_it_back(config, pair,
                                                            legs, tmp_path):
    """'0 derived' on its own reads as a broken replay.

    On a live desk the derive came back with nothing and there was no
    way to tell a filter doing its job from a replay that never ran.
    The replay already counts the reason it refused every entry; this
    is that count reaching the caller.
    """
    coordinator, store = backfill_engine(config, legs, tmp_path)
    plenty = coordinator.backfill_signals(pair.key, days=30)
    assert plenty['written'], plenty            # the control: it DOES trade

    # The same candles, with an edge filter nothing could clear.
    pair.algo_params = dict(pair.algo_params or {}, edge_on=True,
                            edge_multiple=10000.0)
    answer = coordinator.backfill_signals(pair.key, days=30)
    assert answer['ok'] and answer['written'] == 0
    held = answer['pairs'][pair.key]['held']
    assert held, answer
    assert any('edge' in reason for reason in held)
    # And the same counts, summed over the ladders, at the top level.
    assert answer['held'] == held
    assert all(isinstance(count, int) and count > 0
               for count in held.values())
    assert store.signals(pair.key) == []       # the old rows were replaced
