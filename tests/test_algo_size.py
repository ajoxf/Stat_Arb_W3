"""An Algo qty the broker cannot trade is said up front, not discovered
when every order is refused.

One spread on this pair is 0.10 lots a leg, and the future's minimum is
0.10. An Algo qty of 0.5 is 0.05 lots — under it, so every entry would
be refused inside the program. It used to be found only at the order,
with nothing on the screen but an Algo that never traded.
"""

from test_algo_warmup import engine, give_history, stretch


def test_an_untradable_algo_qty_holds_entries_and_says_why(config, pair,
                                                            legs):
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 0,
                                                     'algo_qty': 0.5})
    body = stretch(coordinator, legs, pair)
    assert coordinator.book.positions(pair.key) == []
    assert body['blocked'].startswith('Algo qty: ')
    assert 'minimum' in body['blocked']
    # Shown even with no stretch to enter: it is the state of the ladder.
    assert body['last_blocked']['reason'] == body['blocked']


def test_the_CONTROL_a_tradable_algo_qty_enters(config, pair, legs):
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 0,
                                                     'algo_qty': 1.0})
    body = stretch(coordinator, legs, pair)
    assert len(coordinator.book.positions(pair.key)) == 1
    assert 'Algo qty' not in (body.get('blocked') or '')


def test_the_last_order_and_its_outcome_reach_the_screen(config, pair,
                                                         legs):
    """What the panel's "Last order" line reads: the LIVE intent and
    what became of it — `done`, and the refusal's words when not."""
    give_history(legs)
    coordinator, pair, clock = engine(config, legs, {'warmup_min': 0})
    stretch(coordinator, legs, pair)
    body = coordinator.snapshot()['pairs'][pair.key]['algo_block']
    recent = body['recent']
    assert recent and recent[0]['action'] == 'ENTER'
    assert recent[0]['done'] is True
    assert 'result' in recent[0]


def test_a_held_back_signal_is_journalled_once_per_reason(config, pair,
                                                          legs, tmp_path):
    """The answer to "why did it not trade?" has to survive the screen:
    a held-back signal goes into the journal — once, not once a poll."""
    from okxtrader.database import Store
    from test_algo_desk import QUIET, Clock, engine as desk_engine
    give_history(legs)
    pair.algo_params = dict(QUIET, warmup_min=0, algo_qty=0.5)
    store = Store(str(tmp_path / 'trader.db'))
    clock = Clock()
    coordinator = desk_engine(config, legs, store=store, clock=clock)
    coordinator.set_algo(pair.key, 'ALGO')
    for price in (4352.00, 4352.05, 4352.10):         # three fresh quotes
        legs['acct_b'].broker.quote('GC1226', price, price + 0.1)
        clock.now += 1
        coordinator.poll_once()
    rows = store.events('algo_blocked')
    assert len(rows) == 1
    assert rows[0]['detail']['reason'].startswith('Algo qty: ')
    assert rows[0]['detail']['side'] == 'SELL'
    # The control: a DIFFERENT reason is a new row.
    pair.algo_params = dict(pair.algo_params, algo_qty=1.0,
                            max_entry_z=0.1)
    legs['acct_b'].broker.quote('GC1226', 4352.15, 4352.25)
    clock.now += 1
    coordinator.poll_once()
    rows = store.events('algo_blocked')
    assert len(rows) == 2 and 'cap' in rows[0]['detail']['reason']


def test_the_report_says_why_nothing_was_entered(tmp_path):
    import io
    import sys
    from okxtrader.database import Store
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).parents[1]
                           / 'tools'))
    import algo_report
    store = Store(str(tmp_path / 'trader.db'))
    key = 'USOILZ6.c|UKOILZ26.p'
    store.event('algo_switch', key, algo='ALGO', mode='LIVE')
    for reason in ('past the session cutoff', 'past the session cutoff',
                   'warming up: 12 of 90 min of live prices watched',
                   'warming up: 40 of 90 min of live prices watched'):
        store.event('algo_blocked', key, side='SELL', z=2.1, reason=reason)
    store.event('algo_order', key, action='ENTER', side='BUY', ok=False,
                reason='Qty 0.05 x 0.01 is 0.0005 lots on leg A, under '
                       'its 0.01-lot minimum')
    store.event('refused', key, reason='a manual click', algo=None)
    out = io.StringIO()
    algo_report.report(store, 24, out=out)
    text = out.getvalue()
    assert 'switched → ALGO LIVE' in text
    assert 'held back 2x: past the session cutoff' in text
    assert 'held back 2x: warming up: # of # min' in text
    assert 'REFUSED — Qty 0.05' in text
    assert 'orders refused: 1' in text
    assert 'a manual click' not in text          # not the Algo's
