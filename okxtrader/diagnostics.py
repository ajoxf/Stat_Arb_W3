"""The connectivity checklist behind the Exchanges page.

Three questions, in the order an operator actually asks them:

- **Connect** — is the leg runner there at all, and is its terminal
  attached and logged in?
- **Test** — can this account TRADE? Algo trading on, hedging mode,
  trading permitted, a price arriving.
- **Diagnose** — everything, including whether the two legs fit each
  other: symbols that exist and are priced, contract sizes the hedge
  arithmetic can use, volumes that can carry the configured clip, a
  futures contract that has not expired, and a beta stamped for the
  pair it is actually being used on.

Every check carries a FIX: the step that makes it pass. A checklist
that only says FAIL is a checklist that sends the operator to a forum.

Pure functions over the reports the legs hand back, so the whole thing
is testable without MT5.
"""

import time

from . import hedgeratio, sizing

PASS, WARN, FAIL, INFO = 'PASS', 'WARN', 'FAIL', 'INFO'
_RANK = {PASS: 0, INFO: 0, WARN: 1, FAIL: 2}


class Checklist:
    def __init__(self, clock=time.time):
        self.checks = []
        self.clock = clock

    def add(self, scope, name, status, message, fix=None):
        self.checks.append({'scope': scope, 'name': name, 'status': status,
                            'message': message, 'fix': fix or []})
        return status == PASS

    @property
    def overall(self):
        worst = max((_RANK[check['status']] for check in self.checks),
                    default=0)
        return {0: PASS, 1: WARN, 2: FAIL}[worst]

    def result(self):
        counts = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}
        for check in self.checks:
            counts[check['status']] += 1
        return {'checks': self.checks, 'overall': self.overall,
                'passed': counts[PASS], 'warnings': counts[WARN],
                'failed': counts[FAIL], 'info': counts[INFO],
                'ran_at': self.clock(),
                'ok': counts[FAIL] == 0}


def check_account(checklist, name, terminal, account=None, offset=None,
                  expect_login=None):
    """One account: is it there, logged in, and allowed to trade?

    This is what both Connect and Test run; Diagnose runs it too and
    then keeps going.
    """
    scope = name

    if terminal is None or terminal.get('error'):
        checklist.add(scope, 'Leg runner', FAIL,
                      (terminal or {}).get('error')
                      or 'the leg runner did not answer',
                      ['Start it: python run_leg.py --config config.json '
                       f'--account "{name}"',
                       'Check the endpoint on this row matches the port it '
                       'listens on'])
        return checklist

    if not terminal.get('library'):
        checklist.add(scope, 'MetaTrader5 package', FAIL,
                      'the MetaTrader5 Python package is not installed on '
                      'that machine',
                      ['It is Windows-only — install it on the box running '
                       'the terminals: py -3.11 -m pip install MetaTrader5'])
        return checklist
    checklist.add(scope, 'MetaTrader5 package', PASS, 'installed')

    if not terminal.get('terminal'):
        checklist.add(scope, 'MT5 terminal', FAIL,
                      'Python cannot reach a terminal',
                      ['Open the MT5 terminal for this account and log in',
                       'Start the terminal and Python the SAME way — a '
                       'terminal running as Administrator will not accept '
                       'a connection from a normally-started Python',
                       'Check the terminal path on this row points at THIS '
                       'account\'s installation'])
        return checklist
    checklist.add(scope, 'MT5 terminal', PASS,
                  terminal.get('terminal_path') or 'attached')

    if not terminal.get('terminal_connected', True):
        checklist.add(scope, 'Broker connection', FAIL,
                      'the terminal is open but not connected to the broker',
                      ['Check the connection status in the bottom-right of '
                       'the terminal', 'Check the machine is online'])
    else:
        ping = terminal.get('ping_ms')
        checklist.add(scope, 'Broker connection', PASS,
                      f'connected{f" — {ping:.0f}ms to the server" if ping else ""}')

    if not terminal.get('logged_in'):
        checklist.add(scope, 'Account login', FAIL,
                      'the terminal is attached but not logged in',
                      ['Log the terminal into this account',
                       'Check the login and server on this row'])
        return checklist
    live = terminal.get('login')
    if not expect_login:
        # An account that names no login cannot be checked against
        # anything, so it used to PASS on whatever login it found. That
        # is the one reading that must never be green: it is exactly
        # the state in which a leg trades an account nobody chose.
        checklist.add(
            scope, 'Account login', FAIL,
            f'no login is configured for this account — it is attached '
            f'to {live}, but nothing says that is the right one',
            ['Set this account\'s login on the Exchanges page',
             'Put its password in .env under the matching key',
             'A leg that cannot name its login cannot be checked '
             'against anything, and will trade whatever it attaches to'])
        # Deliberately NOT an early return, unlike the mismatch below.
        # There the terminal belongs to somebody else and the rest of
        # the list would describe the wrong account; here the terminal
        # is real and every other check still tells the operator
        # something true.
    elif expect_login and live and str(live) != str(expect_login):
        # This reported whatever login it FOUND as a pass, so a leg
        # attached to the other leg's terminal read "PASS — CONNECTED"
        # while trading the wrong account. The question is not "is a
        # login present" but "is it the one this account is for".
        checklist.add(
            scope, 'Account login', FAIL,
            f'this terminal is logged into {live}, but this account is '
            f'configured as {expect_login}',
            [f'Log this terminal into {expect_login}',
             'Or correct the login on the Exchanges page',
             'Two accounts need two MT5 installations, each with its own '
             'terminal_path — one terminal holds one login'])
        return checklist
    else:
        checklist.add(scope, 'Account login', PASS,
                      f"{live} on {terminal.get('server')}")

    # Algo Trading is a BUTTON in that terminal, and MT5 answers every
    # order with 10027 until it is on. Nothing else on the screen says
    # so, which is why it is a check of its own.
    if terminal.get('algo_trading'):
        checklist.add(scope, 'Algo Trading', PASS, 'on')
    else:
        checklist.add(scope, 'Algo Trading', FAIL,
                      'OFF — MT5 will refuse every order with '
                      '"10027 AutoTrading disabled by client"',
                      ['Press the Algo Trading button in THIS terminal\'s '
                       'toolbar (it turns green)'])

    if terminal.get('trade_allowed') is False:
        checklist.add(scope, 'Trading permission', FAIL,
                      'the broker has trading disabled on this account',
                      ['Ask the broker — an investor password or a '
                       'read-only account does this'])
    elif terminal.get('trade_allowed'):
        checklist.add(scope, 'Trading permission', PASS, 'allowed')

    # The SERVER's own algo switch, which is a different thing from the
    # button above and fails with a different code. The button is the
    # trader's (10027, "disabled by client"); this is the broker's
    # (10026, "disabled by server"), set per account on their side and
    # not fixable from this machine at all.
    #
    # It is checked because it was read and not checked: a freshly
    # opened account connects, reports its balance and quotes happily,
    # and then refuses the first LIVE order with 10026. Finding that
    # out by clicking on a real ladder is the worst possible time.
    expert = terminal.get('trade_expert')
    if expert is False:
        checklist.add(scope, 'Algo trading (server)', FAIL,
                      'the BROKER has algo trading off for this account — '
                      'every order comes back "10026 AutoTrading disabled '
                      'by server"',
                      ['Ask the broker to enable Expert Advisor / API '
                       'trading on THIS account number',
                       'It is set per account: an account that trades by '
                       'hand can still refuse every order sent by a '
                       'program',
                       'Nothing on this machine can turn it on — the Algo '
                       'Trading button is a different switch'])
    elif expert:
        checklist.add(scope, 'Algo trading (server)', PASS,
                      'the broker allows it on this account')

    hedging = terminal.get('hedging')
    if hedging is True:
        checklist.add(scope, 'Margin mode', PASS,
                      'hedging — closes target a position ticket, as this '
                      'system requires')
    elif hedging is False:
        checklist.add(scope, 'Margin mode', WARN,
                      'NETTING, not hedging. A close by ticket means '
                      'something different here, and two orders on one '
                      'symbol net off instead of standing side by side',
                      ['Ask the broker for a hedging account, or trade '
                       'this leg on one'])

    if account:
        # Equity, not balance: brokers often fund a demo with CREDIT,
        # and balance alone then reads as an empty account.
        equity = account.get('equity')
        checklist.add(scope, 'Account', INFO,
                      f"equity {equity:,.2f} {account.get('currency', '')}"
                      f" (balance {account.get('balance', 0):,.2f})"
                      if equity is not None else 'no figures returned')

    if offset is None:
        checklist.add(scope, 'Broker clock', WARN,
                      'not measured yet — the session cutoff will not fire '
                      'until it is',
                      ['Add a symbol to Market Watch in this terminal; the '
                       'clock is read from a live tick'])
    else:
        checklist.add(scope, 'Broker clock', PASS,
                      f'{offset / 3600.0:+.1f}h from this machine — the '
                      f'session cutoff runs on the broker\'s clock')
    return checklist


def check_symbol(checklist, scope, report, role=None):
    """One symbol on one account: does it exist, is it priced, and are
    its contract specs usable by the hedge arithmetic?"""
    label = f'Symbol {report.get("symbol")}' + (f' ({role})' if role else '')
    if not report.get('found'):
        checklist.add(scope, label, FAIL,
                      report.get('error') or 'not on this account',
                      ['Use Find on the pair to see what this broker calls '
                       'it — gold is XAUUSD, GOLD or XAUUSD.r depending on '
                       'the broker',
                       'If nothing there resembles it, this is probably the '
                       'wrong account for this leg'])
        return checklist
    checklist.add(scope, label, PASS,
                  report.get('description') or report['symbol'])

    if not report.get('visible'):
        checklist.add(scope, f'{label} in Market Watch', WARN,
                      'hidden — a symbol the terminal is not watching '
                      'answers with the last price it happened to have, '
                      'for as long as it stays hidden. The chart moves; '
                      'the API does not; the spread reads STALE while the '
                      'market runs. It is selected automatically on every '
                      'read now, but a terminal that keeps dropping it is '
                      'worth fixing at the source',
                      ['Add it to Market Watch in the terminal, and keep '
                       'it there'])

    levels = report.get('depth_levels')
    if levels:
        checklist.add(scope, f'{label} depth of market', PASS,
                      f'{levels} level(s) published — the ladder shows the '
                      f'size the two books can actually do')
    elif levels is not None:
        checklist.add(scope, f'{label} depth of market', INFO,
                      'none published for this symbol. That is the broker, '
                      'not this software: most retail CFD accounts publish '
                      'no book beyond the touch. The ladder leaves its size '
                      'columns empty rather than inventing a number from '
                      'one leg',
                      ['Check it yourself in MT5: right-click the symbol, '
                       'Depth of Market (Alt+B). What you see there is what '
                       'this can use'])

    if report.get('bid') and report.get('ask'):
        checklist.add(scope, f'{label} price', PASS,
                      f"{report['bid']} / {report['ask']}")
    else:
        checklist.add(scope, f'{label} price', FAIL,
                      'no bid/ask — nothing can be priced or sized on this '
                      'leg',
                      ['Check the market is open',
                       'Check the symbol is in Market Watch'])

    if not report.get('contract_size'):
        checklist.add(scope, f'{label} contract size', FAIL,
                      'MT5 did not report one, and the hedge arithmetic '
                      'cannot be done without it',
                      ['Check the symbol specification in the terminal'])
    else:
        checklist.add(scope, f'{label} contract size', PASS,
                      f"{report['contract_size']} per lot, minimum "
                      f"{report.get('volume_min')} lots, step "
                      f"{report.get('volume_step')}")

    # Does the money arithmetic hold on THIS instrument?
    #
    # Every figure this system prints — the ladder's per-tick value, a
    # position's P&L, break-even, the take-profit — is
    # `spread move x lots x contract_size`. That is only what the
    # TERMINAL will compute if one tick of price on one lot is worth
    # `contract_size x tick_size` in the account's own currency. MT5
    # publishes what it actually uses: `trade_tick_value`. Where the
    # two disagree, every number on the ladder is out by the ratio —
    # silently, and in the same direction all day.
    tick_size = report.get('tick_size') or 0.0
    tick_value = report.get('tick_value')
    contract = report.get('contract_size') or 0.0
    if tick_size and tick_value and contract:
        implied = float(tick_value) / float(tick_size)
        ratio = implied / contract
        if abs(ratio - 1.0) <= 0.01:
            checklist.add(scope, f'{label} money', PASS,
                          f'one tick on one lot is {tick_value:g} '
                          f'{report.get("currency") or ""} — the same '
                          f'{contract:g} per lot the sizing uses'.strip())
        else:
            checklist.add(
                scope, f'{label} money', FAIL,
                f'MT5 prices one tick of this symbol at {tick_value:g} per '
                f'lot, which is {implied:,.4f} per lot of price movement — '
                f'not the {contract:g} contract size every figure on this '
                f'ladder is computed from. P&L, break-even and the '
                f'take-profit would all read {ratio:,.3f}x the truth. The '
                f'usual cause is a symbol whose profit currency '
                f'({report.get("currency") or "unknown"}) is not the '
                f'account currency: MT5 converts, this does not',
                ['Check the symbol specification: contract size, tick '
                 'size and tick value',
                 'If the profit currency is not the account currency, this '
                 'pair is not safe to size here until that is handled'])
    elif contract and not tick_value:
        checklist.add(scope, f'{label} money', INFO,
                      'MT5 reported no tick value, so the contract size '
                      'cannot be cross-checked against what the terminal '
                      'actually computes profit from')

    if report.get('trade_allowed') is False:
        checklist.add(scope, f'{label} trading', FAIL,
                      'the broker has this symbol closed or close-only',
                      ['Check the session times for this symbol',
                       'Some brokers disable a future near expiry'])

    expiry = report.get('expiry') or 0
    if expiry:
        days = (expiry - time.time()) / 86400.0
        if days < 0:
            checklist.add(scope, f'{label} expiry', FAIL,
                          'this contract has EXPIRED',
                          ['Roll the pair onto the next contract month'])
        elif days < 5:
            checklist.add(scope, f'{label} expiry', WARN,
                          f'expires in {days:.1f} days',
                          ['Plan the roll — a position open at expiry is '
                           'the broker\'s decision, not yours'])
        else:
            checklist.add(scope, f'{label} expiry', INFO,
                          f'{days:.0f} days away')
    return checklist


def check_pair(checklist, pair, report_a, report_b,
               account_currency=None):
    """Do the two legs fit each other?

    The checks that only make sense across a pair: a beta stamped for
    THIS pair, a spread that is a difference between the two prices
    rather than one of them scaled, and a clip both legs can carry.
    """
    scope = f'Pair {pair.key}'
    if not (report_a.get('found') and report_b.get('found')):
        checklist.add(scope, 'Pair', FAIL,
                      'both legs must resolve before the pair can be checked',
                      ['Fix the symbols above'])
        return checklist

    price_a = _mid(report_a)
    price_b = _mid(report_b)
    beta = float(pair.hedge_ratio or 1.0)

    signature = hedgeratio.pair_signature(pair.symbol_a, pair.symbol_b)
    if pair.hedge_ratio_for == signature:
        checklist.add(scope, 'Hedge ratio', PASS,
                      f'beta {beta:g}, stamped for this pair')
    else:
        suggested, why = hedgeratio.suggest(pair.pair_type, price_a, price_b)
        checklist.add(scope, 'Hedge ratio', FAIL,
                      f'beta {beta:g} is stamped for '
                      f'"{pair.hedge_ratio_for or "no pair at all"}", not for '
                      f'{pair.symbol_a}/{pair.symbol_b}. A beta left behind '
                      f'by another instrument defines a spread that does '
                      f'not exist',
                      [f'Open the pair, press "Read both legs from MT5" and '
                       f'take the suggested beta'
                       + (f' ({suggested:g})' if suggested else ''),
                       why])

    if price_a and price_b:
        spread = price_b - beta * price_a
        broken = hedgeratio.implausible(beta, price_a, price_b, spread)
        if broken is None:
            checklist.add(scope, 'Spread', PASS,
                          f'{spread:,.4f} = {price_b:,.4f} - {beta:g} x '
                          f'{price_a:,.4f}')
        else:
            checklist.add(scope, 'Spread', FAIL,
                          f'{spread:,.2f} is not a difference between '
                          f'{price_a:,.4f} and {price_b:,.4f} — with this '
                          f'beta the "spread" is really one leg\'s own price',
                          ['Re-derive the hedge ratio for this pair'])

    # The spread is `P_B - beta x P_A`. Subtracting one price from
    # another only means anything if the two are quoted in the SAME
    # money — and the P&L multiplier `k = L_B x C_B` is leg B's units,
    # so a spread built across two currencies is priced in neither.
    # Nothing else on the screen would say so: the ladder would draw,
    # the clicks would fill, and every figure would be wrong by the
    # exchange rate.
    currency_a = (report_a.get('currency') or '').upper()
    currency_b = (report_b.get('currency') or '').upper()
    if currency_a and currency_b and currency_a != currency_b:
        checklist.add(scope, 'Currency', FAIL,
                      f'{pair.symbol_a} is priced in {currency_a} and '
                      f'{pair.symbol_b} in {currency_b}. The spread '
                      f'subtracts one from the other, so it is a number in '
                      f'neither currency — and every figure derived from '
                      f'it (P&L, break-even, the take-profit) is out by '
                      f'the exchange rate',
                      ['Pair two instruments quoted in the same currency',
                       'This system does not convert between them, and it '
                       'will not pretend to'])
    elif currency_a and currency_b:
        account = (account_currency or '').upper()
        if account and account != currency_a:
            checklist.add(scope, 'Currency', WARN,
                          f'both legs are priced in {currency_a}, but the '
                          f'account is in {account}. MT5 converts on the '
                          f'way to your balance; the figures on this '
                          f'ladder are in {currency_a} and are NOT '
                          f'converted',
                          ['Read the ladder\'s money figures as '
                           f'{currency_a}, and MT5\'s as {account}'])
        else:
            checklist.add(scope, 'Currency', PASS,
                          f'both legs priced in {currency_a}')

    contract_a = report_a.get('contract_size') or 0.0
    contract_b = report_b.get('contract_size') or 0.0
    # Both typed by the trader; a blank reads as 1.
    clip_a = float(pair.clip_lots_a or 1.0)
    clip_b = float(pair.clip_lots_b or 1.0)
    floor = sizing.minimum_notional(contract_a, contract_b, price_a, price_b,
                                    beta, report_a.get('volume_min'),
                                    report_b.get('volume_min'))
    # AN OVERRIDDEN CONTRACT SIZE, named. Every money figure on the
    # ladder runs through it, so a number that disagrees with the
    # broker's own is worth a person looking at once.
    for override, report, role, symbol in (
            (getattr(pair, 'contract_size_a', None), report_a, 'A',
             pair.symbol_a),
            (getattr(pair, 'contract_size_b', None), report_b, 'B',
             pair.symbol_b)):
        theirs = report.get('contract_size')
        if override and theirs and abs(float(override) - theirs) > 1e-9:
            checklist.add(
                scope, f'Leg {role} contract size', WARN,
                f'{symbol} is set to {float(override):g} per lot by hand, '
                f'but MT5 reports {theirs:g}. Every money figure on this '
                f'ladder — P&L, break-even, the take-profit — is computed '
                f'from the {float(override):g}',
                ['Clear the Contract override on the ladder to use MT5\'s',
                 'If the broker really is wrong, check the symbol spec '
                 'sheet and leave it set'])

    k = sizing.spread_units(clip_b, contract_b)
    checklist.add(scope, 'One Qty', PASS,
                  f'{clip_a:g} lots of {pair.symbol_a} against '
                  f'{clip_b:g} of {pair.symbol_b} — ${k:,.2f} per 1.00 '
                  f'of spread'
                  + (f', and this pair cannot trade under ${floor:,.0f} '
                     f'a leg' if floor else ''))
    for lots, report, role, symbol in ((clip_a, report_a, 'A', pair.symbol_a),
                                       (clip_b, report_b, 'B', pair.symbol_b)):
        minimum = report.get('volume_min') or 0.0
        if minimum and lots < minimum - 1e-9:
            checklist.add(scope, f'Leg {role} lots', FAIL,
                          f'one Qty is {lots:g} lots of {symbol}, under the '
                          f'{minimum:g}-lot minimum this broker will trade',
                          [f'Raise Leg {role} lots to {minimum:g} or more, '
                           f'or click a bigger Qty'])

    for report, role in ((report_a, 'A'), (report_b, 'B')):
        maximum = report.get('volume_max') or 0.0
        wanted = clip_a if role == 'A' else clip_b
        if maximum and wanted and wanted > maximum:
            checklist.add(scope, f'Leg {role} size', FAIL,
                          f'one spread needs {wanted:g} lots but the broker '
                          f'caps this symbol at {maximum:g}',
                          ['Check the hedge ratio — a beta that is too '
                           'small inflates the hedge leg'])
    return checklist


def _mid(report):
    bid, ask = report.get('bid'), report.get('ask')
    return ((bid + ask) / 2.0) if (bid and ask) else None
