"""A leg runner must not trade an account it was not configured for.

Live, twice: a leg runner whose own terminal was not up yet fell
through to "whatever terminal is already running", found the OTHER
leg's terminal, and traded that account. Both legs then hedged against
themselves on one login while every screen reported two.
"""

from okxtrader import diagnostics
from okxtrader.diagnostics import FAIL, PASS


def login_check(checklist):
    return [c for c in checklist.checks if c['name'] == 'Account login'][0]


def terminal(login):
    """A healthy terminal report — everything check_account needs to
    reach the login check, so the login is the only thing under test."""
    return {'library': True, 'terminal': True, 'path': r'C:\MT5-2',
            'running': True, 'connected': True, 'logged_in': True,
            'login': login, 'server': 'MentoMarkets-Server',
            'algo_trading': True, 'trade_allowed': True,
            'margin_mode': 'hedging'}


def test_diagnose_fails_when_the_terminal_is_on_another_account():
    """It reported whatever login it FOUND as a pass, so a leg attached
    to the other leg's terminal read "PASS — CONNECTED" while trading
    the wrong account."""
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegB', terminal(100006),
                              expect_login=100002)

    check = login_check(checklist)
    assert check['status'] == FAIL
    assert '100006' in check['message'] and '100002' in check['message']


def test_diagnose_passes_when_the_terminal_is_on_the_right_account():
    """The CONTROL. A check that failed either way would only teach the
    operator to ignore it."""
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegB', terminal(100002),
                              expect_login=100002)

    assert login_check(checklist)['status'] == PASS


def test_a_login_of_a_different_type_is_not_a_mismatch():
    """Config carries an int, MT5 answers with an int, and the webapp
    has handed both around as strings. Comparing them raw would fail a
    correctly configured account."""
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegB', terminal(100002),
                              expect_login='100002')

    assert login_check(checklist)['status'] == PASS


def test_an_account_with_no_configured_login_is_a_FAILURE():
    """This used to PASS, on purpose: "nothing configured is not a
    mismatch — an account may legitimately attach to whatever terminal
    is open".

    That is indefensible on a live account. Every other guard is
    written `if login and ...`, so a blank Login box skipped all of
    them: the leg attached to whichever terminal was open, traded THAT
    account, and the checklist reported it healthy because there was
    nothing configured to disagree with. A leg that cannot name its
    login cannot be checked against anything.
    """
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegB', terminal(100006))

    check = login_check(checklist)
    assert check['status'] == FAIL
    # And it names what it actually attached to, so the operator can
    # see whose account they were one click away from trading.
    assert '100006' in check['message']


class FakeInfo:
    def __init__(self, login):
        self.login = login
        self.server = 'MentoMarkets-Server'
        self.name = 'Trader'


class FakeMT5:
    """One terminal, signed into `login`, that answers every attach."""

    def __init__(self, login):
        self.login = login
        self.attaches = 0

    def initialize(self, **kwargs):
        self.attaches += 1
        return True

    def account_info(self):
        return FakeInfo(self.login)

    def shutdown(self):
        pass

    def last_error(self):
        return (-2, 'no terminal')


def broker_for(monkeypatch, terminal_login, configured_login):
    from okxtrader import broker as broker_mod
    from okxtrader.config import AccountConfig
    fake = FakeMT5(terminal_login)
    monkeypatch.setattr(broker_mod, 'mt5', fake)
    account = AccountConfig('LegB', terminal_path=r'C:\MT5-2',
                            login=configured_login,
                            server='MentoMarkets-Server')
    return broker_mod.BrokerSession(account), fake


def test_the_runner_refuses_a_terminal_on_another_account(monkeypatch):
    """The whole failure, in one line: it used to WARN and connect, so
    the leg traded the other leg's account all session."""
    broker, fake = broker_for(monkeypatch, 100006, 100002)

    assert broker.initialize() is False
    assert broker.connected is False
    assert fake.attaches > 0, 'it never even tried — the test proves nothing'


def test_the_runner_connects_when_the_terminal_is_the_right_one(monkeypatch):
    """The CONTROL. A refusal that fired either way would just be a leg
    runner that never starts."""
    broker, _ = broker_for(monkeypatch, 100002, 100002)

    assert broker.initialize() is True
    assert broker.connected is True


def test_an_account_with_no_login_configured_does_NOT_attach(monkeypatch):
    """This was supported on purpose — a blank login meant "attach to
    whatever is open", for a single-account desk.

    It is withdrawn. On a two-legged hedge that convenience is how a
    leg ends up trading an account nobody chose, and it defeats every
    other guard at once: they are all written `if login and ...`.
    """
    broker, _ = broker_for(monkeypatch, 100006, None)

    assert broker.initialize() is False
    assert broker.connected is False


def test_a_leg_refuses_to_start_with_no_login_configured():
    """The runner's half of the same rule.

    `leg_runner.main()` exits when initialize() is False, so refusing
    here is refusing to trade at all — which is the only safe answer
    for a leg that cannot say which account it is for.
    """
    import types
    from okxtrader import broker as broker_mod

    account = types.SimpleNamespace(
        name='LegA', login=None, password='x', server='S',
        terminal_path=r'C:\MT5-15\terminal64.exe')
    reached = []

    class NeverCalled:
        def initialize(self, **kwargs):
            reached.append(kwargs)
            return True

        def shutdown(self):
            pass

    original = broker_mod.mt5
    broker_mod.mt5 = NeverCalled()
    try:
        session = broker_mod.BrokerSession(account)
        assert session.initialize() is False
        assert reached == [], (
            'it tried to attach to a terminal despite having no login '
            f'to check against: {reached}')
        assert session.connected is False
    finally:
        broker_mod.mt5 = original


def test_the_control_a_leg_WITH_a_login_still_starts():
    """Without this the refusal above passes on a broker that never
    connects to anything at all."""
    import types
    from okxtrader import broker as broker_mod

    account = types.SimpleNamespace(
        name='LegA', login=100015, password='x', server='S',
        terminal_path=r'C:\MT5-15\terminal64.exe')

    class Ok:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return FakeInfo(100015)

        def shutdown(self):
            pass

    original = broker_mod.mt5
    broker_mod.mt5 = Ok()
    try:
        session = broker_mod.BrokerSession(account)
        assert session.initialize() is True
        assert session.connected is True
    finally:
        broker_mod.mt5 = original
