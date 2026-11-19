"""Is the money arithmetic the same on EVERY instrument?

The structure is: `P&L = spread move x lots x contract size`, and the
spread is `P_B - beta x P_A`. That holds for any pair of instruments —
but only while two things are true, and neither of them is true by
nature:

- MT5 must actually price one tick of one lot at `contract size x tick
  size`. It publishes what it really uses (`trade_tick_value`), and
  where the two disagree every figure on the ladder is out by the same
  ratio, in the same direction, all day.
- both legs must be quoted in the SAME currency. Subtracting a price in
  EUR from a price in USD gives a number in neither, and `k = L_B x
  C_B` then prices it in leg B's money.

Neither was checked anywhere. Both are cheap to check and impossible to
notice, which is the worst combination a number can have.
"""

from okxtrader import diagnostics
from okxtrader.diagnostics import FAIL, PASS, WARN


def symbol(**over):
    """A gold CFD that behaves: 100 oz a lot, a $0.01 tick worth $1."""
    report = {'symbol': 'XAUUSD', 'found': True, 'visible': True,
              'description': 'Gold vs USD', 'bid': 4600.0, 'ask': 4600.2,
              'contract_size': 100.0, 'tick_size': 0.01, 'tick_value': 1.0,
              'volume_min': 0.01, 'volume_step': 0.01, 'volume_max': 100.0,
              'currency': 'USD', 'depth_levels': 0, 'trade_allowed': True}
    report.update(over)
    return report


def money_check(checklist):
    return [c for c in checklist.checks if c['name'].endswith('money')][0]


def test_a_symbol_whose_tick_value_agrees_is_passed():
    """The control. Most instruments are like this, and a check that
    complained about them would be turned off within a day."""
    checklist = diagnostics.Checklist()
    diagnostics.check_symbol(checklist, 'acct', symbol())
    assert money_check(checklist)['status'] == PASS


def test_a_symbol_MT5_prices_differently_is_named_and_the_ratio_given():
    """Silver at 5,000 oz a lot with a tick value that says otherwise:
    the ladder would print P&L, break-even and the take-profit all off
    by the same factor, and nothing else on the screen would show it."""
    checklist = diagnostics.Checklist()
    diagnostics.check_symbol(checklist, 'acct', symbol(
        symbol='XAGUSD', contract_size=5000.0, tick_size=0.001,
        tick_value=2.5))                       # implies 2,500, not 5,000

    check = money_check(checklist)
    assert check['status'] == FAIL
    assert '2,500' in check['message']         # what MT5 really uses
    assert '0.500x' in check['message']        # ...and how far out we are
    assert check['fix']


def test_an_unreported_tick_value_is_said_rather_than_assumed():
    """Unmeasured is not agreement."""
    checklist = diagnostics.Checklist()
    diagnostics.check_symbol(checklist, 'acct', symbol(tick_value=None))
    assert money_check(checklist)['status'] == 'INFO'
    assert 'cannot be cross-checked' in money_check(checklist)['message']


# -- and the two legs against each other ----------------------------------

class Pair:
    key = 'XAUUSD|GC1226'
    symbol_a = 'XAUUSD'
    symbol_b = 'GC1226'
    hedge_ratio = 1.0
    hedge_ratio_for = 'XAUUSD|GC1226'
    pair_type = 'SPOT_FUTURE'
    clip_lots_a = 0.01
    clip_lots_b = 0.01


def currency_check(checklist):
    rows = [c for c in checklist.checks if c['name'] == 'Currency']
    return rows[0] if rows else None


def test_two_legs_in_different_currencies_is_refused():
    """`P_B - beta x P_A` across two currencies is a number in neither,
    and every figure derived from it is out by the exchange rate."""
    checklist = diagnostics.Checklist()
    diagnostics.check_pair(checklist, Pair(), symbol(currency='USD'),
                           symbol(symbol='GC1226', currency='EUR'))
    check = currency_check(checklist)
    assert check['status'] == FAIL
    assert 'USD' in check['message'] and 'EUR' in check['message']


def test_two_legs_in_ONE_currency_are_passed():
    """The control."""
    checklist = diagnostics.Checklist()
    diagnostics.check_pair(checklist, Pair(), symbol(),
                           symbol(symbol='GC1226'))
    assert currency_check(checklist)['status'] == PASS


def test_a_ladder_priced_in_a_currency_the_ACCOUNT_is_not_says_so():
    """Not a refusal — the two legs still hedge each other, and MT5
    converts on the way to the balance. But the figures on the ladder
    are NOT converted, and a ladder reading in one currency beside an
    MT5 window reading in another is a reconciliation nobody wins."""
    checklist = diagnostics.Checklist()
    diagnostics.check_pair(checklist, Pair(), symbol(currency='EUR'),
                           symbol(symbol='GC1226', currency='EUR'),
                           account_currency='USD')
    check = currency_check(checklist)
    assert check['status'] == WARN
    assert 'EUR' in check['message'] and 'USD' in check['message']


def test_the_same_currency_as_the_account_is_not_warned_about():
    """The control for the control."""
    checklist = diagnostics.Checklist()
    diagnostics.check_pair(checklist, Pair(), symbol(),
                           symbol(symbol='GC1226'), account_currency='USD')
    assert currency_check(checklist)['status'] == PASS
