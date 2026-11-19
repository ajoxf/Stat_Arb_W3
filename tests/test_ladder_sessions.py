"""Each ladder's own trading hours, on the broker's clock.

An oil future and a gold CFD do not keep the same day. A ladder can
carry a session open, its own close (its cutoff) and a daily break;
blank is the desk-wide cutoff, exactly as before. Each guard here has a
control that turns it off and shows the opposite.
"""

import json
from datetime import datetime

import pytest

from okxtrader import algo, config as cfg
from okxtrader.coordinator import Coordinator
from okxtrader.models import OvernightMode, SpreadSide
from okxtrader.session import PairSession, clean_hhmm, parse_hhmm

from test_algo_signal import STATS, feed, touch_signal


DESK = {'OVERNIGHT_CLOSE_HOUR': 16, 'OVERNIGHT_CLOSE_MINUTE': 55}


class Hours:
    def __init__(self, **kw):
        self.session_open = kw.get('open')
        self.session_close = kw.get('close')
        self.break_start = kw.get('brk', (None, None))[0]
        self.break_end = kw.get('brk', (None, None))[1]


def at(hh, mm):
    return datetime(2026, 10, 5, hh, mm)


# -- the times -----------------------------------------------------------------


def test_times_are_hhmm_on_the_broker_clock():
    assert parse_hhmm('1:05') == (1, 5) and clean_hhmm('1:05') == '01:05'
    assert parse_hhmm('') is None and clean_hhmm(None) is None
    for bad in ('25:00', '12:60', 'noon', '1205'):
        with pytest.raises(ValueError):
            parse_hhmm(bad)
        assert clean_hhmm(bad) is None       # a broken file still loads


# -- the session -------------------------------------------------------------------


def test_with_no_hours_set_it_is_the_desk_cutoff_as_before():
    hours = PairSession(Hours(), DESK)
    assert hours.cutoff == (16, 55)
    assert hours.state(at(10, 0)) == 'OPEN'
    assert hours.state(at(17, 0)) == 'CLOSED'
    # The desk cutoff alone says nothing about whether the market still
    # trades after it: a silent feed then is still worth a warning.
    assert hours.quiet(at(17, 0)) is False
    # ...and past it the Algo's cutoff buffer holds entries, as before.
    assert hours.entry_block(at(17, 0)) is None
    assert hours.minutes_to_close(at(17, 0)) < 0


def test_its_own_open_and_close():
    hours = PairSession(Hours(open='01:05', close='23:45'), DESK)
    assert hours.cutoff == (23, 45)
    assert hours.state(at(0, 30)) == 'CLOSED'
    assert hours.quiet(at(0, 30)) is True
    assert 'opens 01:05' in hours.entry_block(at(0, 30))
    # The CONTROL: inside the session nothing is held.
    assert hours.state(at(1, 10)) == 'OPEN'
    assert hours.entry_block(at(1, 10)) is None
    assert hours.minutes_to_close(at(23, 50)) > 0       # the NEXT close


def test_a_session_over_midnight():
    hours = PairSession(Hours(open='18:00', close='17:00'), DESK)
    assert hours.state(at(17, 30)) == 'CLOSED'
    assert hours.state(at(19, 0)) == 'OPEN'
    assert hours.state(at(10, 0)) == 'OPEN'


def test_the_daily_break():
    hours = PairSession(Hours(brk=('23:55', '01:05')), DESK)
    assert hours.state(at(0, 30)) == 'BREAK'
    assert hours.quiet(at(0, 30)) is True
    assert 'break until 01:05' in hours.entry_block(at(0, 30))
    # The CONTROL: outside it, nothing is held.
    assert hours.entry_block(at(2, 0)) is None


def test_a_half_break_is_no_break():
    hours = PairSession(Hours(brk=('09:55', None)), DESK)
    assert hours.brk is None and hours.state(at(9, 58)) == 'OPEN'


# -- the Algo -----------------------------------------------------------------------


def test_the_algo_does_not_enter_outside_its_session():
    signal = touch_signal()
    body = feed(signal, 10.26, 10.30, 3,
                gates={'session': 'session: closed - opens 01:05 broker '
                                  'time'})
    assert body['intents'] == []
    assert 'opens 01:05' in body['blocked']


def test_the_CONTROL_in_session_it_enters():
    body = feed(touch_signal(), 10.26, 10.30, 3, gates={'session': None})
    assert body['intents'][0]['action'] == 'ENTER'


def test_an_exit_is_never_held_by_the_session():
    signal = touch_signal()
    held = {'position_id': 'p1', 'side': 'BUY', 'entry_spread': 9.80,
            'tp': 10.0, 'sl': 9.5, 'break_even': 9.80, 'quantity': 1.0}
    body = signal.evaluate(0.0, {'short_spread': 10.05, 'long_spread': 10.07,
                                 'mid_spread': 10.06, 'quote_id': 1},
                           STATS, [held],
                           {'session': 'session: daily break until 01:05'})
    assert body['intents'][0]['action'] == 'EXIT'


# -- the engine ---------------------------------------------------------------------


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def broker_at(coordinator, local, ahead_sec=0):
    coordinator.session_clock.now = lambda: local
    coordinator.session_clock.offset = lambda: ahead_sec


def test_the_engine_holds_entries_on_the_ladders_own_hours(engine, pair):
    pair.session_open, pair.session_close = '01:05', '23:45'
    broker_at(engine, at(0, 30))
    gates = engine._algo_gates(pair, engine.market[pair.key])
    assert 'opens 01:05' in gates['session']
    broker_at(engine, at(10, 0))                        # the control
    assert engine._algo_gates(pair, engine.market[pair.key])['session'] \
        is None


def test_the_cutoff_is_the_ladders_own_close(engine, pair):
    pair.overnight = OvernightMode.EXIT_ALWAYS
    result = engine.executor.market_entry(pair, SpreadSide.SELL,
                                          engine.market[pair.key], 1.0)
    engine.book.add_position(result.position)
    pair.session_close = '23:00'
    broker_at(engine, at(17, 0))
    assert engine.run_session_cutoff() == []            # not ITS cutoff yet
    pair.session_close = None                           # the control: desk
    events = engine.run_session_cutoff()
    assert events and events[0]['action'] == 'overnight_close'


def test_the_overnight_rule_reads_the_brokers_clock_not_this_machines(
        engine, pair, legs):
    """The cutoff fired on the broker's clock, then the overnight rule
    asked THIS machine's - on a box three hours behind, it was never
    past the cutoff, and EXIT_ALWAYS closed nothing, every day."""
    pair.overnight = OvernightMode.EXIT_ALWAYS
    result = engine.executor.market_entry(pair, SpreadSide.SELL,
                                          engine.market[pair.key], 1.0)
    engine.book.add_position(result.position)
    broker_at(engine, at(13, 56), ahead_sec=3 * 3600)   # broker 16:56
    events = engine.run_session_cutoff()
    assert events and events[0]['action'] == 'overnight_close'
    assert legs['acct_a'].broker.open_positions() == []


def test_the_CONTROL_allow_keeps_it(engine, pair, legs):
    pair.overnight = OvernightMode.ALLOW
    result = engine.executor.market_entry(pair, SpreadSide.SELL,
                                          engine.market[pair.key], 1.0)
    engine.book.add_position(result.position)
    broker_at(engine, at(13, 56), ahead_sec=3 * 3600)
    engine.run_session_cutoff()
    assert legs['acct_a'].broker.open_positions()


def test_the_snapshot_carries_each_ladders_hours(engine, pair):
    pair.session_open, pair.session_close = '01:05', '23:45'
    pair.break_start, pair.break_end = '12:00', '12:30'
    broker_at(engine, at(12, 10))
    session = engine.snapshot()['pairs'][pair.key]['session']
    assert session == {'open': '01:05', 'close': '23:45',
                       'break': '12:00-12:30', 'own_hours': True,
                       'state': 'BREAK', 'quiet': True}


def test_the_hours_are_saved_and_read_back(pair):
    pair.session_open = '1:05'
    raw = cfg.PairConfig.from_dict(pair.key, dict(
        pair.to_dict(), session_open='1:05', session_close='23:45')).to_dict()
    assert raw['session_open'] == '01:05' and raw['session_close'] == '23:45'


# -- the save -------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    from okxtrader.webapp import create_app
    paths = {k: str(tmp_path / k) for k in ('status.json', 'commands.jsonl',
                                             'results.json', 'config.json')}
    cfg.save_raw(paths['config.json'], {'accounts': {'a': {}},
                                         'pairs': {'A|B': {'name': 'A'}}})
    app = create_app(paths['status.json'], paths['commands.jsonl'],
                     paths['results.json'], paths['config.json'])
    app.config.update(TESTING=True)
    return app.test_client(), paths['config.json']


def test_a_time_that_will_not_parse_is_refused_and_says_why(client):
    http, path = client
    answer = http.post('/api/pairs/A|B', json={'session_close': '25:00'})
    assert answer.status_code == 400
    assert 'Session close' in answer.get_json()['error']
    assert 'session_close' not in json.load(open(path))['pairs']['A|B']


def test_a_good_time_is_saved_tidy(client):
    http, path = client
    assert http.post('/api/pairs/A|B', json={'session_open': '1:05'}) \
        .status_code == 200
    assert json.load(open(path))['pairs']['A|B']['session_open'] == '01:05'


def test_a_break_needs_both_ends(client):
    http, _ = client
    answer = http.post('/api/pairs/A|B', json={'break_start': '23:55'})
    assert answer.status_code == 400 and 'both' in answer.get_json()['error']
    assert http.post('/api/pairs/A|B', json={'break_start': '23:55',
                                             'break_end': '01:05'}) \
        .status_code == 200
