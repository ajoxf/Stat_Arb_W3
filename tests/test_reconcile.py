"""Reconciliation: orphans, ghosts, and the numbers that must be right
when the engine is cleaning up after itself.
"""

from pathlib import Path

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import OrderSide, SpreadSide


@pytest.fixture
def engine(config, pair, legs, tmp_path):
    """A coordinator with a database, started cleanly.

    The database is what makes the book COMPLETE: without one, recovery
    cannot know what was open, and the reconciler holds its fire rather
    than call a live position an orphan.
    """
    from okxtrader.database import Store
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()
    coordinator.poll_once()
    assert coordinator.reconciler.book_complete
    return coordinator


def an_orphan(legs, symbol='GC1226', side=OrderSide.SELL, volume=0.1,
              account='acct_b', comment='stray'):
    """A position carrying OUR magic that the book has never heard of.

    That is what an orphan is: a fill of ours we lost track of — not the
    trader's own terminal click, which carries a different magic and is
    never ours to touch.

    The default comment is deliberately NOT one of ours. This helper
    used to open every orphan with `LADDER-lost`, which under the rule
    added after a live incident means "this system placed it" — and a
    position that says that is never auto-closed. The tests below are
    about the OTHER case: our magic, on a position we cannot show we
    placed, which is the only kind the reconciler may tidy away.
    """
    result = legs[account].broker.send_market_order(symbol, side, volume,
                                                    comment=comment)
    return result.ticket


def a_manual_trade(legs, symbol='GC1226', side=OrderSide.SELL, volume=0.1,
                   account='acct_b'):
    """The trader's own click in the terminal. Different magic."""
    result = legs[account].broker.send_market_order(symbol, side, volume,
                                                    comment='by hand')
    return result.ticket


def test_the_traders_own_terminal_position_is_never_touched(engine, legs):
    """Magic-scoped, always. Every query, every sweep, every close."""
    coordinator = engine
    ticket = a_manual_trade(legs)

    for _ in range(6):
        report = coordinator.reconciler.run()

    assert report['orphans'] == []
    assert report['closed'] == []
    assert legs['acct_b'].broker.positions.get(ticket)


def test_an_orphan_is_closed_on_the_third_strike_not_the_first(engine, legs):
    """A single poll can catch the broker mid-fill, and acting on that is
    how a healthy position gets closed for being briefly invisible."""
    coordinator = engine
    ticket = an_orphan(legs)

    for expected in (1, 2):
        report = coordinator.reconciler.run()
        assert report['orphans'][0]['strikes'] == expected
        assert report['closed'] == []
        assert legs['acct_b'].broker.positions.get(ticket)

    report = coordinator.reconciler.run()
    assert report['closed'][0]['ok']
    assert legs['acct_b'].broker.positions.get(ticket) is None


def test_the_orphan_close_pnl_multiplies_by_the_contract_size(engine, legs):
    """Booked at 1% of what they cost for months — $0.81 where the answer
    was $81.10."""
    coordinator = engine
    an_orphan(legs, side=OrderSide.SELL, volume=0.1)
    # It was sold at the bid; the market moves up, so buying it back
    # costs money.
    legs['acct_b'].broker.quote('GC1226', 4352.00, 4352.40)

    for _ in range(3):
        report = coordinator.reconciler.run()

    booked = report['closed'][0]
    assert booked['contract_size'] == pytest.approx(100.0)
    assert booked['contract_size_assumed'] is False
    # sold 4351.00, bought back at the 4352.40 ask, 0.1 lots x 100/lot.
    assert booked['pnl'] == pytest.approx((4351.00 - 4352.40) * 0.1 * 100.0)
    assert coordinator.reconciler.untracked_closes == [booked]


def test_an_unknown_symbols_contract_size_is_assumed_and_says_so(engine,
                                                                 legs):
    """1.0, and a note — never a silent guess that reads as a
    measurement."""
    coordinator = engine
    from conftest import FakeSymbol
    legs['acct_b'].broker.symbols['SI1226'] = FakeSymbol(
        'SI1226', 38.0, 38.1, contract_size=5000.0)
    an_orphan(legs, symbol='SI1226', volume=0.1)

    for _ in range(3):
        report = coordinator.reconciler.run()

    booked = report['closed'][0]
    assert booked['contract_size'] == 1.0
    assert booked['contract_size_assumed'] is True
    assert 'ASSUMED' in booked['note']
    assert 'lower bound' in booked['note']


def test_our_own_positions_are_never_orphans(engine, pair, legs):
    coordinator = engine
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    coordinator.book.add_position(result.position)

    for _ in range(3):
        report = coordinator.reconciler.run()

    assert report['orphans'] == []
    assert legs['acct_a'].broker.open_positions()
    assert legs['acct_b'].broker.open_positions()


def test_a_position_whose_close_failed_is_still_ours(engine, pair, legs):
    """A close that did not go through leaves the position OPEN and
    ACTIVE. Dropping its tickets here would turn it into an orphan we
    then close twice."""
    coordinator = engine
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    position = coordinator.book.add_position(result.position)
    legs['acct_a'].broker.fail_closes.add(pair.symbol_a)

    closed = coordinator.executor.close_position(pair, position,
                                                 coordinator.market[pair.key])
    assert not closed['ok']
    assert position.is_open                      # still ACTIVE

    for _ in range(3):
        report = coordinator.reconciler.run()
    assert report['orphans'] == []               # leg A is still ours


def test_a_ghost_is_force_cleared_after_three_strikes(engine, pair, legs):
    coordinator = engine
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    position = coordinator.book.add_position(result.position)
    # Someone closed leg A by hand, in the terminal.
    legs['acct_a'].broker.positions.clear()

    for expected in (1, 2):
        report = coordinator.reconciler.run()
        assert report['ghosts'][0]['strikes'] == expected
        assert position.is_open

    coordinator.reconciler.run()
    assert not position.is_open
    assert 'force-cleared' in position.close_reason
    assert 'leg A' in position.close_reason


def test_an_account_that_cannot_be_read_produces_no_orphans_and_no_ghosts(
        engine, pair, legs):
    """None is UNKNOWN, not flat. Treating it as flat would clear the
    book while the money is still out there."""
    coordinator = engine
    result = coordinator.executor.market_entry(
        pair, SpreadSide.SELL, coordinator.market[pair.key], 1.0)
    position = coordinator.book.add_position(result.position)
    legs['acct_a'].positions = lambda symbol=None: None

    for _ in range(4):
        report = coordinator.reconciler.run()

    assert report['unknown_accounts'] == ['acct_a']
    assert report['ghosts'] == []
    assert position.is_open

    # The control: once the account reads again, a genuinely missing leg
    # does strike out.
    legs['acct_a'].positions = lambda symbol=None: []
    for _ in range(3):
        report = coordinator.reconciler.run()
    assert report['ghosts'] and not position.is_open


def test_a_close_that_will_not_go_escalates_once_and_stops_hammering(
        engine, legs):
    """After three attempts: CLOSE IT BY HAND, and then leave the broker
    alone."""
    coordinator = engine
    an_orphan(legs)
    legs['acct_b'].broker.fail_closes.add('GC1226')

    def closes():
        return len([e for e in legs['acct_b'].broker.sent
                    if e['action'] == 'close'])

    # Three strikes to notice it, then CLOSE_ATTEMPTS tries at closing it.
    for _ in range(5):
        report = coordinator.reconciler.run()
    assert closes() == 3
    assert report['escalated'] and 'CLOSE IT BY HAND' in \
        report['escalated'][0]['say']

    # And then it stops. The broker is not asked again.
    for _ in range(5):
        report = coordinator.reconciler.run()
    assert closes() == 3
    assert report['escalated'] == []           # escalated ONCE, not each pass


def test_a_position_already_there_at_startup_is_never_auto_closed(
        config, pair, legs, tmp_path):
    """A position we cannot explain is exactly the one an automatic
    close must not touch. It goes on the screen for a person instead."""
    from okxtrader.database import Store
    an_orphan(legs)                      # it is there BEFORE we start
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()
    coordinator.poll_once()

    assert len(coordinator.recovery['unclaimed']) == 1
    for _ in range(6):
        report = coordinator.reconciler.run()

    assert report['closed'] == []
    assert legs['acct_b'].broker.open_positions()          # still there
    assert report['unclaimed'][0]['symbol'] == 'GC1226'
    # And it is in the snapshot the monitor renders from.
    assert coordinator.snapshot()['reconciler']['unclaimed']


def test_without_a_database_nothing_is_auto_closed_at_all(config, pair, legs):
    """No database means the book cannot be known to be complete, and an
    orphan is only an orphan if we are sure it is not ours."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    an_orphan(legs)

    for _ in range(6):
        report = coordinator.reconciler.run()

    assert report['book_complete'] is False
    assert report['closed'] == []
    assert 'incomplete' in report['orphans'][0]['held']
    assert legs['acct_b'].broker.open_positions()
    assert 'no database' in coordinator.recovery['error']


def test_an_unclaimed_pair_can_be_adopted_and_is_then_managed(
        config, pair, legs, tmp_path):
    """For what the operator can see and we cannot: two legs that went
    on before this build. Adopted, it is marked and closable like any
    other position."""
    from okxtrader.database import Store
    from okxtrader.models import OrderSide
    ticket_b = an_orphan(legs, 'GC1226', OrderSide.SELL, 0.1, 'acct_b')
    ticket_a = an_orphan(legs, 'XAUUSD_', OrderSide.BUY, 0.1, 'acct_a')
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()
    coordinator.poll_once()
    assert len(coordinator.recovery['unclaimed']) == 2

    adopted = coordinator.adopt_unclaimed(pair.key, ticket_a, ticket_b)

    assert adopted['ok']
    position = coordinator.book.positions(pair.key)[0]
    assert position.side.value == 'SELL'          # short B = short the spread
    assert position.leg_b.position_tickets == [ticket_b]
    assert coordinator.reconciler.unclaimed == {}
    # It survives a restart now, like anything else in the book.
    assert Store(str(tmp_path / 'test.db')).open_positions()


def test_adopting_two_tickets_that_are_not_a_hedge_is_refused(
        config, pair, legs, tmp_path):
    """The control: adopting two positions on the SAME side would book
    a spread that does not exist and mark it against a hedge nobody
    holds."""
    from okxtrader.database import Store
    from okxtrader.models import OrderSide
    ticket_b = an_orphan(legs, 'GC1226', OrderSide.SELL, 0.1, 'acct_b')
    ticket_a = an_orphan(legs, 'XAUUSD_', OrderSide.SELL, 0.1, 'acct_a')
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()

    refused = coordinator.adopt_unclaimed(pair.key, ticket_a, ticket_b)

    assert not refused['ok']
    assert 'not a hedge' in refused['reason']
    assert coordinator.book.positions(pair.key) == []


def test_an_unclaimed_position_can_be_closed_by_hand(config, pair, legs,
                                                      tmp_path):
    from okxtrader.database import Store
    ticket = an_orphan(legs)
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()

    result = coordinator.close_unclaimed('acct_b', ticket)

    assert result['ok']
    assert legs['acct_b'].broker.open_positions() == []
    assert coordinator.reconciler.unclaimed == {}


def test_the_reconciler_runs_on_its_own_interval(engine):
    coordinator = engine
    runs = []
    coordinator.reconciler.run = lambda: runs.append(1)

    coordinator.reconcile_if_due()
    coordinator.reconcile_if_due()
    assert len(runs) == 1                       # not on every poll

    coordinator._last_reconcile -= 21.0
    coordinator.reconcile_if_due()
    assert len(runs) == 2


def test_our_own_leg_seen_through_a_second_account_is_not_an_orphan(engine,
                                                                    legs):
    """Two configured accounts attached to ONE MT5 terminal see each
    other's positions. Every leg we open then appears on the other
    account with no entry in our book — and the reconciler closed it
    within seconds of it being placed. On a live box that ate every
    trade the desk tried to put on.

    A ticket we already hold is ours, whichever account name it comes
    back under: tickets are unique inside a terminal, and two accounts
    reporting the same one are one terminal.
    """
    coordinator = engine
    pair = list(coordinator.config.pairs.values())[0]
    from okxtrader.models import OrderType
    pair.order_type = OrderType.MARKET          # a POSITION, not an order
    answer = coordinator.click(
        pair.key, SpreadSide.BUY,
        coordinator.market[pair.key]['long_spread'])
    assert answer.get('ok'), answer
    assert coordinator.book.positions()

    # The other terminal connection now reports the same tickets.
    for name, leg in legs.items():
        for other_name, other in legs.items():
            if other_name == name:
                continue
            for ticket, position in list(other.broker.positions.items()):
                leg.broker.positions.setdefault(ticket, dict(position))

    closed = []
    for _ in range(5):                       # well past the strike count
        closed += coordinator.reconciler.run()['closed']

    assert closed == [], closed
    assert coordinator.book.positions(), 'our own position was cleared'


def test_a_genuine_orphan_is_still_closed(engine, legs):
    """The control. Without it the fix above would pass on a reconciler
    that never closes anything — and an unhedged leg left by a crash is
    exactly what this machinery is for."""
    coordinator = engine
    an_orphan(legs)

    closed = []
    for _ in range(5):
        closed += coordinator.reconciler.run()['closed']

    assert closed, 'a real orphan was left at the broker'


def test_an_adopted_position_appears_on_the_dashboard(config, pair, legs,
                                                      tmp_path):
    """The whole point of adopting, stated as the trader sees it.

    Until it is adopted the position is only on the Reconciler tab -
    real money the screen cannot mark, ladder or Flatten. Afterwards it
    is in the pair's own row, which is what the Positions tab renders."""
    from okxtrader.database import Store
    from okxtrader.models import OrderSide
    ticket_b = an_orphan(legs, 'GC1226', OrderSide.SELL, 0.1, 'acct_b')
    ticket_a = an_orphan(legs, 'XAUUSD_', OrderSide.BUY, 0.1, 'acct_a')
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()
    coordinator.poll_once()

    # Before: on the Reconciler tab, and nowhere else.
    before = coordinator.snapshot()
    assert before['pairs'][pair.key]['positions'] == []
    assert len(before['reconciler']['unclaimed']) == 2

    assert coordinator.adopt_unclaimed(pair.key, ticket_a, ticket_b)['ok']

    after = coordinator.snapshot()
    assert len(after['pairs'][pair.key]['positions']) == 1
    assert after['reconciler']['unclaimed'] == []
    # And it is a POSITION, not a note: the things the tab draws from.
    row = after['pairs'][pair.key]['positions'][0]
    assert row['position_id'] and row['side'] and row['leg_a'] and row['leg_b']


def test_control_a_refused_adopt_leaves_the_dashboard_alone(config, pair,
                                                            legs, tmp_path):
    """The control. A refusal must not half-adopt: the position stays
    on the Reconciler tab, where a person can still close it."""
    from okxtrader.database import Store
    from okxtrader.models import OrderSide
    ticket_b = an_orphan(legs, 'GC1226', OrderSide.SELL, 0.1, 'acct_b')
    ticket_a = an_orphan(legs, 'XAUUSD_', OrderSide.SELL, 0.1, 'acct_a')
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()
    coordinator.poll_once()

    assert not coordinator.adopt_unclaimed(pair.key, ticket_a, ticket_b)['ok']

    after = coordinator.snapshot()
    assert after['pairs'][pair.key]['positions'] == []
    assert len(after['reconciler']['unclaimed']) == 2


def test_the_snapshot_says_which_account_each_leg_trades(config, pair, legs):
    """The adopt form offers tickets per leg, and a ticket belongs to
    one account. Without this the screen would offer leg B's tickets on
    leg A - a choice the engine is bound to refuse."""
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    row = coordinator.snapshot()['pairs'][pair.key]
    assert row['leg_a_account'] == pair.account_a
    assert row['leg_b_account'] == pair.account_b


# --- The screen the operator actually uses ------------------------------

APP_JS = (Path(__file__).resolve().parent.parent /
          'okxtrader' / 'static' / 'app.js').read_text(encoding='utf-8')


def test_the_reconciler_tab_offers_both_things_it_tells_you_to_do():
    """The pane's own words were 'adopt one into a pair, or close it by
    hand', and for a long time only Close existed. A screen that names
    an action it does not offer is worse than one that stays quiet."""
    assert 'adopt one into a pair' in APP_JS
    assert 'close-unclaimed' in APP_JS
    assert 'adopt-go' in APP_JS
    assert "send('adopt_unclaimed'" in APP_JS


def test_control_the_adopt_button_needs_both_legs_before_it_will_send():
    """The control: half a hedge adopted as a pair is a naked leg the
    screen would then call hedged."""
    assert '!want.pair || !want.a || !want.b' in APP_JS
    assert "var ready = chosen && state.adopt.a && state.adopt.b;" in APP_JS


def test_the_half_filled_form_survives_a_snapshot():
    """That pane is rebuilt from innerHTML on every snapshot, twice a
    second. A selection kept only in the DOM would be cleared under the
    operator's hands."""
    assert "adopt: {pair: '', a: '', b: ''}" in APP_JS
    assert "state.adopt.a = e.target.value" in APP_JS


# --- Never close a position this system opened --------------------------
#
#     A trader put a spread on and watched both legs close sixty
#     seconds later - three strikes at a twenty-second poll - with
#     RECONCILE in the MT5 Reason column. The positions were ours: they
#     carried the LADDER tag this system writes on every order it
#     sends.
#
#     positions_by_magic did not report the comment, so the reconciler
#     compared TICKETS and nothing else. A leg we placed that had
#     fallen out of the book was indistinguishable from a stray
#     position somebody opened by hand.

from okxtrader.reconcile import is_ours                      # noqa: E402


def test_a_position_carrying_our_own_comment_is_ours():
    assert is_ours({'comment': 'LADDER0001-887b:A'})
    assert is_ours({'comment': 'HEDGE0002-aa11'})
    assert is_ours({'comment': 'ladder0003-66a1:A'})     # case, and spacing
    assert is_ours({'comment': '  LADDER0004-1234  '})


def test_control_anything_else_is_not_and_the_old_path_still_applies():
    """The control that keeps this from becoming 'never close
    anything'. A stray position with no comment, or somebody's manual
    trade, is exactly what the reconciler exists for."""
    for comment in ('', None, 'manual', 'RECONCILE', 'UNCLAIMED'):
        assert not is_ours({'comment': comment}), comment
    assert not is_ours({})
    assert not is_ours(None)


def test_the_broker_reports_the_comment_at_all():
    """The root cause. Without this field the guard above can never
    fire, however correct it is."""
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / 'okxtrader' /
              'broker.py').read_text(encoding='utf-8')
    block = source[source.index('def positions_by_magic'):]
    block = block[:block.index('def account_is_hedging')]
    assert "'comment': p.comment or ''," in block


def test_our_own_leg_is_never_auto_closed(config, pair, legs, tmp_path):
    """The whole point, end to end: a position at the broker carrying
    our comment, missing from the book, survives every strike."""
    from okxtrader.database import Store
    from okxtrader.models import OrderSide
    ticket = an_orphan(legs, 'XAUUSD_', OrderSide.BUY, 0.1, 'acct_a',
                       comment='LADDER0001-887b:A')
    broker = legs['acct_a'].broker
    assert ticket
    coordinator = Coordinator(config, legs, sleep=lambda s: None,
                              store=Store(str(tmp_path / 'test.db')))
    coordinator.start()
    coordinator.poll_once()
    coordinator.reconciler.book_complete = True
    coordinator.reconciler.unclaimed = {}

    reports = [coordinator.reconciler.run() for _ in range(6)]

    # Six passes is twice the strikes it takes to close an orphan.
    assert all(r['closed'] == [] for r in reports), 'we closed our own leg'
    assert broker.open_positions(), 'the money is gone'
    # Said on the first pass, and on the screen from then on: after it
    # is listed as unclaimed it is a person's to decide, which is the
    # same place it ends up either way.
    held = [row for row in reports[0]['orphans'] if row.get('held')]
    assert held and 'our own order comment' in held[0]['held']
    assert ('acct_a', str(ticket)) in coordinator.reconciler.unclaimed


# --- OUR record, not the broker's -----------------------------------------
#
#     The comment guard reads the broker's copy of our own words back.
#     True, but truncated, dropped or reformatted differently by every
#     liquidity provider - and this desk changes LP as it needs to. A
#     money guard must not quietly weaken because a broker changed.
#
#     So: every ticket the book holds is stamped into our own database
#     on every poll, and kept after the book stops holding it. That is
#     the case that cost a trader two legs of a live spread.


def test_a_ticket_we_have_ever_held_is_never_auto_closed(config, pair, legs,
                                                          tmp_path):
    """No comment at all, no book entry - and still not closed, because
    OUR OWN records say we placed it."""
    from okxtrader.database import Store
    from okxtrader.models import OrderSide
    store = Store(str(tmp_path / 'test.db'))
    ticket = an_orphan(legs, 'XAUUSD_', OrderSide.BUY, 0.1, 'acct_a',
                       comment='')          # an LP that drops comments
    store.remember_tickets([('acct_a', ticket, pair.key)])

    coordinator = Coordinator(config, legs, sleep=lambda s: None, store=store)
    coordinator.start()
    coordinator.poll_once()
    coordinator.reconciler.book_complete = True
    coordinator.reconciler.unclaimed = {}

    reports = [coordinator.reconciler.run() for _ in range(6)]

    assert all(r['closed'] == [] for r in reports), 'we closed our own ticket'
    assert legs['acct_a'].broker.open_positions(), 'the money is gone'


def test_control_a_ticket_that_is_not_in_our_records_is_still_an_orphan():
    """The control, and the one that keeps the reconciler useful. A
    position carrying our magic that we have never held, with no
    comment of ours, is exactly what it is for."""
    from okxtrader.database import Store
    import tempfile
    store = Store(str(Path(tempfile.mkdtemp()) / 'test.db'))
    store.remember_tickets([('acct_a', '111', 'A|B')])
    assert ('acct_a', '111') in store.our_tickets()
    assert ('acct_a', '222') not in store.our_tickets()
    assert ('acct_b', '111') not in store.our_tickets()   # per ACCOUNT


def test_the_ledger_remembers_a_ticket_the_book_has_forgotten(config, pair,
                                                              legs, tmp_path):
    """The whole point. The book holding it is not the guard - the book
    is what failed. The ledger is what is left when it does."""
    from okxtrader.database import Store
    store = Store(str(tmp_path / 'test.db'))
    coordinator = Coordinator(config, legs, sleep=lambda s: None, store=store)
    coordinator.start()
    coordinator.poll_once()
    # A position in the book, by the shortest honest route: what is
    # being tested is the STAMPING, not the execution path.
    from okxtrader.models import LegFill, OrderSide, SpreadPosition, \
        SpreadSide, OrderType
    leg_a = LegFill(pair.account_a, pair.symbol_a, OrderSide.BUY, 0.1, 100.0,
                    position_tickets=[9001])
    leg_b = LegFill(pair.account_b, pair.symbol_b, OrderSide.SELL, 0.1, 110.0,
                    position_tickets=[9002])
    coordinator.book.add_position(SpreadPosition(
        pair.key, SpreadSide.BUY, 1, leg_a, leg_b, 10.0, OrderType.MARKET, 1))
    coordinator.poll_once()

    held = store.our_tickets()
    assert (pair.account_a, '9001') in held, 'leg A was never stamped'
    assert (pair.account_b, '9002') in held, 'leg B was never stamped'

    # Now lose it, exactly as the incident did.
    for position in list(coordinator.book.positions()):
        coordinator.book._positions.pop(position.position_id, None)
    assert coordinator.book.positions() == []
    assert store.our_tickets() == held, 'the ledger forgot with the book'


def test_a_broken_ledger_is_not_a_claim_that_nothing_is_ours(config, pair,
                                                             legs):
    """Unmeasured is not zero. A database that will not read must leave
    the book and the comment doing their job, and say so - never assert
    that every position at the broker is a stray."""
    class Broken:
        def our_tickets(self):
            raise RuntimeError('disk gone')

    from okxtrader.reconcile import Reconciler
    reconciler = Reconciler(config, legs, None, None, store=Broken())
    assert reconciler.ledger() == set()
