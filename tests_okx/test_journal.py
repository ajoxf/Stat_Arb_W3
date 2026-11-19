"""The trade journal: every fill the BROKER reports.

Not what we meant to do — what MT5 says happened. That is why it also
carries the trader's own terminal clicks, and why it is keyed by MT5's
deal id rather than by anything we invent.
"""

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.database import Store
from okxtrader.models import OrderSide, SpreadSide


@pytest.fixture
def engine(config, pair, legs, tmp_path):
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'trader.db')))
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def test_every_fill_reaches_the_journal_from_the_brokers_own_history(
        engine, pair, legs):
    coordinator = engine
    coordinator.config.pairs[pair.key].order_type = \
        type(pair.order_type)('MARKET')
    coordinator.click(pair.key, SpreadSide.SELL, None)

    written = coordinator.journal_if_due()

    fills = coordinator.store.fills()
    assert written == 2 and len(fills) == 2
    symbols = {fill['symbol'] for fill in fills}
    assert symbols == {'XAUUSD_', 'GC1226'}
    for fill in fills:
        assert fill['is_ours'] == 1
        assert fill['entry'] == 'open'
        assert fill['volume'] == pytest.approx(0.1)
        assert fill['commission'] < 0             # the broker's, not ours
        # The BROKER's clock, kept beside how far it is from ours: read
        # as an ordinary timestamp it would put every row hours away
        # from the same trade in MT5's own History.
        assert fill['broker_time_ms'] > 0
        assert fill['server_offset_s'] == 3 * 3600


def test_a_fill_is_written_once_however_often_history_is_re_read(engine,
                                                                  pair):
    coordinator = engine
    coordinator.config.pairs[pair.key].order_type = \
        type(pair.order_type)('MARKET')
    coordinator.click(pair.key, SpreadSide.SELL, None)

    coordinator.journal_if_due()
    first = coordinator.store.fills()
    seen_at = {fill['deal_id']: fill['seen_at'] for fill in first}

    for _ in range(3):
        coordinator._last_journal = None          # force another pass
        coordinator.journal_if_due()

    again = coordinator.store.fills()
    assert len(again) == len(first)               # a journal, not a log
    # And the first time we saw each one is not rewritten.
    assert {fill['deal_id']: fill['seen_at'] for fill in again} == seen_at


def test_the_traders_own_terminal_click_is_journalled_and_flagged(engine,
                                                                   legs):
    """A fill on the account is a fill on the account. `is_ours` says
    which is which — dropping theirs would make the journal disagree
    with the broker's own statement."""
    coordinator = engine
    legs['acct_b'].broker.send_market_order(
        'GC1226', OrderSide.BUY, 0.2, comment='by hand')

    coordinator.journal_if_due()

    fills = coordinator.store.fills()
    assert len(fills) == 1
    assert fills[0]['is_ours'] == 0
    assert fills[0]['comment'] == 'by hand'
    assert fills[0]['volume'] == pytest.approx(0.2)
    # Ours only, when that is the question being asked.
    assert coordinator.store.fills(ours_only=True) == []


def test_a_fill_is_mapped_onto_the_pair_and_leg_it_belongs_to(engine, pair):
    coordinator = engine
    coordinator.config.pairs[pair.key].order_type = \
        type(pair.order_type)('MARKET')
    coordinator.click(pair.key, SpreadSide.BUY, None)
    coordinator.journal_if_due()

    by_symbol = {fill['symbol']: fill for fill in coordinator.store.fills()}
    assert by_symbol['XAUUSD_']['pair_key'] == pair.key
    assert by_symbol['XAUUSD_']['leg'] == 'A'
    assert by_symbol['GC1226']['leg'] == 'B'
    assert coordinator.store.fills(pair_key=pair.key)


def test_a_fill_on_a_symbol_no_pair_uses_is_still_journalled(engine, legs):
    """A journal that drops what it does not recognise is not an audit
    trail."""
    from conftest import FakeSymbol
    legs['acct_b'].broker.symbols['SI1226'] = FakeSymbol('SI1226', 38.0, 38.1)
    legs['acct_b'].broker.send_market_order('SI1226', OrderSide.BUY, 0.1,
                                            comment='by hand')

    coordinator = engine
    coordinator.journal_if_due()

    fills = coordinator.store.fills()
    assert len(fills) == 1
    assert fills[0]['symbol'] == 'SI1226'
    assert fills[0]['pair_key'] is None            # unmapped, not dropped


def test_the_round_trip_shows_both_ends_and_what_the_broker_booked(engine,
                                                                   pair):
    coordinator = engine
    coordinator.config.pairs[pair.key].order_type = \
        type(pair.order_type)('MARKET')
    coordinator.click(pair.key, SpreadSide.SELL, None)
    position = coordinator.book.positions(pair.key)[0]
    coordinator.executor.close_position(pair, position,
                                        coordinator.market[pair.key])
    coordinator.journal_if_due()

    fills = coordinator.store.fills()
    assert len(fills) == 4                          # two legs, both ends
    assert sorted(fill['entry'] for fill in fills) == \
        ['close', 'close', 'open', 'open']

    totals = coordinator.store.fill_totals(pair.key)
    assert totals['fills'] == 4
    assert totals['volume'] == pytest.approx(0.4)
    assert totals['commission'] < 0
    # The broker's own P&L on the closing deals — the honest
    # counterweight to our marks. Flat-and-back-to-flat pays one round
    # turn of both books.
    assert totals['profit'] == pytest.approx(
        -coordinator.market[pair.key]['spread_cost'] * 10.0, abs=0.01)


def test_the_journal_is_not_read_three_times_a_second(engine, pair, legs):
    """A history query is not something to do on the price poll."""
    coordinator = engine
    calls = []
    real = legs['acct_a'].order_log
    legs['acct_a'].order_log = lambda hours=24: (calls.append(hours),
                                                 real(hours))[1]

    coordinator.journal_if_due()
    coordinator.journal_if_due()
    assert len(calls) == 1

    coordinator._last_journal -= 11.0
    coordinator.journal_if_due()
    assert len(calls) == 2


def test_an_account_that_cannot_be_read_is_skipped_not_emptied(engine, legs):
    """None is UNKNOWN, not 'nothing traded'."""
    coordinator = engine
    legs['acct_a'].order_log = lambda hours=24: None
    legs['acct_b'].broker.send_market_order(
        'GC1226', OrderSide.BUY, 0.1, comment='by hand')

    written = coordinator.journal_if_due()

    assert written == 1                             # leg B's, and only that
    assert len(coordinator.store.fills()) == 1


def test_the_audit_trail_records_what_the_engine_decided(engine, pair, legs):
    """Refusals and reconciler decisions are answerable afterwards —
    without ever being the place the operator is SENT to."""
    coordinator = engine
    coordinator.config.pairs[pair.key].order_type = \
        type(pair.order_type)('MARKET')
    legs['acct_b'].broker.reject_orders[pair.symbol_b] = \
        '10027 - AutoTrading disabled by client'

    coordinator.click(pair.key, SpreadSide.SELL, None)

    events = coordinator.store.events('refused')
    assert events and '10027' in events[0]['detail']['reason']
    assert coordinator.store.events('recovery')     # written at startup
