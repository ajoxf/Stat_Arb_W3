"""How this ladder gets OUT, by default.

(What one Qty means in lots on each leg used to live here too, back
when leg B was derived. Both are typed now and `test_sizing.py` pins
them.)
"""

import pytest

from okxtrader.config import PairConfig
from okxtrader.coordinator import Coordinator


@pytest.fixture
def engine(config, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


# -- how this ladder gets OUT, by default ---------------------------------

def test_the_exit_type_defaults_to_MARKET():
    """The way out crosses now unless the trader says otherwise. A
    default that WAITS is a position nobody is getting out of."""
    pair = PairConfig.from_dict('K', {'leg_a': {}, 'leg_b': {}})
    assert pair.exit_type.value == 'MARKET'


def test_the_exit_type_is_selectable_and_applies_without_a_restart():
    pair = PairConfig.from_dict('K', {'leg_a': {}, 'leg_b': {}})
    assert 'exit_type' in pair.apply_hot({'exit_type': 'LIMIT'})
    assert pair.exit_type.value == 'LIMIT'
    # ...and it survives a save/load round trip.
    assert PairConfig.from_dict('K', pair.to_dict()).exit_type.value == 'LIMIT'


def test_a_blank_exit_type_is_MARKET_not_an_exception():
    """A config written by an older UI carries no exit type at all, and
    `OrderType(None)` raising inside the launcher is what took the
    engine down for nineteen hours once already."""
    assert PairConfig.from_dict(
        'K', {'leg_a': {}, 'leg_b': {}, 'exit_type': None}
    ).exit_type.value == 'MARKET'
    # The control: a value that is not one of the two is still refused.
    with pytest.raises(ValueError):
        PairConfig.from_dict('K', {'exit_type': 'WHENEVER'})


def test_the_exit_type_never_changes_what_reaches_the_broker(engine, pair):
    """It selects WHEN, not what. Both settings close by TICKET at
    market — a closing PENDING would open a second position on a
    hedging account, which is why there is no third option."""
    pair.exit_type = pair.exit_type.__class__('LIMIT')
    pair.order_type = pair.order_type.__class__('MARKET')
    md = engine.market[pair.key]
    assert engine.click(pair.key, 'BUY', md['long_spread'])['ok']

    # CLOSE ALL still crosses now, with the exit type set to LIMIT.
    position = engine.book.positions(pair.key)[0]
    engine.executor.close_position(pair, position,
                                   engine.market.get(pair.key),
                                   reason='flattened by trader')
    assert position.is_open is False
