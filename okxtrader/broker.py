"""Broker session: the only module that touches the MetaTrader5 package.

HARD CONSTRAINT: the MetaTrader5 Python package holds ONE global
connection per process. Initializing a second session replaces the
first. True simultaneous streaming from two accounts therefore
requires one process per account plus a coordinator — which is why
this product runs one leg runner per account (see the spec, section 1).
This class makes the connection explicit (path/login/server from
config) so each such process can be pointed at its own terminal.

Ported from the stat-arb system, where every quirk in here was paid
for on a live account: 10015 invalid price, the filling-mode bitmask,
10027 AutoTrading disabled, deal history lagging positions_get, re-peg
by MODIFY rather than cancel-and-replace, and closes that target a
position TICKET because these accounts are hedging mode.
"""

import logging
import time
from datetime import datetime, timedelta

from . import mt5_errors
from .models import MAGIC_NUMBER, OrderSide

try:
    import MetaTrader5 as mt5
except ImportError:  # not available off-Windows; tests use FakeBroker
    mt5 = None


#: MT5's own return codes for a request the broker ACCEPTED, and the
#: whole of the orphan-leg fault.
#:
#:   10008 PLACED       - accepted, not executed yet
#:   10009 DONE         - executed in full
#:   10010 DONE_PARTIAL - executed in PART. A POSITION IS OPEN for the
#:                        part that filled.
#:
#: Every send in here compared against DONE alone, so 10010 was
#: reported upwards as a flat failure. `PairExecutor.market_entry`
#: reads a failed FIRST leg as "nothing is on. This is a refusal, not a
#: naked position", returns, and never sends leg B - so a partially
#: filled leg A stayed at the broker, hedged by nothing, absent from
#: the book, and surfaced in the reconciler as a position carrying our
#: own comment that nothing is allowed to close. Two desks, live
#: 2026-09-09: leg A on, leg B never sent, three positions nobody could
#: get rid of.
#:
#: Numbers rather than mt5.TRADE_RETCODE_*: they are fixed by the
#: protocol, and this module has to answer the same way on a box where
#: the package is not installed at all.
RETCODE_PLACED = 10008
RETCODE_DONE = 10009
RETCODE_DONE_PARTIAL = 10010
RETCODE_INVALID_FILL = 10030

#: The request EXECUTED - in full or in part. Either way money moved
#: and there is a position to account for.
FILLED_RETCODES = (RETCODE_DONE, RETCODE_DONE_PARTIAL)

#: A pending order the broker is now holding. MT5 answers PLACED for a
#: resting order and DONE only where it filled on arrival; both mean
#: the order reached the book, and reading PLACED as a failure left
#: real orders resting that this system had written off.
RESTED_RETCODES = (RETCODE_PLACED, RETCODE_DONE)


class OrderResult:
    """Outcome of a market order, decoupled from mt5 result objects."""

    def __init__(self, success, requested_price=None, executed_price=None,
                 ticket=None, error=None, volume=0.0):
        self.success = success
        self.requested_price = requested_price
        self.executed_price = executed_price
        self.ticket = ticket
        self.error = error
        self.volume = volume  # filled volume (IOC may partially fill)


class BrokerSession:
    """One MT5 terminal connection for one account."""

    def __init__(self, account):
        self.account = account
        self.connected = False
        #: symbol -> whether this terminal has a DOM for it. The
        #: subscription is made once; a refusal is remembered too, so a
        #: broker with no depth is not asked three times a second.
        self._depth_symbols = {}
        #: symbol -> was it in Market Watch when we last read a tick?
        self.last_visible = {}

    def initialize(self):
        """Connect this process to one MT5 terminal.

        Attempts, in order (first success wins):
        1. path + credentials — launch/attach a SPECIFIC terminal
           installation (needed when two brokers run side by side);
        2. credentials only — attach to the terminal that is ALREADY
           OPEN and log into this account;
        3. bare attach — use whatever terminal is open and logged in.

        The fallbacks matter: launching terminal64.exe from Python is
        brittle (wrong path, portable mode, auto-login disabled),
        while attaching to a terminal the operator already opened is
        the pattern that works reliably in practice.
        """
        if mt5 is None:
            logging.error(
                "MetaTrader5 package not installed (Windows-only). "
                "Install it on the trading machine.")
            return False

        # NO LOGIN IS NOT "ANY LOGIN".
        #
        # Every guard below — the wrong-login refusal, the diagnostics
        # login check — is written `if self.account.login and ...`, so
        # an account row saved with the Login box empty skipped all of
        # them and attached to whatever terminal happened to be open.
        # It then traded THAT account, and both the runner and the
        # startup checklist reported it as healthy, because there was
        # nothing configured to disagree with.
        #
        # This used to be deliberate ("an account may legitimately
        # attach to whatever terminal is open"). It is not defensible
        # on a live account: a leg that cannot name the login it is
        # for cannot be checked against anything, and the failure is
        # silent and total. An account states its login or it does not
        # trade.
        if not self.account.login:
            logging.error(
                "[%s] refusing to run: no login is configured for this "
                "account, so it would attach to whichever terminal is "
                "open and trade that account. Set the login on the "
                "Exchanges page (and its password in .env).",
                self.account.name)
            return False

        credentials = {}
        if self.account.login:
            credentials = {'login': self.account.login,
                           'password': self.account.password or "",
                           'server': self.account.server or ""}

        attempts = []
        if self.account.terminal_path:
            attempts.append(("terminal path + credentials",
                             dict(credentials,
                                  path=self.account.terminal_path)))
        else:
            # ATTACH FIRST, log in only if the terminal is on the wrong
            # account. `initialize(login=...)` makes the terminal
            # re-authenticate even when it is already logged into that
            # very account, and a re-login drops Market Watch and
            # interrupts the feed for a moment. With two runners
            # attached to one terminal that happens twice a start, and
            # the symptom is a spread that ticks for a few seconds and
            # then reads stale again.
            attempts.append(("running terminal (already logged in)", {}))
        if credentials:
            attempts.append(("running terminal + credentials",
                             dict(credentials)))
        if self.account.terminal_path:
            attempts.append(("running terminal (already logged in)", {}))

        # A terminal we reached that was signed into somebody else.
        # Kept so the final refusal can name it rather than reporting
        # the last attempt's own, unrelated fault.
        wrong_login = None

        for label, kwargs in attempts:
            # Announce BEFORE the call, at INFO. mt5.initialize(path=)
            # launches a terminal and waits for it to log in, so it can
            # block for a long time or forever — and until it returns
            # there is nothing on screen at all. Live 2026-08-11 the
            # second leg runner logged "Configuration loaded" and then
            # went silent, while the console showed only a coordinator
            # restart loop with no hint of which leg was stuck or why.
            logging.info("[%s] connecting to MT5 via %s%s", self.account.name,
                         label,
                         (f" ({self.account.terminal_path})"
                          if kwargs.get('path') else ''))
            try:
                ok = mt5.initialize(**kwargs)
            except Exception as e:                      # bad path types etc
                logging.debug("MT5 initialize(%s) raised: %s", label, e)
                ok = False
            if not ok:
                # Release before trying the next form. A failed
                # initialize can still leave the library half-attached
                # to a terminal, and the next attempt then reports a
                # fault belonging to the previous one — which makes the
                # attempt list actively misleading rather than
                # progressively more forgiving.
                try:
                    mt5.shutdown()
                except Exception:
                    pass
            if ok:
                info = mt5.account_info()
                if self.account.login and info \
                        and info.login != self.account.login:
                    # Attached to a terminal signed into someone else.
                    # LET GO. This used to hold only for the uncredentialed
                    # attach and merely WARN for the rest, so a leg runner
                    # whose terminal was not up yet fell through to
                    # "whatever is already running", found the OTHER leg's
                    # terminal, and traded that account all session. Both
                    # legs then hedged against themselves on one login
                    # while every screen reported two.
                    logging.info(
                        "[%s] the terminal reached via %s is logged into "
                        "%s, not %s — letting go", self.account.name, label,
                        info.login, self.account.login)
                    wrong_login = info.login
                    try:
                        mt5.shutdown()
                    except Exception:
                        pass
                    continue
                self.connected = True
                if info:
                    logging.info("Connected [%s] via %s: %s / %s (login %s)",
                                 self.account.name, label, info.server,
                                 info.name, info.login)
                else:
                    logging.info("Connected [%s] via %s",
                                 self.account.name, label)
                return True
            logging.debug("MT5 initialize failed (%s) for '%s': %s",
                          label, self.account.name, mt5.last_error())

        if wrong_login is not None:
            # Every terminal we could reach was signed into someone
            # else. Say THAT, with both numbers: the last attempt's own
            # error describes a door that was never the problem, and it
            # is what sent the operator round the settings for an hour
            # last time. Refusing is the point — a leg runner on the
            # wrong account trades that account, and nothing downstream
            # can tell.
            logging.error(
                "[%s] refusing to run: every terminal reachable for this "
                "account is logged into %s, but the config says %s. Open "
                "the terminal for %s, log it in, and start again — or fix "
                "the login on the Exchanges page.",
                self.account.name, wrong_login, self.account.login,
                self.account.login)
            logging.error("    fix: log terminal %s into account %s",
                          self.account.terminal_path or '(whichever opens)',
                          self.account.login)
            logging.error("    fix: start BOTH terminals before the "
                          "launcher, so neither leg has to guess")
            return False

        # Decode it rather than printing the raw tuple. -6 in
        # particular is not always a typo: live 2026-08-11 the terminal
        # journal read "authorization ... failed (Invalid account)" for
        # a login that simply did not exist on that server, and the
        # generic "check login/server/password" sent the operator round
        # the settings for an hour.
        error = mt5.last_error()
        summary, fixes = mt5_errors.explain(error)
        logging.error("MT5 connection failed for account '%s': %s%s",
                      self.account.name, error,
                      f' — {summary}' if summary else '')
        for fix in (fixes or [
                'Open the MT5 terminal for this account and log in '
                '(then a blank terminal path is fine)',
                'Or set the correct path to terminal64.exe in Settings',
                'Or check login / server / password']):
            logging.error("    fix: %s", fix)
        return False

    def shutdown(self):
        if mt5 is not None:
            for symbol, subscribed in list(self._depth_symbols.items()):
                if subscribed:
                    mt5.market_book_release(symbol)
            self._depth_symbols.clear()
            mt5.shutdown()
        self.connected = False

    def is_alive(self):
        return mt5 is not None and mt5.terminal_info() is not None

    def account_info(self):
        return mt5.account_info() if mt5 else None

    def symbol_info(self, symbol):
        return mt5.symbol_info(symbol) if mt5 else None

    def ensure_symbol(self, symbol):
        """Return symbol info, selecting it into Market Watch if hidden."""
        info = self.symbol_info(symbol)
        if info and not info.visible:
            mt5.symbol_select(symbol, True)
        return info

    def resubscribe(self, symbol):
        """Drop a symbol from Market Watch and take it back.

        The one thing that reliably restarts a feed the terminal has
        gone quiet on: MT5 re-subscribes on select, and a symbol that
        was answering with a price from twenty minutes ago starts
        ticking again. Returns the tick that came back, so the caller
        can say whether it worked rather than claim it did.
        """
        if mt5 is None:
            return None
        mt5.symbol_select(symbol, False)
        mt5.symbol_select(symbol, True)
        return mt5.symbol_info_tick(symbol)

    def symbol_tick(self, symbol):
        """The last tick — from a symbol that is IN Market Watch.

        A symbol the terminal is not subscribed to still answers
        `symbol_info_tick`: it answers with the last value it happened
        to have, for ever. The chart in front of the trader updates, the
        API returns the same bid and ask for twenty-five minutes, and
        this system correctly calls its own feed stale while the market
        moves. Selecting it is idempotent and costs a local lookup, so
        it is done on the read rather than hoped for at startup — a
        terminal can drop a symbol from Market Watch at any time, and
        the second account attached to one terminal does exactly that
        when it switches login.
        """
        if mt5 is None:
            return None
        info = self.ensure_symbol(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None:
            # Carried with the price so the screen can say WHY a leg
            # looks frozen: a symbol that is not visible is not
            # subscribed, and a tick whose own stamp is not advancing
            # is the terminal receiving nothing — two different faults
            # with two different fixes.
            self.last_visible[symbol] = bool(getattr(info, 'visible', False))
        return tick

    def find_symbols(self, pattern, limit=40):
        """Symbols on THIS broker whose name or description matches —
        brokers name the same instrument differently (XAUUSD, GOLD,
        XAUUSD.r), so the operator needs to search rather than guess."""
        if mt5 is None:
            return []
        needle = (pattern or '').strip().upper()
        found = []
        for info in (mt5.symbols_get() or ()):
            name = info.name.upper()
            description = (getattr(info, 'description', '') or '').upper()
            if needle and needle not in name and needle not in description:
                continue
            found.append({
                'symbol': info.name,
                'description': getattr(info, 'description', ''),
                'path': getattr(info, 'path', ''),
                'visible': bool(info.visible),
                'contract_size': getattr(info, 'trade_contract_size', None),
                'volume_min': getattr(info, 'volume_min', None),
                'volume_max': getattr(info, 'volume_max', None),
                'volume_step': getattr(info, 'volume_step', None),
                'currency': getattr(info, 'currency_profit', ''),
                'expiry': getattr(info, 'expiration_time', 0),
            })
            if len(found) >= limit:
                break
        return found

    def margin_for(self, symbol, side, volume, price=None):
        """Margin for `volume` lots of `symbol`, as THIS terminal
        computes it.

        Asked of MT5 rather than derived from notional: margin depends
        on the broker's own leverage, the symbol's margin mode and the
        account's group, and a number computed here would be a guess
        presented as a figure. None when it cannot be computed — the
        screen then shows an em dash rather than a target built on
        nothing.
        """
        if mt5 is None:
            return None
        info = self.ensure_symbol(symbol)
        if info is None:
            return None
        tick = mt5.symbol_info_tick(symbol)
        if price is None:
            price = (tick.ask if str(side).upper() == 'BUY' else tick.bid) \
                if tick else None
        if not price:
            return None
        action = (mt5.ORDER_TYPE_BUY if str(side).upper() == 'BUY'
                  else mt5.ORDER_TYPE_SELL)
        try:
            return mt5.order_calc_margin(action, symbol, float(volume),
                                         float(price))
        except Exception:
            return None

    def depth(self, symbol):
        """This symbol's market depth, or None when the broker has none.

        MT5 only fills a DOM for symbols it is subscribed to, and the
        subscription is per symbol and per terminal — so it is made once
        here and remembered. Most CFD accounts publish no depth at all;
        that comes back as None, and the ladder shows nothing rather
        than inventing a size from the tick volume.
        """
        if mt5 is None:
            return None
        if symbol not in self._depth_symbols:
            if not mt5.market_book_add(symbol):
                # Remember the refusal too: asking three times a second
                # for a book this broker does not have is a round trip
                # per poll for nothing.
                self._depth_symbols[symbol] = False
            else:
                self._depth_symbols[symbol] = True
        if not self._depth_symbols.get(symbol):
            return None
        rows = mt5.market_book_get(symbol)
        if not rows:
            return None
        out = []
        for row in rows:
            kind = getattr(row, 'type', None)
            # 1 = BOOK_TYPE_SELL (an offer), 2 = BOOK_TYPE_BUY (a bid);
            # the _MARKET variants are 3 and 4.
            side = ('ask' if kind in (1, 3) else
                    'bid' if kind in (2, 4) else None)
            if side is None:
                continue
            out.append({'type': side,
                        'price': float(getattr(row, 'price', 0.0) or 0.0),
                        'volume': float(getattr(row, 'volume_real', 0.0)
                                        or getattr(row, 'volume', 0.0)
                                        or 0.0)})
        return out or None

    #: Minutes -> MT5's own timeframe constant name. Only these: they are
    #: the candles the Algo offers.
    TIMEFRAMES = {1: 'TIMEFRAME_M1', 5: 'TIMEFRAME_M5', 15: 'TIMEFRAME_M15',
                  30: 'TIMEFRAME_M30', 60: 'TIMEFRAME_H1',
                  240: 'TIMEFRAME_H4'}

    def rates(self, symbol, timeframe_min, count):
        """The last `count` bars of `symbol`, oldest first, or None.

        `copy_rates_from_pos` from bar 0, which is the bar still
        FORMING — the caller drops it, because a candle that has not
        closed is not history. Each row carries the bar's own recorded
        spread and the symbol's point, so the bid-priced close can be
        moved to a mid by the caller.

        `time` is the BROKER's wall clock encoded as an epoch, exactly
        as a deal's is (see `server_time_offset_sec`): the caller moves
        it to UTC with the measured offset. None — not [] — when the
        terminal will not answer: no history is not an empty history.
        """
        if mt5 is None:
            return None
        name = self.TIMEFRAMES.get(int(timeframe_min))
        frame = getattr(mt5, name, None) if name else None
        if frame is None:
            return None
        self.ensure_symbol(symbol)
        try:
            raw = mt5.copy_rates_from_pos(symbol, frame, 0, int(count))
        except Exception as e:
            logging.warning('%s: no %s-minute bars from the terminal: %s',
                            symbol, timeframe_min, e)
            return None
        if raw is None or len(raw) == 0:
            logging.warning('%s: the terminal returned no %s-minute bars '
                            '(%s)', symbol, timeframe_min, mt5.last_error())
            return None
        info = self.symbol_info(symbol)
        point = float(getattr(info, 'point', 0.0) or 0.0)
        return [{'time': int(row['time']), 'close': float(row['close']),
                 'spread': int(row['spread']), 'point': point}
                for row in raw]

    def session_stats(self, symbol):
        """This symbol's own session O/H/L and volume, as the TERMINAL
        reports them.

        Read defensively with getattr: brokers fill in different subsets
        of these fields, and a missing one must come back as None rather
        than as a zero that would be drawn as a real high of 0.00.
        """
        if mt5 is None:
            return None
        info = self.ensure_symbol(symbol)
        if info is None:
            return None

        def value(*names):
            for name in names:
                found = getattr(info, name, None)
                if found not in (None, 0.0, 0):
                    return float(found)
            return None

        return {
            'symbol': symbol,
            'open': value('session_open'),
            'high': value('session_high', 'bidhigh'),
            'low': value('session_low', 'bidlow'),
            # Tick volume, which is what MT5 has for most instruments.
            'volume': value('session_volume', 'volume'),
        }

    def symbol_report(self, symbol):
        """Everything the connectivity checklist needs about one symbol
        on this account: does it exist, is it in Market Watch, is it
        priced, and what are the contract specs the sizing math and the
        hedge ratio depend on."""
        if mt5 is None:
            return {'symbol': symbol, 'found': False,
                    'error': 'MetaTrader5 package not installed'}
        info = self.ensure_symbol(symbol)
        if info is None:
            return {'symbol': symbol, 'found': False,
                    'error': f'{symbol} does not exist on this broker'}
        tick = mt5.symbol_info_tick(symbol)
        trade_mode = getattr(info, 'trade_mode', None)
        return {
            'symbol': symbol, 'found': True,
            'description': getattr(info, 'description', ''),
            'visible': bool(info.visible),
            'bid': tick.bid if tick else None,
            'ask': tick.ask if tick else None,
            'tick_time': int(getattr(tick, 'time', 0)) if tick else None,
            'digits': getattr(info, 'digits', None),
            'point': getattr(info, 'point', None),
            'tick_size': (getattr(info, 'trade_tick_size', 0)
                          or getattr(info, 'point', None)),
            'contract_size': getattr(info, 'trade_contract_size', None),
            # What MT5 says one tick of movement is WORTH on one lot.
            # tick_value / tick_size is the contract size the terminal
            # will actually compute profit from, whatever
            # trade_contract_size claims — so when a broker's spec
            # sheet and the terminal disagree, this settles it, because
            # this is what the money is calculated from.
            'tick_value': getattr(info, 'trade_tick_value', None),
            'volume_min': getattr(info, 'volume_min', None),
            'volume_max': getattr(info, 'volume_max', None),
            'volume_step': getattr(info, 'volume_step', None),
            'currency': getattr(info, 'currency_profit', ''),
            'filling_mode': getattr(info, 'filling_mode', None),
            'trade_mode': trade_mode,
            'trade_allowed': (trade_mode not in
                              (getattr(mt5, 'SYMBOL_TRADE_MODE_DISABLED', 0),
                               getattr(mt5, 'SYMBOL_TRADE_MODE_CLOSEONLY',
                                       -1))),
            'expiry': int(getattr(info, 'expiration_time', 0) or 0),
            # How many DEPTH levels this broker publishes for the
            # symbol, or 0 for none. Most retail CFD accounts publish
            # none at all, and then the ladder's size columns stay
            # empty — which is the broker, not this software, and the
            # checklist should be able to say so.
            'depth_levels': len(self.depth(symbol) or ()),
            'swap_long': getattr(info, 'swap_long', None),
            'swap_short': getattr(info, 'swap_short', None),
            # WHAT swap_long/short are denominated in. Without it the
            # numbers are unusable: the same "-4.5" is 4.5 points on one
            # symbol, 4.5 account-currency units on another and 4.5
            # percent a year on a third. Reading it as money regardless
            # is how the old carry term produced a basis nobody could
            # reconcile.
            'swap_mode': getattr(info, 'swap_mode', None),
            # Which weekday is charged triple for the weekend, so a
            # holding period can count nights rather than days.
            'swap_rollover3days': getattr(info, 'swap_rollover3days', None),
        }

    def verify_ticket(self, ticket, attempts=3, delay=0.4):
        """Ask MT5 what IT has for this ticket — the independent proof
        that an order really reached the broker rather than just
        returning success from order_send.

        Looks for the position (still open), then the deals it
        produced, then the order record. Deal history lags a fill by a
        moment, so a miss is retried before it is believed."""
        if mt5 is None:
            return {'ticket': ticket, 'confirmed': False,
                    'error': 'MetaTrader5 package not installed'}
        found = {'ticket': ticket, 'confirmed': False, 'deals': [],
                 'position_open': False}
        for attempt in range(attempts):
            try:
                positions = mt5.positions_get(ticket=int(ticket)) or ()
                for position in positions:
                    found.update({
                        'confirmed': True, 'position_open': True,
                        'symbol': position.symbol,
                        'volume': position.volume,
                        'price': position.price_open,
                        'time': int(position.time),
                        'magic': position.magic,
                        'comment': position.comment or '',
                        'source': 'open position'})

                deals = (mt5.history_deals_get(position=int(ticket))
                         or mt5.history_deals_get(ticket=int(ticket)) or ())
                for deal in deals:
                    found['deals'].append({
                        'deal_id': deal.ticket, 'order_id': deal.order,
                        'symbol': deal.symbol, 'volume': deal.volume,
                        'price': deal.price,
                        'commission': deal.commission,
                        'profit': deal.profit,
                        'time': int(deal.time),
                        'comment': deal.comment or ''})
                if deals:
                    last = found['deals'][-1]
                    found.update({'confirmed': True,
                                  'symbol': last['symbol'],
                                  'volume': last['volume'],
                                  'price': last['price'],
                                  'time': last['time'],
                                  'source': 'deal history'})

                if not found['confirmed']:
                    orders = (mt5.history_orders_get(ticket=int(ticket))
                              or ())
                    for order in orders:
                        found.update({
                            'confirmed': True, 'symbol': order.symbol,
                            'volume': order.volume_initial,
                            'price': order.price_open,
                            'time': int(getattr(order, 'time_done',
                                                order.time_setup)),
                            'state': order.state,
                            'comment': order.comment or '',
                            'source': 'order history'})
            except Exception as e:
                found['error'] = str(e)
            if found['confirmed'] or attempt == attempts - 1:
                break
            time.sleep(delay)          # history lags a fill briefly
        if not found['confirmed']:
            found.setdefault('error', 'not found in MT5 positions, deals '
                                      'or order history')
        return found

    def terminal_report(self):
        """Terminal- and account-level facts the checklist reports:
        whether the terminal is attached, who is logged in, whether
        algo trading is switched on, and the account's margin mode."""
        if mt5 is None:
            return {'library': False, 'terminal': False,
                    'error': 'MetaTrader5 package not installed '
                             '(Windows only)'}
        report = {'library': True, 'terminal': False}
        terminal = mt5.terminal_info()
        if terminal is None:
            report['error'] = str(mt5.last_error())
            return report
        report.update({
            'terminal': True,
            'terminal_name': getattr(terminal, 'name', ''),
            'terminal_path': getattr(terminal, 'path', ''),
            'terminal_connected': bool(getattr(terminal, 'connected', False)),
            'algo_trading': bool(getattr(terminal, 'trade_allowed', False)),
            'ping_ms': (getattr(terminal, 'ping_last', 0) or 0) / 1000.0,
        })
        info = mt5.account_info()
        if info is None:
            report['logged_in'] = False
            return report
        margin_mode = getattr(info, 'margin_mode', None)
        report.update({
            'logged_in': True,
            'login': info.login, 'server': info.server,
            'name': getattr(info, 'name', ''),
            'currency': getattr(info, 'currency', ''),
            'leverage': getattr(info, 'leverage', None),
            'balance': getattr(info, 'balance', 0.0),
            # equity = balance + credit + floating P&L. Brokers often
            # fund a demo with CREDIT rather than balance, which makes
            # balance alone read as an empty account: live 2026-08-11,
            # balance 0.00 against equity 5,000, and on the account
            # before it balance -13.70 against equity 4,986.30.
            'credit': getattr(info, 'credit', 0.0),
            'equity': getattr(info, 'equity', 0.0),
            'margin_free': getattr(info, 'margin_free', 0.0),
            'trade_allowed': bool(getattr(info, 'trade_allowed', False)),
            'trade_expert': bool(getattr(info, 'trade_expert', False)),
            'margin_mode': margin_mode,
            'hedging': margin_mode == getattr(
                mt5, 'ACCOUNT_MARGIN_MODE_RETAIL_HEDGING', 2),
        })
        return report

    def server_time_offset_sec(self):
        """Seconds the BROKER's displayed clock runs ahead of UTC.

        MT5 stamps every deal and order with the server's WALL CLOCK
        encoded as a Unix epoch, and the History tab displays that same
        wall clock. Read one of those stamps as an ordinary timestamp —
        which is what the dashboard was doing — and you get the
        browser's local rendering of a number that was never in the
        browser's time zone. On a GMT+3 broker seen from a GMT+5:30
        box, every row in our order log sits 2.5 hours away
        from the same trade in MT5's History, which is enough on its
        own to make the two tables look like different accounts.

        Measured from the freshest tick we can see (a tick's `time` is
        stamped the same way), so it needs a symbol in Market Watch and
        returns None when it cannot be established — a guess here would
        be worse than an honest blank."""
        if mt5 is None:
            return None
        newest = None
        for name in self._time_probe_symbols():
            tick = mt5.symbol_info_tick(name)
            stamp = getattr(tick, 'time', None) if tick else None
            if stamp:
                newest = max(newest or 0, int(stamp))
        if not newest:
            return None
        return int(round(newest - time.time()))

    def _time_probe_symbols(self):
        """Symbols to read the server clock off: whatever is already in
        Market Watch. Cheap, and it needs no configuration."""
        try:
            return [s.name for s in (mt5.symbols_get() or ()) if s.visible]
        except Exception:
            return []

    def order_log(self, hours=24):
        """Everything this MT5 account did recently, normalised for the
        order log: filled deals (with fee/swap/profit), plus
        orders that never filled (cancelled/rejected) and anything
        still resting. Includes manual trades placed in the terminal —
        `is_bot` marks the ones this engine sent."""
        if mt5 is None:
            return []
        rows = []
        offset = self.server_time_offset_sec()
        try:
            # These bounds are matched against SERVER-clock stamps
            # while `datetime.now()` is this box's local clock. A
            # `now + 1 minute` ceiling therefore silently dropped the
            # most recent deals whenever the server clock ran ahead of
            # the box — the newest rows, which are exactly the ones an
            # operator is checking against MT5's History. The widest
            # real gap (UTC-12 to UTC+14, plus DST) is 27 hours, so pad
            # both ends by two days and trim afterwards on the rows'
            # own stamps. Over-fetching an audit log costs nothing;
            # missing a fill costs trust in the whole table.
            slack = timedelta(days=2)
            since = datetime.now() - timedelta(hours=hours) - slack
            now = datetime.now() + slack

            order_types = {
                getattr(mt5, name, -1): label for name, label in [
                    ('ORDER_TYPE_BUY', 'market buy'),
                    ('ORDER_TYPE_SELL', 'market sell'),
                    ('ORDER_TYPE_BUY_LIMIT', 'buy limit'),
                    ('ORDER_TYPE_SELL_LIMIT', 'sell limit'),
                    ('ORDER_TYPE_BUY_STOP', 'buy stop'),
                    ('ORDER_TYPE_SELL_STOP', 'sell stop')]}

            # A DEAL record does not carry the order type, so the log
            # used to print a literal "market/limit" on every filled
            # row — the one column an operator checks to confirm the
            # limit path actually rested rather than crossing. The
            # ORDER that produced the deal does know, and deal.order
            # points straight at it, so resolve it here rather than
            # showing both and meaning neither.
            history_orders = list(mt5.history_orders_get(since, now) or ())
            type_by_order = {
                str(o.ticket): order_types.get(o.type, str(o.type))
                for o in history_orders}

            deal_types = {0: 'buy', 1: 'sell'}
            for deal in (mt5.history_deals_get(since, now) or ()):
                if deal.type not in deal_types:
                    continue          # balance/credit entries, not trades
                order_id = str(deal.order or deal.ticket)
                rows.append({
                    'order_id': order_id,
                    'deal_id': str(deal.ticket),
                    'symbol': deal.symbol,
                    'inst_type': 'DEAL',
                    'side': deal_types[deal.type],
                    'pos_side': ('open' if deal.entry == mt5.DEAL_ENTRY_IN
                                 else 'close'),
                    # Unknown only when the originating order has aged
                    # out of the history window — never a guess.
                    'order_type': type_by_order.get(order_id, 'unknown'),
                    'quantity': deal.volume,
                    'fill_qty': deal.volume,
                    'fill_price': deal.price,
                    # COMMISSION AND SWAP, SEPARATELY, AS THE BROKER
                    # REPORTS THEM — and `fee` as their sum, which is
                    # what the order log prints.
                    #
                    # Only `fee` used to be emitted. The journal's
                    # commission and swap columns are read straight off
                    # these keys, so they were NULL on every fill ever
                    # written, and the totals - COALESCE(SUM(...), 0) -
                    # published "$0.00" for a charge nobody had
                    # measured. That number is the counterweight to the
                    # TYPED COMMISSION_PER_LOT the P&L is marked with;
                    # with both reading zero, a commission nobody
                    # entered could never be caught, and EXIT_IF_PROFIT
                    # would close a position still under water by it.
                    #
                    # None, not 0.0, where the broker reports nothing:
                    # unmeasured is not zero.
                    'commission': deal.commission,
                    'swap': deal.swap,
                    'fee': (deal.commission or 0.0) + (deal.swap or 0.0),
                    'fee_ccy': '',
                    'pnl': deal.profit or 0.0,
                    'state': 'filled',
                    'filled_at': int(deal.time) * 1000,
                    'position_id': deal.position_id,
                    'magic': deal.magic,
                    'is_bot': deal.magic == MAGIC_NUMBER,
                    'comment': deal.comment or '',
                })

            states = {
                getattr(mt5, name, -1): label for name, label in [
                    ('ORDER_STATE_STARTED', 'started'),
                    ('ORDER_STATE_PLACED', 'placed'),
                    ('ORDER_STATE_CANCELED', 'cancelled'),
                    ('ORDER_STATE_PARTIAL', 'partial'),
                    ('ORDER_STATE_FILLED', 'filled'),
                    ('ORDER_STATE_REJECTED', 'rejected'),
                    ('ORDER_STATE_EXPIRED', 'expired')]}

            # Orders that never produced a deal still matter — a
            # rejection or a cancel is exactly what you go looking for.
            for order in history_orders:
                state = states.get(order.state, str(order.state))
                if state in ('filled', 'partial'):
                    continue          # already covered by its deal
                rows.append({
                    'order_id': str(order.ticket), 'deal_id': '',
                    'symbol': order.symbol, 'inst_type': 'ORDER',
                    'side': ('buy' if 'buy' in
                             order_types.get(order.type, '') else 'sell'),
                    'pos_side': '-',
                    'order_type': order_types.get(order.type,
                                                  str(order.type)),
                    'quantity': order.volume_initial,
                    'fill_qty': 0.0,
                    'fill_price': order.price_open or 0.0,
                    # An order that never dealt was charged nothing, and
                    # carries no commission or swap of its own to read.
                    'commission': None, 'swap': None,
                    'fee': 0.0, 'fee_ccy': '', 'pnl': 0.0,
                    'state': state,
                    'filled_at': int(getattr(order, 'time_done',
                                             order.time_setup)) * 1000,
                    'position_id': order.position_id,
                    'magic': order.magic,
                    'is_bot': order.magic == MAGIC_NUMBER,
                    'comment': order.comment or '',
                })

            for order in (mt5.orders_get() or ()):
                rows.append({
                    'order_id': str(order.ticket), 'deal_id': '',
                    'symbol': order.symbol, 'inst_type': 'PENDING',
                    'side': ('buy' if 'buy' in
                             order_types.get(order.type, '') else 'sell'),
                    'pos_side': '-',
                    'order_type': order_types.get(order.type,
                                                  str(order.type)),
                    'quantity': order.volume_initial,
                    'fill_qty': (order.volume_initial
                                 - order.volume_current),
                    'fill_price': order.price_open or 0.0,
                    # Still resting: nothing dealt, so nothing charged.
                    'commission': None, 'swap': None,
                    'fee': 0.0, 'fee_ccy': '', 'pnl': 0.0,
                    'state': 'working',
                    'filled_at': int(order.time_setup) * 1000,
                    'position_id': order.position_id,
                    'magic': order.magic,
                    'is_bot': order.magic == MAGIC_NUMBER,
                    'comment': order.comment or '',
                })
        except Exception as e:
            logging.error("order_log failed: %s", e)

        # The padded window above over-fetches on purpose. Trim back to
        # what was asked for using each row's OWN stamp against the
        # server clock — the same clock the stamps are in. Without a
        # measured offset we keep everything rather than cut blind.
        if offset is not None and hours:
            cutoff_ms = int((time.time() + offset - hours * 3600) * 1000)
            rows = [r for r in rows
                    if r['state'] == 'working'
                    or (r.get('filled_at') or 0) >= cutoff_ms]
        # Every row carries the offset so the dashboard can render the
        # broker's own clock beside MT5's History instead of the
        # browser's, which is what made the two tables disagree.
        for row in rows:
            row['server_offset_sec'] = offset
        return rows

    def positions_by_magic(self, symbol=None):
        """Open positions created by THIS system (magic-scoped) —
        never touches manual or third-party positions."""
        if mt5 is None:
            return []
        raw = (mt5.positions_get(symbol=symbol) if symbol
               else mt5.positions_get()) or ()
        out = []
        for p in raw:
            if p.magic != MAGIC_NUMBER:
                continue
            out.append({
                'ticket': p.ticket,
                'symbol': p.symbol,
                'side': ('BUY' if p.type == mt5.POSITION_TYPE_BUY
                         else 'SELL'),
                'volume': p.volume,
                'price_open': p.price_open,
                # THE COMMENT, which is what we wrote on the order that
                # opened it. Without it the reconciler compares tickets
                # and nothing else, so a leg WE placed that is missing
                # from our book is indistinguishable from a stray
                # position somebody opened by hand - and it closed one
                # sixty seconds after the trader put it on.
                'comment': p.comment or '',
            })
        return out

    def account_is_hedging(self):
        """True when the account holds one position per order (hedging
        mode) rather than netting per symbol."""
        if mt5 is None:
            return False
        info = mt5.account_info()
        return bool(info) and info.margin_mode == \
            mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING

    def symbol_filling_modes(self, symbol):
        """Which type_filling values this symbol allows (broker-dependent)."""
        info = self.symbol_info(symbol)
        if not info:
            return []
        mask = getattr(info, 'filling_mode', 0)
        modes = []
        if mask & 1:
            modes.append('FOK')
        if mask & 2:
            modes.append('IOC')
        modes.append('RETURN')  # always available for pending orders
        return modes

    def _market_filling_modes(self, symbol):
        """Filling modes to try for a MARKET order, best first.

        symbol_info.filling_mode is a bitmask of what the broker allows
        (FOK=1, IOC=2). Hardcoding IOC here used to make every close
        fail with 10030 'Unsupported filling mode' on brokers that only
        allow FOK — the engine could open but never exit."""
        info = self.symbol_info(symbol)
        mask = getattr(info, 'filling_mode', 0) if info else 0
        modes = []
        if mask & 2:
            modes.append(mt5.ORDER_FILLING_IOC)   # allows partial fills
        if mask & 1:
            modes.append(mt5.ORDER_FILLING_FOK)
        if not modes:
            # Nothing declared: try all three rather than guess wrong.
            modes = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK,
                     mt5.ORDER_FILLING_RETURN]
        elif mt5.ORDER_FILLING_RETURN not in modes:
            modes.append(mt5.ORDER_FILLING_RETURN)
        return modes

    def _send_market(self, request, symbol):
        """order_send, retrying with another filling mode if the broker
        rejects the one we chose. Returns (result, last_mode)."""
        result = None
        for mode in self._market_filling_modes(symbol):
            request["type_filling"] = mode
            result = mt5.order_send(request)
            if result is None:
                continue
            if result.retcode != RETCODE_INVALID_FILL:
                return result, mode        # not a filling-mode problem
            logging.debug("%s rejected filling mode %s (10030) — retrying",
                          symbol, mode)
        return result, None

    def _filling_hint(self, symbol):
        info = self.symbol_info(symbol)
        mask = getattr(info, 'filling_mode', 0) if info else 0
        allowed = [name for bit, name in ((1, 'FOK'), (2, 'IOC')) if mask & bit]
        return (f"broker allows {'/'.join(allowed)} for {symbol}"
                if allowed else
                f"{symbol} declares no filling mode")

    def _pending_filling_mode(self, symbol):
        """Pick a filling mode the broker actually accepts for pending
        orders. RETURN is the default, but some brokers only allow
        FOK/IOC (flags in symbol_info.filling_mode) and reject RETURN
        with 'Unsupported filling mode' — live-tested 2026-06."""
        info = self.symbol_info(symbol)
        mask = getattr(info, 'filling_mode', 0) if info else 0
        if mask & 1:
            return mt5.ORDER_FILLING_FOK
        if mask & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def legal_limit_price(self, symbol, side, price):
        """The nearest price MT5 will actually ACCEPT for this limit.

        A pending price has to clear two separate constraints, and
        missing either one comes back as the same opaque 10015 Invalid
        price:

        1. It must land on a `trade_tick_size` boundary.
        2. It must sit at least `trade_stops_level` POINTS away from the
           market — BUY_LIMIT that far below the ask, SELL_LIMIT that
           far above the bid. Brokers set this per symbol.

        Only (1) was enforced. That was survivable on CFI's gold, where
        the stops level is 0, so a limit one tick inside the touch was
        legal. On their oil symbols it is not: live 2026-08-07, every
        BUY_SPOT LIMIT scenario failed `10015 - Invalid price` on
        USOIL_U6 while the identical code passed on XAUUSD_.

        Returns (price, note) — `note` is None when nothing had to move,
        otherwise it says what the broker's rule forced, so a limit that
        could not rest where it was asked to says so instead of looking
        like a clean fill at a price nobody chose.
        """
        info = self.symbol_info(symbol)
        tick_size = (getattr(info, 'trade_tick_size', 0)
                     or getattr(info, 'point', 0.01) or 0.01)

        def to_tick(value, up=False):
            steps = value / tick_size
            steps = (int(steps + 1 - 1e-9) if up else int(steps + 1e-9))
            return round(steps * tick_size, 10)

        wanted = round(round(price / tick_size) * tick_size, 10)
        point = getattr(info, 'point', 0) or tick_size
        stops = getattr(info, 'trade_stops_level', 0) or 0
        gap = max(stops * point, tick_size)

        tick = mt5.symbol_info_tick(symbol)
        bid = getattr(tick, 'bid', 0) if tick else 0
        ask = getattr(tick, 'ask', 0) if tick else 0
        if not bid or not ask:
            return wanted, None          # no book to measure against

        if side is OrderSide.BUY:
            limit = to_tick(ask - gap)           # must be BELOW the ask
            if wanted <= limit:
                return wanted, None
            return limit, (f"buy limit moved {wanted:.5f} -> {limit:.5f}: "
                           f"{symbol} requires {gap:.5f} below the "
                           f"{ask:.5f} ask")
        limit = to_tick(bid + gap, up=True)      # must be ABOVE the bid
        if wanted >= limit:
            return wanted, None
        return limit, (f"sell limit moved {wanted:.5f} -> {limit:.5f}: "
                       f"{symbol} requires {gap:.5f} above the "
                       f"{bid:.5f} bid")

    def place_pending_limit(self, symbol, side, volume, price, comment=""):
        """Rest a limit order. It OPENS a position when it executes.

        THERE IS NO SUCH THING AS A CLOSING PENDING, and this used to
        take a `position_ticket` and put it on the request as if there
        were. MT5 honours `position` on TRADE_ACTION_DEAL; on
        TRADE_ACTION_PENDING it is ignored, so the order rests as an
        ordinary limit and, on a hedging account, OPENS a second
        position facing the other way.

        Live 2026-09-02: ticket 2092 was rested to close ticket 2090 and
        filled as a BUY 0.01 beside the SELL 0.01 it was meant to close.
        The engine believed that leg was flat, closed the other leg, and
        the reconciler swept both futures as orphans a minute later.

        A resting CLOSE is synthetic — this system holds the level and
        sends a close by ticket when the market reaches it (quoter).

        Price must be rounded to trade_tick_size and far enough from the
        book (see legal_limit_price) or brokers reject with Invalid
        Price (10015) — live-tested 2026-06 and again 2026-08."""
        try:
            price, moved = self.legal_limit_price(symbol, side, price)
            if moved:
                logging.info("%s", moved)

            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": volume,
                "type": (mt5.ORDER_TYPE_BUY_LIMIT if side is OrderSide.BUY
                         else mt5.ORDER_TYPE_SELL_LIMIT),
                "price": price,
                "magic": MAGIC_NUMBER,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._pending_filling_mode(symbol),
            }
            result = mt5.order_send(request)
            # PLACED is the ordinary answer for an order that RESTS;
            # DONE is what a broker says when it filled on arrival.
            # Only DONE was accepted, so on a broker that answers PLACED
            # every working order was written off as refused while it
            # sat live at the broker.
            retcode = None if result is None else getattr(result, 'retcode',
                                                          None)
            if retcode not in RESTED_RETCODES:
                error = (mt5.last_error() if result is None
                         else f"{retcode} - {result.comment}")
                return {'ok': False, 'ticket': getattr(result, 'order', None),
                        'error': str(error)}
            return {'ok': True, 'ticket': result.order, 'error': None,
                    'price': price, 'price_note': moved}
        except Exception as e:
            return {'ok': False, 'ticket': None, 'error': str(e)}

    def pending_orders_by_magic(self, symbol=None):
        """Pending orders created by THIS system. Used to sweep stale
        orders before a new execution — orphan pendings accumulate
        after timeouts and failed cancels (live-tested 2026-06)."""
        if mt5 is None:
            return []
        raw = (mt5.orders_get(symbol=symbol) if symbol
               else mt5.orders_get()) or ()
        return [{'ticket': o.ticket, 'symbol': o.symbol,
                 'volume': getattr(o, 'volume_current', 0.0),
                 'price': getattr(o, 'price_open', 0.0)}
                for o in raw if o.magic == MAGIC_NUMBER]

    def modify_pending(self, ticket, price):
        """Re-peg a resting limit in place — no cancel/replace round trip.

        The re-peg is subject to the SAME minimum-distance rule as the
        original placement, so it is legalised the same way. Without
        this, a symbol with a stops level lets the order rest and then
        rejects every attempt to chase the market with it."""
        try:
            order = next(iter(mt5.orders_get(ticket=ticket) or ()), None)
            if order is not None:
                side = (OrderSide.BUY
                        if getattr(order, 'type', None)
                        == mt5.ORDER_TYPE_BUY_LIMIT else OrderSide.SELL)
                price, moved = self.legal_limit_price(
                    order.symbol, side, price)
                if moved:
                    logging.info("%s", moved)
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_MODIFY,
                "order": ticket,
                "price": price,
            })
            ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
            return {'ok': ok,
                    'error': None if ok else
                    (str(mt5.last_error()) if result is None
                     else f"{result.retcode} - {result.comment}")}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def cancel_pending(self, ticket):
        """Remove a resting order, then ALWAYS report what filled first —
        a 'cancelled' order can carry partial fills, and the deal
        history can lag briefly after a cancel (live-tested 2026-06),
        so a zero-fill result is re-read once."""
        try:
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": ticket,
            })
            state = self.order_fill_state(ticket)
            if state['filled_volume'] == 0 and not state['still_open']:
                time.sleep(0.05)   # deal history lag after cancel
                state = self.order_fill_state(ticket)
            if state.get('from_position'):
                state['leaked_fill'] = True
            if state['filled_volume'] == 0:
                # Deal history can still be behind. MT5 turns a filled
                # pending order into a POSITION carrying the same
                # ticket, and positions_get never lags — this is the
                # authoritative "it actually filled" check. Missing it
                # reports a clean cancel while a live position sits on
                # the book (seen 2026-08-06: a 'cancelled' scenario
                # order reappeared as an orphan seconds later).
                for position in (mt5.positions_get(ticket=int(ticket))
                                 or ()):
                    state.update({
                        'filled_volume': position.volume,
                        'price': position.price_open,
                        'position_tickets': [position.ticket],
                        'still_open': False,
                        'leaked_fill': True,
                    })
            state['cancelled'] = (result is not None and
                                  result.retcode == mt5.TRADE_RETCODE_DONE)
            return state
        except Exception as e:
            state = self.order_fill_state(ticket)
            state['cancelled'] = False
            state['error'] = str(e)
            return state

    def order_fill_state(self, ticket):
        """Filled volume / VWAP / position tickets for an order, from the
        deal history (works for pending and market orders alike)."""
        filled = 0.0
        notional = 0.0
        position_tickets = []
        # Whether the fill was found as a POSITION rather than a deal.
        # cancel_pending turns this into `leaked_fill`: a cancel that
        # did not prevent a fill is a distinct event and has to stay
        # visible in the report, not be smoothed into a normal fill.
        from_position = False
        # DID THE TERMINAL ANSWER AT ALL?
        #
        # MetaTrader5 returns None when it could not ask (terminal link
        # down, symbol gone, IPC upset) and an EMPTY TUPLE when it asked
        # and there was nothing. `or ()` erased that difference, so a
        # terminal that answered nothing read exactly like an order that
        # had not filled - a confident "no" built out of silence.
        #
        # Live 2026-09-16: a quoting leg's pending filled while this
        # returned 0.0, the hedge never fired, and four lots sat naked
        # for twelve minutes. `filled_volume` STAYS 0.0 - `cancel_pending`
        # compares it against 0 and a None there would skip its own
        # leaked-fill re-read - so the honesty goes in a flag of its own.
        readable = False
        # WHEN THE BROKER SAYS IT FILLED, on the broker's own clock.
        #
        # Without this the only fill time anyone had was the moment WE
        # noticed, and those are not the same instant: on 2026-09-16
        # they were twelve minutes and fifty-one seconds apart. The
        # report showed a one-second hedge on a trade that was naked
        # the whole time, because it timed itself from the noticing.
        #
        # MT5 stamps a deal with the SERVER's wall clock encoded as an
        # epoch, so the offset has to travel with it or the subtraction
        # is done between two different time zones. Both stay None when
        # they cannot be established: unmeasured is not zero, and a
        # guessed naked window is worse than an honest blank.
        filled_at = None
        try:
            raw_deals = mt5.history_deals_get(ticket=ticket)
            readable = raw_deals is not None
            deals = raw_deals or ()
            for deal in deals:
                if deal.order != ticket:
                    continue
                filled += deal.volume
                notional += deal.volume * deal.price
                stamp = int(getattr(deal, 'time', 0) or 0)
                if stamp:
                    # The LAST deal of a partial fill: the leg is naked
                    # until the whole of it is hedged.
                    filled_at = max(filled_at or 0, stamp)
                if deal.position_id and deal.position_id not in position_tickets:
                    position_tickets.append(deal.position_id)
            raw_orders = mt5.orders_get(ticket=ticket)
            readable = readable or raw_orders is not None
            still_open = bool(raw_orders)
            if not filled and not still_open:
                # Gone from the book with no deal recorded yet. MT5
                # turns a filled pending into a POSITION carrying the
                # ORDER's ticket, and positions_get shows it BEFORE
                # deal history does — the same lag cancel_pending
                # already works around.
                #
                # Reading deals alone therefore called a real fill "no
                # fill", so the scenario went down leak recovery, which
                # flattens at once: live 2026-08-10 a 120-second hold
                # closed in nine seconds because of it. The position IS
                # the fill; report it as one.
                raw_positions = mt5.positions_get(ticket=int(ticket))
                readable = readable or raw_positions is not None
                for position in (raw_positions or ()):
                    filled += position.volume
                    notional += position.volume * position.price_open
                    stamp = int(getattr(position, 'time', 0) or 0)
                    if stamp:
                        filled_at = max(filled_at or 0, stamp)
                    if position.ticket not in position_tickets:
                        position_tickets.append(position.ticket)
                        from_position = True
        except Exception as e:
            return {'ok': False, 'readable': False,
                    'filled_volume': filled, 'price': None,
                    'position_tickets': position_tickets,
                    'filled_at': None, 'server_offset_sec': None,
                    'still_open': False, 'error': str(e)}
        vwap = notional / filled if filled > 0 else None
        # Only on a FILL. This is read three times a second per resting
        # order and the offset costs a tick; a fill is rare and is the
        # only time the answer is used.
        offset = self.server_time_offset_sec() if filled > 0 and filled_at \
            else None
        return {'ok': True, 'readable': readable,
                'filled_volume': filled, 'price': vwap,
                'position_tickets': position_tickets,
                'from_position': from_position,
                'filled_at': filled_at if filled > 0 else None,
                'server_offset_sec': offset,
                'still_open': still_open,
                'error': None if readable else
                'the terminal answered nothing about this order'}

    def close_position_ticket(self, symbol, ticket, volume, entry_side,
                              slippage_points=1.0, comment=""):
        """Close a specific position by ticket. REQUIRED on hedging-mode
        accounts, where a plain opposite order would open a second
        position instead of closing this one."""
        try:
            tick = self.symbol_tick(symbol)
            info = self.symbol_info(symbol)
            if not tick or not info:
                return OrderResult(False, error=f"No market data for {symbol}")
            close_side = entry_side.opposite
            price = tick.ask if close_side is OrderSide.BUY else tick.bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": (mt5.ORDER_TYPE_BUY if close_side is OrderSide.BUY
                         else mt5.ORDER_TYPE_SELL),
                "position": ticket,
                "price": price,
                "deviation": int(slippage_points / info.point),
                "magic": MAGIC_NUMBER,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
            }
            result, _mode = self._send_market(request, symbol)
            retcode = None if result is None else getattr(result, 'retcode',
                                                          None)
            if retcode not in FILLED_RETCODES:
                error = (mt5.last_error() if result is None
                         else f"{retcode} - {result.comment}")
                if retcode == RETCODE_INVALID_FILL:
                    error += f" (tried every filling mode; " \
                             f"{self._filling_hint(symbol)})"
                return OrderResult(False, requested_price=price,
                                   ticket=getattr(result, 'order', None),
                                   error=f"Close failed: {error}")
            done = float(getattr(result, 'volume', 0.0) or 0.0)
            if retcode == RETCODE_DONE_PARTIAL:
                # PART of the position came off. Reporting this as a
                # failed close left the whole ticket on our books while
                # some of it was already gone at the broker; the caller
                # measures what is LEFT from the broker's own book and
                # books a partial close.
                logging.warning(
                    "%s ticket %s: broker closed %s of %s lots (10010 "
                    "partial)", symbol, ticket, done, volume)
            else:
                done = done or volume
            return OrderResult(True, requested_price=price,
                               executed_price=result.price,
                               ticket=result.order, volume=done)
        except Exception as e:
            return OrderResult(False, error=f"Close error: {e}")

    def send_market_order(self, symbol, side, volume,
                          slippage_points=1.0, comment=""):
        """Send an IOC market order; returns OrderResult."""
        try:
            info = self.ensure_symbol(symbol)
            if not info:
                return OrderResult(False, error=f"Symbol {symbol} not found")

            tick = self.symbol_tick(symbol)
            if not tick:
                return OrderResult(False, error=f"No tick data for {symbol}")

            price = tick.ask if side is OrderSide.BUY else tick.bid
            deviation = int(slippage_points / info.point)

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": (mt5.ORDER_TYPE_BUY if side is OrderSide.BUY
                         else mt5.ORDER_TYPE_SELL),
                "price": price,
                "deviation": deviation,
                "magic": MAGIC_NUMBER,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
            }

            result, _mode = self._send_market(request, symbol)
            if result is None:
                return OrderResult(False, requested_price=price,
                                   error=f"order_send failed: {mt5.last_error()}")
            retcode = getattr(result, 'retcode', None)
            ticket = getattr(result, 'order', None)
            filled = float(getattr(result, 'volume', 0.0) or 0.0)
            if retcode not in FILLED_RETCODES:
                detail = f"{retcode} - {result.comment}"
                if retcode == RETCODE_INVALID_FILL:
                    detail += f" (tried every filling mode; " \
                              f"{self._filling_hint(symbol)})"
                # THE TICKET COMES BACK EVEN ON A REFUSAL, and that is
                # the point of this branch. A broker that answers 10008
                # PLACED, or that rejects after part of the order has
                # already dealt, has left something of ours at the
                # broker; without the ticket nobody upstream can find
                # out, and "refused" becomes a guess that reads as
                # "nothing is on". legs.LocalLeg.order asks the fill
                # state off this ticket whichever way the verdict went.
                return OrderResult(False, requested_price=price,
                                   ticket=ticket,
                                   error=f"Order failed: {detail}")

            if retcode == RETCODE_DONE_PARTIAL:
                # NOT a failure. The broker filled what it could and
                # said so; the caller hedges to what filled.
                logging.warning(
                    "%s: broker filled %s of %s lots (10010 partial) - "
                    "order %s", symbol, filled, volume, ticket)
            else:
                # A full execution that reports no volume is the
                # broker being terse, not a zero fill.
                filled = filled or volume
            return OrderResult(True, requested_price=price,
                               executed_price=result.price,
                               ticket=ticket, volume=filled)

        except Exception as e:
            logging.error("Order exception on %s: %s", symbol, e)
            return OrderResult(False, error=f"Execution error: {e}")
