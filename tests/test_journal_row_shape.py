"""What the journal writes, checked against the REAL producer.

The commission and swap columns were NULL on every fill ever written,
on every account, because `BrokerSession.order_log()` merged the
broker's two figures into one `fee` and never emitted them separately —
while `Store.record_fills` read `row.get('commission')` and
`row.get('swap')`.

FOUR LAYERS SAID THOSE COLUMNS WERE MEANT TO BE FILLED: the schema, the
insert, the Fills table's own Comm/Swap columns, and a test asserting
`fill['commission'] < 0` — "the broker's, not ours". Only the producer
disagreed.

IT WENT UNCAUGHT BECAUSE THE DOUBLE WAS RICHER THAN THE REAL THING.
`FakeBroker._record_deal` emitted commission, swap and magic; the real
`order_log` emitted none of them. Every test of the journal ran against
the fake, asserted the broker's commission was there, and passed. A
double that supplies what production does not cannot fail the way
production does — and it manufactures confidence, which is worse than
an outright gap.

So the tests here drive the REAL `order_log` against a fake MT5 module,
and pin the two producers' key sets equal so the double can never drift
richer again.

Why it costs money: the engine marks P&L with the TYPED
`COMMISSION_PER_LOT_A/B`, which default to 0.0. The journal is the
counterweight that catches a commission nobody entered. With the
counterweight stuck at zero, both numbers agree at $0.00 and
EXIT_IF_PROFIT closes a position still under water by the commission.
"""

import types

import pytest

from okxtrader import broker as broker_mod
from okxtrader.database import Store
from okxtrader.models import MAGIC_NUMBER, OrderSide


class FakeDeal:
    """One row of MT5's own deal history, as the package hands it over."""

    def __init__(self, ticket, commission, swap, profit=0.0, magic=None):
        self.ticket = ticket
        self.order = ticket
        self.symbol = 'USOILX6.kp'
        self.type = 0                      # buy
        self.entry = 0                     # DEAL_ENTRY_IN
        self.volume = 1.0
        self.price = 92.5210
        self.commission = commission
        self.swap = swap
        self.profit = profit
        self.time = 1757400000
        self.position_id = ticket
        self.magic = MAGIC_NUMBER if magic is None else magic
        self.comment = 'LADDER0011-d8dd'


class FakeMT5:
    """Enough of the package for order_log to run."""

    DEAL_ENTRY_IN = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1

    def __init__(self, deals):
        self.deals = deals

    def history_deals_get(self, since, now):
        return tuple(self.deals)

    def history_orders_get(self, since, now):
        return ()

    def orders_get(self, **kwargs):
        return ()

    def symbols_get(self):
        return ()

    def symbol_info_tick(self, name):
        return None


def real_order_log(deals):
    """`BrokerSession.order_log()` itself — not a stand-in for it."""
    session = broker_mod.BrokerSession(
        types.SimpleNamespace(name='AC-10006', login=1, password='x',
                              server='S', terminal_path=None))
    original = broker_mod.mt5
    broker_mod.mt5 = FakeMT5(deals)
    try:
        return session.order_log(24)
    finally:
        broker_mod.mt5 = original


# -- the producer ------------------------------------------------------

def test_the_brokers_commission_and_swap_survive_as_separate_numbers():
    """THE FAULT. A broker charging $7 commission and $2 swap reported
    a single -9.00 `fee`, and the two columns the journal keeps for
    them were never written at all."""
    rows = real_order_log([FakeDeal(5295, commission=-7.0, swap=-2.0)])

    assert len(rows) == 1
    row = rows[0]
    assert row['commission'] == pytest.approx(-7.0)
    assert row['swap'] == pytest.approx(-2.0)
    # `fee` is their sum and stays that way — the order log prints it.
    assert row['fee'] == pytest.approx(-9.0)


def test_control_a_broker_that_charges_nothing_reports_nothing():
    """The control. Without it the assertion above would pass on a
    build that invents a commission for every fill."""
    rows = real_order_log([FakeDeal(5295, commission=0.0, swap=0.0)])

    assert rows[0]['commission'] == 0.0
    assert rows[0]['swap'] == 0.0


def test_a_commission_the_broker_does_not_report_is_None_not_zero():
    """Unmeasured is not zero, and 0.00 is a claim about money."""
    rows = real_order_log([FakeDeal(5295, commission=None, swap=None)])

    assert rows[0]['commission'] is None
    assert rows[0]['swap'] is None


def test_the_deals_magic_reaches_the_row():
    rows = real_order_log([FakeDeal(5295, -7.0, -2.0, magic=999)])

    assert rows[0]['magic'] == 999
    assert rows[0]['is_bot'] is False


# -- producer and double, pinned together ------------------------------

def test_the_fake_broker_emits_EXACTLY_what_the_real_one_does(legs, pair):
    """THE RULE THIS FILE EXISTS FOR.

    Every journal test in the suite runs against FakeBroker. If it
    emits a key the real `order_log` does not, those tests assert a
    field that is NULL in production and pass anyway — which is what
    happened here for the entire life of the commission column.

    Missing keys are the ordinary kind of gap and would fail loudly
    downstream. EXTRA keys are the dangerous kind: they fail nothing,
    ever.
    """
    real = set(real_order_log([FakeDeal(5295, -7.0, -2.0)])[0])
    broker = legs['acct_a'].broker
    broker.send_market_order(pair.symbol_a, OrderSide.BUY, 0.1)
    fake = set(broker.order_log()[0])
    fake.discard('account')          # added by LocalLeg, not the broker

    assert fake == real, (
        f'the double and the real producer disagree — '
        f'only in the fake: {sorted(fake - real)}; '
        f'only in the real: {sorted(real - fake)}')


# -- the totals --------------------------------------------------------

def store_with(tmp_path, rows):
    store = Store(str(tmp_path / 'trader.db'))
    store.record_fills('AC-10006', rows)
    return store


def test_a_charge_nobody_measured_is_not_a_charge_of_zero(tmp_path):
    """`COALESCE(SUM(commission), 0)` over a column of NULLs published
    "$0.00" — a measurement claim about money nobody had read."""
    rows = real_order_log([FakeDeal(5295, commission=None, swap=None)])
    totals = store_with(tmp_path, rows).fill_totals()

    assert totals['fills'] == 1
    assert totals['commission'] is None, 'unmeasured came back as a number'
    assert totals['commission_measured'] == 0
    assert totals['swap'] is None
    assert totals['swap_measured'] == 0


def test_control_a_charge_the_broker_DID_report_is_totalled(tmp_path):
    """The control: without it the fix above is just "always say —"."""
    rows = real_order_log([FakeDeal(5295, commission=-7.0, swap=-2.0)])
    totals = store_with(tmp_path, rows).fill_totals()

    assert totals['commission'] == pytest.approx(-7.0)
    assert totals['commission_measured'] == 1
    assert totals['swap'] == pytest.approx(-2.0)


def test_a_partial_total_says_how_many_fills_it_covers(tmp_path):
    """Some measured and some not is a REAL total over PART of the
    session, and the screen has to be able to say which — a number
    covering half the fills, presented as the whole, is a guess."""
    rows = real_order_log([FakeDeal(5295, commission=-7.0, swap=-2.0),
                           FakeDeal(5296, commission=None, swap=None)])
    totals = store_with(tmp_path, rows).fill_totals()

    assert totals['fills'] == 2
    assert totals['commission'] == pytest.approx(-7.0)
    assert totals['commission_measured'] == 1


# -- what the totals cover ----------------------------------------------------


def test_the_totals_say_their_stretch_and_split_by_account(tmp_path):
    """The header's "broker's own P&L" is EVERY fill in the journal - all
    days, every account the desk has ever been connected to. It now says
    which stretch that is, and what each account contributed."""
    store = store_with(tmp_path, real_order_log(
        [FakeDeal(1, None, None, profit=-1500.0)]))
    store.record_fills('AC-100018', real_order_log(
        [FakeDeal(2, None, None, profit=-16.05)]))
    totals = store.fill_totals()
    assert totals['profit'] == pytest.approx(-1516.05)
    assert totals['first_ms'] and totals['last_ms']
    by = {a['account']: a['profit'] for a in totals['by_account']}
    assert by == {'AC-10006': pytest.approx(-1500.0),
                  'AC-100018': pytest.approx(-16.05)}


def test_ours_only_totals_leave_out_the_terminals_own_clicks(tmp_path):
    store = store_with(tmp_path, real_order_log(
        [FakeDeal(1, None, None, profit=10.0),
         FakeDeal(2, None, None, profit=-500.0, magic=0)]))   # by hand
    assert store.fill_totals()['profit'] == pytest.approx(-490.0)
    # The CONTROL is the line above: without the filter it is all in.
    assert store.fill_totals(ours_only=True)['profit'] == pytest.approx(10.0)
