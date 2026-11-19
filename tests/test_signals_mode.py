"""SIGNALS mode: the Algo signals, the trader trades.

What the trader is told - an entry the Algo would take, or THEIR
position reaching its exit - carries an id so the desk and Telegram
each say it once. Nothing is sent. In LIVE the Algo trades it itself,
so there is nothing to tell.
"""

from okxtrader import algodesk
from okxtrader.database import Store
from okxtrader.models import OrderType, SpreadSide

from tests.test_algo_desk import QUIET, block, engine, give_history, \
    nothing_sent

PARAMS = {'algo_qty': 0.5}


def test_an_entry_says_the_trade_it_would_take():
    alert = algodesk.signal_alert(
        {'action': 'ENTER', 'side': 'SELL', 'spread': 13.805, 'z': 1.523},
        [], PARAMS, 100.0)
    assert alert['kind'] == 'ENTRY' and alert['side'] == 'SELL'
    assert alert['text'] == 'H to L — SELL 0.5 @ 13.8050 (z +1.52)'


def test_an_exit_is_about_the_traders_own_position():
    alert = algodesk.signal_alert(
        {'action': 'EXIT', 'side': 'BUY', 'spread': 12.7271,
         'reason': 'STOP_LOSS', 'position_id': 'P1', 'net_pnl': -3.9},
        [{'position_id': 'P1', 'quantity': 2.0}], PARAMS, 100.0)
    assert alert['kind'] == 'EXIT' and alert['position_id'] == 'P1'
    assert 'close your BUY 2 at 12.7271' in alert['text']
    assert alert['text'].endswith('(net -3.90)')


def test_an_unmeasured_price_is_a_dash_not_zero():
    alert = algodesk.signal_alert({'action': 'ENTER', 'side': 'BUY'}, [],
                                  PARAMS, 100.0)
    assert alert['text'] == 'L to H — BUY 0.5 @ — (z —)'


def test_each_alert_has_its_own_id_even_in_the_same_second():
    first = algodesk.signal_alert({'action': 'ENTER', 'side': 'SELL'}, [],
                                  PARAMS, 100.0)
    second = algodesk.signal_alert({'action': 'ENTER', 'side': 'SELL'}, [],
                                   PARAMS, 100.0, first)
    assert first['id'] != second['id']


def test_signals_mode_tells_the_trader_and_sends_nothing(config, pair, legs,
                                                         tmp_path):
    give_history(legs)
    pair.algo_params = dict(QUIET)
    coordinator = engine(config, legs, store=Store(str(tmp_path / 't.db')))
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    assert block(coordinator, pair)['signal_alert'] is None    # at the mean
    legs['acct_b'].broker.quote('GC1226', 4352.00, 4352.10)
    coordinator.poll_once()
    alert = block(coordinator, pair)['signal_alert']
    assert alert['kind'] == 'ENTRY' and alert['side'] == 'SELL'
    assert alert['text'].startswith('H to L — SELL')
    assert nothing_sent(legs)
    assert coordinator.book.positions(pair.key) == []


def test_the_CONTROL_in_LIVE_the_algo_trades_and_tells_nobody(config, pair,
                                                              legs, tmp_path):
    give_history(legs)
    pair.algo_params = dict(QUIET)
    coordinator = engine(config, legs, store=Store(str(tmp_path / 't.db')))
    assert coordinator.algos.turn_on(pair, mode='LIVE', confirmed=True)['ok']
    coordinator.poll_once()
    legs['acct_b'].broker.quote('GC1226', 4352.00, 4352.10)
    coordinator.poll_once()
    assert block(coordinator, pair)['signal_alert'] is None
    assert not nothing_sent(legs)                 # it traded it itself


def test_your_position_reaching_its_target_is_told(config, pair, legs,
                                                   tmp_path):
    give_history(legs)
    pair.algo_params = dict(QUIET)
    pair.tp_target_pct_of_margin = 0.0
    coordinator = engine(config, legs, store=Store(str(tmp_path / 't.db')))
    pair.order_type = OrderType.MARKET
    md = coordinator.market[pair.key]
    assert coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread']).get('ok')
    [held] = coordinator.book.positions(pair.key)
    coordinator.set_algo(pair.key, 'ALGO')
    coordinator.poll_once()
    legs['acct_b'].broker.quote('GC1226', 4352.00, 4352.40)
    coordinator.poll_once()
    alert = block(coordinator, pair)['signal_alert']
    assert alert['kind'] == 'EXIT'
    assert alert['position_id'] == held.position_id
    assert 'close your BUY' in alert['text']
    assert held.is_open                            # told, not done
