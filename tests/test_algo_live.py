"""The Algo, LIVE: it trades — and the ladder is then the Algo's alone.

What these hold the engine to, each beside a CONTROL:

- LIVE enters with MARKET on both legs and exits by TICKET; DRY_RUN, on
  the same market, sends nothing;
- while LIVE, a NEW manual order on that ladder is refused in words, and
  a close is not;
- LIVE is refused on a ladder holding a manual position or a working
  order;
- leaving LIVE with a position asks: close it, or hand it to manual;
- a restart comes back OFF, shows the Algo's position as unmanaged, and
  LIVE again adopts it;
- the day's limits stop entries and never exits; a refused entry is not
  re-sent every poll, and a failed exit is retried at a pace.
"""

import pytest

from okxtrader.commands import CommandRunner
from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import ALGO_SOURCE, MANUAL, OrderType, SpreadSide

from test_algo_desk import QUIET, Clock, give_history

#: The SELL stretch every test here uses: the future's bid lifted far
#: over the band (see test_algo_desk.give_history for the band).
STRETCH = (4352.00, 4352.10)
#: ...and back: the offer a short closes at, under what it was sold at.
BACK = (4350.80, 4350.90)


def engine(config, legs, store=None, clock=None):
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              clock=clock or Clock(), store=store)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def live(coordinator, pair, **kw):
    answer = coordinator.set_algo(pair.key, 'ALGO', mode='LIVE',
                                  confirmed=True, **kw)
    assert answer['ok'], answer
    return answer


def sent(legs):
    return legs['acct_a'].broker.sent + legs['acct_b'].broker.sent


def quote_b(legs, bid_ask):
    legs['acct_b'].broker.quote('GC1226', *bid_ask)


@pytest.fixture
def ready(config, pair, legs):
    give_history(legs)
    pair.algo_params = dict(QUIET)
    pair.tp_target_pct_of_margin = 0.0          # TP = break-even here
    return config, pair, legs


def test_LIVE_enters_MARKET_on_both_legs_and_tags_it_ALGO(ready, tmp_path):
    config, pair, legs = ready
    store = Store(str(tmp_path / 'trader.db'))
    coordinator = engine(config, legs, store=store)
    live(coordinator, pair)
    quote_b(legs, STRETCH)
    coordinator.poll_once()

    [position] = coordinator.book.positions(pair.key)
    assert position.side is SpreadSide.SELL and position.source == ALGO_SOURCE
    assert legs['acct_a'].broker.open_positions()
    assert legs['acct_b'].broker.open_positions()
    [order] = store.events('algo_order')
    assert order['detail']['ok'] is True
    assert order['detail']['position_id'] == position.position_id
    # Remembered as the Algo's: a restart must know whose it is.
    [row] = store.open_positions()
    assert row['source'] == ALGO_SOURCE


def test_DRY_RUN_on_the_same_market_sends_nothing(ready):
    config, pair, legs = ready
    coordinator = engine(config, legs)
    coordinator.set_algo(pair.key, 'ALGO', mode='DRY_RUN')
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    assert coordinator.snapshot()['pairs'][pair.key]['algo_block'][
        'signal'] == 'SELL'
    assert sent(legs) == [] and coordinator.book.positions(pair.key) == []


def test_LIVE_exits_by_TICKET_when_the_target_is_reached(ready):
    config, pair, legs = ready
    coordinator = engine(config, legs)
    live(coordinator, pair)
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    [position] = coordinator.book.positions(pair.key)
    held = {str(p['ticket']) for leg in ('acct_a', 'acct_b')
            for p in legs[leg].broker.open_positions()}

    quote_b(legs, BACK)
    coordinator.poll_once()

    assert not position.is_open
    assert 'Algo' in position.close_reason
    assert legs['acct_a'].broker.open_positions() == []
    assert legs['acct_b'].broker.open_positions() == []
    # Both legs closed BY TICKET — the tickets the entry opened — and
    # never by an opposite market order, which on these hedging
    # accounts would open a second position.
    closes = [e for e in sent(legs) if e.get('action') == 'close']
    assert {str(e['ticket']) for e in closes} == held


def test_while_LIVE_a_manual_order_is_refused_and_a_close_is_not(ready,
                                                                tmp_path):
    config, pair, legs = ready
    pair.order_type = OrderType.MARKET
    coordinator = engine(config, legs)
    live(coordinator, pair)
    md = coordinator.market[pair.key]
    answer = coordinator.click(pair.key, SpreadSide.BUY, md['long_spread'])
    assert answer['ok'] is False and 'LIVE' in answer['reason']
    assert sent(legs) == []

    # A close still works: the Algo's own position, flattened by hand.
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    [position] = coordinator.book.positions(pair.key)
    runner = CommandRunner(coordinator, str(tmp_path / 'c.jsonl'),
                           str(tmp_path / 'r.json'))
    result = runner._do_flatten_pair({'pair': pair.key})
    assert result['closed'] == 1 and result['failed'] == []
    assert not position.is_open

    # The control: in a dry run the same click goes through.
    coordinator.set_algo(pair.key, 'ALGO', mode='DRY_RUN')
    md = coordinator.market[pair.key]
    assert coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread']).get('ok')


def test_LIVE_is_refused_on_a_ladder_holding_a_manual_position(ready):
    config, pair, legs = ready
    pair.order_type = OrderType.MARKET
    coordinator = engine(config, legs)
    md = coordinator.market[pair.key]
    assert coordinator.click(pair.key, SpreadSide.BUY,
                             md['long_spread']).get('ok')
    answer = coordinator.set_algo(pair.key, 'ALGO', mode='LIVE',
                                  confirmed=True)
    assert answer['ok'] is False and 'manual position' in answer['reason']
    assert coordinator.algos.is_on(pair.key) is False

    # The control: flat again, and LIVE is allowed.
    [position] = coordinator.book.positions(pair.key)
    coordinator.executor.close_position(pair, position,
                                        coordinator.market[pair.key])
    assert coordinator.set_algo(pair.key, 'ALGO', mode='LIVE',
                                confirmed=True)['ok']


def test_LIVE_is_refused_while_a_working_order_rests(ready):
    config, pair, legs = ready
    pair.order_type = OrderType.LIMIT
    coordinator = engine(config, legs)
    md = coordinator.market[pair.key]
    coordinator.click(pair.key, SpreadSide.BUY, md['short_spread'] - 1.0)
    assert coordinator.book.orders(pair.key)
    answer = coordinator.set_algo(pair.key, 'ALGO', mode='LIVE',
                                  confirmed=True)
    assert answer['ok'] is False and 'working order' in answer['reason']
    # The control: cancelled, and LIVE is allowed.
    coordinator.cancel_where(pair.key)
    assert coordinator.set_algo(pair.key, 'ALGO', mode='LIVE',
                                confirmed=True)['ok']


def test_leaving_LIVE_with_a_position_asks_what_to_do_with_it(ready):
    config, pair, legs = ready
    coordinator = engine(config, legs)
    live(coordinator, pair)
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    [position] = coordinator.book.positions(pair.key)

    answer = coordinator.set_algo(pair.key, 'NONE')
    assert answer['ok'] is False and answer['choose'] == ['close', 'manual']
    assert coordinator.algos.mode(pair.key) == 'LIVE'      # nothing changed

    # Hand it to manual: still open, the trader's now, the Algo off.
    answer = coordinator.set_algo(pair.key, 'NONE', off_action='manual')
    assert answer['ok'] and position.is_open and position.source == MANUAL
    assert coordinator.algos.is_on(pair.key) is False


def test_leaving_LIVE_can_close_the_position_instead(ready):
    config, pair, legs = ready
    coordinator = engine(config, legs)
    live(coordinator, pair)
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    [position] = coordinator.book.positions(pair.key)
    answer = coordinator.set_algo(pair.key, 'ALGO', mode='DRY_RUN',
                                  off_action='close')
    assert answer['ok'] and not position.is_open
    assert coordinator.algos.mode(pair.key) == 'DRY_RUN'


def test_a_restart_shows_the_Algos_position_unmanaged_until_LIVE_again(
        ready, tmp_path):
    config, pair, legs = ready
    path = str(tmp_path / 'trader.db')
    first = engine(config, legs, store=Store(path))
    live(first, pair)
    quote_b(legs, STRETCH)
    first.poll_once()
    [held] = first.book.positions(pair.key)

    second = engine(config, legs, store=Store(path))
    second.recover()
    row = second.snapshot()['pairs'][pair.key]
    assert row['algo_on'] is False
    assert row['algo_unmanaged'] == [held.position_id]
    # Nothing manages it: the target is reached and nothing is sent.
    before = len(sent(legs))
    quote_b(legs, BACK)
    second.poll_once()
    assert len(sent(legs)) == before

    # LIVE again adopts it — and manages it on the next pass.
    live(second, pair)
    assert second.snapshot()['pairs'][pair.key]['algo_unmanaged'] == []
    second.poll_once()
    assert second.book.positions(pair.key) == []


def test_Algo_qty_is_the_Algos_own_size(ready):
    config, pair, legs = ready
    pair.algo_params = dict(QUIET, algo_qty=2)
    coordinator = engine(config, legs)
    live(coordinator, pair)
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    [position] = coordinator.book.positions(pair.key)
    assert position.quantity == 2
    assert {round(e['volume'], 6) for e in sent(legs)} == {0.2}


# -- the day's limits, and the pacing of failures ----------------------------


class FakeLive:
    """A live sink that records what it was asked and answers as told."""

    def __init__(self, enter_ok=True, leave_ok=True):
        self.entered, self.left = [], []
        self.enter_ok, self.leave_ok = enter_ok, leave_ok

    def enter(self, pair, side, quantity):
        self.entered.append((side, quantity))
        return ({'ok': True, 'position_id': f'P{len(self.entered)}'}
                if self.enter_ok else {'ok': False, 'reason': '10027 nope'})

    def leave(self, pair, position_id, reason):
        self.left.append((position_id, reason))
        return {'ok': self.leave_ok, 'reason': None if self.leave_ok
                else '10018 market closed'}


def test_a_refused_entry_is_not_resent_every_poll(ready):
    config, pair, legs = ready
    pair.algo_params = dict(QUIET, cooldown_min=5)
    clock = Clock()
    coordinator = engine(config, legs, clock=clock)
    live(coordinator, pair)
    fake = coordinator.algos.live = FakeLive(enter_ok=False)
    quote_b(legs, STRETCH)
    for _ in range(5):
        coordinator.poll_once()
    assert len(fake.entered) == 1
    body = coordinator.snapshot()['pairs'][pair.key]['algo_block']
    assert 'cooldown' in (body['blocked'] or '')
    # The control: after the cooldown it tries again.
    clock.now += 301
    coordinator.poll_once()
    assert len(fake.entered) == 2


def test_the_days_trade_limit_stops_entries_not_exits(ready):
    config, pair, legs = ready
    pair.algo_params = dict(QUIET, max_trades_day=1, cooldown_min=0)
    coordinator = engine(config, legs)
    live(coordinator, pair)
    quote_b(legs, STRETCH)
    coordinator.poll_once()                          # trade 1 on
    quote_b(legs, BACK)
    coordinator.poll_once()                          # ...and off: an exit
    assert coordinator.book.positions(pair.key) == []
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    body = coordinator.snapshot()['pairs'][pair.key]['algo_block']
    assert coordinator.book.positions(pair.key) == []
    assert "day's limit" in body['blocked']

    # The control: no limit, and the same stretch trades.
    pair.algo_params = dict(QUIET, max_trades_day=0, cooldown_min=0)
    coordinator.poll_once()
    assert len(coordinator.book.positions(pair.key)) == 1


def test_a_losing_run_and_the_daily_loss_stop_entries(ready):
    config, pair, legs = ready
    pair.algo_params = dict(QUIET, max_losses_row=2, daily_loss_limit=0)
    coordinator = engine(config, legs)
    live(coordinator, pair)
    coordinator.poll_once()                  # the Algo has seen its day
    run = coordinator.algos._runs[pair.key]
    run.day.update(losses_row=2)
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    assert coordinator.book.positions(pair.key) == []
    assert 'in a row' in coordinator.snapshot()['pairs'][pair.key][
        'algo_block']['blocked']

    pair.algo_params = dict(QUIET, max_losses_row=0, daily_loss_limit=50)
    run.day.update(losses_row=0, pnl=-60.0)
    coordinator.poll_once()
    assert coordinator.book.positions(pair.key) == []
    assert 'limit' in coordinator.snapshot()['pairs'][pair.key][
        'algo_block']['blocked']

    # The control: both limits off, and it trades.
    pair.algo_params = dict(QUIET, max_losses_row=0, daily_loss_limit=0)
    coordinator.poll_once()
    assert len(coordinator.book.positions(pair.key)) == 1


def test_a_loss_is_counted_when_the_Algos_position_closes(ready):
    config, pair, legs = ready
    coordinator = engine(config, legs)
    live(coordinator, pair)
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    [position] = coordinator.book.positions(pair.key)
    # Closed by hand at a loss: it still counts against the Algo's day.
    quote_b(legs, (4352.40, 4352.50))
    coordinator.executor.close_position(pair, position,
                                        coordinator.market[pair.key])
    coordinator.poll_once()
    day = coordinator.algos._runs[pair.key].day
    assert day['losses_row'] == 1 and day['pnl'] < 0


def test_a_failed_exit_is_retried_at_a_pace(ready):
    config, pair, legs = ready
    clock = Clock()
    coordinator = engine(config, legs, clock=clock)
    live(coordinator, pair)
    quote_b(legs, STRETCH)
    coordinator.poll_once()
    fake = coordinator.algos.live = FakeLive(leave_ok=False)
    quote_b(legs, BACK)
    for _ in range(5):
        coordinator.poll_once()
    assert len(fake.left) == 1
    clock.now += 5
    coordinator.poll_once()
    assert len(fake.left) == 2


def test_an_add_goes_on_at_its_own_size(ready):
    """The ADD has a size of its own where the trader gives it one; the
    trade keeps the Algo's. An add whose own size is not set does not go
    on - and says why."""
    config, pair, legs = ready
    pair.algo_params = dict(QUIET, algo_qty=2.0, add_size_by='QTY',
                            add_qty=0.5)
    coordinator = engine(config, legs)
    live(coordinator, pair)
    coordinator.poll_once()
    fake = coordinator.algos.live = FakeLive()
    run = coordinator.algos._runs[pair.key]
    desk = coordinator.algos
    desk._send(pair, run, {'action': 'ENTER', 'side': 'SELL', 'z': 2.6,
                           'add': True}, 0.0)
    desk._send(pair, run, {'action': 'ENTER', 'side': 'SELL', 'z': 2.6},
               0.0)
    assert [q for _side, q in fake.entered] == [0.5, 2.0]
    # Same as the trade: the Algo's own Qty.
    run.params = dict(run.params, add_size_by='SAME')
    desk._send(pair, run, {'action': 'ENTER', 'side': 'SELL', 'z': 2.6,
                           'add': True}, 0.0)
    assert fake.entered[-1][1] == 2.0
    # A size of its own, not set: nothing is sent.
    run.params = dict(run.params, add_size_by='QTY', add_qty=0.0)
    answer = desk._send(pair, run, {'action': 'ENTER', 'side': 'SELL',
                                    'z': 2.6, 'add': True}, 0.0)
    assert not answer['ok'] and 'add size' in answer['reason']
    assert len(fake.entered) == 3


def test_the_trade_and_its_add_share_one_averaged_set_of_levels(ready):
    """Short at one price, an add further out: both fills are judged
    against ONE break-even and target - the weighted average - so they
    come off together, each by its own ticket."""
    config, pair, legs = ready
    pair.algo_params = dict(QUIET, cooldown_min=0)
    pair.tp_target_pct_of_margin = 2.0
    coordinator = engine(config, legs)
    live(coordinator, pair)
    quote_b(legs, STRETCH)
    coordinator.poll_once()                          # the trade
    [first] = coordinator.book.positions(pair.key)
    side = first.side
    md = coordinator.market.get(pair.key)
    alone = {r['position_id']: r
             for r in coordinator._algo_positions(pair, md)}
    assert 'averaged' not in alone[first.position_id]
    # The add, further out.
    quote_b(legs, (4353.00, 4353.10))
    coordinator.poll_once()
    answer = coordinator.algo_enter(pair, side, first.quantity)
    assert answer['ok'], answer
    md = coordinator.market.get(pair.key)
    rows = coordinator._algo_positions(pair, md)
    assert len(rows) == 2
    a, b = rows
    assert a['break_even'] == b['break_even'] and a['tp'] == b['tp']
    assert a['entry_spread'] != b['entry_spread']    # each keeps its fill
    assert a['averaged']['fills'] == 2
    assert a['averaged']['quantity'] == pytest.approx(2 * first.quantity)
    lo, hi = sorted([a['entry_spread'], b['entry_spread']])
    assert lo < a['averaged']['entry'] < hi
    # One target worth the sum of what each fill makes there.
    assert a['averaged']['tp_money'] == pytest.approx(
        a['tp_money'] + b['tp_money'], abs=0.02)
