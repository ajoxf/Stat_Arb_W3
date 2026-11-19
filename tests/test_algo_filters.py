"""The Algo's entry filters — and that LIVE cannot trade past them.

Each filter here is shown BLOCKING, and beside it a CONTROL that turns
that filter off and shows the same stretch trading. A filter test that
only ever sees a refusal passes on an Algo that never trades at all.
"""

import pytest

from okxtrader import algofilters as f
from okxtrader.coordinator import Coordinator

from test_algo_desk import QUIET, Clock, give_history

STRETCH = (4352.00, 4352.10)


# -- the arithmetic ---------------------------------------------------------


def test_the_round_trip_is_crossing_plus_commission_plus_slippage():
    cost = f.round_trip_cost(width=0.3, k=10.0, qty=2, commission=4.0,
                             slippage=1.0)
    assert cost['crossing'] == pytest.approx(6.0)
    assert cost['total'] == pytest.approx(11.0)


def test_an_unpriced_commission_is_an_unknown_cost_not_a_cheap_one():
    cost = f.round_trip_cost(width=0.3, k=10.0, qty=1, commission=None)
    assert cost['total'] is None
    assert f.edge(2.5, 0.1, 10.0, 1, cost['total'])['ok'] is None


def test_the_edge_is_capture_over_cost_against_the_multiple():
    # 0.5 x 2.5 x 0.4 x 10 x 1 = 5.0 capture against a 2.0 cost: 2.5x.
    passed = f.edge(2.5, 0.4, 10.0, 1, 2.0, capture_frac=0.5, multiple=1.5)
    assert passed['capture'] == pytest.approx(5.0)
    assert passed['ratio'] == pytest.approx(2.5) and passed['ok'] is True
    # The control: the same trade against a 4.0 cost is 1.25x — not enough.
    assert f.edge(2.5, 0.4, 10.0, 1, 4.0)['ok'] is False


def test_erfi_matches_its_published_values():
    assert f.erfi(1.0) == pytest.approx(1.6504257587975, rel=1e-10)
    assert f.erfi(2.0) == pytest.approx(18.564802414575, rel=1e-10)


def test_the_win_chance_falls_as_the_entry_nears_the_stop():
    assert f.win_probability(2.5, 4.0) > 0.95
    assert f.win_probability(3.9, 4.0) < 0.5
    assert f.win_probability(4.2, 4.0) == 0.0


def test_the_probability_gate_names_what_failed():
    near_stop = f.probability(3.9, 0.4, 10.0, 1, 0.5, stop_z=4.0)
    assert near_stop['ok'] is False and 'win chance' in near_stop['reason']
    under_cost = f.probability(0.5, 0.01, 10.0, 1, 5.0, stop_z=4.0)
    assert under_cost['ok'] is False and 'break-even' in under_cost['reason']
    # The control: an ordinary 2.5 stretch that pays for itself.
    assert f.probability(2.5, 0.4, 10.0, 1, 0.5, stop_z=4.0)['ok'] is True


def test_a_trend_and_a_range_are_told_apart():
    trend = f.regime([i * 0.1 for i in range(30)])
    assert trend['state'] == 'TRENDING' and trend['efficiency_ratio'] == 1.0
    assert f.regime([(-1) ** i for i in range(30)])['state'] == 'RANGE'
    assert f.regime([1.0, 2.0])['state'] == 'COLLECTING'


def test_half_life_is_measured_on_a_reverting_series_only():
    reverting = [1.0]
    for _ in range(40):
        reverting.append(reverting[-1] * 0.5 + (0.3 if len(reverting) % 7 == 0
                                                else 0.0))
    assert f.half_life(reverting) is not None
    assert f.half_life([float(i) for i in range(40)]) is None or \
        f.half_life([float(i) for i in range(40)]) > 5


# -- LIVE cannot trade past them ---------------------------------------------


def engine(config, legs, params):
    pair = next(iter(config.pairs.values()))
    pair.algo_params = dict(QUIET, **params)
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=Clock())
    coordinator.start()
    coordinator.poll_once()
    answer = coordinator.set_algo(pair.key, 'ALGO', mode='LIVE',
                                  confirmed=True)
    assert answer['ok'], answer
    coordinator.poll_once()
    return coordinator, pair


def stretched(coordinator, legs, pair):
    legs['acct_b'].broker.quote('GC1226', *STRETCH)
    coordinator.poll_once()
    return coordinator.snapshot()['pairs'][pair.key]['algo_block']


def test_the_edge_filter_stops_a_LIVE_entry_and_says_so(config, pair, legs):
    give_history(legs)
    coordinator, pair = engine(config, legs, {'edge_on': True})
    body = stretched(coordinator, legs, pair)
    assert coordinator.book.positions(pair.key) == []
    assert 'edge filter' in body['blocked']
    assert body['last_blocked']['side'] == 'SELL'
    assert 'edge filter' in body['last_blocked']['reason']
    assert body['filters']['edge']['on'] is True


def test_the_CONTROL_without_the_edge_filter_it_trades(config, pair, legs):
    give_history(legs)
    coordinator, pair = engine(config, legs, {'edge_on': False})
    stretched(coordinator, legs, pair)
    assert len(coordinator.book.positions(pair.key)) == 1


def test_an_edge_that_pays_is_traded(config, pair, legs):
    """The filter on, and a cheap enough round trip: it clears and the
    Algo enters — the filter is a test, not a wall."""
    give_history(legs)
    coordinator, pair = engine(config, legs, {'edge_on': True,
                                              'edge_multiple': 0.3})
    stretched(coordinator, legs, pair)
    assert len(coordinator.book.positions(pair.key)) == 1


def test_collecting_data_cannot_trade_LIVE(config, pair, legs):
    """No history from MT5 and nothing collected yet: the band does not
    exist, and no entry can be taken against it."""
    coordinator, pair = engine(config, legs, {})
    body = stretched(coordinator, legs, pair)
    assert coordinator.book.positions(pair.key) == []
    assert body['filters']['ready'] is False
    assert 'candles' in body['blocked']


def test_a_trending_spread_is_not_entered(config, pair, legs):
    give_history(legs)
    # Turn the history into a steady climb: a trend, not a range.
    for i, bar in enumerate(legs['acct_b'].broker.bars['GC1226']):
        bar['close'] = 4292.10 + 58.0 + 0.01 * i     # a gentle climb
    coordinator, pair = engine(config, legs, {'regime_on': True})
    body = stretched(coordinator, legs, pair)
    assert body['filters']['regime']['state'] == 'TRENDING'
    assert coordinator.book.positions(pair.key) == []
    assert 'TRENDING' in body['blocked']
    # The control: the same trend with the regime filter off.
    pair.algo_params = dict(QUIET, regime_on=False)
    coordinator.poll_once()
    coordinator.poll_once()
    assert len(coordinator.book.positions(pair.key)) == 1


def test_the_probability_gate_is_withdrawn_even_if_a_config_asks_for_it(
        config, pair, legs):
    """It priced the mean before the z-stop, not TP before SL, and read
    ~99% on every entry. Withdrawn: a saved prob_on=True must not keep
    it filtering trades from behind a screen that no longer shows it."""
    from okxtrader import algo
    assert algo.DEFAULT_PARAMS['prob_on'] is False
    assert algo.clean_params({'prob_on': True})['prob_on'] is False
    give_history(legs)
    # A stop close behind the entry: the gate WOULD have blocked this.
    coordinator, pair = engine(config, legs, {'prob_on': True, 'stop_z': 3.2})
    body = stretched(coordinator, legs, pair)
    assert 'probability' not in (body['blocked'] or '')
    assert len(coordinator.book.positions(pair.key)) == 1


def test_the_half_life_band_holds_an_entry_when_it_is_set(config, pair, legs):
    give_history(legs)
    coordinator, pair = engine(config, legs, {'half_life_min_min': 10000})
    body = stretched(coordinator, legs, pair)
    assert coordinator.book.positions(pair.key) == []
    assert 'half-life' in body['blocked']
    # The control: the band unset.
    pair.algo_params = dict(QUIET, half_life_min_min=0)
    coordinator.poll_once()
    assert len(coordinator.book.positions(pair.key)) == 1


def test_the_panel_reads_the_filters_at_the_entry_threshold(config, pair,
                                                            legs):
    """Real numbers before z ever gets there: cost, capture and the
    win chance at the entry z, with the cost split into its parts."""
    give_history(legs)
    coordinator, pair = engine(config, legs, {})
    filters = coordinator.snapshot()['pairs'][pair.key]['algo_block'][
        'filters']
    assert filters['ready'] is True
    assert filters['cost']['crossing'] > 0
    assert filters['cost']['total'] == pytest.approx(
        filters['cost']['crossing'] + (filters['cost']['commission'] or 0)
        + (filters['cost']['slippage'] or 0))
    assert filters['edge']['capture'] > 0
    assert filters['regime']['state'] in ('RANGE', 'TRENDING')
