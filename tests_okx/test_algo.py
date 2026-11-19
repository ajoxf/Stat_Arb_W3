"""The fair-value algo — and the line an algo must not cross.

This system is a MANUAL ladder with signals beside it. The rule that
matters most here is the one an algo is most likely to break: signals
may be computed and shown, but nothing places, modifies or cancels an
order by itself. (The Algo's own rules are in test_algo_signal.py and
test_algo_desk.py.)

So these tests are in two halves. The first says the arithmetic is
right — which KIND of pair this is, and therefore what its carry runs
to. The second says that whatever it concludes, NOTHING HAPPENS: no
order, no change to a click, no difference to the manual path at all.
"""

import pytest

from okxtrader import algo


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += seconds
        return self.now


# -- what KIND of pair this is, which decides the fair-value arithmetic


def test_a_spot_and_a_future_carries_to_the_futures_expiry(pair):
    """Spot does not expire; the future converges to it on its own
    date, so that is where the carry runs to."""
    kind = algo.pair_kind(pair, expiry_a=None, expiry_b='2026-09-26')
    assert kind == algo.SPOT_FUTURE
    nights, note = algo.carry_nights(kind, None, 30)
    assert nights == 30 and 'spot vs a future' in note


def test_a_calendar_carries_to_the_NEAR_expiry(pair):
    """Two futures: the spread is decided when the FIRST one expires.
    Running the carry to the far leg prices a trade that is over."""
    pair.pair_type = 'FUTURE_FUTURE'
    kind = algo.pair_kind(pair, expiry_a='2026-09-26', expiry_b='2026-12-26')
    assert kind == algo.FUTURE_FUTURE

    nights, note = algo.carry_nights(kind, 30, 121)

    assert nights == 30
    assert 'NEAR' in note


def test_a_calendar_missing_one_leg_prices_nothing(pair):
    """Half a calendar is not a shorter calendar."""
    nights, note = algo.carry_nights(algo.FUTURE_FUTURE, None, 121)
    assert nights is None and 'BOTH' in note


def test_two_different_instruments_have_no_date_at_all(pair):
    pair.pair_type = 'RELATED'
    kind = algo.pair_kind(pair, expiry_a='2026-09-26', expiry_b='2026-12-26')
    assert kind == algo.RELATED
    nights, note = algo.carry_nights(kind, 30, 121)
    assert nights is None and 'no date' in note


def test_two_expiries_do_NOT_make_a_calendar(pair):
    """The fault this replaced. UKOILV6 against USOILV6 is two futures
    with two dates and NO carry between them — Brent and WTI are
    different oil — and nothing in an expiry says whether two contracts
    share an underlying. Inferred, that pair got a fair value with
    nothing behind it.

    The operator declares it, and the dates never overrule them."""
    pair.pair_type = 'RELATED'
    assert algo.pair_kind(pair, '2026-09-26', '2026-12-26') == algo.RELATED
    nights, note = algo.carry_nights(algo.RELATED, 30, 121)
    assert nights is None and 'no date' in note

    # ...and a pair the operator DID declare a calendar still is one.
    pair.pair_type = 'FUTURE_FUTURE'
    assert algo.pair_kind(pair, '2026-09-26', '2026-12-26') == \
        algo.FUTURE_FUTURE


# -- the line an algo must not cross -------------------------------------


def test_no_algo_module_can_reach_the_broker():
    """Read as CODE, not as prose. An algo that never places an order
    is a claim that should be checkable without running it, because
    the way this breaks is a later edit that looks harmless.

    Names, calls and imports only — the comments are free to talk
    about brokers and orders, and they should."""
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path('okxtrader/algo.py').read_text())
    forbidden = {'place_limit', 'send_market_order', 'close_ticket',
                 'cancel_order', 'modify_order', 'order_send', 'executor',
                 'broker', 'legs', 'book', 'quoter', 'click'}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            found.add(node.attr)
        if isinstance(node, ast.Name) and node.id in forbidden:
            found.add(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                assert alias.name in ('math', 'time', 'collections', 'deque'), \
                    alias.name

    assert found == set(), found


#: What PLACES, CHANGES or CANCELS an order, by any route. The Algo's
#: runtime may read a leg — it asks MT5 for history bars — but nothing
#: on this list may appear in it as code.
ORDER_VERBS = {'place_limit', 'send_market_order', 'close_ticket',
               'cancel_order', 'modify_order', 'order_send', 'executor',
               'quoter', 'click', 'close_position', 'order', 'place',
               'flatten', 'arm'}


def _code_names(path):
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(path).read_text())
    names, imports = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        if isinstance(node, ast.Name):
            names.add(node.id)
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            imports.update(f"{node.module or '.'}:{alias.name}"
                           for alias in node.names)
    return names, imports


def test_the_band_is_arithmetic_and_nothing_else():
    names, imports = _code_names('okxtrader/bands.py')
    assert imports == {'math'}, imports
    assert names & (ORDER_VERBS | {'broker', 'legs'}) == set()


def test_the_entry_filters_are_arithmetic_and_nothing_else():
    names, imports = _code_names('okxtrader/algofilters.py')
    assert imports == {'math'}, imports
    assert names & (ORDER_VERBS | {'broker', 'legs'}) == set()


def test_the_algo_runtime_reads_history_and_cannot_place_an_order():
    """The seam execution will plug into is the SINK. Until then, the
    module that runs the Algo must not be able to reach an order by any
    route — checked as code, because the way this breaks is an edit
    that looks harmless."""
    names, imports = _code_names('okxtrader/algodesk.py')
    assert imports <= {'logging', 're', 'collections:deque', '.:algo',
                       '.:bands', '.:algofilters'}, \
        imports
    assert names & ORDER_VERBS == set(), names & ORDER_VERBS
    # The control: the checker does see a verb when there is one.
    import ast
    tree = ast.parse('leg.order("X", "BUY", 1)')
    assert {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)} & ORDER_VERBS == {'order'}


def test_selecting_an_algo_changes_NOTHING_about_a_click(config, pair, legs):
    """The whole promise. The same click, on the same market, with the
    algo off and then with it on and shouting — and the two must be
    indistinguishable at the broker."""
    from okxtrader.coordinator import Coordinator
    from okxtrader.models import SpreadSide

    def click_once(window_on):
        coordinator = Coordinator(config, legs, sleep=lambda s: None)
        pair.algo_window = window_on
        pair.order_type = pair.order_type.__class__('MARKET')
        coordinator.start()
        coordinator.poll_once()
        legs['acct_a'].broker.sent.clear()
        legs['acct_b'].broker.sent.clear()
        answer = coordinator.click(pair.key, SpreadSide.BUY,
                                   coordinator.market[pair.key]['long_spread'])
        sent = [dict(e) for e in legs['acct_a'].broker.sent
                + legs['acct_b'].broker.sent]
        for entry in sent:
            entry.pop('comment', None)          # carries a unique id
        return answer.get('ok'), sent

    off_ok, off_sent = click_once(False)
    on_ok, on_sent = click_once(True)

    assert off_ok and on_ok
    assert off_sent == on_sent, (off_sent, on_sent)


def test_a_ladder_running_NONE_computes_nothing_at_all(config, pair, legs):
    """"Nothing is incorporated until it is enabled" has to be true of
    the computing as well as the acting: no reading, nothing on the
    wire, nothing to go wrong."""
    from okxtrader.coordinator import Coordinator
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    for _ in range(5):
        coordinator.poll_once()

    assert pair.algo == 'NONE'          # derived: the window is shut
    block = coordinator.snapshot()['pairs'][pair.key]['algo_block']
    assert block == {'algo': 'NONE', 'window': False}

    # The control: open the window, and the reading appears.
    pair.algo_window = True
    coordinator.poll_once()
    block = coordinator.snapshot()['pairs'][pair.key]['algo_block']
    assert block['algo'] == 'FAIR_SPREAD' and 'fair' in block


def test_the_live_sink_reaches_orders_ONLY_through_the_coordinator():
    """The one Algo module that may trade does it through the two
    coordinator methods a manual click's executor sits behind — never a
    broker, a leg or the executor directly."""
    names, imports = _code_names('okxtrader/algoexec.py')
    assert imports == set(), imports
    assert names & {'broker', 'legs', 'executor', 'order_send',
                    'send_market_order', 'close_ticket', 'place_limit'} \
        == set()
    calls = {n for n in names if n.startswith('algo_')}
    assert calls == {'algo_enter', 'algo_exit'}, calls


def test_the_signal_journals_shadow_algo_cannot_reach_an_order():
    """The journal runs the Algo's own code beside the desk - which is
    only safe while that code has nothing to send to. It imports the
    Algo, the filters and `judge_filters`, and nothing that places,
    changes or cancels an order may appear in it as code."""
    names, imports = _code_names('okxtrader/shadow.py')
    assert imports == {'.:algo', '.:algofilters',
                       'algodesk:WARMUP_CARRY_SEC',
                       'algodesk:WARMUP_GAP_SEC',
                       'algodesk:judge_filters'}, imports
    assert names & (ORDER_VERBS | {'broker', 'legs', 'live', 'sink',
                                   'send', 'enter', 'leave'}) == set(), \
        names & ORDER_VERBS


def test_the_sigma_zones_are_arithmetic_and_nothing_else():
    """The zone track is a READING. It records what the spread did and
    what the Algo would have made of it; it must not be able to reach
    an order by any route — the same rule `bands` and `algofilters`
    are held to, and for the same reason."""
    names, imports = _code_names('okxtrader/zones.py')
    assert imports == {'math'}, imports
    assert names & (ORDER_VERBS | {'broker', 'legs'}) == set()
