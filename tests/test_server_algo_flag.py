"""The broker's own algo switch, which is not the button on the toolbar.

Two different switches answer with two different codes, and only one of
them can be fixed from this machine:

    10027  AutoTrading disabled by CLIENT  — the toolbar button
    10026  AutoTrading disabled by SERVER  — the broker, per account

The second was READ from MT5 (`account_info().trade_expert`) and never
checked. So a freshly opened account connected, reported its balance,
quoted happily, passed every startup check — and then refused the first
LIVE order with 10026. Finding that out by clicking on a real ladder is
the worst possible moment.
"""

from okxtrader import diagnostics
from okxtrader.diagnostics import FAIL, PASS


def named(checklist, name):
    found = [c for c in checklist.checks if c['name'] == name]
    return found[0] if found else None


def terminal(**over):
    """A terminal report healthy in every way but the one under test."""
    report = {'library': True, 'terminal': True, 'path': r'C:\MT5-15',
              'running': True, 'connected': True, 'logged_in': True,
              'login': 100015, 'server': 'MentoMarkets-Server',
              'algo_trading': True, 'trade_allowed': True,
              'trade_expert': True, 'margin_mode': 'hedging'}
    report.update(over)
    return report


def run(**over):
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegA', terminal(**over))
    return checklist


def test_the_broker_refusing_algo_trading_is_a_startup_failure():
    check = named(run(trade_expert=False), 'Algo trading (server)')
    assert check is not None, 'the server-side switch is not checked at all'
    assert check['status'] == FAIL
    assert '10026' in check['message']


def test_the_control_an_account_the_broker_allows_passes():
    """Without this the test above passes on a check that always fails."""
    check = named(run(trade_expert=True), 'Algo trading (server)')
    assert check is not None
    assert check['status'] == PASS


def test_the_two_switches_are_told_apart():
    """The toolbar button and the broker's flag are different checks
    with different codes. Reporting one as the other sends the trader
    to press a button that was never the problem."""
    both_off = run(algo_trading=False, trade_expert=False)
    client = named(both_off, 'Algo Trading')
    server = named(both_off, 'Algo trading (server)')
    assert client['status'] == FAIL and server['status'] == FAIL
    assert '10027' in client['message']
    assert '10026' in server['message']

    # And the button being ON does not excuse the broker's flag.
    button_on = run(algo_trading=True, trade_expert=False)
    assert named(button_on, 'Algo Trading')['status'] == PASS
    assert named(button_on, 'Algo trading (server)')['status'] == FAIL


def test_the_remedy_says_it_cannot_be_fixed_from_this_machine():
    """A refusal carries what to DO about it. Pressing the toolbar
    button will not fix 10026, and the checklist has to say so or the
    trader spends the session pressing it."""
    check = named(run(trade_expert=False), 'Algo trading (server)')
    remedy = ' '.join(check.get('fix') or [])
    assert 'broker' in remedy.lower()
    assert 'account' in remedy.lower()


def test_an_unreported_flag_is_not_a_failure():
    """UNMEASURED IS NOT ZERO. An older report with no trade_expert key
    must not be read as "the broker has it off" — that would fail every
    account on a terminal that never reported it."""
    report = terminal()
    report.pop('trade_expert')
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegA', report)
    assert named(checklist, 'Algo trading (server)') is None
