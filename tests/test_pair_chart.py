"""The pair chart: each ladder's spread and the Algo's own z, as candles.

The chart reads; it never decides. Its z is the Algo's own - the number
it armed and entered on - so the line crosses the entry exactly where
the Algo did.
"""

import json

import pytest

from okxtrader import chart
from okxtrader.coordinator import Coordinator
from okxtrader.webapp import create_app

from test_signal_journal import ARM, HOME, REENTRY, Desk


def md(spread, jump=None):
    return {'mid_spread': spread, 'jump_reason': jump}


BODY = {'z_mid': 1.25, 'mean': 59.1, 'sigma': 0.1, 'ready': True,
        'count': 20, 'needed': 20}
PARAMS = {'entry_z': 2.0, 'stop_z': 4.0, 'stop_z_on': True}


def test_every_poll_goes_into_a_ten_second_candle():
    """Open, high, low and close of the spread AND of the Algo's z: a
    high or a low a poll saw is never thinned away."""
    tape = chart.SpreadTape()
    for now, spread, z in ((100.0, 59.0, 1.0), (102.0, 59.4, 2.6),
                           (105.0, 58.8, -0.4), (109.9, 59.1, 0.9),
                           (110.0, 59.2, 1.1)):
        tape.observe('P', now, md(spread), dict(BODY, z_mid=z), PARAMS)
    first, second = tape.series('P')['candles']
    assert (first['t'], first['o'], first['h'], first['l'], first['c']) == \
        (100.0, 59.0, 59.4, 58.8, 59.1)
    assert (first['zo'], first['zh'], first['zl'], first['zc']) == \
        (1.0, 2.6, -0.4, 0.9)
    assert second['t'] == 110.0 and second['o'] == 59.2


def test_only_the_last_two_hours_are_kept():
    tape = chart.SpreadTape(keep_sec=60)
    for i in range(200):
        tape.observe('P', float(i), md(59.0), BODY, PARAMS)
    candles = tape.series('P')['candles']
    assert candles[0]['t'] >= 199 - 60 - 10 and candles[-1]['t'] == 190.0


def test_a_price_the_jump_guard_holds_back_is_not_drawn():
    tape = chart.SpreadTape()
    tape.observe('P', 1.0, md(59.0), BODY, PARAMS)
    tape.observe('P', 2.0, md(65.0, jump='jumped 8 sigma'), BODY, PARAMS)
    [candle] = tape.series('P')['candles']
    assert candle['h'] == 59.0                 # no wick to the glitch
    # Control: the same price, not held back, is in the candle.
    tape.observe('P', 3.0, md(65.0), BODY, PARAMS)
    assert tape.series('P')['candles'][0]['h'] == 65.0


def test_no_z_yet_is_no_z_not_zero():
    tape = chart.SpreadTape()
    tape.observe('P', 1.0, md(59.0), dict(BODY, z_mid=None), PARAMS)
    [candle] = tape.series('P')['candles']
    assert candle['o'] == 59.0 and candle.get('zo') is None


def test_min_and_max_are_the_wicks():
    tape = chart.SpreadTape()
    for i, spread in enumerate((59.0, 59.9, 58.1, 59.0)):
        tape.observe('P', float(i), md(spread), BODY, PARAMS)
    series = tape.series('P')
    assert (series['spread_min'], series['spread_max']) == (58.1, 59.9)


def test_the_stop_line_is_drawn_only_while_the_z_stop_is_on():
    tape = chart.SpreadTape()
    tape.observe('P', 1.0, md(59.0), BODY, PARAMS)
    assert tape.series('P')['stop_z'] == 4.0
    tape.observe('P', 3.0, md(59.0), BODY, dict(PARAMS, stop_z_on=False))
    assert tape.series('P')['stop_z'] is None
    assert tape.series('P')['entry_z'] == 2.0


# -- the engine ---------------------------------------------------------------

def test_the_engine_charts_the_z_the_algo_decided_on(config, pair, legs,
                                                     tmp_path):
    desk = Desk(config, pair, legs, tmp_path, **REENTRY)
    # 20 s apart: each poll is its own candle.
    bodies = desk.push(HOME, ARM, HOME)
    candles = desk.co.tape.series(pair.key)['candles']
    assert len(candles) >= 3
    # The chart's z IS the Algo's: the same number, poll for poll.
    assert [c['zc'] for c in candles[-3:]] == pytest.approx(
        [b['z_mid'] for b in bodies], abs=1e-3)
    assert desk.co.tape.series(pair.key)['entry_z'] == 2.5


def test_the_chart_file_is_written_beside_the_status_file(config, pair, legs,
                                                         tmp_path):
    status = tmp_path / 'status.json'
    co = Coordinator(config, legs, status_path=str(status),
                     sleep=lambda s: None)
    co.start()
    co.poll_once()
    co.publish()
    body = json.loads((tmp_path / 'chart.json').read_text(encoding='utf-8'))
    assert pair.key in body['pairs']


# -- the web route ------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / 'status.json'),
                     str(tmp_path / 'commands.jsonl'),
                     str(tmp_path / 'results.json'),
                     str(tmp_path / 'config.json'))
    app.config.update(TESTING=True)
    return app.test_client()


def test_the_route_draws_only_the_minutes_asked_for(client, tmp_path):
    tape = chart.SpreadTape()
    for i in range(0, 3600, 60):
        tape.observe('A|B', 10_000.0 + i, md(59.0 + i / 3600.0), BODY,
                     PARAMS)
    tape.write(str(tmp_path / 'chart.json'), 10_000.0 + 3540, force=True)
    body = client.get('/api/chart/A|B?minutes=15').get_json()
    assert body['ok'] and body['entry_z'] == 2.0
    assert len(body['candles']) == 16
    assert body['spread_min'] == pytest.approx(59.0 + 2640 / 3600.0)
    # Control: the whole hour.
    assert len(client.get('/api/chart/A|B?minutes=60')
               .get_json()['candles']) == 60


def test_a_ladder_with_nothing_yet_says_so(client):
    body = client.get('/api/chart/A|B').get_json()
    assert body['ok'] and body['candles'] == []
    assert 'fills while the engine runs' in body['note']


# -- 15m / 1h / 4h candles from MT5's own bars --------------------------------

from okxtrader import bands  # noqa: E402


def bar(stamp, close):
    return {'time': stamp, 'close': close, 'spread': 0, 'point': 0.01}


def test_a_candle_is_built_from_the_spreads_of_its_finer_bars():
    """A spread's high is not leg B's high less leg A's: the two are
    seldom the same minute. So a 15-minute candle is fifteen 1-minute
    SPREADS, and its open, high, low and close are theirs."""
    a = [bar(60 * i, 100.0) for i in range(30)]
    b = [bar(60 * i, 105.0 + (i % 15) * (1 if i % 2 else -1) * 0.1)
         for i in range(30)]
    candles = bands.spread_ohlc(a, b, 1.0, 60, 900, 0, 0)
    assert [c['t'] for c in candles] == [0, 900]
    first = candles[0]
    spreads = [b[i]['close'] - 100.0 for i in range(15)]
    assert first['o'] == pytest.approx(spreads[0])
    assert first['h'] == pytest.approx(max(spreads))
    assert first['l'] == pytest.approx(min(spreads))
    assert first['c'] == pytest.approx(spreads[-1])


def test_a_minute_only_one_leg_traded_is_not_in_the_candle():
    a = [bar(0, 100.0), bar(60, 100.0)]
    b = [bar(0, 105.0), bar(60, 150.0), bar(120, 199.0)]
    [candle] = bands.spread_ohlc(a, b, 1.0, 60, 900, 0, 0)
    assert candle['h'] == pytest.approx(50.0)       # not 99: no leg A then


def test_an_unplaced_clock_gives_no_candles_rather_than_wrong_ones():
    a = [bar(0, 100.0)]
    b = [bar(0, 105.0)]
    assert bands.spread_ohlc(a, b, 1.0, 60, 900, None, 0) == []


@pytest.fixture
def engine(config, pair, legs, tmp_path):
    from test_algo_desk import T0, OFFSET, Clock
    start = T0 - T0 % 900 - 60 * 15 * 40
    a, b = [], []
    for i in range(15 * 40):
        stamp = start + 60 * i + OFFSET
        a.append(bar(stamp, 4292.10))
        b.append(bar(stamp, 4351.20 + 0.3 * ((i * 7) % 11) / 10.0))
    legs['acct_a'].broker.bars['XAUUSD_'] = a
    legs['acct_b'].broker.bars['GC1226'] = b
    co = Coordinator(config, legs, status_path=str(tmp_path / 'status.json'),
                     sleep=lambda s: None, clock=Clock(T0))
    co.start()
    co.poll_once()
    return co


def test_the_engine_writes_the_candles_beside_the_status_file(engine, pair,
                                                             tmp_path):
    answer = engine.chart_history(pair.key, 15)
    assert answer['ok'], answer
    # The command answers that it is written - not the history itself.
    assert 'candles' in answer and isinstance(answer['candles'], int)
    body = json.loads((tmp_path / 'chart_history.json')
                      .read_text(encoding='utf-8'))[pair.key + '#15']
    assert len(body['candles']) == answer['candles'] >= 40
    assert body['length'] == 20 and body['entry_z'] == 2.5
    assert body['algo_timeframe_min'] == 15
    candle = body['candles'][0]
    assert candle['l'] <= min(candle['o'], candle['c'])
    assert candle['h'] >= max(candle['o'], candle['c'])


def test_each_timeframe_reads_the_finer_bars_it_is_built_from(engine, pair,
                                                              legs):
    asked = legs['acct_a'].broker.rates_asked
    for tf, sub in ((15, 1), (60, 5), (240, 15)):
        assert engine.chart_history(pair.key, tf)['ok']
        assert asked[-1][1] == sub


def test_a_history_is_reused_not_refetched_every_time(engine, pair, legs):
    asked = legs['acct_a'].broker.rates_asked
    engine.chart_history(pair.key, 15)
    before = len(asked)
    engine.chart_history(pair.key, 15)
    assert len(asked) == before
    # Control: five minutes on, it is fetched again.
    engine.clock.now += 301
    engine.chart_history(pair.key, 15)
    assert len(asked) == before + 1


def test_a_chart_that_cannot_be_drawn_says_why(engine, pair, legs):
    assert 'no 5-minute chart' in engine.chart_history(pair.key, 5)['reason']
    legs['acct_b'].broker.bars['GC1226'] = []
    engine.clock.now += 301
    answer = engine.chart_history(pair.key, 60)
    assert not answer['ok'] and 'leg B' in answer['reason']


def test_the_screen_asks_for_it_through_the_command_log(engine, pair):
    from okxtrader.commands import CommandRunner
    assert hasattr(CommandRunner, '_do_chart_history')


def test_the_route_serves_a_written_history_and_says_when_there_is_none(
        client, tmp_path):
    path = chart.history_path(str(tmp_path / 'status.json'))
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'A|B#60': {'ok': True, 'candles': [{'t': 0, 'o': 1,
                                                      'h': 2, 'l': 0,
                                                      'c': 1}]}}, f)
    body = client.get('/api/chart_history/A|B?tf=60').get_json()
    assert body['ok'] and len(body['candles']) == 1
    assert client.get('/api/chart_history/A|B?tf=240').get_json() == \
        {'ok': False, 'missing': True}


def test_a_candle_opens_at_its_start_when_the_bars_carry_opens():
    """OKX's bars carry an OPEN. A 15-minute candle from three 5-minute
    bars used to open at the first one's CLOSE - five minutes late, at
    the bottom of a drop that happened inside it - and to miss every
    open from its high and low. Both legs' opens are the same instant,
    so the spread at each bar's start is a real price."""
    a = [{'time': 300 * i, 'open': 100.0, 'close': 100.0} for i in range(3)]
    b = [{'time': 0, 'open': 104.0, 'close': 92.0},
         {'time': 300, 'open': 92.0, 'close': 101.0},
         {'time': 600, 'open': 101.0, 'close': 103.0}]
    [candle] = bands.spread_ohlc(a, b, 1.0, 300, 900, 0, 0)
    assert candle['o'] == pytest.approx(4.0)        # not -8, the first close
    assert candle['h'] == pytest.approx(4.0)
    assert candle['l'] == pytest.approx(-8.0)
    assert candle['c'] == pytest.approx(3.0)


def test_a_history_that_could_not_be_written_is_not_kept_or_called_done(
        config, pair, legs, tmp_path, monkeypatch, caplog):
    """The screen reads the history from a file. When the write failed
    the engine still held the candles and answered from them for five
    minutes, 'done', while the screen drew the OLD file: a chart frozen
    hours back with nothing said. Now it says why, logs it, and the next
    ask reads OKX again."""
    from okxtrader import atomicfile
    from okxtrader.coordinator import Coordinator
    legs['acct_a'].broker.bars[pair.symbol_a] = [
        {'time': 900 * i, 'close': 4300.0 + i} for i in range(200)]
    legs['acct_b'].broker.bars[pair.symbol_b] = [
        {'time': 900 * i, 'close': 4360.0 + i} for i in range(200)]
    coordinator = Coordinator(config, legs,
                              status_path=str(tmp_path / 'status.json'))
    monkeypatch.setattr(atomicfile, 'replace', lambda tmp, path: False)
    answer = coordinator.chart_history(pair.key, 15)
    assert answer['ok'] is False
    assert 'could not be written' in answer['reason']
    assert 'no fresh history' in caplog.text
    assert not coordinator._chart_history                # not kept
    assert not [p for p in tmp_path.iterdir() if p.name.endswith('.tmp')]
    monkeypatch.undo()
    assert coordinator.chart_history(pair.key, 15)['ok'] is True


def test_a_candle_carries_its_legs_volume_at_the_prices_it_traded():
    """VOLUME PROFILE: a spread has no volume of its own, so each candle
    carries both legs' traded value (USDT) - in total, and per finer bar
    at that bar's spread, so the profile puts it where it traded."""
    a = [{'time': 300 * i, 'open': 100.0, 'close': 100.0,
          'volume': 1000.0} for i in range(3)]
    b = [{'time': 0, 'open': 104.0, 'close': 92.0, 'volume': 500.0},
         {'time': 300, 'open': 92.0, 'close': 101.0, 'volume': 700.0},
         {'time': 600, 'open': 101.0, 'close': 103.0, 'volume': 300.0}]
    [candle] = bands.spread_ohlc(a, b, 1.0, 300, 900, 0, 0)
    assert candle['v'] == pytest.approx(4500.0)
    assert candle['f'] == [[pytest.approx(-8.0), 1500.0],
                           [pytest.approx(1.0), 1700.0],
                           [pytest.approx(3.0), 1300.0]]


def test_bars_without_volume_give_candles_without_it():
    a = [bar(60 * i, 100.0) for i in range(3)]
    b = [bar(60 * i, 105.0) for i in range(3)]
    [candle] = bands.spread_ohlc(a, b, 1.0, 60, 900, 0, 0)
    assert 'v' not in candle and 'f' not in candle
