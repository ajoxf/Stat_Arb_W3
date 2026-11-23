"""The Algo's live warm-up: ready is the band AND a feed it has watched.

MT5's history fills the band the second the Algo is turned on — and a
band from history is not this session's tape. So no entry is taken until
the Algo has watched `warmup_min` minutes of LIVE prices, and time with
no price does not count toward it.

Every test that shows the warm-up withholding an entry has a CONTROL
beside it, the warm-up done (or off), showing the same stretch entered.
"""

from okxtrader.coordinator import Coordinator

from test_algo_desk import QUIET, Clock, give_history

#: Far enough out that it is still past 2.5 sigma once the live prices
#: watched during the warm-up are in the forming candle.
STRETCH = (4352.20, 4352.30)


def engine(config, legs, params, mode='LIVE'):
    pair = next(iter(config.pairs.values()))
    pair.algo_params = dict(QUIET, **params)
    clock = Clock()
    coordinator = Coordinator(config, legs, sleep=lambda s: None, clock=clock)
    coordinator.start()
    coordinator.poll_once()
    answer = coordinator.set_algo(pair.key, 'ALGO', mode=mode,
                                  confirmed=True)
    assert answer['ok'], answer
    coordinator.poll_once()
    return coordinator, pair, clock


def tick(coordinator, legs, clock, seconds):
    """`seconds` of live prices, one fresh quote on BOTH legs every 5
    seconds — a leg left unquoted goes stale, and stale is no price."""
    for _ in range(int(seconds // 5)):
        clock.now += 5
        for name, symbol in (('acct_a', 'XAUUSD_'), ('acct_b', 'GC1226')):
            book = legs[name].broker.symbols[symbol]
            legs[name].broker.quote(symbol, book.bid, book.ask)
        coordinator.poll_once()


def stretch(coordinator, legs, pair):
    legs['acct_b'].broker.quote('GC1226', *STRETCH)
    coordinator.poll_once()
    return coordinator.snapshot()['pairs'][pair.key]['algo_block']


def test_the_default_is_a_90_minute_warm_up():
    from okxtrader import algo
    assert algo.DEFAULT_PARAMS['warmup_min'] == 90


def test_a_band_from_history_is_not_ready_until_the_warm_up(config, pair,
                                                            legs):
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 90})
    body = stretch(coordinator, legs, pair)
    # The band itself is full, from MT5 — and still no entry.
    assert body['ready'] is True
    assert body['filters']['ready'] is False
    assert body['warmup']['done'] is False
    assert body['warmup']['need_sec'] == 90 * 60
    assert coordinator.book.positions(pair.key) == []
    assert 'warming up' in body['blocked']


def test_the_CONTROL_with_the_warm_up_off_it_enters_at_once(config, pair,
                                                            legs):
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 0})
    body = stretch(coordinator, legs, pair)
    assert body['filters']['ready'] is True
    assert len(coordinator.book.positions(pair.key)) == 1


def test_once_warmed_up_the_same_stretch_is_entered(config, pair, legs):
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 1})
    tick(coordinator, legs, clock, 30)
    body = stretch(coordinator, legs, pair)
    assert body['warmup']['done'] is False
    assert coordinator.book.positions(pair.key) == []
    # The control: the rest of the minute watched, and it enters.
    tick(coordinator, legs, clock, 40)
    stretch(coordinator, legs, pair)
    assert len(coordinator.book.positions(pair.key)) == 1


def test_time_with_no_price_does_not_count(config, pair, legs):
    """A feed that went quiet for ten minutes has not been watched for
    ten minutes."""
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 1})
    clock.now += 600                  # ten minutes, and no new quote
    coordinator.poll_once()
    body = stretch(coordinator, legs, pair)
    assert body['warmup']['sec'] < 60
    assert coordinator.book.positions(pair.key) == []


def test_a_dry_run_warm_up_carries_into_LIVE(config, pair, legs):
    """Warm up in a dry run, then go LIVE without waiting again."""
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 1},
                                      mode='DRY_RUN')
    tick(coordinator, legs, clock, 70)
    answer = coordinator.set_algo(pair.key, 'ALGO', mode='LIVE',
                                  confirmed=True)
    assert answer['ok'], answer
    stretch(coordinator, legs, pair)
    assert len(coordinator.book.positions(pair.key)) == 1


def test_turning_it_off_starts_the_warm_up_again(config, pair, legs):
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 1})
    tick(coordinator, legs, clock, 70)
    coordinator.set_algo(pair.key, 'NONE')
    coordinator.set_algo(pair.key, 'ALGO', mode='LIVE', confirmed=True)
    body = stretch(coordinator, legs, pair)
    assert body['warmup']['done'] is False
    assert coordinator.book.positions(pair.key) == []


def test_a_new_timeframe_keeps_the_ladder_LIVE(config, pair, legs):
    """A new candle series is the same Algo. Rebuilding it used to drop a
    LIVE ladder to a dry run, which then entered nothing."""
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 0})
    pair.algo_params = dict(pair.algo_params, length=30)
    coordinator.poll_once()
    assert coordinator.snapshot()['pairs'][pair.key]['algo_mode'] == 'LIVE'
    stretch(coordinator, legs, pair)
    assert len(coordinator.book.positions(pair.key)) == 1


def test_the_live_price_builds_candles(config, pair, legs):
    """No MT5 history at all: the candles come from the live mid, one per
    15 minutes. The mid was published as `spread` and read as
    `mid_spread`, so this never happened — a band from history went
    stale all session, and one without history collected forever."""
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 0})
    before = coordinator.snapshot()['pairs'][pair.key]['algo_block']['count']
    for _ in range(3):
        clock.now += 900                    # into the next candle
        tick(coordinator, legs, clock, 5)
    after = coordinator.snapshot()['pairs'][pair.key]['algo_block']['count']
    assert after == before + 3


def test_the_market_carries_the_mid_by_the_name_the_algo_reads(config, pair,
                                                                legs):
    coordinator, pair, clock = engine(config, legs, {})
    md = coordinator.market[pair.key]
    assert md['mid_spread'] == md['spread']
    assert md['short_spread'] <= md['mid_spread'] <= md['long_spread']


# -- across a restart ----------------------------------------------------------


def restart_engine(config, legs, store, clock):
    """A fresh engine on the same journal: what a restart is."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=clock, store=store)
    coordinator.start()
    coordinator.poll_once()
    answer = coordinator.set_algo(pair_of(config).key, 'ALGO', mode='LIVE',
                                  confirmed=True)
    assert answer['ok'], answer
    coordinator.poll_once()
    return coordinator


def pair_of(config):
    return next(iter(config.pairs.values()))


def warmed(config, legs, tmp_path, minutes=2):
    from okxtrader.database import Store
    give_history(legs)
    pair = pair_of(config)
    pair.algo_params = dict(QUIET, warmup_min=minutes)
    store = Store(str(tmp_path / 'trader.db'))
    clock = Clock()
    first = restart_engine(config, legs, store, clock)
    tick(first, legs, clock, 70)
    sec = first.snapshot()['pairs'][pair.key]['algo_block']['warmup']['sec']
    assert sec >= 60
    return store, clock, sec


def test_a_quick_restart_carries_the_warm_up_on(config, pair, legs,
                                                 tmp_path):
    store, clock, sec = warmed(config, legs, tmp_path)
    clock.now += 120                     # back two minutes later
    second = restart_engine(config, legs, store, clock)
    warmup = second.snapshot()['pairs'][pair.key]['algo_block']['warmup']
    # Within one save interval of where it was — and the gap not counted.
    assert sec - 10 <= warmup['sec'] <= sec + 5


def test_an_update_with_its_tests_still_carries_it(config, pair, legs,
                                                   tmp_path):
    """git pull, the test suite and a restart take longer than five
    minutes on the desk PC; fourteen still carries."""
    store, clock, sec = warmed(config, legs, tmp_path)
    clock.now += 14 * 60
    second = restart_engine(config, legs, store, clock)
    warmup = second.snapshot()['pairs'][pair.key]['algo_block']['warmup']
    assert sec - 10 <= warmup['sec'] <= sec + 5


def test_the_CONTROL_a_long_gap_starts_it_again(config, pair, legs,
                                                tmp_path):
    store, clock, sec = warmed(config, legs, tmp_path)
    clock.now += 20 * 60                 # twenty minutes later
    second = restart_engine(config, legs, store, clock)
    warmup = second.snapshot()['pairs'][pair.key]['algo_block']['warmup']
    assert warmup['sec'] < 10


def test_switching_it_off_starts_it_again(config, pair, legs, tmp_path):
    store, clock, sec = warmed(config, legs, tmp_path)
    assert store.warmup(pair.key) is not None
    second = restart_engine(config, legs, store, clock)
    second.set_algo(pair.key, 'NONE')
    assert store.warmup(pair.key) is None


# -- the ARMING carries over with it ----------------------------------------

def _armed_restart(config, legs, store, clock):
    """A restart, read the moment the Algo is back on - before a price
    has had a chance to arm or disarm anything."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=clock, store=store)
    coordinator.start()
    coordinator.poll_once()
    assert coordinator.set_algo(pair_of(config).key, 'ALGO', mode='LIVE',
                                confirmed=True)['ok']
    return coordinator.algos._runs[pair_of(config).key].signal._armed


def test_an_armed_side_is_still_armed_after_a_quick_restart(config, pair,
                                                            legs, tmp_path):
    """ETH vs BTC, 23 Nov: H to L armed at z +2.0, the desk restarted for
    an update, z back to +1.6 - and nothing, because the arming was in
    memory only. It is saved the moment it changes, and comes back with
    the warm-up."""
    store, clock, _sec = warmed(config, legs, tmp_path)
    pair.algo_params['reentry_on'] = True
    first = restart_engine(config, legs, store, clock)
    first.algos._runs[pair.key].signal._armed = {'SELL': True, 'BUY': False}
    tick(first, legs, clock, 5)
    assert store.warmup_state(pair.key)['armed'] == {'SELL': True,
                                                      'BUY': False}
    clock.now += 120
    assert _armed_restart(config, legs, store, clock) == {'SELL': True,
                                                         'BUY': False}


def test_the_CONTROL_after_a_long_gap_the_arming_is_gone(config, pair, legs,
                                                         tmp_path):
    store, clock, _sec = warmed(config, legs, tmp_path)
    first = restart_engine(config, legs, store, clock)
    first.algos._runs[pair.key].signal._armed = {'SELL': True, 'BUY': False}
    tick(first, legs, clock, 5)
    clock.now += 20 * 60
    assert _armed_restart(config, legs, store, clock) == {'SELL': False,
                                                         'BUY': False}


def test_a_warm_up_saved_before_arming_was_kept_reads_as_unarmed(tmp_path):
    from okxtrader.database import Store
    store = Store(str(tmp_path / 'trader.db'))
    store.save_warmup('K', 300.0, 1000.0)
    assert store.warmup_state('K')['armed'] == {'SELL': False, 'BUY': False}
    assert store.warmup('K') == (300.0, 1000.0)
