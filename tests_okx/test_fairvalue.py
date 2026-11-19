"""The fair spread, and the pair of legs that turned out to be one
account.

Neither of these is a signal. The first is a number beside the market;
the second is a refusal — and the difference matters, because a guard
may withhold an ORDER and must never prevent a close.
"""

from datetime import date, datetime

import pytest

from okxtrader import fairvalue


def test_fair_value_is_the_carry_to_expiry():
    """A basis converges to zero at expiry and is worth its carry until
    then. 0.02 a day, 30 days out, is 0.60 of spread."""
    days = fairvalue.days_to_expiry('2026-09-26', datetime(2026, 8, 27))
    assert days == 30
    assert fairvalue.fair_spread(0.02, days) == pytest.approx(0.60)


def test_at_expiry_the_fair_spread_is_zero_not_negative():
    """A contract past its last day has no carry left, and a negative
    fair value is a price the market can never trade at."""
    assert fairvalue.days_to_expiry('2026-08-01', datetime(2026, 8, 27)) == 0
    assert fairvalue.fair_spread(0.02, 0) == 0.0


def test_without_an_expiry_or_a_swap_there_is_no_fair_value():
    """Unmeasured is not zero: 0.0000 on the screen would read as "the
    market is exactly fair", which is the one thing it does not say."""
    assert fairvalue.days_to_expiry(None, datetime(2026, 8, 27)) is None
    assert fairvalue.fair_spread(None, 30) is None
    assert fairvalue.fair_spread(0.02, None) is None

    body = fairvalue.describe(53.6, None, None, datetime(2026, 8, 27))
    assert body['fair_spread'] is None and body['gap'] is None
    assert 'Exchanges page' in body['note']


def test_rich_means_above_its_own_carry():
    """The sign of a basis is the thing everyone gets backwards once, so
    the screen says the word as well as the number."""
    body = fairvalue.describe(0.75, 0.02, '2026-09-26',
                              datetime(2026, 8, 27))
    assert body['fair_spread'] == pytest.approx(0.60)
    assert body['gap'] == pytest.approx(0.15)
    assert body['rich'] is True

    cheap = fairvalue.describe(0.40, 0.02, '2026-09-26',
                               datetime(2026, 8, 27))
    assert cheap['rich'] is False
    assert cheap['gap'] == pytest.approx(-0.20)


def test_a_negative_carry_is_a_fair_spread_below_zero():
    """Backwardation is not an error. A spread whose carry is negative
    is fair BELOW the spot, and clamping that to zero would report a
    perfectly ordinary market as cheap."""
    body = fairvalue.describe(-0.30, -0.01, '2026-09-26',
                              datetime(2026, 8, 27))
    assert body['fair_spread'] == pytest.approx(-0.30)
    assert body['gap'] == pytest.approx(0.0)


def test_the_expiry_is_read_the_way_a_human_types_it():
    assert fairvalue.parse_expiry('2026-09-26') == date(2026, 9, 26)
    assert fairvalue.parse_expiry('26/09/2026') == date(2026, 9, 26)
    assert fairvalue.parse_expiry('not a date') is None
    assert fairvalue.parse_expiry('') is None


def test_the_days_are_counted_on_the_brokers_calendar(config, pair, legs):
    """A contract expires on the broker's day, not on this machine's,
    and on a box hours away those are different dates for part of every
    day. The coordinator passes the broker's clock in."""
    from okxtrader.coordinator import Coordinator
    pair.expiry = '2026-09-26'
    coordinator = Coordinator(config, legs)
    coordinator.session_clock.now = lambda: datetime(2026, 9, 25, 23, 0)
    # The broker is a day ahead at this hour: its calendar says the 26th,
    # which is expiry — no carry left.
    coordinator.session_clock.offset = lambda: 3600 * 2
    coordinator.poll_once()

    row = coordinator.snapshot()['pairs'][pair.key]

    assert row['fair']['days_to_expiry'] == 0


# -- two legs, one account -------------------------------------------------


def test_two_accounts_that_are_secretly_one_terminal_refuse_an_entry(
        config, pair, legs):
    """Two runners can attach to the SAME running terminal — a blank
    terminal path on both is enough. The config then says "two accounts"
    while every order lands on one login, the margin is one pool nobody
    is watching, and nothing on the screen looks wrong."""
    from okxtrader.coordinator import Coordinator
    from okxtrader.models import SpreadSide
    coordinator = Coordinator(config, legs)
    # Refusing is OFF by default — one account carrying both legs is an
    # ordinary spread, and only the desk knows which of the two this
    # is. A desk that wants it refused turns this on.
    coordinator.config.settings['REFUSE_SHARED_ACCOUNT'] = True
    for leg in legs.values():
        leg.broker.login = 100006             # one terminal, both legs
    coordinator.poll_once()

    answer = coordinator.click(pair.key, SpreadSide.BUY,
                               coordinator.market[pair.key]['long_spread'])

    assert answer['ok'] is False and answer['refused']
    assert '#100006' in answer['reason']
    assert 'REFUSE_SHARED_ACCOUNT' in answer['reason']
    # Nothing was sent to either broker.
    for leg in legs.values():
        assert not [entry for entry in leg.broker.sent
                    if entry['action'] == 'market']


def test_two_separate_accounts_trade_normally(config, pair, legs):
    """The control. Without it this test would pass on a system that
    refuses every click."""
    from okxtrader.coordinator import Coordinator
    coordinator = Coordinator(config, legs)
    from okxtrader.models import SpreadSide
    coordinator.poll_once()

    answer = coordinator.click(pair.key, SpreadSide.BUY,
                               coordinator.market[pair.key]['long_spread'])

    assert answer.get('ok'), answer
    # The pair quotes by default, so the click works an order rather
    # than crossing — either way it was ACCEPTED, which is the point.
    assert coordinator.book.orders(pair.key) or coordinator.book.positions()


def test_the_same_account_is_named_on_the_screen(config, pair, legs):
    from okxtrader.coordinator import Coordinator
    coordinator = Coordinator(config, legs)
    for leg in legs.values():
        leg.broker.login = 100006

    health = coordinator.account_health()

    assert list(health['same_login']) == ['100006']
    assert sorted(health['same_login']['100006']) == sorted(legs)


def test_one_account_trading_both_legs_is_ordinary_and_allowed(config, pair,
                                                                legs):
    """Spot silver and the silver future at ONE broker is a spread, and
    a hedging account holds both sides at once. Configured deliberately
    — both legs pointed at the same account — it trades like any other
    pair, and the two-accounts-one-terminal check must not touch it."""
    from okxtrader.coordinator import Coordinator
    from okxtrader.models import SpreadSide
    from tests_okx.conftest import FakeSymbol       # noqa: F401  (path shim)

    broker = legs['acct_a'].broker
    other = legs['acct_b'].broker
    # One broker, both symbols — which is what one account looks like.
    for name, symbol in other.symbols.items():
        broker.symbols[name] = symbol
    pair.leg_b = dict(pair.leg_b, account='acct_a')
    coordinator = Coordinator(config, {'acct_a': legs['acct_a']})
    coordinator.poll_once()

    assert coordinator.legs_share_an_account(pair) is None
    answer = coordinator.click(pair.key, SpreadSide.BUY,
                               coordinator.market[pair.key]['long_spread'])

    assert answer.get('ok'), answer
    assert coordinator.book.orders(pair.key) or coordinator.book.positions()


def test_by_default_it_is_said_and_not_refused(config, pair, legs):
    """The banner names the login and both readings; the trade still
    goes. Which of the two situations this is, is the desk's to say —
    and a screen that refuses on its own guess would have stopped a
    legitimate spread from trading at all."""
    from okxtrader.coordinator import Coordinator
    from okxtrader.models import SpreadSide
    coordinator = Coordinator(config, legs)
    for leg in legs.values():
        leg.broker.login = 100006
    coordinator.poll_once()

    answer = coordinator.click(pair.key, SpreadSide.BUY,
                               coordinator.market[pair.key]['long_spread'])

    assert answer.get('ok'), answer
    # ...and it is still on the screen, named.
    assert coordinator.account_health()['same_login'] == {
        '100006': sorted(legs)} or list(
            coordinator.account_health()['same_login']) == ['100006']
