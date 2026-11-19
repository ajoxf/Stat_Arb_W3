"""Qty is the lots, and the trader types both legs.

The desk's own words, 2026-09-03:

    Leg A is Gold, Leg B is Gold Future. When 1 lot is placed on the
    ladder it should place 1 lot of Leg A and 1 lot of Leg B. The
    system picks up the contract size from MT5 (say 100oz). A Qty of 1
    lot would be 1 x 100 oz on each leg. A Qty of 100 lots would be
    100 x 100 oz on each leg.

    Ignore the concept of notional value and remove it from the
    settings.

So `clip_lots_a` and `clip_lots_b` are what ONE unit of Qty is on each
leg, both typed, both defaulting to 1. Leg B is no longer derived from
anything, and the three sizing bases that used to derive it — the units
hedge, lot for lot, by notional — are gone.

This file is the end-to-end proof: what is typed is what the two
brokers receive, on a click and on a fill, and nothing in between
recomputes it.
"""

import logging

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide


@pytest.fixture
def engine(config, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def sent(legs, account):
    """The volumes that actually reached one broker."""
    return [e for e in legs[account].broker.sent if e.get('volume')]


# -- a click ---------------------------------------------------------------

def test_a_click_sends_QTY_LOTS_to_each_broker(engine, pair, legs):
    """1 and 1, Qty 3: three lots of leg A and three of leg B. Not a
    ratio of three, not three of something derived — three lots."""
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 1.0
    pair.order_type = pair.order_type.__class__('MARKET')
    md = engine.market[pair.key]

    assert engine.click(pair.key, SpreadSide.BUY, md['long_spread'], 3.0)['ok']

    position = engine.book.positions(pair.key)[0]
    assert position.leg_a.volume == pytest.approx(3.0)
    assert position.leg_b.volume == pytest.approx(3.0)


def test_an_unequal_ratio_reaches_the_brokers_exactly_as_typed(engine, pair):
    """1 and 2, Qty 10: ten lots of A against twenty of B. Nothing
    second-guesses it against the beta or the contract sizes."""
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 2.0
    pair.order_type = pair.order_type.__class__('MARKET')
    md = engine.market[pair.key]

    assert engine.click(pair.key, SpreadSide.BUY, md['long_spread'],
                        10.0)['ok']

    position = engine.book.positions(pair.key)[0]
    assert position.leg_a.volume == pytest.approx(10.0)
    assert position.leg_b.volume == pytest.approx(20.0)


def test_a_fractional_leg_is_taken_as_typed_too(engine, pair):
    """The desk trades in tenths on this pair. 0.10 and 0.10 at Qty 1
    is a tenth a side, which is the smallest both brokers will take."""
    pair.clip_lots_a, pair.clip_lots_b = 0.10, 0.10
    pair.order_type = pair.order_type.__class__('MARKET')
    md = engine.market[pair.key]

    assert engine.click(pair.key, SpreadSide.SELL, md['short_spread'],
                        1.0)['ok']

    position = engine.book.positions(pair.key)[0]
    assert position.leg_a.volume == pytest.approx(0.10)
    assert position.leg_b.volume == pytest.approx(0.10)


# -- a LIMIT fill crosses the other leg on the same ratio ------------------

@pytest.mark.quote
def test_a_fill_crosses_the_other_leg_on_the_TYPED_ratio(engine, pair, legs):
    """A click and a fill must cross the same pair of sizes, or the
    pair is hedged on one route and not on the other. The quoting leg
    rests 2 lots for a Qty of 1 at 1:2; when it fills, leg A is crossed
    for 1."""
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 2.0
    md = engine.market[pair.key]

    answer = engine.click(pair.key, SpreadSide.BUY, md['long_spread'], 1.0)
    assert answer['ok']
    engine.poll_once()

    quote = engine.quoter.snapshot(pair.key)[0]
    assert quote['leg'] == 'B'
    assert quote['volume'] == pytest.approx(2.0)

    legs['acct_b'].broker.fill_pending(quote['ticket'])
    engine.poll_once()

    position = engine.book.positions(pair.key)[0]
    assert position.leg_b.volume == pytest.approx(2.0)
    assert position.leg_a.volume == pytest.approx(1.0)


# -- the money multiplier follows the typed lots --------------------------

def test_the_snapshot_prices_one_QTY_not_one_lot(engine, pair):
    """`k` is leg B's units: what a 1.00 move in the spread is worth
    for ONE unit of Qty. The exit levels are priced per unit and must
    not move when the keypad is touched."""
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 2.0
    row = engine.snapshot()['pairs'][pair.key]
    contract_b = pair.meta_b['contract_size']
    assert row['spread_units'] == pytest.approx(2.0 * contract_b)
    assert row['clip_lots_a'] == 1.0 and row['clip_lots_b'] == 2.0


# -- and what is no longer there ------------------------------------------

def test_nothing_derives_leg_B_any_more(engine, pair):
    """The control for the whole change. Leg B used to be computed —
    from the hedge arithmetic, or lot for lot, or by equal notional —
    and each of those could round it to zero, size a pair the trader
    had not asked for, or refuse to settle at all. Setting leg A must
    now leave leg B exactly where the trader put it."""
    pair.clip_lots_a, pair.clip_lots_b = 5.0, 1.0
    engine.poll_once()
    engine.resolve_symbols()
    assert (pair.clip_lots_a, pair.clip_lots_b) == (5.0, 1.0)


def test_a_blank_leg_is_ONE_through_the_hot_apply_too(pair):
    """Clearing a box is not clearing the leg: with nothing left to
    derive, a blank has one honest reading."""
    pair.apply_hot({'clip_lots_a': None, 'clip_lots_b': ''})
    assert (pair.clip_lots_a, pair.clip_lots_b) == (1.0, 1.0)


def test_both_legs_apply_without_a_restart(pair):
    changed = pair.apply_hot({'clip_lots_a': 1.0, 'clip_lots_b': 2.0})
    assert set(changed) >= {'clip_lots_a', 'clip_lots_b'}
    assert (pair.clip_lots_a, pair.clip_lots_b) == (1.0, 2.0)


# -- the contract size, and the one case for overriding it ----------------

def test_the_contract_size_comes_from_MT5_and_is_not_configured(engine, pair):
    """100 oz of gold, 5,000 of silver, 1,000 barrels of oil — MT5
    publishes all three as `trade_contract_size`. A contract size
    somebody typed is a contract size that can be wrong, and every
    money figure on the screen runs through it."""
    assert pair.contract_size_a is None and pair.contract_size_b is None
    assert pair.meta_a['contract_size'] == pytest.approx(100.0)
    # ...and what MT5 said is kept beside it, so an override is
    # visible AS an override rather than merely as a number.
    assert pair.meta_a['contract_size_mt5'] == pytest.approx(100.0)


def test_an_override_is_used_and_MT5s_own_is_kept_beside_it(config, legs):
    """The one case: the broker's number is wrong. It is the trader's
    to set, and it is loud."""
    pair = config.pairs['XAUUSD_|GC1226']
    pair.contract_size_a = 50.0
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()

    assert pair.meta_a['contract_size'] == pytest.approx(50.0)
    assert pair.meta_a['contract_size_mt5'] == pytest.approx(100.0)
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['contract_a'] == 50.0 and row['contract_a_mt5'] == 100.0
    # ...and the MONEY follows the override, which is the whole point
    # and the whole danger: `k` is leg B's units, so leg A's override
    # moves what a lot of A is worth everywhere it is priced.
    pair.contract_size_b = 50.0
    coordinator.resolve_symbols()
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['spread_units'] == pytest.approx(pair.clip_lots_b * 50.0)


def test_clearing_the_override_goes_back_to_MT5(pair):
    pair.apply_hot({'contract_size_a': 50.0})
    assert pair.contract_size_a == 50.0
    pair.apply_hot({'contract_size_a': ''})
    assert pair.contract_size_a is None, 'blank must be MT5\'s, never 1'


def test_diagnose_names_a_contract_size_that_disagrees_with_the_broker():
    from okxtrader import diagnostics
    from okxtrader.diagnostics import WARN

    class Pair:
        key = 'K'
        symbol_a, symbol_b = 'XAUUSD', 'GC1226'
        hedge_ratio, hedge_ratio_for = 1.0, 'XAUUSD|GC1226'
        pair_type = 'SPOT_FUTURE'
        clip_lots_a = clip_lots_b = 1.0
        contract_size_a, contract_size_b = 50.0, None

    report = {'symbol': 'X', 'found': True, 'bid': 100.0, 'ask': 100.2,
              'contract_size': 100.0, 'volume_min': 0.01,
              'volume_step': 0.01, 'volume_max': 100.0, 'currency': 'USD'}
    checklist = diagnostics.Checklist()
    diagnostics.check_pair(checklist, Pair(), dict(report), dict(report))

    check = [c for c in checklist.checks
             if c['name'] == 'Leg A contract size'][0]
    assert check['status'] == WARN
    assert '50' in check['message'] and '100' in check['message']
    assert check['fix']


# -- an order that cannot rest says so, in the LOG too --------------------

class Heard(logging.Handler):
    """What reached the log, without pytest's caplog — which the suite
    is often run without (`-p no:logging`)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def __enter__(self):
        logging.getLogger().addHandler(self)
        return self

    def __exit__(self, *_):
        logging.getLogger().removeHandler(self)


@pytest.mark.quote
def test_an_order_that_cannot_be_sized_is_logged_not_only_shown(engine, pair):
    """It ran silently for the whole life of the module. An entry that
    could not be placed set `group.reason` and returned, so the reason
    existed in exactly two places — the eleventh column of a panel that
    scrolls sideways, and 9px of ellipsised text on the rail. Nothing
    reached `coordinator.log`, so there was no way to answer "why is
    there nothing in MT5?" without a screenshot of the right pixels.
    """
    coordinator = engine
    # A size no broker will take: the fixture caps a leg at 100 lots.
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 1.0

    with Heard() as heard:
        assert coordinator.click(pair.key, SpreadSide.SELL, 40.0, 500.0)['ok']
        coordinator.poll_once()

    said = '\n'.join(heard.lines)
    assert 'NOT at the broker' in said, said
    assert 'maximum' in said
    # ...and the order really is in the book with nothing at the broker,
    # which is the state the log line exists to explain.
    order = coordinator.book.orders(pair.key)[0]
    assert order.is_working
    assert not [q for q in coordinator.quoter.snapshot(pair.key)
                if q.get('ticket')]


def test_it_is_logged_on_the_CHANGE_not_on_every_poll(engine, pair):
    """The level is worked three times a second. A line per pass buries
    itself and the one that matters with it."""
    coordinator = engine
    pair.clip_lots_a, pair.clip_lots_b = 1.0, 1.0
    coordinator.click(pair.key, SpreadSide.SELL, 40.0, 500.0)

    with Heard() as heard:
        for _ in range(5):
            coordinator.poll_once()

    held = [line for line in heard.lines if 'NOT at the broker' in line]
    assert len(held) <= 1, f'{len(held)} lines for one unchanged reason'


# -- a pair that is merely QUIET is not a dead feed ------------------------

def test_a_slow_pair_can_have_its_own_staleness_threshold(config, legs):
    """From the desk's log, 2026-09-03 19:37:

        leg B (UKOILX6.s): visible=True ... last change 15.1s ago
        USOILX6.c|UKOILX6.s: stale — re-subscribed both legs

    15.1s against a 15s limit. Every order on that ladder was withheld
    — correctly, by a guard doing exactly what it says — because a
    dated oil future trades a few times a minute while the threshold
    was tuned on a leg that ticks several times a second.

    The global was already raised from 5s to 15s once for this. One
    number cannot serve both pairs, so it is per pair now.
    """
    pair = config.pairs['XAUUSD_|GC1226']
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()

    # The desk-wide default, when the pair says nothing.
    assert pair.max_quote_age_sec is None
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['max_quote_age_effective'] == \
        config.get('MAX_QUOTE_AGE_SEC')

    # ...and this pair's own, when it does.
    pair.max_quote_age_sec = 60.0
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['max_quote_age_effective'] == 60.0


def test_the_quiet_pair_still_prices_orders_while_the_fast_one_guards(
        config, legs, gold_symbols):
    """The control that matters: raising it for one pair must actually
    let that pair rest an order, and a leg that has genuinely stopped
    must still be caught."""
    from okxtrader.spread import stale_quote
    md = {'leg_a_quote_age_sec': 2.0, 'leg_b_quote_age_sec': 20.0}

    # At the desk-wide 15s this pair is stale and every order is held.
    held = stale_quote(md, 15.0)
    assert held and 'Leg B' in held and '20.0s' in held
    # Given its own 60s, it prices normally.
    assert stale_quote(md, 60.0) is None
    # ...and a leg that really has stopped is still caught at 60s.
    assert stale_quote({'leg_a_quote_age_sec': 2.0,
                        'leg_b_quote_age_sec': 300.0}, 60.0)


def test_the_stale_reason_names_the_limit_it_broke():
    """So the NOT AT THE BROKER row is the whole answer: which leg, how
    long, and the number it was measured against."""
    from okxtrader.spread import stale_quote
    said = stale_quote({'leg_a_quote_age_sec': 1.0,
                        'leg_b_quote_age_sec': 15.1}, 15.0)
    assert 'Leg B' in said and '15.1s' in said and 'limit 15s' in said
