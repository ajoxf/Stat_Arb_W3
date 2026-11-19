"""The Algo in the running engine: the switch, the history, the record.

What these hold the engine to:

- the Algo is OFF for every ladder at start, and a ladder whose Algo is
  off computes nothing;
- turned on, its band comes from MT5's own bars at once — and when the
  terminals will not give them, it collects from the live price and
  SAYS so;
- what it decides is recorded, and NOTHING reaches a broker: no order,
  and no difference at all to a click;
- a restart brings it back OFF, with its candles kept.
"""

import json

import pytest

from okxtrader import algo
from okxtrader.commands import CommandRunner
from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import OrderType, SpreadPosition, SpreadSide

T0 = 1_800_000_450.0                     # half way into a 15-minute candle
OFFSET = 3 * 3600                        # both fake brokers run at UTC+3


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


#: Settings that keep these tests about the Algo and not about the hour
#: they happen to run at: no cutoff buffer (the session clock is real),
#: one tick to confirm, no cap.
QUIET = {'cutoff_buffer_min': 0, 'confirm_ticks': 1, 'max_entry_z': 0,
         # The entry filters are tested on their own (test_algo_filters);
         # these tests are about what happens once an entry is allowed.
         'edge_on': False, 'regime_on': False, 'prob_on': False,
         # The live warm-up is tested on its own (test_algo_warmup).
         'warmup_min': 0,
         # Re-entry and the trend filter: test_algo_protections.
         'reentry_on': False, 'trend_on': False}


def give_history(legs, count=40):
    """40 closed 15-minute bars on both legs, the spread swinging 59.0 /
    59.2 around a 59.10 mid — stamped on the BROKER's clock."""
    start = T0 - (T0 % 900) - 900 * count
    bars_a, bars_b = [], []
    for i in range(count):
        stamp = start + 900 * i + OFFSET
        bars_a.append({'time': stamp, 'close': 4292.10, 'spread': 0,
                       'point': 0.01})
        bars_b.append({'time': stamp,
                       'close': 4292.10 + (59.0 if i % 2 else 59.2),
                       'spread': 0, 'point': 0.01})
    legs['acct_a'].broker.bars['XAUUSD_'] = bars_a
    legs['acct_b'].broker.bars['GC1226'] = bars_b


def engine(config, legs, store=None, clock=None):
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=clock or Clock(), store=store)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def block(coordinator, pair):
    return coordinator.snapshot()['pairs'][pair.key]['algo_block']


def nothing_sent(legs):
    return legs['acct_a'].broker.sent == [] and \
        legs['acct_b'].broker.sent == []


def test_the_algo_is_OFF_at_start_and_computes_nothing(config, pair, legs):
    give_history(legs)
    coordinator = engine(config, legs)
    for _ in range(3):
        coordinator.poll_once()
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['algo_on'] is False and row['algo'] == 'NONE'
    assert row['algo_block'] == {'algo': 'NONE', 'window': False}
    # The control: switched on, it reads.
    assert coordinator.set_algo(pair.key, 'ALGO')['ok']
    coordinator.poll_once()
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['algo_on'] is True and row['algo_block']['algo'] == 'ALGO'


def test_turning_it_on_loads_MT5_history_so_the_band_is_there_at_once(
        config, pair, legs):
    give_history(legs)
    coordinator = engine(config, legs)
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    body = block(coordinator, pair)
    assert body['ready'] is True
    assert body['history']['state'] == 'done'
    assert body['mean'] == pytest.approx(59.1, abs=0.05)
    assert body['sigma'] == pytest.approx(0.1, abs=0.02)
    assert body['upper'] > body['mean'] > body['lower']


def test_no_history_from_the_terminals_means_collecting_and_saying_so(
        config, pair, legs):
    coordinator = engine(config, legs)                 # no bars at all
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    body = block(coordinator, pair)
    assert body['ready'] is False and body['z_sell'] is None
    assert body['history']['state'] == 'collecting'
    assert 'no history' in body['history']['note']
    assert body['state'] == 'BLOCKED' and '/20' in body['blocked']


def test_an_unmeasured_broker_clock_is_not_a_guess(config, pair, legs):
    """A bar whose hour cannot be placed is not matched to the other
    leg's — the Algo collects live instead, and says why."""
    give_history(legs)
    legs['acct_b'].broker.server_offset_sec = None
    coordinator = engine(config, legs)
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    body = block(coordinator, pair)
    assert body['ready'] is False
    assert 'clock' in body['history']['note']


def test_a_signal_is_RECORDED_and_nothing_is_sent(config, pair, legs,
                                                  tmp_path):
    give_history(legs)
    pair.algo_params = dict(QUIET)
    store = Store(str(tmp_path / 'trader.db'))
    coordinator = engine(config, legs, store=store)
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    assert block(coordinator, pair)['signal'] is None      # at the mean

    # The future's bid lifts: the bid-side spread is far over the band.
    legs['acct_b'].broker.quote('GC1226', 4352.00, 4352.10)
    coordinator.poll_once()
    body = block(coordinator, pair)
    assert body['state'] == 'SIGNAL' and body['signal'] == 'SELL'
    assert body['mode'] == 'DRY_RUN'

    [event] = store.events('algo_signal')
    assert event['pair_key'] == pair.key
    assert event['detail']['action'] == 'ENTER'
    assert event['detail']['side'] == 'SELL'
    assert event['detail']['mode'] == 'DRY_RUN'
    assert body['recent'][0]['action'] == 'ENTER'
    # The whole promise: a signal, and not one request at either broker.
    assert nothing_sent(legs)
    assert coordinator.book.positions(pair.key) == []


def test_an_exit_is_signalled_on_the_traders_REAL_position(config, pair,
                                                           legs, tmp_path):
    """The exit is measured from the position's own fill: break-even
    after costs plus the target. Here the target is 0 and commission is
    0, so break-even is the price paid."""
    give_history(legs)
    pair.algo_params = dict(QUIET)
    pair.tp_target_pct_of_margin = 0.0
    store = Store(str(tmp_path / 'trader.db'))
    coordinator = engine(config, legs, store=store)
    pair.order_type = OrderType.MARKET
    md = coordinator.market[pair.key]
    assert coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread']).get('ok')
    [held] = coordinator.book.positions(pair.key)
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    body = block(coordinator, pair)
    assert body['state'] == 'IN_POSITION'
    assert body['positions'][0]['position_id'] == held.position_id

    sent_before = (len(legs['acct_a'].broker.sent),
                   len(legs['acct_b'].broker.sent))
    # A long leaves on the BID: lift it past what was paid.
    legs['acct_b'].broker.quote('GC1226', 4352.00, 4352.40)
    coordinator.poll_once()
    body = block(coordinator, pair)
    assert body['state'] == 'EXIT'
    assert body['positions'][0]['exit'] == 'PROFIT_TARGET'
    [event] = [e for e in store.events('algo_signal')
               if e['detail']['action'] == 'EXIT']
    assert event['detail']['position_id'] == held.position_id
    # Signalled, not done: the position is still on, nothing was sent.
    assert held.is_open
    assert (len(legs['acct_a'].broker.sent),
            len(legs['acct_b'].broker.sent)) == sent_before


def test_a_restart_comes_back_OFF_with_its_candles_kept(config, pair, legs,
                                                         tmp_path):
    give_history(legs)
    path = str(tmp_path / 'trader.db')
    first = engine(config, legs, store=Store(path))
    first.set_algo(pair.key, 'ALGO')
    first.poll_once()
    assert block(first, pair)['ready']

    # The terminals have nothing now — the saved candles must carry it.
    legs['acct_a'].broker.bars.clear()
    legs['acct_b'].broker.bars.clear()
    second = engine(config, legs, store=Store(path))
    assert second.algos.is_on(pair.key) is False
    assert second.snapshot()['pairs'][pair.key]['algo_on'] is False
    second.set_algo(pair.key, 'ALGO')
    second.poll_once()
    assert block(second, pair)['ready'] is True


def test_a_click_is_IDENTICAL_with_the_algo_on_and_signalling(config, pair,
                                                              legs):
    """Manual trading stays exactly as it was. The same click with the
    Algo off and then on, shouting SELL — indistinguishable at the
    broker."""
    give_history(legs)
    pair.algo_params = dict(QUIET)
    pair.order_type = OrderType.MARKET

    def click_once(algo_on):
        legs['acct_b'].broker.quote('GC1226', 4352.00, 4352.10)
        coordinator = engine(config, legs)
        if algo_on:
            coordinator.set_algo(pair.key, 'ALGO')
            coordinator.poll_once()
            assert block(coordinator, pair)['signal'] == 'SELL'
        legs['acct_a'].broker.sent.clear()
        legs['acct_b'].broker.sent.clear()
        answer = coordinator.click(pair.key, SpreadSide.BUY,
                                   coordinator.market[pair.key]['long_spread'])
        sent = [dict(e) for e in legs['acct_a'].broker.sent
                + legs['acct_b'].broker.sent]
        for entry in sent:
            entry.pop('comment', None)          # carries a unique id
        return answer.get('ok'), sent

    assert click_once(False) == click_once(True)


def test_LIVE_is_never_implied_it_has_to_be_confirmed(config, pair, legs):
    coordinator = engine(config, legs)
    answer = coordinator.algos.turn_on(pair, mode='LIVE')
    assert answer['ok'] is False and 'confirmed' in answer['reason']
    assert coordinator.algos.is_on(pair.key) is False
    # The control: confirmed, it goes LIVE.
    assert coordinator.algos.turn_on(pair, mode='LIVE', confirmed=True)['ok']
    assert coordinator.algos.mode(pair.key) == 'LIVE'
    # And a dry run never takes the ladder from the trader.
    coordinator.algos.turn_on(pair, mode='DRY_RUN')
    assert coordinator.algos.manual_order_refusal(pair.key) is None


def test_an_unknown_algo_is_refused_not_ignored(config, pair, legs):
    coordinator = engine(config, legs)
    answer = coordinator.set_algo(pair.key, 'MAGIC')
    assert answer['ok'] is False and 'MAGIC' in answer['reason']


def test_algo_through_the_settings_path_no_longer_crashes(config, pair, legs,
                                                          tmp_path):
    """`algo` had no setter: sending it through set_pair raised
    AttributeError half way through a save."""
    coordinator = engine(config, legs)
    runner = CommandRunner(coordinator, str(tmp_path / 'c.jsonl'),
                           str(tmp_path / 'r.json'))
    runner._do_set_pair({'pair': pair.key, 'fields': {'algo': 'ALGO'}})
    assert coordinator.algos.is_on(pair.key)
    runner._do_set_algo({'pair': pair.key, 'algo': 'FAIR_SPREAD'})
    assert not coordinator.algos.is_on(pair.key)
    assert pair.algo_window is True and pair.algo == 'FAIR_SPREAD'
    runner._do_set_pair({'pair': pair.key, 'fields': {'algo': 'NONE'}})
    assert pair.algo_window is False


def test_new_settings_restart_the_band_on_the_new_candle(config, pair, legs):
    give_history(legs)
    coordinator = engine(config, legs)
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    assert block(coordinator, pair)['timeframe_min'] == 15
    assert legs['acct_a'].broker.rates_asked[-1][:2] == ('XAUUSD_', 15)
    pair.algo_params = {'timeframe_min': 5}
    coordinator.poll_once()
    body = block(coordinator, pair)
    assert body['timeframe_min'] == 5
    # A new series: MT5 is asked again, for FIVE-minute bars.
    assert legs['acct_a'].broker.rates_asked[-1][:2] == ('XAUUSD_', 5)
    assert legs['acct_b'].broker.rates_asked[-1][:2] == ('GC1226', 5)




def test_every_position_is_MANUAL_and_says_so_after_a_restart(tmp_path):
    from okxtrader.models import MANUAL
    position = SpreadPosition('P', 'BUY', 1.0, None, None, 1.0, 'MARKET',
                              100.0)
    assert position.source == MANUAL
    store = Store(str(tmp_path / 'trader.db'))
    store.save_position(position)
    [row] = store.open_positions()
    assert SpreadPosition.from_dict(row).source == MANUAL


# -- the web side ---------------------------------------------------------------


@pytest.fixture
def web(tmp_path):
    from okxtrader.webapp import create_app
    paths = {name: str(tmp_path / name) for name in
             ('status.json', 'commands.jsonl', 'results.json', 'config.json',
              'trader.db')}
    with open(paths['config.json'], 'w', encoding='utf-8') as f:
        json.dump({'accounts': {}, 'pairs': {'A|B': {'name': 'A vs B'}}}, f)
    app = create_app(paths['status.json'], paths['commands.jsonl'],
                     paths['results.json'], paths['config.json'],
                     paths['trader.db'])
    app.config.update(TESTING=True)
    return app.test_client(), paths


def test_bad_algo_settings_are_refused_before_anything_is_saved(web):
    client, paths = web
    answer = client.post('/api/pairs/A|B', json={
        'algo_params': {'timeframe_min': 7, 'entry_z': 0}})
    assert answer.status_code == 400
    assert 'timeframe' in answer.get_json()['error']
    with open(paths['config.json'], encoding='utf-8') as f:
        assert 'algo_params' not in json.load(f)['pairs']['A|B']
    # The control: good settings are saved.
    assert client.post('/api/pairs/A|B', json={
        'algo_params': {'entry_z': 3.0, 'timeframe_min': 5}}).status_code == 200
    with open(paths['config.json'], encoding='utf-8') as f:
        saved = json.load(f)['pairs']['A|B']
    assert saved['algo_params'] == {'entry_z': 3.0, 'timeframe_min': 5}


def test_the_algo_switch_is_never_written_to_the_config(web):
    client, paths = web
    client.post('/api/pairs/A|B', json={'algo': 'ALGO', 'algo_window': True})
    with open(paths['config.json'], encoding='utf-8') as f:
        saved = json.load(f)['pairs']['A|B']
    assert saved.get('algo') != 'ALGO'
    assert saved['algo_window'] is True


def test_the_signals_export_as_csv_with_blanks_not_zeros(web):
    client, paths = web
    store = Store(paths['trader.db'])
    store.event('algo_signal', 'A|B', action='ENTER', side='SELL', z=2.61,
                spread=59.5, mode='DRY_RUN', net_pnl=None)
    answer = client.get('/api/algo_signals.csv')
    assert answer.status_code == 200
    lines = answer.get_data(as_text=True).strip().splitlines()
    assert lines[0].startswith('at,pair_key,mode,action,side')
    header = lines[0].split(',')
    row = dict(zip(header, lines[1].split(',')))
    assert row['action'] == 'ENTER' and row['side'] == 'SELL'
    assert row['z'] == '2.61' and row['net_pnl'] == ''
    assert row['mode'] == 'DRY_RUN'


def test_the_defaults_reach_the_screen(config, pair, legs):
    coordinator = engine(config, legs)
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['algo_params'] == algo.DEFAULT_PARAMS


def test_a_new_threshold_keeps_the_band(config, pair, legs):
    """Entry z is how the band is READ, not what it is: changing it must
    not throw the candles away and ask MT5 again."""
    give_history(legs)
    coordinator = engine(config, legs)
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    asked = len(legs['acct_a'].broker.rates_asked)
    before = block(coordinator, pair)
    pair.algo_params = {'entry_z': 1.5}
    coordinator.poll_once()
    after = block(coordinator, pair)
    assert after['params']['entry_z'] == 1.5
    assert after['ready'] and after['mean'] == pytest.approx(before['mean'])
    assert len(legs['acct_a'].broker.rates_asked) == asked
    assert after['upper'] - after['mean'] == pytest.approx(1.5 * after['sigma'])


def test_a_real_position_gets_a_stop_loss_from_its_margin(config, pair, legs):
    """The stop is break-even less 2% of the margin the position ties
    up, through the same k as the take-profit. The control: the stop
    turned off is None, not a stop at break-even."""
    give_history(legs)
    coordinator = engine(config, legs)
    pair.order_type = OrderType.MARKET
    md = coordinator.market[pair.key]
    assert coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread']).get('ok')
    [held] = coordinator.book.positions(pair.key)
    margin = coordinator.margin_detail(pair)['money']
    assert margin
    [row] = coordinator._algo_positions(pair, coordinator.market[pair.key])
    expected = 0.02 * margin * held.quantity / (held.spread_units
                                                * held.quantity)
    assert row['sl'] == pytest.approx(row['break_even'] - expected)

    pair.algo_params = {'stop_loss_on': False}
    [row] = coordinator._algo_positions(pair, coordinator.market[pair.key])
    assert row['sl'] is None


def test_a_new_entrys_stop_is_shown_beside_its_target(config, pair, legs):
    give_history(legs)
    coordinator = engine(config, legs)
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    row = coordinator.snapshot()['pairs'][pair.key]
    body = row['algo_block']
    assert body['sl_buy'] < row['exit']['break_even_buy']
    assert body['sl_sell'] > row['exit']['break_even_sell']
