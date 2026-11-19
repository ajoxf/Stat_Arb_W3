"""The Algo arms only on a price the desk would trade on.

A live journal filled with stretches that "came back through the window
before 3 fresh prices confirmed it" - z +4 to +8 on gold spot vs gold
futures, back inside the mean within a second - and one at z -1006.90 on
an oil pair. That one was a leg read as 0.0/0.0: MT5's answer, right
after a re-subscribe, for a side it has no price for. The others were
one leg a moment behind the other.

The feed guards (a stale leg, a spread that jumped) already held the
ENTRY back on such a price. They did not hold back the ARMING, and
re-entry is arm-then-enter: a glitch that armed it left the first real
price back inside the window to be taken as the stretch coming back.

Every guard here has its control.
"""

import pytest

from okxtrader import algo
from okxtrader.coordinator import Coordinator, priced


STATS = {'ready': True, 'count': 20, 'needed': 20, 'mean': 10.0,
         'sigma': 0.1}
# entry 2.0, back 0.5, 50 %: armed at |z| 2.0, enters 1.5 .. 0.75.
PARAMS = {'reentry_on': True, 'entry_z': 2.0, 'reentry_back': 0.5,
          'reentry_window_pct': 50.0, 'confirm_ticks': 3}


def market(z, quote, jump=None):
    spread = 10.0 + z * 0.1
    return {'short_spread': spread, 'long_spread': spread,
            'mid_spread': spread, 'quote_id': quote, 'jump_reason': jump}


def step(signal, z, quote, health=None, jump=None):
    return signal.evaluate(0.0, market(z, quote, jump), STATS, (),
                           {'health': health})


def test_a_stale_price_does_not_arm():
    signal = algo.AlgoSignal(PARAMS)
    body = step(signal, 7.7, 1, health='leg B (GC1226): 15.2s old')
    assert body['armed'] == {'BUY': False, 'SELL': False}


def test_a_jumped_price_does_not_arm():
    signal = algo.AlgoSignal(PARAMS)
    body = step(signal, 7.7, 1, jump='the spread jumped 0.77 (6.0 sigma)')
    assert body['armed'] == {'BUY': False, 'SELL': False}


def test_CONTROL_the_same_stretch_on_a_good_price_arms():
    signal = algo.AlgoSignal(PARAMS)
    body = step(signal, 7.7, 1)
    assert body['armed']['SELL'] is True


def test_a_glitch_cannot_hand_the_window_an_entry():
    """THE FAULT: armed by the glitch, entered on the way back."""
    signal = algo.AlgoSignal(PARAMS)
    step(signal, 0.2, 1)
    step(signal, 7.7, 2, jump='the spread jumped 0.75 (6.0 sigma)')
    intents = []
    for quote in range(3, 7):                # back inside, in the window
        intents += step(signal, 1.2, quote)['intents']
    assert intents == []


def test_CONTROL_a_real_stretch_coming_back_is_entered():
    signal = algo.AlgoSignal(PARAMS)
    step(signal, 0.2, 1)
    step(signal, 2.4, 2)                     # a real stretch
    intents = []
    for quote in range(3, 7):
        intents += step(signal, 1.2, quote)['intents']
    assert [i['side'] for i in intents] == ['SELL']


def test_a_bad_price_does_not_disarm_either():
    """The other half: a glitch back through the far edge must not throw
    away a real arming."""
    signal = algo.AlgoSignal(PARAMS)
    step(signal, 2.4, 1)
    body = step(signal, -3.0, 2, jump='the spread jumped 0.54 (6.0 sigma)')
    assert body['armed']['SELL'] is True
    # Control: the same move on a good price lets it go.
    body = step(signal, -3.0, 3)
    assert body['armed']['SELL'] is False


# -- a side with no price is not a price of zero ---------------------------

@pytest.mark.parametrize('tick', [
    {'bid': 0.0, 'ask': 0.0},
    {'bid': 0.0, 'ask': 96.2},
    {'bid': 96.2, 'ask': 0.0},
    {'bid': None, 'ask': 96.2},
    {},
])
def test_a_zero_side_is_no_price(tick):
    assert not priced(tick)


def test_CONTROL_a_real_quote_is_a_price():
    assert priced({'bid': 96.18, 'ask': 96.22})


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def test_a_leg_reading_zero_makes_the_pair_dark_not_a_thousand_sigma(
        engine, pair, legs):
    coordinator = engine
    real = legs['acct_a'].tick
    legs['acct_a'].tick = lambda symbol: dict(real(symbol), bid=0.0,
                                              ask=0.0)
    coordinator.poll_once()
    assert coordinator.market[pair.key] is None
    # Control: the real quote back, the pair is priced again.
    legs['acct_a'].tick = real
    coordinator.poll_once()
    md = coordinator.market[pair.key]
    assert md is not None and md['leg_a_bid'] > 0
