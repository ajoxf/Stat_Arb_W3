"""The connectivity checklist behind the Exchanges page.

Two questions, in the order an operator actually asks them:

- **Test** — can this OKX account TRADE? OKX reachable, the key
  accepted and allowed to trade, long/short position mode, an account
  mode that can trade derivatives and short spot (`check_okx_account`).
- **Diagnose** — everything, including whether the two legs fit each
  other: symbols that exist and are priced, contract sizes the hedge
  arithmetic can use, volumes that can carry the configured clip, a
  futures contract that has not expired, and a beta stamped for the
  pair it is actually being used on.

Every check carries a FIX: the step that makes it pass. A checklist
that only says FAIL is a checklist that sends the operator to a forum.

Pure functions over the reports the legs hand back, so the whole thing
is testable without OKX.
"""

import time

from . import hedgeratio, sizing

PASS, WARN, FAIL, INFO = 'PASS', 'WARN', 'FAIL', 'INFO'
_RANK = {PASS: 0, INFO: 0, WARN: 1, FAIL: 2}

#: Dollars and the dollar stablecoins OKX quotes in: one money here.
USD_LIKE = {'USD', 'USDT', 'USDC'}


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
        checklist.add(scope, f'{label} listed on OKX', WARN,
                      'hidden — an instrument OKX is not quoting '
                      'answers with the last price it happened to have, '
                      'for as long as it stays hidden. The chart moves; '
                      'the API does not; the spread reads STALE while the '
                      'market runs. It is selected automatically on every '
                      'read now, but a feed that keeps dropping it is '
                      'worth fixing at the source',
                      ['Check it is still listed on OKX, and keep '
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
                      ['Check the order book on the instrument\'s OKX '
                       'trading page. What you see there is what this can '
                       'use'])

    if report.get('bid') and report.get('ask'):
        checklist.add(scope, f'{label} price', PASS,
                      f"{report['bid']} / {report['ask']}")
    else:
        checklist.add(scope, f'{label} price', FAIL,
                      'no bid/ask — nothing can be priced or sized on this '
                      'leg',
                      ['Check the market is open',
                       'Check the instrument is still listed on OKX'])

    if not report.get('contract_size'):
        checklist.add(scope, f'{label} contract size', FAIL,
                      'OKX did not report one, and the hedge arithmetic '
                      'cannot be done without it',
                      ['Check the instrument\'s specification on OKX'])
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
                f'OKX prices one tick of this symbol at {tick_value:g} per '
                f'lot, which is {implied:,.4f} per lot of price movement — '
                f'not the {contract:g} contract size every figure on this '
                f'ladder is computed from. P&L, break-even and the '
                f'take-profit would all read {ratio:,.3f}x the truth. The '
                f'usual cause is a symbol whose profit currency '
                f'({report.get("currency") or "unknown"}) is not the '
                f'account currency: OKX converts, this does not',
                ['Check the symbol specification: contract size, tick '
                 'size and tick value',
                 'If the profit currency is not the account currency, this '
                 'pair is not safe to size here until that is handled'])
    elif contract and not tick_value:
        checklist.add(scope, f'{label} money', INFO,
                      'OKX reported no tick value, so the contract size '
                      'cannot be cross-checked against what OKX '
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
                      [f'Open the pair, press "Read both legs from OKX" and '
                       f'take the suggested beta'
                       + (f' ({suggested:g})' if suggested else ''),
                       why])

    if price_a and price_b:
        spread = price_b - beta * price_a
        broken = hedgeratio.implausible(beta, price_a, price_b, spread)
        suggested_now, _ = hedgeratio.suggest(pair.pair_type, price_a,
                                              price_b)
        if broken is None:
            checklist.add(scope, 'Spread', PASS,
                          f'{spread:,.4f} = {price_b:,.4f} - {beta:g} x '
                          f'{price_a:,.4f}')
        else:
            checklist.add(scope, 'Spread', FAIL,
                          f'{spread:,.2f} is not a difference between '
                          f'{price_a:,.4f} and {price_b:,.4f} — with this '
                          f'beta the "spread" is really one leg\'s own price',
                          [f'Open the pair, press "Read both legs from OKX" '
                           f'and Save to take the suggested beta'
                           + (f' ({suggested_now:g})' if suggested_now
                              else ''),
                           'The beta is stale: the two prices have moved '
                           'a long way since it was set'])

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
        if (account and account != currency_a
                and {account, currency_a} <= USD_LIKE):
            # OKX states every account's equity in USD; legs in USDT or
            # USDC are the same money to within the peg. Not a warning.
            checklist.add(scope, 'Currency', PASS,
                          f'both legs priced in {currency_a}; OKX shows '
                          f'your equity in {account} (1 {currency_a} is '
                          f'about 1 {account})')
        elif account and account != currency_a:
            checklist.add(scope, 'Currency', WARN,
                          f'both legs are priced in {currency_a}, but the '
                          f'account is in {account}. OKX converts on the '
                          f'way to your balance; the figures on this '
                          f'ladder are in {currency_a} and are NOT '
                          f'converted',
                          ['Read the ladder\'s money figures as '
                           f'{currency_a}, and OKX\'s as {account}'])
        else:
            checklist.add(scope, 'Currency', PASS,
                          f'both legs priced in {currency_a}')

    # Leverage is set PER INSTRUMENT on OKX. Two different numbers on
    # the two legs of one hedge make each leg tie up a different share
    # of margin - and the take-profit and stop, being a % of that
    # margin, sit somewhere neither leg's leverage alone would put them.
    lev_a, lev_b = report_a.get('leverage'), report_b.get('leverage')
    if lev_a and lev_b:
        if abs(float(lev_a) - float(lev_b)) > 1e-9:
            checklist.add(scope, 'Leverage', WARN,
                          f'{pair.symbol_a} is at {float(lev_a):g}x and '
                          f'{pair.symbol_b} at {float(lev_b):g}x on OKX. '
                          f'One hedge, two leverages: the margin, and the '
                          f'TP and stop sized from it, are a mix of both',
                          [f'On OKX set both to the same leverage (Cross): '
                           f'open each instrument\'s trading page and '
                           f'click the leverage figure',
                           'The desk reads the new value within 5 minutes'])
        else:
            checklist.add(scope, 'Leverage', PASS,
                          f'both legs at {float(lev_a):g}x (cross)')

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
                f'but OKX reports {theirs:g}. Every money figure on this '
                f'ladder — P&L, break-even, the take-profit — is computed '
                f'from the {float(override):g}',
                ['Clear the Contract override on the ladder to use OKX\'s',
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


# -- OKX --------------------------------------------------------------------

#: OKX's own codes for a key that is wrong, and what to do about each.
OKX_KEY_FIXES = {
    '50101': ['This key is for the OTHER environment: a demo-trading key '
              'only works with Demo on, a live key only with it off',
              'Flip Demo / Live on this row, or create the key in the '
              'other environment on OKX'],
    '50102': ['The request timestamp was refused - this PC\'s clock is '
              'off. Turn on automatic time sync in the system settings'],
    '50103': ['The API key is missing - paste it into this row'],
    '50105': ['The passphrase is wrong - it is the one you typed when '
              'creating THIS API key on OKX, not your login password'],
    '50110': ['OKX refused this PC\'s IP address - add it to the key\'s IP '
              'whitelist on OKX, or create a key without one'],
    '50111': ['OKX does not know this API key - check it was pasted whole, '
              'and that it was not deleted on OKX'],
    '50113': ['The signature did not match - the SECRET KEY is wrong. '
              'Paste it again (it is shown only once, when the key is '
              'created; if it is lost, create a new key)'],
}

ACCT_LEVELS = {'1': 'Spot mode', '2': 'Futures mode (spot and futures)',
               '3': 'Multi-currency margin', '4': 'Portfolio margin'}


def check_okx_account(checklist, name, broker, has_keys=True):
    """One OKX account: reachable, the key accepted, and set up the way
    this desk trades - Trade permission, long/short position mode, an
    account mode that can short spot and trade derivatives.

    What Connect, Test and Diagnose all run for an OKX account. Every
    failure names the switch on OKX, in OKX's words."""
    scope = name
    where = 'OKX demo trading' if broker.demo else 'OKX LIVE'
    try:
        offset = broker.client.sync_clock()
    except Exception as exc:                      # OKXError or network
        checklist.add(scope, 'OKX reachable', FAIL, str(exc),
                      ['Check this PC is online and can reach www.okx.com',
                       'A firewall or VPN can block it'])
        return checklist
    checklist.add(scope, 'OKX reachable', PASS,
                  f'{where} answered; this PC\'s clock is '
                  f'{abs(offset) * 1000:.0f} ms '
                  f'{"behind" if offset > 0 else "ahead of"} OKX\'s')
    if abs(offset) > 5:
        checklist.add(scope, 'Clock', WARN,
                      f'{offset:+.1f}s from OKX - OKX refuses a request '
                      f'more than 30s off', ['Turn on automatic time sync'])
    if not has_keys:
        checklist.add(scope, 'API key', FAIL,
                      'no API key, secret and passphrase saved for this '
                      'account',
                      ['Create an API key on OKX (Profile > API) with READ '
                       'and TRADE permission - never Withdraw',
                       'Paste the key, secret and passphrase into this row '
                       'and Save: they go to .env, never to the config'])
        return checklist
    try:
        config = broker.client.get('/api/v5/account/config', private=True)[0]
    except Exception as exc:
        code = getattr(exc, 'code', None) or getattr(exc, 's_code', None)
        checklist.add(scope, 'API key', FAIL, str(exc),
                      OKX_KEY_FIXES.get(str(code),
                                        ['Check the key, secret and '
                                         'passphrase on this row']))
        return checklist
    broker._config = config
    checklist.add(scope, 'API key', PASS,
                  f"accepted - account UID {config.get('uid')} on {where}")
    perm = config.get('perm') or ''
    if 'trade' in perm:
        checklist.add(scope, 'Trade permission', PASS, perm)
    else:
        checklist.add(scope, 'Trade permission', FAIL,
                      f'this key is {perm or "read-only"} - every order '
                      f'will be refused',
                      ['On OKX: Profile > API > edit this key > tick Trade'])
    if 'withdraw' in perm:
        checklist.add(scope, 'Withdraw permission', WARN,
                      'this key can WITHDRAW funds - a trading desk never '
                      'needs that', ['Create a key with Read and Trade only'])
    if config.get('posMode') == 'long_short_mode':
        checklist.add(scope, 'Position mode', PASS,
                      'long/short - each ticket is closed on its own side')
    else:
        checklist.add(scope, 'Position mode', FAIL,
                      'net mode - an opposite order NETS instead of closing '
                      'the ticket it was meant for',
                      ['On OKX: Trade > Settings > Position mode > '
                       'Long/short mode (with no open positions)'])
    level = str(config.get('acctLv') or '')
    words = ACCT_LEVELS.get(level, f'account level {level}')
    if level == '1':
        checklist.add(scope, 'Account mode', FAIL,
                      f'{words}: no perpetuals or futures, and no spot '
                      f'short', ['On OKX: Trade > Settings > Account mode > '
                                 'Single-currency or Multi-currency margin'])
    elif level == '2':
        checklist.add(scope, 'Account mode', WARN,
                      f'{words}: derivatives yes, but spot cannot be sold '
                      f'short - a SELL on a spot leg needs the coin',
                      ['Multi-currency margin lets the spot leg borrow: on OKX, '
                       'Trade > the "Futures mode" panel > Account mode > '
                       'Multi-currency margin (with nothing open)'])
    else:
        checklist.add(scope, 'Account mode', PASS, words)
    try:
        maker, taker = broker.fees_bps('BTC-USDT-SWAP') or (None, None)
    except Exception:
        maker = taker = None
    if maker is not None:
        checklist.add(scope, 'Fee tier', INFO,
                      f"{config.get('level') or 'VIP ?'}: perpetuals "
                      f"{maker:.1f} bp maker / {taker:.1f} bp taker - what "
                      f"B/E and TP are priced from")
    info = broker.account_info()
    if info is not None:
        checklist.add(scope, 'Equity', PASS if info.equity > 0 else WARN,
                      f'{info.equity:,.2f} USD equity, '
                      f'{info.margin_free:,.2f} free')
    return checklist
