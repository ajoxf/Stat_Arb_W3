"""At most N positions on a ladder (2 unless it says otherwise).

Counted: the positions open now, plus every working order that would
OPEN one - a working add that fills is the next position. Only the
opening part of a click is judged; a close is never refused. Every
refusal here has a control that turns the limit off, or takes the
counted thing away, and shows the same click going through.
"""

import pytest

from okxtrader.commands import CommandRunner
from okxtrader.config import PairConfig, _clean_max_positions
from okxtrader.coordinator import Coordinator
from okxtrader.models import OrderType


@pytest.fixture
def engine(config, pair, legs):
    co = Coordinator(config, legs, sleep=lambda s: None)
    co.start()
    co.poll_once()
    return co


def market(engine, pair):
    md = engine.market[pair.key]
    return md['short_spread'], md['long_spread']


def away(engine, pair, side, ticks=0.5):
    bid, offer = market(engine, pair)
    return round(bid - ticks, 2) if side == 'BUY' else round(offer + ticks, 2)


def at_market(engine, pair, side, quantity=1):
    """A click that crosses NOW, from the screen."""
    bid, offer = market(engine, pair)
    pair.order_type = OrderType.MARKET
    try:
        return engine.click(pair.key, side, offer if side == 'BUY' else bid,
                            quantity)
    finally:
        pair.order_type = OrderType.LIMIT


def limited(answer):
    return bool(answer.get('refused')) and \
        'the most this ladder may hold' in (answer.get('reason') or '')


# -- the setting --------------------------------------------------------------

@pytest.mark.parametrize('raw,value', [
    (None, 2), ('', 2), ('3', 3), (1, 1), ('0', 0), ('lots', 2), (-4, 0)])
def test_the_setting_reads_blank_as_two_and_zero_as_off(raw, value):
    assert _clean_max_positions(raw) == value


def test_the_setting_is_saved_and_hot_applied():
    pair = PairConfig('A|B', leg_a={'account': 'a', 'symbol': 'A'},
                      leg_b={'account': 'b', 'symbol': 'B'})
    assert pair.max_positions == 2                       # the default
    assert pair.to_dict()['max_positions'] == 2
    assert pair.apply_hot({'max_positions': '3'}) == ['max_positions']
    assert pair.max_positions == 3
    again = PairConfig.from_dict('A|B', pair.to_dict())
    assert again.max_positions == 3


# -- working orders count ------------------------------------------------------

def test_flat_a_third_working_opening_order_is_refused(engine, pair):
    assert engine.click(pair.key, 'BUY', away(engine, pair, 'BUY'), 1)['ok']
    assert engine.click(pair.key, 'SELL', away(engine, pair, 'SELL'), 1)['ok']
    third = engine.click(pair.key, 'BUY', away(engine, pair, 'BUY', 0.6), 1)
    assert limited(third), third
    assert '2 working orders that would open another' in third['reason']
    assert len(engine.book.orders(pair.key)) == 2


def test_CONTROL_with_the_limit_off_a_third_rests(engine, pair):
    pair.max_positions = 0
    for side, ticks in (('BUY', 0.5), ('SELL', 0.5), ('BUY', 0.6)):
        assert engine.click(pair.key, side, away(engine, pair, side, ticks),
                            1)['ok']
    assert len(engine.book.orders(pair.key)) == 3


def test_one_on_and_one_working_add_is_the_limit(engine, pair):
    assert at_market(engine, pair, 'SELL')['ok']
    add = engine.click(pair.key, 'SELL', away(engine, pair, 'SELL'), 1)
    assert add['ok'], add
    again = engine.click(pair.key, 'SELL', away(engine, pair, 'SELL', 0.6), 1)
    assert limited(again)
    assert '1 open position and 1 working order' in again['reason']
    # Control: cancel the working add, and the same click goes on.
    engine.cancel_order(add['order']['order_id'])
    assert engine.click(pair.key, 'SELL', away(engine, pair, 'SELL', 0.6),
                        1)['ok']


# -- positions count -----------------------------------------------------------

def test_two_on_a_third_opening_click_is_refused_at_market_too(engine, pair,
                                                               legs):
    assert at_market(engine, pair, 'SELL')['ok']
    assert at_market(engine, pair, 'SELL')['ok']
    sent = sum(len(legs[a].broker.positions) for a in ('acct_a', 'acct_b'))
    third = at_market(engine, pair, 'SELL')
    assert limited(third) and '2 open positions' in third['reason']
    assert len(engine.book.positions(pair.key)) == 2
    # Nothing reached either broker.
    assert sum(len(legs[a].broker.positions)
               for a in ('acct_a', 'acct_b')) == sent


def test_CONTROL_with_two_on_a_close_is_never_refused(engine, pair):
    assert at_market(engine, pair, 'SELL')['ok']
    assert at_market(engine, pair, 'SELL')['ok']
    close = at_market(engine, pair, 'BUY')            # covers one of two
    assert close['ok'] and not limited(close), close
    assert len(engine.book.positions(pair.key)) == 1


def test_a_resting_close_with_two_on_is_not_refused(engine, pair):
    assert at_market(engine, pair, 'SELL')['ok']
    assert at_market(engine, pair, 'SELL')['ok']
    tp = engine.click(pair.key, 'BUY', away(engine, pair, 'BUY'), 1)
    assert tp['ok'] and not limited(tp), tp
    assert tp['order']['position_id']                 # a CLOSE, by ticket


def test_a_flip_that_ends_inside_the_limit_is_allowed(engine, pair):
    """Two shorts of 1, BUY 3: both close, one long opens - one held."""
    assert at_market(engine, pair, 'SELL')['ok']
    assert at_market(engine, pair, 'SELL')['ok']
    flip = at_market(engine, pair, 'BUY', 3)
    assert not limited(flip), flip


def test_flatten_with_two_on_still_closes_everything(engine, pair, tmp_path):
    assert at_market(engine, pair, 'SELL')['ok']
    assert at_market(engine, pair, 'SELL')['ok']
    runner = CommandRunner(engine, str(tmp_path / 'c.jsonl'),
                           str(tmp_path / 'r.json'))
    result = runner._do_flatten_pair({'pair': pair.key})
    assert result['closed'] == 2 and result['failed'] == []
    assert engine.book.positions(pair.key) == []


def test_the_limit_is_per_ladder_and_can_be_raised(engine, pair):
    pair.max_positions = 3
    assert at_market(engine, pair, 'SELL')['ok']
    assert at_market(engine, pair, 'SELL')['ok']
    assert at_market(engine, pair, 'SELL')['ok']          # 3 of 3
    assert limited(at_market(engine, pair, 'SELL'))       # 4th


def test_the_LIVE_Algo_entry_never_meets_the_limit(engine, pair):
    """The Algo enters through `algo_enter`, not the click: the limit is
    the trader's, and the Algo already holds one trade at a time."""
    def explode(*a, **k):
        raise AssertionError('the Algo path consulted the position limit')
    engine._over_position_limit = explode
    answer = engine.algo_enter(pair, 'SELL', 1)
    assert answer.get('ok'), answer
    # Control: a CLICK does consult it.
    with pytest.raises(AssertionError):
        at_market(engine, pair, 'SELL')


# -- OKX: a W3 maker fill still resting counts as an opening order --------------

def test_a_maker_fill_in_flight_counts_as_an_opener(engine, pair):
    import types
    assert at_market(engine, pair, 'BUY').get('ok')
    engine.maker.jobs[pair.key] = types.SimpleNamespace(legs={})
    try:
        assert limited(engine.click(pair.key, 'BUY',
                                    away(engine, pair, 'BUY'), 1))
    finally:
        engine.maker.jobs.pop(pair.key, None)
    # CONTROL: the same click with nothing in flight rests.
    answer = engine.click(pair.key, 'BUY', away(engine, pair, 'BUY'), 1)
    assert not limited(answer) and answer.get('ok')
