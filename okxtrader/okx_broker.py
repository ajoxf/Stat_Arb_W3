"""OKX broker session: the ONLY module that talks to OKX.

It stands where Mirror's MetaTrader5 `BrokerSession` stood and answers
the same interface, so the engine above it — coordinator, executor,
quoter, reconciler, Algo — runs unchanged. The translation:

  MT5                         OKX
  -------------------------   ------------------------------------------
  lots, contract size         sz (contracts on perps/futures, base ccy
                              on spot), ctVal (1 for spot)
  volume_min / volume_step    minSz / lotSz
  point / tick size           tickSz
  hedging account             long/short position mode (posSide)
  position TICKET             our own ticket per opening fill, kept in a
                              persisted ledger and reconciled against
                              OKX's own totals per (instrument, posSide)
  magic number                clOrdId prefix 'nx' + tag 'nexus'
  close by ticket             the opposite side on the SAME posSide, for
                              that ticket's size — never a new position
  market order + deviation    IOC limit at the touch +/- a protection,
                              so a fill is never worse than the cap and
                              spot is sized in base currency exactly
  pending limit               post_only limit (maker or nothing)
  swap                        funding (perps); borrow on spot shorts
  broker clock                /public/time

The ledger exists because OKX nets: two clicks long the same perp are
one OKX position. The desk's book wants one position per click, closed
on its own, and the ledger is that book of tickets at the exchange. It
is persisted (a restart must not orphan every position) and trimmed to
what OKX actually holds, so a position closed outside the desk — a
liquidation, an ADL, a manual close on the OKX app — shows up as gone
instead of being believed.
"""

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .models import MAGIC_NUMBER, OrderSide
from .okx_api import OKXClient, OKXError


class OrderResult:
    """Outcome of a market order — the same shape the engine always had."""

    def __init__(self, success, requested_price=None, executed_price=None,
                 ticket=None, error=None, volume=0.0):
        self.success = success
        self.requested_price = requested_price
        self.executed_price = executed_price
        self.ticket = ticket
        self.error = error
        self.volume = volume


#: Minutes -> OKX bar name. Only the candles the Algo offers.
BARS = {1: '1m', 5: '5m', 15: '15m', 30: '30m', 60: '1H', 240: '4H'}

#: Our orders carry this prefix in clOrdId, and this tag. It is how an
#: order of ours is told from the trader's own on the OKX app — the
#: magic number of this system.
CLORD_PREFIX = 'nx'
ORDER_TAG = 'nexus'

#: Default cap on an IOC "market" order: never fill further than this
#: from the touch it was sent at.
DEFAULT_PROTECTION_BPS = 20.0

_EPS = 1e-9


def _f(value, default=None):
    try:
        if value in (None, ''):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _decimals(step):
    text = ('%.12f' % float(step)).rstrip('0')
    return len(text.split('.')[1]) if '.' in text else 0


def fmt_size(value, step):
    """A size OKX accepts: floored to the lot step, no float dust."""
    step = float(step or 0) or 1e-8
    units = int((float(value) + step * 1e-6) / step)
    return ('%.' + str(_decimals(step)) + 'f') % (units * step)


def fmt_price(value, tick):
    tick = float(tick or 0) or 0.01
    return ('%.' + str(_decimals(tick)) + 'f') % (round(float(value) / tick)
                                                    * tick)


class OKXBroker:
    """One OKX account (demo or live) for every leg routed to it."""

    def __init__(self, account, client=None, ledger_path=None):
        self.account = account
        demo = bool(getattr(account, 'demo', True))
        self.client = client or OKXClient(
            api_key=getattr(account, 'api_key', '') or '',
            secret=getattr(account, 'api_secret', '') or '',
            passphrase=getattr(account, 'api_passphrase', '') or '',
            demo=demo,
            base_url=(getattr(account, 'base_url', None)
                      or os.environ.get('OKX_BASE_URL')
                      or 'https://www.okx.com'))
        self.demo = demo
        self.connected = False
        self.last_error = ''
        self.last_visible = {}
        self.protection_bps = float(
            os.environ.get('OKX_IOC_PROTECTION_BPS', DEFAULT_PROTECTION_BPS))
        self._lock = threading.RLock()
        self._instruments = {}                 # instId -> row
        self._inst_loaded = set()              # instTypes loaded
        self._tick_cache = {}                  # instType -> (at, {id: row})
        self._book_cache = {}                  # instId -> (at, rows)
        self._pos_cache = None                 # (at, rows)
        self._bal_cache = None                 # (at, row)
        self._fee_cache = {}                   # instType -> (at, (m, t))
        self._lever_cache = {}                 # instId -> (at, lever)
        self._funding_cache = {}               # instId -> (at, row)
        self._shortfall = {}                   # key -> first seen short
        self._config = None
        self._offset_at = 0.0
        self.tick_ttl = 0.25
        name = getattr(account, 'name', 'okx')
        self.ledger_path = Path(ledger_path or os.environ.get(
            'OKX_LEDGER_DIR', 'data')) / f'okx_ledger_{name}.json'
        self.ledger = {'next': 1000, 'positions': {}, 'orders': {},
                       'ords': {}}
        self._load_ledger()

    # -- the ledger ----------------------------------------------------------

    def _load_ledger(self):
        try:
            with open(self.ledger_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for key in ('positions', 'orders', 'ords'):
                data.setdefault(key, {})
            self.ledger = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logging.critical('OKX ledger %s could not be read (%s) — open '
                             'positions will look like strangers until '
                             'it is restored', self.ledger_path, exc)

    def _save_ledger(self):
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.ledger_path.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.ledger, f)
            os.replace(tmp, self.ledger_path)
        except OSError as exc:
            logging.error('could not save the OKX ledger: %s', exc)

    def _new_ticket(self):
        self.ledger['next'] = int(self.ledger.get('next', 1000)) + 1
        return int(self.ledger['next'])

    @staticmethod
    def _cl_ord_id(ticket):
        # alphanumeric, <= 32 chars: prefix, ticket, entropy.
        return f'{CLORD_PREFIX}{ticket}{uuid.uuid4().hex[:12]}'

    # -- session -------------------------------------------------------------

    def initialize(self):
        try:
            self.client.sync_clock()
            self._offset_at = time.time()
            for kind in ('SPOT', 'SWAP', 'FUTURES'):
                self._load_instruments(kind)
            if self.client.has_credentials or self.client.transport:
                self._config = self.client.get('/api/v5/account/config',
                                               private=True)[0]
            self.connected = True
            self.last_error = ''
            return True
        except OKXError as exc:
            self.connected = False
            self.last_error = str(exc)
            logging.error('OKX %s: %s', getattr(self.account, 'name', ''),
                          exc)
            return False

    def shutdown(self):
        self.connected = False
        self._save_ledger()

    def is_alive(self):
        return self.connected

    def server_time_offset_sec(self):
        """Seconds OKX's clock runs ahead of ours. OKX stamps in true
        UTC, so this is the box's own drift — near zero, and measured,
        not assumed."""
        if time.time() - self._offset_at > 60:
            try:
                self.client.sync_clock()
                self._offset_at = time.time()
            except OKXError:
                return None
        return round(self.client.clock_offset, 3)

    # -- instruments ---------------------------------------------------------

    def _load_instruments(self, kind):
        rows = self.client.get('/api/v5/public/instruments',
                               {'instType': kind})
        with self._lock:
            for row in rows:
                # Linear (USDT-margined) contracts only: inverse
                # contracts are sized in USD and settle in coin — a
                # different P&L, and not what this desk prices.
                if kind != 'SPOT' and row.get('ctType') not in ('linear',
                                                                ''):
                    continue
                self._instruments[row['instId']] = row
            self._inst_loaded.add(kind)

    def _inst(self, symbol):
        row = self._instruments.get(symbol)
        if row is None:
            kind = ('SWAP' if symbol.endswith('-SWAP') else
                    'SPOT' if symbol.count('-') == 1 else 'FUTURES')
            try:
                self._load_instruments(kind)
            except OKXError:
                return None
            row = self._instruments.get(symbol)
        return row

    def inst_type(self, symbol):
        row = self._inst(symbol)
        return row.get('instType') if row else None

    def is_derivative(self, symbol):
        return self.inst_type(symbol) in ('SWAP', 'FUTURES')

    def contract_size(self, symbol):
        row = self._inst(symbol)
        if not row:
            return None
        return _f(row.get('ctVal'), 1.0) or 1.0

    def symbol_info(self, symbol):
        row = self._inst(symbol)
        if row is None:
            return None
        tick = _f(row.get('tickSz'), 0.01)
        swap_long, swap_short = self._swap_per_lot_day(symbol, row)
        expiry = _f(row.get('expTime'))
        return SimpleNamespace(
            name=symbol, inst_type=row.get('instType'),
            contract_size=_f(row.get('ctVal'), 1.0) or 1.0,
            ct_val_ccy=row.get('ctValCcy') or row.get('baseCcy'),
            volume_min=_f(row.get('minSz'), 0.0),
            volume_step=_f(row.get('lotSz'), 0.0),
            volume_max=_f(row.get('maxLmtSz'), 1e9),
            point=tick, trade_tick_size=tick, tick_size=tick,
            digits=_decimals(tick),
            trade_allowed=row.get('state') == 'live',
            expiry=int(expiry / 1000) if expiry else 0,
            # Funding, priced as Mirror's carry reads a swap: money per
            # lot per day (all three 8h fundings), SWAP_CURRENCY_DEPOSIT
            # (4). Spot and dated futures pay no funding: 0, disabled
            # (0). A spot SHORT does pay borrow interest; that is not
            # in here.
            swap_long=(swap_long if swap_long is not None
                       else (None if row.get('instType') == 'SWAP'
                             else 0.0)),
            swap_short=(swap_short if swap_short is not None
                        else (None if row.get('instType') == 'SWAP'
                              else 0.0)),
            swap_mode=(4 if row.get('instType') == 'SWAP' else 0),
            currency_profit='USDT',
            base_ccy=row.get('baseCcy') or row.get('ctValCcy'),
            quote_ccy=row.get('quoteCcy') or row.get('settleCcy'),
        )

    def ensure_symbol(self, symbol):
        info = self.symbol_info(symbol)
        self.last_visible[symbol] = info is not None
        return info

    def find_symbols(self, pattern, limit=40):
        for kind in ('SPOT', 'SWAP', 'FUTURES'):
            if kind not in self._inst_loaded:
                try:
                    self._load_instruments(kind)
                except OKXError:
                    pass
        needle = (pattern or '').upper().replace('/', '-')
        out = []
        for inst_id, row in sorted(self._instruments.items()):
            if needle and needle not in inst_id:
                continue
            kind = row.get('instType')
            expiry = _f(row.get('expTime'))
            label = {'SPOT': 'Spot', 'SWAP': 'Perpetual',
                     'FUTURES': 'Future'}.get(kind, kind)
            out.append({
                'symbol': inst_id, 'inst_type': kind,
                'description': (f'{label} {inst_id}'
                                + (time.strftime(' (expires %d %b %Y)',
                                                 time.gmtime(expiry / 1000))
                                   if expiry else '')),
                'contract_size': _f(row.get('ctVal'), 1.0) or 1.0,
                'volume_min': _f(row.get('minSz'), 0.0),
                'volume_max': _f(row.get('maxLmtSz'), 1e9),
                'volume_step': _f(row.get('lotSz'), 0.0),
            })
            if len(out) >= limit:
                break
        return out

    # -- prices ------------------------------------------------------------

    def _tickers(self, kind, fresh=False):
        now = time.time()
        cached = self._tick_cache.get(kind)
        if cached and not fresh and now - cached[0] < self.tick_ttl:
            return cached[1]
        rows = self.client.get('/api/v5/market/tickers', {'instType': kind})
        table = {r['instId']: r for r in rows}
        self._tick_cache[kind] = (now, table)
        return table

    def _ticker(self, symbol, fresh=False):
        kind = self.inst_type(symbol)
        if kind is None:
            return None
        try:
            return self._tickers(kind, fresh).get(symbol)
        except OKXError as exc:
            self.last_error = str(exc)
            return None

    def symbol_tick(self, symbol):
        row = self._ticker(symbol)
        if not row:
            return None
        bid, ask = _f(row.get('bidPx')), _f(row.get('askPx'))
        if not bid or not ask:
            return None
        ts = int(row.get('ts') or 0)
        self.last_visible[symbol] = True
        return SimpleNamespace(bid=bid, ask=ask,
                               last=_f(row.get('last'), (bid + ask) / 2),
                               time=ts / 1000.0, time_msc=ts)

    def resubscribe(self, symbol):
        row = self._ticker(symbol, fresh=True)
        if not row:
            return None
        bid, ask = _f(row.get('bidPx')), _f(row.get('askPx'))
        return SimpleNamespace(bid=bid, ask=ask, last=_f(row.get('last')),
                               time=int(row.get('ts') or 0) / 1000.0)

    def depth(self, symbol):
        """Five levels a side, in the instrument's own size units
        (contracts on a perp, base currency on spot)."""
        now = time.time()
        cached = self._book_cache.get(symbol)
        if cached and now - cached[0] < 0.5:
            return cached[1]
        try:
            rows = self.client.get('/api/v5/market/books',
                                   {'instId': symbol, 'sz': '5'})
        except OKXError:
            return None
        if not rows:
            return None
        out = []
        for side, key in (('ask', 'asks'), ('bid', 'bids')):
            for level in rows[0].get(key) or ():
                out.append({'type': side, 'price': float(level[0]),
                            'volume': float(level[1])})
        self._book_cache[symbol] = (now, out or None)
        return out or None

    def rates(self, symbol, timeframe_min, count):
        """The last `count` bars, oldest first, the forming one last; or
        None when OKX has none. OKX candles are LAST-trade prices with no
        recorded bid-ask, so `spread` is 0 and the close stands as is."""
        bar = BARS.get(int(timeframe_min))
        if bar is None:
            return None
        row = self._inst(symbol)
        point = _f(row.get('tickSz'), 0.01) if row else 0.01
        wanted = int(count)
        out = []
        after = None
        try:
            path = '/api/v5/market/candles'
            while len(out) < wanted:
                params = {'instId': symbol, 'bar': bar,
                          'limit': str(min(300 if path.endswith('candles')
                                           and after is None else 100,
                                           wanted - len(out)))}
                if after:
                    params['after'] = after
                page = self.client.get(path, params)
                if not page:
                    break
                out.extend(page)
                after = page[-1][0]
                path = '/api/v5/market/history-candles'
        except OKXError as exc:
            logging.warning('%s: no %s candles from OKX: %s', symbol, bar,
                            exc)
            if not out:
                return None
        if not out:
            return None
        bars = [{'time': int(int(r[0]) / 1000), 'close': float(r[4]),
                 'spread': 0, 'point': point} for r in out[:wanted]]
        bars.sort(key=lambda b: b['time'])
        return bars

    def session_stats(self, symbol):
        """OKX's rolling 24 h figures: crypto has no session, and these
        are what the exchange itself publishes as the day."""
        row = self._ticker(symbol)
        if not row:
            return None
        return {'symbol': symbol, 'open': _f(row.get('open24h')),
                'high': _f(row.get('high24h')), 'low': _f(row.get('low24h')),
                'volume': _f(row.get('vol24h'))}

    # -- funding (the crypto "swap") -----------------------------------------

    def funding(self, symbol):
        """{'rate', 'next_time', 'interval_h'} for a perp, else None."""
        if self.inst_type(symbol) != 'SWAP':
            return None
        now = time.time()
        cached = self._funding_cache.get(symbol)
        if cached and now - cached[0] < 60:
            return cached[1]
        try:
            rows = self.client.get('/api/v5/public/funding-rate',
                                   {'instId': symbol})
        except OKXError:
            return cached[1] if cached else None
        if not rows:
            return None
        row = rows[0]
        this_t, next_t = _f(row.get('fundingTime')), _f(
            row.get('nextFundingTime'))
        interval = ((next_t - this_t) / 3_600_000.0
                    if this_t and next_t and next_t > this_t else 8.0)
        result = {'rate': _f(row.get('fundingRate'), 0.0),
                  'next_time': int(this_t / 1000) if this_t else None,
                  'interval_h': interval}
        self._funding_cache[symbol] = (now, result)
        return result

    def _swap_per_lot_day(self, symbol, row):
        """Funding as MT5 expressed swap: money per lot per day, for a
        long and for a short. Longs PAY a positive rate."""
        if row.get('instType') != 'SWAP':
            return (None, None)
        info = self.funding(symbol)
        tick = self._ticker(symbol)
        if not info or not tick:
            return (None, None)
        mid = (_f(tick.get('bidPx'), 0) + _f(tick.get('askPx'), 0)) / 2
        per_day = 24.0 / (info['interval_h'] or 8.0)
        money = info['rate'] * per_day * mid * (_f(row.get('ctVal'), 1.0))
        return (-money, money)

    # -- fees -----------------------------------------------------------

    def fees_bps(self, symbol):
        """(maker_bps, taker_bps) THIS account pays on this instrument —
        its own VIP tier, as OKX reports it. Positive = a cost."""
        kind = self.inst_type(symbol)
        if kind is None:
            return None
        now = time.time()
        cached = self._fee_cache.get(kind)
        if cached and now - cached[0] < 3600:
            return cached[1]
        params = {'instType': kind}
        row = self._inst(symbol) or {}
        if kind == 'SPOT':
            params['instId'] = symbol
        elif row.get('instFamily'):
            params['instFamily'] = row['instFamily']
        try:
            rows = self.client.get('/api/v5/account/trade-fee', params,
                                   private=True)
        except OKXError:
            return cached[1] if cached else None
        if not rows:
            return None
        maker = -(_f(rows[0].get('makerU'), None)
                  if kind != 'SPOT' and rows[0].get('makerU')
                  else _f(rows[0].get('maker'), 0.0)) * 10000.0
        taker = -(_f(rows[0].get('takerU'), None)
                  if kind != 'SPOT' and rows[0].get('takerU')
                  else _f(rows[0].get('taker'), 0.0)) * 10000.0
        result = (round(maker, 4), round(taker, 4))
        self._fee_cache[kind] = (now, result)
        return result

    def fee_level(self):
        return (self._config or {}).get('level')

    # -- account ----------------------------------------------------------

    def _balance(self, fresh=False):
        now = time.time()
        if self._bal_cache and not fresh and now - self._bal_cache[0] < 2.0:
            return self._bal_cache[1]
        rows = self.client.get('/api/v5/account/balance', private=True)
        row = rows[0] if rows else {}
        self._bal_cache = (now, row)
        return row

    def account_info(self):
        try:
            row = self._balance()
            config = self._config or self.client.get(
                '/api/v5/account/config', private=True)[0]
            self._config = config
        except OKXError as exc:
            self.last_error = str(exc)
            return None
        usdt = next((d for d in row.get('details') or ()
                     if d.get('ccy') == 'USDT'), {})
        equity = _f(row.get('totalEq'), 0.0)
        imr = _f(row.get('imr'), 0.0)
        ratio = _f(row.get('mgnRatio'))
        return SimpleNamespace(
            login=config.get('uid'),
            server='OKX demo' if self.demo else 'OKX live',
            company='OKX', name=getattr(self.account, 'name', 'okx'),
            currency='USD', leverage=None,
            balance=_f(usdt.get('cashBal'), 0.0), credit=0.0,
            equity=equity, margin=imr,
            margin_free=_f(row.get('availEq'), equity - imr),
            # OKX's margin ratio is equity over MAINTENANCE margin:
            # liquidation at 100%, OKX's own warning at 300%.
            margin_level=(ratio * 100.0) if ratio else None,
            margin_so_call=300.0, margin_so_so=100.0,
            profit=_f(row.get('upl'), 0.0),
            mmr=_f(row.get('mmr'), 0.0),
            fee_level=config.get('level'),
            pos_mode=config.get('posMode'),
        )

    def account_is_hedging(self):
        """Long/short position mode is OKX's hedging account: the
        desk's closes depend on it."""
        try:
            config = self.client.get('/api/v5/account/config',
                                     private=True)[0]
            self._config = config
        except OKXError:
            return None
        return config.get('posMode') == 'long_short_mode'

    def terminal_report(self):
        report = {'library': True, 'terminal': False, 'logged_in': False,
                  'algo_trading': False, 'hedging': False, 'login': None,
                  'server': 'OKX demo' if self.demo else 'OKX live',
                  'error': None}
        try:
            self.client.sync_clock()
            report['terminal'] = True
            config = self.client.get('/api/v5/account/config',
                                     private=True)[0]
            self._config = config
            report.update(
                logged_in=True, login=config.get('uid'),
                algo_trading='trade' in (config.get('perm') or ''),
                hedging=config.get('posMode') == 'long_short_mode',
                fee_level=config.get('level'))
            if not report['algo_trading']:
                report['error'] = ('this API key is read-only — give it '
                                   'Trade permission on OKX')
            elif not report['hedging']:
                report['error'] = ('the account is in net mode — switch '
                                   'OKX to Long/Short position mode')
        except OKXError as exc:
            report['error'] = str(exc)
        return report

    def margin_for(self, symbol, side, volume, price=None):
        """Margin for `volume` of `symbol` at this account's leverage."""
        if price is None:
            tick = self.symbol_tick(symbol)
            if not tick:
                return None
            price = tick.ask if str(side).upper().endswith('BUY') else tick.bid
        lever = self._leverage(symbol)
        size = self.contract_size(symbol)
        if not lever or not size:
            return None
        return float(volume) * size * float(price) / lever

    def _leverage(self, symbol):
        now = time.time()
        cached = self._lever_cache.get(symbol)
        if cached and now - cached[0] < 300:
            return cached[1]
        try:
            rows = self.client.get('/api/v5/account/leverage-info',
                                   {'instId': symbol, 'mgnMode': 'cross'},
                                   private=True)
        except OKXError:
            return cached[1] if cached else None
        lever = _f(rows[0].get('lever')) if rows else None
        self._lever_cache[symbol] = (now, lever)
        return lever

    def symbol_report(self, symbol):
        row = self._inst(symbol)
        if row is None:
            return {'symbol': symbol, 'found': False,
                    'error': f'{symbol} is not an OKX instrument'}
        info = self.symbol_info(symbol)
        tick = self.symbol_tick(symbol)
        fees = self.fees_bps(symbol)
        funding = self.funding(symbol)
        return {
            'symbol': symbol, 'found': True,
            'description': f"{row.get('instType')} {symbol}",
            'inst_type': row.get('instType'), 'visible': True,
            'bid': tick.bid if tick else None,
            'ask': tick.ask if tick else None,
            'digits': info.digits, 'point': info.point,
            'tick_size': info.tick_size,
            'tick_value': info.contract_size * info.tick_size,
            'contract_size': info.contract_size,
            'ct_val_ccy': info.ct_val_ccy,
            'volume_min': info.volume_min, 'volume_max': info.volume_max,
            'volume_step': info.volume_step, 'currency': 'USDT',
            'filling_mode': None, 'trade_mode': None,
            'trade_allowed': info.trade_allowed, 'expiry': info.expiry,
            'swap_long': info.swap_long, 'swap_short': info.swap_short,
            'swap_mode': info.swap_mode, 'swap_rollover3days': None,
            'maker_bps': fees[0] if fees else None,
            'taker_bps': fees[1] if fees else None,
            'fee_level': self.fee_level(),
            'funding_rate': funding['rate'] if funding else None,
            'funding_next': funding['next_time'] if funding else None,
            'funding_interval_h': funding['interval_h'] if funding else None,
            'leverage': self._leverage(symbol),
        }

    # -- positions: the ticket ledger, held to OKX's own totals ----------------

    def _exchange_positions(self):
        """{(instId, posSide): contracts} OKX holds, or None if unread."""
        now = time.time()
        if self._pos_cache and now - self._pos_cache[0] < 1.0:
            return self._pos_cache[1]
        try:
            rows = self.client.get('/api/v5/account/positions', private=True)
        except OKXError as exc:
            self.last_error = str(exc)
            return None
        table = {}
        details = {}
        for r in rows:
            key = (r['instId'], r.get('posSide') or 'net')
            table[key] = table.get(key, 0.0) + abs(_f(r.get('pos'), 0.0))
            details[key] = r
        self._pos_cache = (now, table)
        self._pos_details = details
        return table

    def exchange_position_details(self):
        """OKX's own rows (avgPx, upl, liqPx, lever, notional) keyed by
        (instId, posSide) — for the Accounts tab's liquidation prices."""
        self._exchange_positions()
        return dict(getattr(self, '_pos_details', {}))

    def _spot_holdings(self):
        """{ccy: (balance, liability)} for spot legs, or None."""
        try:
            row = self._balance()
        except OKXError:
            return None
        out = {}
        for d in row.get('details') or ():
            out[d.get('ccy')] = (_f(d.get('cashBal'), 0.0),
                                 _f(d.get('liab'), 0.0))
        return out

    def _closable(self, symbol, position, volume, exclude_order=None):
        """(volume this close may send, refusal or None).

        A close must REDUCE the position it belongs to - never be an
        opposite order that opens a new one. So before any closing order
        goes out, OKX's own book is read and the close is capped to what
        is really there:

        - **Perpetual / future** (long/short mode): what OKX holds on the
          position's own posSide, less our closing orders already
          resting on it. A close on posSide=long can only reduce a long,
          and capping it means it is never refused half-way either.
        - **Spot**: closing a long SELLS the coin, and selling coin that
          is not there BORROWS it - a short nobody asked for. Closing a
          short BUYS it back, and buying more than is owed is a new
          long. So a spot long closes at most the coin actually held,
          and a spot short at most the coin actually owed.

        Unreadable is not flat: when OKX cannot be read, nothing is sent.
        """
        volume = min(float(volume), float(position['volume']))
        side = OrderSide(position['side'])
        if self.is_derivative(symbol):
            held = self._exchange_positions()
            if held is None:
                return 0.0, ('OKX positions could not be read - nothing was '
                             'sent, so a close cannot open anything')
            there = held.get((symbol, position.get('pos_side')), 0.0)
            resting = sum(
                o['volume'] - o.get('filled', 0.0)
                for t, o in self.ledger['orders'].items()
                if t != exclude_order and o.get('position_ticket') is not None
                and o.get('symbol') == symbol
                and o.get('pos_side') == position.get('pos_side')
                and o.get('state') in ('live', 'partially_filled'))
            room = max(0.0, there - resting)
            what = f"{position.get('pos_side')} {symbol}"
        else:
            holdings = self._spot_holdings()
            if holdings is None:
                return 0.0, ('OKX balances could not be read - nothing was '
                             'sent, so a close cannot open anything')
            coin = symbol.split('-')[0]
            balance, liability = holdings.get(coin, (0.0, 0.0))
            if side is OrderSide.BUY:
                room = max(0.0, balance)            # a long: the coin held
                what = f'{coin} held'
            else:
                room = max(liability, -balance, 0.0)  # a short: coin owed
                what = f'{coin} owed'
        if room <= _EPS:
            return 0.0, (f'already flat at OKX - no {what} to close, so no '
                         f'order was sent')
        return min(volume, room), None

    def _trim_to_exchange(self):
        """Shrink the ledger to what OKX holds. A position closed outside
        the desk must read as gone, never be believed. Newest tickets are
        trimmed first: they are the ones least likely to be the trader's
        intention to keep. Returns False when OKX could not be read."""
        held = self._exchange_positions()
        if held is None:
            return False
        groups = {}
        for ticket, p in self.ledger['positions'].items():
            if self.is_derivative(p['symbol']):
                key = (p['symbol'], p.get('pos_side'))
            else:
                key = ('SPOT', p['symbol'], p['side'])
            groups.setdefault(key, []).append(ticket)
        holdings = None
        changed = False
        now = time.time()
        for key in list(self._shortfall):
            if key not in groups:
                self._shortfall.pop(key, None)
        for key, tickets in groups.items():
            if key[0] == 'SPOT':
                if holdings is None:
                    holdings = self._spot_holdings()
                    if holdings is None:
                        continue
                row = self._inst(key[1]) or {}
                base = row.get('baseCcy')
                cash, liab = holdings.get(base, (0.0, 0.0))
                available = max(cash, 0.0) if key[2] == 'BUY' else max(
                    liab, -cash, 0.0)
            else:
                available = held.get(key, 0.0)
            ours = sum(self.ledger['positions'][t]['volume'] for t in tickets)
            excess = ours - available
            if excess <= _EPS * 10 + 1e-8:
                self._shortfall.pop(key, None)
                continue
            # A shortfall must PERSIST before it is believed: OKX's
            # positions view can trail a fill by a moment, and trimming
            # on one read would throw away a position that is really on.
            first = self._shortfall.setdefault(key, now)
            if now - first < self.TRIM_PERSIST_SEC:
                continue
            for ticket in sorted(tickets, key=int, reverse=True):
                if excess <= 1e-12:
                    break
                p = self.ledger['positions'][ticket]
                if now - float(p.get('filled_at') or 0) < self.TRIM_GRACE_SEC:
                    continue                  # too new to be doubted
                cut = min(p['volume'], excess)
                p['volume'] -= cut
                excess -= cut
                logging.warning(
                    'OKX: ticket %s (%s %s) — %.8g no longer at OKX '
                    '(closed outside the desk?)', ticket, p['symbol'],
                    p['side'], cut)
                if p['volume'] <= 1e-12:
                    del self.ledger['positions'][ticket]
                changed = True
        if changed:
            self._save_ledger()
        return True

    #: A shortfall against OKX must last this long before a ticket is
    #: trimmed, and a ticket this young is never trimmed at all.
    TRIM_PERSIST_SEC = 5.0
    TRIM_GRACE_SEC = 10.0

    def positions_by_magic(self, symbol=None):
        with self._lock:
            self._sync_open_orders()
            if not self._trim_to_exchange():
                return None                   # unknown is not flat
            return [{'ticket': int(t), 'symbol': p['symbol'],
                     'side': p['side'], 'volume': p['volume'],
                     'price_open': p['price_open'],
                     'comment': p.get('comment', '')}
                    for t, p in sorted(self.ledger['positions'].items(),
                                       key=lambda kv: int(kv[0]))
                    if symbol is None or p['symbol'] == symbol]

    def verify_ticket(self, ticket, attempts=3, delay=0.4):
        p = self.ledger['positions'].get(str(int(ticket)))
        if p is None:
            return {'ticket': ticket, 'confirmed': False,
                    'error': 'not an open position of this desk'}
        return {'ticket': ticket, 'confirmed': True, 'position_open': True,
                'symbol': p['symbol'], 'volume': p['volume'],
                'price': p['price_open'], 'source': 'open position'}

    # -- orders ------------------------------------------------------------

    def _order_body(self, symbol, side, volume, ord_type, price=None,
                    pos_side=None, cl_ord_id=None):
        row = self._inst(symbol)
        if row is None:
            raise OKXError(f'{symbol} is not an OKX instrument')
        body = {'instId': symbol, 'tdMode': 'cross',
                'side': side.value.lower(), 'ordType': ord_type,
                'sz': fmt_size(volume, row.get('lotSz')), 'tag': ORDER_TAG}
        if float(body['sz']) <= 0:
            raise OKXError(f"size {volume} rounds to zero on {symbol} "
                           f"(lot {row.get('lotSz')})")
        if price is not None:
            body['px'] = fmt_price(price, row.get('tickSz'))
        if self.is_derivative(symbol):
            body['posSide'] = pos_side
        if cl_ord_id:
            body['clOrdId'] = cl_ord_id
        return body

    def _poll_order(self, symbol, ord_id, until_final=True, timeout=2.0):
        deadline = time.time() + timeout
        row = None
        while True:
            rows = self.client.get('/api/v5/trade/order',
                                   {'instId': symbol, 'ordId': ord_id},
                                   private=True)
            row = rows[0] if rows else None
            if row is None or not until_final:
                return row
            if row.get('state') in ('filled', 'canceled', 'mmp_canceled'):
                return row
            if time.time() >= deadline:
                return row
            time.sleep(0.05)

    def _protected_price(self, symbol, side, slippage_points):
        tick = self.symbol_tick(symbol)
        if not tick:
            raise OKXError(f'no price for {symbol}')
        row = self._inst(symbol)
        tick_size = _f(row.get('tickSz'), 0.01)
        touch = tick.ask if side is OrderSide.BUY else tick.bid
        cap = max(float(slippage_points or 0) * tick_size,
                  touch * self.protection_bps / 10000.0)
        price = touch + cap if side is OrderSide.BUY else touch - cap
        return touch, price

    def send_market_order(self, symbol, side, volume, slippage_points=1.0,
                          comment=''):
        """OPEN a position: an IOC limit at the touch plus the protection.
        Never closes anything — closes go through close_position_ticket."""
        with self._lock:
            ticket = self._new_ticket()
            try:
                touch, price = self._protected_price(symbol, side,
                                                     slippage_points)
                pos_side = 'long' if side is OrderSide.BUY else 'short'
                body = self._order_body(symbol, side, volume, 'ioc', price,
                                        pos_side, self._cl_ord_id(ticket))
                placed = self.client.post('/api/v5/trade/order', body)
                ord_id = placed[0]['ordId']
                row = self._poll_order(symbol, ord_id)
            except OKXError as exc:
                return OrderResult(False, error=str(exc))
            filled = _f(row.get('accFillSz'), 0.0) if row else 0.0
            avg = _f(row.get('avgPx')) if row else None
            self.ledger['ords'][ord_id] = {'ticket': ticket, 'entry': 'open',
                                           'comment': comment}
            if filled > 0:
                self.ledger['positions'][str(ticket)] = {
                    'symbol': symbol, 'side': side.value, 'volume': filled,
                    'price_open': avg, 'comment': comment,
                    'pos_side': pos_side if self.is_derivative(symbol)
                    else None,
                    'ord_id': ord_id, 'filled_at': time.time()}
            self._save_ledger()
            self._pos_cache = None
            self._bal_cache = None
            asked = float(body['sz'])
            if filled >= asked - 1e-12:
                return OrderResult(True, requested_price=touch,
                                   executed_price=avg, ticket=ticket,
                                   volume=filled)
            error = (f'OKX filled {filled:g} of {asked:g} inside the '
                     f'{self.protection_bps:g} bps cap — the rest was not '
                     f'there' if filled > 0 else
                     f'OKX found nothing within {self.protection_bps:g} bps '
                     f'of {touch:g} — not filled')
            return OrderResult(False, requested_price=touch,
                               executed_price=avg,
                               ticket=ticket if filled > 0 else None,
                               error=error, volume=filled)

    def close_position_ticket(self, symbol, ticket, volume, entry_side,
                              slippage_points=1.0, comment=''):
        """Close THIS ticket's size on its own posSide — on a long/short
        account the opposite order closes, it never opens a second."""
        with self._lock:
            p = self.ledger['positions'].get(str(int(ticket)))
            if p is None:
                return OrderResult(False, error=f'no open position {ticket} '
                                                f'on this desk')
            close_side = OrderSide(entry_side).opposite
            volume, refusal = self._closable(symbol, p, volume)
            if refusal is not None:
                if 'already flat' in refusal:
                    self._pos_cache = None
                    self._trim_to_exchange()
                return OrderResult(False, error=refusal)
            try:
                touch, price = self._protected_price(symbol, close_side,
                                                     slippage_points)
                body = self._order_body(
                    symbol, close_side, volume, 'ioc', price,
                    p.get('pos_side'), self._cl_ord_id(ticket))
                placed = self.client.post('/api/v5/trade/order', body)
                ord_id = placed[0]['ordId']
                row = self._poll_order(symbol, ord_id)
            except OKXError as exc:
                if exc.s_code == '51169':
                    # OKX holds nothing on this side: the position is
                    # already gone. Say so; the reconciler sees the trim.
                    self._pos_cache = None
                    self._trim_to_exchange()
                    return OrderResult(False, error=f'{exc} — already flat '
                                                    f'at OKX')
                return OrderResult(False, error=str(exc))
            filled = _f(row.get('accFillSz'), 0.0) if row else 0.0
            avg = _f(row.get('avgPx')) if row else None
            self.ledger['ords'][ord_id] = {'ticket': int(ticket),
                                           'entry': 'close',
                                           'comment': comment}
            if filled > 0:
                p['volume'] -= filled
                if p['volume'] <= 1e-12:
                    del self.ledger['positions'][str(int(ticket))]
            self._save_ledger()
            self._pos_cache = None
            self._bal_cache = None
            if filled >= float(body['sz']) - 1e-12:
                return OrderResult(True, requested_price=touch,
                                   executed_price=avg, ticket=int(ticket),
                                   volume=filled)
            return OrderResult(
                False, requested_price=touch, executed_price=avg,
                ticket=int(ticket), volume=filled,
                error=(f'OKX closed {filled:g} of {body["sz"]} inside the '
                       f'{self.protection_bps:g} bps cap'))

    # -- resting (post-only) orders ----------------------------------------

    def legal_limit_price(self, symbol, side, price):
        row = self._inst(symbol)
        return float(fmt_price(price, row.get('tickSz') if row else 0.01))

    def place_pending_limit(self, symbol, side, volume, price, comment='',
                            position_ticket=None, post_only=True):
        """A resting limit. Post-only by default (maker or nothing);
        `post_only=False` is a plain limit, which is what the W3 maker
        fill rests: priced passive, so it makes, but never refused for
        touching. With `position_ticket` it CLOSES that ticket (same
        posSide, opposite side)."""
        with self._lock:
            ticket = self._new_ticket()
            if position_ticket is not None:
                target = self.ledger['positions'].get(str(int(position_ticket)))
                if target is None:
                    return {'ok': False, 'ticket': None, 'price': price,
                            'error': f'no open position {position_ticket}'}
                # A resting CLOSE is capped to what OKX holds on that
                # position, like any close: it reduces, it never opens.
                volume, refusal = self._closable(symbol, target, volume)
                if refusal is not None:
                    return {'ok': False, 'ticket': None, 'price': price,
                            'error': refusal}
                pos_side = target.get('pos_side')
            else:
                pos_side = 'long' if side is OrderSide.BUY else 'short'
            try:
                body = self._order_body(symbol, side, volume,
                                        'post_only' if post_only else 'limit',
                                        price, pos_side,
                                        self._cl_ord_id(ticket))
                placed = self.client.post('/api/v5/trade/order', body)
            except OKXError as exc:
                return {'ok': False, 'ticket': None, 'price': price,
                        'error': str(exc)}
            ord_id = placed[0]['ordId']
            self.ledger['orders'][str(ticket)] = {
                'ord_id': ord_id, 'symbol': symbol, 'side': side.value,
                'volume': float(body['sz']), 'price': float(body['px']),
                'comment': comment, 'filled': 0.0, 'state': 'live',
                'pos_side': pos_side if self.is_derivative(symbol) else None,
                'position_ticket': (int(position_ticket)
                                    if position_ticket is not None else None)}
            self.ledger['ords'][ord_id] = {
                'ticket': ticket,
                'entry': 'close' if position_ticket is not None else 'open',
                # The POSITION this order closes, so its fills are filed
                # against that position's ladder, not the order's.
                'position_ticket': (int(position_ticket)
                                    if position_ticket is not None
                                    else None),
                'comment': comment}
            self._save_ledger()
            return {'ok': True, 'ticket': ticket, 'error': None,
                    'price': float(body['px'])}

    def _apply_order_row(self, ticket, row):
        """Bring the ledger up to what OKX says this resting order has
        done: new fills open (or close) position volume."""
        order = self.ledger['orders'].get(str(ticket))
        if order is None or row is None:
            return order
        acc = _f(row.get('accFillSz'), 0.0)
        delta = acc - order.get('filled', 0.0)
        avg = _f(row.get('avgPx'))
        if delta > 1e-12:
            closes = order.get('position_ticket')
            if closes is not None:
                target = self.ledger['positions'].get(str(closes))
                if target is not None:
                    target['volume'] -= delta
                    if target['volume'] <= 1e-12:
                        del self.ledger['positions'][str(closes)]
            else:
                # A filled resting order becomes a position carrying the
                # ORDER's ticket, as MT5 did.
                p = self.ledger['positions'].get(str(ticket))
                if p is None:
                    self.ledger['positions'][str(ticket)] = {
                        'symbol': order['symbol'], 'side': order['side'],
                        'volume': delta, 'price_open': avg or order['price'],
                        'comment': order.get('comment', ''),
                        'pos_side': order.get('pos_side'),
                        'ord_id': order['ord_id'], 'filled_at': time.time()}
                else:
                    p['volume'] += delta
                    p['price_open'] = avg or p['price_open']
            order['filled'] = acc
            order['avg'] = avg
            order['filled_at'] = time.time()
            # What OKX holds just changed: the next read must see it.
            self._pos_cache = None
            self._bal_cache = None
        order['state'] = row.get('state', order.get('state'))
        return order

    def _sync_open_orders(self):
        live = [t for t, o in self.ledger['orders'].items()
                if o.get('state') in ('live', 'partially_filled')]
        if not live:
            return
        changed = False
        for ticket in live:
            order = self.ledger['orders'][ticket]
            try:
                row = self._poll_order(order['symbol'], order['ord_id'],
                                       until_final=False)
            except OKXError:
                continue
            before = (order.get('filled'), order.get('state'))
            self._apply_order_row(ticket, row)
            changed = changed or before != (order.get('filled'),
                                            order.get('state'))
        if changed:
            self._pos_cache = None
            self._save_ledger()

    def order_fill_state(self, ticket):
        key = str(int(ticket))
        order = self.ledger['orders'].get(key)
        offset = self.client.clock_offset
        if order is not None:
            try:
                row = self._poll_order(order['symbol'], order['ord_id'],
                                       until_final=False)
                self._apply_order_row(key, row)
                self._save_ledger()
            except OKXError as exc:
                return {'ok': False, 'filled_volume': order.get('filled', 0.0),
                        'price': order.get('avg'), 'position_tickets': [],
                        'still_open': None, 'filled_at': None,
                        'server_offset_sec': None, 'error': str(exc)}
            filled = order.get('filled', 0.0)
            tickets = ([order['position_ticket']] if order.get(
                'position_ticket') is not None else
                [int(key)] if filled > 0 else [])
            return {'ok': True, 'filled_volume': filled,
                    'price': order.get('avg') if filled > 0 else None,
                    'position_tickets': tickets if filled > 0 else [],
                    'filled_at': (int(order['filled_at'])
                                  if filled > 0 and order.get('filled_at')
                                  else None),
                    'server_offset_sec': offset,
                    'still_open': order.get('state') in ('live',
                                                         'partially_filled'),
                    'error': None}
        p = self.ledger['positions'].get(key)
        if p is not None:
            return {'ok': True, 'filled_volume': p['volume'],
                    'price': p['price_open'], 'position_tickets': [int(key)],
                    'filled_at': int(p.get('filled_at') or time.time()),
                    'server_offset_sec': offset, 'still_open': False,
                    'error': None}
        return {'ok': True, 'filled_volume': 0.0, 'price': None,
                'position_tickets': [], 'still_open': False,
                'filled_at': None, 'server_offset_sec': None, 'error': None}

    def pending_orders_by_magic(self, symbol=None):
        """Our resting orders: the ledger's, refreshed, PLUS any order at
        OKX carrying our clOrdId that the ledger does not know — so the
        startup sweep finds what a crash left behind."""
        with self._lock:
            try:
                rows = self.client.get('/api/v5/trade/orders-pending', {},
                                       private=True)
            except OKXError as exc:
                self.last_error = str(exc)
                return None
            live_ids = {r['ordId']: r for r in rows}
            known = {o['ord_id']: t for t, o in self.ledger['orders'].items()}
            for ord_id, r in live_ids.items():
                if not (r.get('clOrdId') or '').startswith(CLORD_PREFIX):
                    continue                       # the trader's own
                if ord_id not in known:
                    ticket = self._new_ticket()
                    side = OrderSide(r['side'].upper())
                    self.ledger['orders'][str(ticket)] = {
                        'ord_id': ord_id, 'symbol': r['instId'],
                        'side': side.value, 'volume': _f(r.get('sz'), 0.0),
                        'price': _f(r.get('px'), 0.0), 'comment': 'recovered',
                        'filled': _f(r.get('accFillSz'), 0.0),
                        'state': r.get('state'), 'pos_side': r.get('posSide'),
                        'position_ticket': None}
                    known[ord_id] = str(ticket)
            for ticket, order in list(self.ledger['orders'].items()):
                if order.get('state') not in ('live', 'partially_filled'):
                    continue
                row = live_ids.get(order['ord_id'])
                if row is None:
                    try:
                        row = self._poll_order(order['symbol'],
                                               order['ord_id'],
                                               until_final=False)
                    except OKXError:
                        continue
                self._apply_order_row(ticket, row)
            self._save_ledger()
            return [{'ticket': int(t), 'symbol': o['symbol'],
                     'side': o['side'],
                     'volume': o['volume'] - o.get('filled', 0.0),
                     'price': o['price'], 'comment': o.get('comment', ''),
                     'position_ticket': o.get('position_ticket')}
                    for t, o in self.ledger['orders'].items()
                    if o.get('state') in ('live', 'partially_filled')
                    and (symbol is None or o['symbol'] == symbol)]

    def modify_pending(self, ticket, price):
        with self._lock:
            order = self.ledger['orders'].get(str(int(ticket)))
            if order is None:
                return {'ok': False, 'error': f'no resting order {ticket}'}
            row = self._inst(order['symbol']) or {}
            new_px = fmt_price(price, row.get('tickSz'))
            try:
                self.client.post('/api/v5/trade/amend-order', {
                    'instId': order['symbol'], 'ordId': order['ord_id'],
                    'newPx': new_px})
            except OKXError as exc:
                # A post-only amend that would cross is CANCELLED by OKX.
                try:
                    self._apply_order_row(str(int(ticket)), self._poll_order(
                        order['symbol'], order['ord_id'], until_final=False))
                    self._save_ledger()
                except OKXError:
                    pass
                return {'ok': False, 'error': str(exc)}
            order['price'] = float(new_px)
            self._save_ledger()
            return {'ok': True, 'error': None}

    def cancel_pending(self, ticket):
        with self._lock:
            key = str(int(ticket))
            order = self.ledger['orders'].get(key)
            if order is None:
                state = self.order_fill_state(ticket)
                state.update(cancelled=False, error=f'no resting order '
                                                    f'{ticket}')
                return state
            cancelled = True
            error = None
            try:
                self.client.post('/api/v5/trade/cancel-order', {
                    'instId': order['symbol'], 'ordId': order['ord_id']})
            except OKXError as exc:
                cancelled = False
                error = str(exc)
            state = self.order_fill_state(ticket)
            state['cancelled'] = cancelled
            if error:
                state['error'] = error
            return state

    # -- the journal: OKX's own fills ---------------------------------------

    def order_log(self, hours=24):
        """Every fill OKX reports on this account in the window — ours and
        the trader's own clicks on the OKX app alike — keyed exactly as
        the journal reads them."""
        since = (time.time() - hours * 3600) * 1000
        rows = []
        for kind in ('SPOT', 'SWAP', 'FUTURES'):
            try:
                path = ('/api/v5/trade/fills' if hours <= 72
                        else '/api/v5/trade/fills-history')
                rows.extend(self.client.get(path, {'instType': kind,
                                                   'limit': '100'},
                                            private=True))
            except OKXError as exc:
                self.last_error = str(exc)
        out = []
        offset = self.client.clock_offset
        for r in rows:
            ts = int(r.get('ts') or r.get('fillTime') or 0)
            if ts < since:
                continue
            ours = (r.get('clOrdId') or '').startswith(CLORD_PREFIX)
            meta = self.ledger['ords'].get(r.get('ordId'), {})
            side = (r.get('side') or '').lower()
            pos_side = r.get('posSide') or 'net'
            entry = meta.get('entry') or (
                'open' if (pos_side, side) in (('long', 'buy'),
                                               ('short', 'sell'))
                else 'close' if pos_side in ('long', 'short') else 'open')
            fee = _f(r.get('fee'), 0.0)
            out.append({
                'order_id': r.get('ordId'), 'deal_id': r.get('tradeId'),
                # A DEAL, in the journal's own vocabulary: every row
                # here is a fill (the instrument's type is in its id).
                'symbol': r.get('instId'), 'inst_type': 'DEAL',
                'side': side, 'pos_side': entry, 'order_type':
                    'maker' if r.get('execType') == 'M' else 'taker',
                'quantity': _f(r.get('fillSz'), 0.0),
                'fill_qty': _f(r.get('fillSz'), 0.0),
                'fill_price': _f(r.get('fillPx'), 0.0),
                'commission': fee, 'swap': 0.0, 'fee': fee,
                'fee_ccy': r.get('feeCcy') or 'USDT',
                'pnl': _f(r.get('fillPnl'), 0.0), 'state': 'filled',
                'filled_at': ts,
                # The position ticket: an opening fill IS its position;
                # a closing fill points at the position it closed.
                'position_id': (meta.get('position_ticket')
                                or meta.get('ticket')),
                'magic': MAGIC_NUMBER if ours else 0, 'is_bot': ours,
                'comment': meta.get('comment', ''),
                'server_offset_sec': offset,
            })
        out.sort(key=lambda d: d['filled_at'])
        return out
