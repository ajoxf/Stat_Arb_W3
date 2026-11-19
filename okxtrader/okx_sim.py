"""A simulated OKX exchange that answers the v5 REST API.

For tests and demos only — never on a live desk. It speaks the same
envelope OKX does (`{'code': '0', 'data': [...]}`, sCode/sMsg on a
refusal), so the broker that talks to it is the broker that talks to
OKX: the same requests, the same parsing, the same error handling.

Two ways in:
  * `SimOKX().handle(method, path, params, body)` — in-process, injected
    as `OKXClient(transport=...)`;
  * `python -m okxtrader.okx_sim --port 8771` — an HTTP server, so the
    web process and the engine (two processes) share ONE exchange, and
    the broker's real HTTP + signing path is exercised. Point the desk
    at it with OKX_BASE_URL=http://127.0.0.1:8771.

What it models, because the desk depends on it:
  * instruments with OKX's own specs (ctVal, lotSz, minSz, tickSz,
    expTime) for spot, USDT-margined perps and a quarterly future;
  * a BTC and an ETH index with each instrument's own basis on top, so
    spreads between legs MEAN-REVERT like a real basis does;
  * a 5-level book per instrument, candles, 24 h stats, funding;
  * an account in long/short position mode: positions per
    (instrument, posSide), spot margin balances that go negative when
    sold short (a liability), equity, IMR/MMR, margin ratio, liq price;
  * orders: market, IOC, limit and post_only. A post_only that would
    cross is refused (51124 in OKX's words); a resting one fills when
    the market trades through it, or at the touch now and then as a
    queue would; maker/taker fees by instrument type and VIP tier;
  * fills history, the source of the journal.
"""

import argparse
import itertools
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

#: Fee schedule per VIP tier (maker, taker), as OKX quotes them: a
#: NEGATIVE rate is charged to the user. Regular user = 'Lv1'.
FEES = {
    'Lv1': {'SPOT': (-0.0008, -0.0010), 'SWAP': (-0.0002, -0.0005),
            'FUTURES': (-0.0002, -0.0005)},
    'VIP1': {'SPOT': (-0.00045, -0.0005), 'SWAP': (-0.0001, -0.0003),
             'FUTURES': (-0.0001, -0.0003)},
}

QUARTERLY = 'BTC-USD_UM-261225'
QUARTERLY_EXPIRY_MS = 1798185600000          # 2026-12-25 08:00 UTC

INSTRUMENTS = {
    'BTC-USDT': dict(instType='SPOT', baseCcy='BTC', quoteCcy='USDT',
                     ctVal='', lotSz='0.00000001', minSz='0.00001',
                     tickSz='0.1', base='BTC', basis=0.0, half=0.05),
    'ETH-USDT': dict(instType='SPOT', baseCcy='ETH', quoteCcy='USDT',
                     ctVal='', lotSz='0.000001', minSz='0.0001',
                     tickSz='0.01', base='ETH', basis=0.0, half=0.005),
    'BTC-USDT-SWAP': dict(instType='SWAP', ctVal='0.01', ctValCcy='BTC',
                          settleCcy='USDT', lotSz='0.01', minSz='0.01',
                          tickSz='0.1', base='BTC', basis=0.00045,
                          half=0.05),
    'ETH-USDT-SWAP': dict(instType='SWAP', ctVal='0.1', ctValCcy='ETH',
                          settleCcy='USDT', lotSz='0.01', minSz='0.01',
                          tickSz='0.01', base='ETH', basis=0.00040,
                          half=0.005),
    QUARTERLY: dict(instType='FUTURES', ctVal='0.01', ctValCcy='BTC',
                    settleCcy='USDT', lotSz='0.1', minSz='0.1',
                    tickSz='0.1', base='BTC', basis=0.0105, half=0.1,
                    expTime=str(QUARTERLY_EXPIRY_MS)),
}

INDEX_START = {'BTC': 65000.0, 'ETH': 3500.0}

BARS = {'1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1H': 3600,
        '4H': 14400, '1Dutc': 86400}


def _ok(rows):
    return {'code': '0', 'msg': '', 'data': rows}


def _refuse(s_code, s_msg, code='1', msg='Operation failed.'):
    return {'code': code, 'msg': msg,
            'data': [{'sCode': str(s_code), 'sMsg': s_msg}]}


def _decimals(step):
    text = str(step)
    return len(text.split('.')[1]) if '.' in text else 0


def _round_to(value, step):
    step = float(step)
    return round(round(value / step) * step, _decimals(step))


class SimOKX:
    """One OKX account and the market it trades in."""

    def __init__(self, seed=7, vip='Lv1', usdt=100_000.0, tick_sec=0.25,
                 maker_fill_prob=0.35, autostart=True):
        self.rng = random.Random(seed)
        self.vip = vip
        self.maker_fill_prob = maker_fill_prob
        self.lock = threading.RLock()
        self.index = dict(INDEX_START)
        #: Each instrument's own basis over its index, an OU process
        #: around the instrument's mean — the thing a spread trader buys
        #: and sells.
        self.basis = {k: v['basis'] for k, v in INSTRUMENTS.items()}
        self.vol24 = {k: 0.0 for k in INSTRUMENTS}
        self.open24 = {}
        self.high24 = {}
        self.low24 = {}
        self.balances = {'USDT': float(usdt), 'BTC': 0.0, 'ETH': 0.0}
        #: (instId, posSide) -> {'pos': contracts, 'avgPx': float, 'cTime'}
        self.positions = {}
        self.leverage = {k: ('10' if v['instType'] == 'SPOT' else '3')
                         for k, v in INSTRUMENTS.items()}
        self.orders = {}
        self.fills = []
        self._ord = itertools.count(3_578_400_000_000_000_001)
        self._trade = itertools.count(912_000_001)
        self.pos_mode = 'long_short_mode'
        self.uid = '44871023598'
        self._stop = threading.Event()
        self.tick_sec = tick_sec
        for inst in INSTRUMENTS:
            mid = self._mid(inst)
            self.open24[inst] = mid * (1 - 0.004)
            self.high24[inst] = mid * 1.012
            self.low24[inst] = mid * 0.991
        if autostart:
            threading.Thread(target=self._run, daemon=True,
                             name='SimOKX').start()

    # -- the market ---------------------------------------------------------

    def _mid(self, inst):
        spec = INSTRUMENTS[inst]
        return self.index[spec['base']] * (1.0 + self.basis[inst])

    def _touch(self, inst):
        spec = INSTRUMENTS[inst]
        mid = self._mid(inst)
        tick = float(spec['tickSz'])
        half = max(tick, mid * spec['half'] / 10000.0)
        bid = math.floor((mid - half) / tick) * tick
        ask = max(bid + tick, math.ceil((mid + half) / tick) * tick)
        return (round(bid, _decimals(spec['tickSz'])),
                round(ask, _decimals(spec['tickSz'])))

    def step(self, dt=None):
        """Move the market one tick and match resting orders."""
        dt = self.tick_sec if dt is None else dt
        with self.lock:
            for base in self.index:
                vol = 0.55 / math.sqrt(365 * 86400)     # ~55% a year
                self.index[base] *= math.exp(
                    self.rng.gauss(0, vol * math.sqrt(dt)))
            for inst, spec in INSTRUMENTS.items():
                if spec['instType'] == 'SPOT':
                    continue
                mean = spec['basis']
                # Mean-reverting basis with a half-life of a few minutes
                # and noise of ~1 bp a tick: tradeable stretches.
                theta = 0.004
                noise = 0.00009 if spec['instType'] == 'SWAP' else 0.00012
                self.basis[inst] += (theta * (mean - self.basis[inst])
                                     + self.rng.gauss(0, noise))
                mid = self._mid(inst)
                self.high24[inst] = max(self.high24[inst], mid)
                self.low24[inst] = min(self.low24[inst], mid)
            for inst in INSTRUMENTS:
                self.vol24[inst] += abs(self.rng.gauss(0, 3))
            self._match_resting()

    def _run(self):
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self.tick_sec)

    def stop(self):
        self._stop.set()

    # -- request dispatch ------------------------------------------------

    def handle(self, method, path, params=None, body=None):
        params = params or {}
        routes = {
            ('GET', '/api/v5/public/time'): self.public_time,
            ('GET', '/api/v5/public/instruments'): self.instruments,
            ('GET', '/api/v5/public/funding-rate'): self.funding_rate,
            ('GET', '/api/v5/public/mark-price'): self.mark_price,
            ('GET', '/api/v5/market/ticker'): self.ticker,
            ('GET', '/api/v5/market/tickers'): self.tickers,
            ('GET', '/api/v5/market/books'): self.books,
            ('GET', '/api/v5/market/candles'): self.candles,
            ('GET', '/api/v5/market/history-candles'): self.candles,
            ('GET', '/api/v5/account/config'): self.account_config,
            ('GET', '/api/v5/account/balance'): self.balance,
            ('GET', '/api/v5/account/positions'): self.account_positions,
            ('GET', '/api/v5/account/trade-fee'): self.trade_fee,
            ('GET', '/api/v5/account/leverage-info'): self.leverage_info,
            ('POST', '/api/v5/account/set-leverage'): self.set_leverage,
            ('POST', '/api/v5/trade/order'): self.place_order,
            ('POST', '/api/v5/trade/amend-order'): self.amend_order,
            ('POST', '/api/v5/trade/cancel-order'): self.cancel_order,
            ('GET', '/api/v5/trade/order'): self.get_order,
            ('GET', '/api/v5/trade/orders-pending'): self.orders_pending,
            ('GET', '/api/v5/trade/fills'): self.get_fills,
            ('GET', '/api/v5/trade/fills-history'): self.get_fills,
        }
        handler = routes.get((method.upper(), path))
        if handler is None:
            return {'code': '50014', 'msg': f'Path not found: {path}',
                    'data': []}
        with self.lock:
            if method.upper() == 'GET':
                return handler(params)
            return handler(body or {})

    __call__ = handle

    # -- public ----------------------------------------------------------

    def public_time(self, params):
        return _ok([{'ts': str(int(time.time() * 1000))}])

    def _inst_row(self, inst):
        spec = INSTRUMENTS[inst]
        row = {
            'instId': inst, 'instType': spec['instType'], 'state': 'live',
            'lotSz': spec['lotSz'], 'minSz': spec['minSz'],
            'tickSz': spec['tickSz'], 'ctVal': spec['ctVal'],
            'ctValCcy': spec.get('ctValCcy', ''),
            'ctType': 'linear' if spec['instType'] != 'SPOT' else '',
            'settleCcy': spec.get('settleCcy', ''),
            'baseCcy': spec.get('baseCcy', ''),
            'quoteCcy': spec.get('quoteCcy', ''),
            'uly': (f"{spec['base']}-USDT"
                    if spec['instType'] != 'SPOT' else ''),
            'instFamily': (f"{spec['base']}-USDT"
                           if spec['instType'] != 'SPOT' else ''),
            'expTime': spec.get('expTime', ''),
            'listTime': '1590000000000',
            'lever': '' if spec['instType'] == 'SPOT' else '100',
            'maxMktSz': '1000000', 'maxLmtSz': '1000000',
        }
        return row

    def instruments(self, params):
        kind = params.get('instType')
        wanted = params.get('instId')
        rows = [self._inst_row(i) for i, s in INSTRUMENTS.items()
                if (not kind or s['instType'] == kind)
                and (not wanted or i == wanted)]
        return _ok(rows)

    def _ticker_row(self, inst):
        bid, ask = self._touch(inst)
        mid = (bid + ask) / 2
        return {
            'instId': inst, 'instType': INSTRUMENTS[inst]['instType'],
            'last': str(round(mid, _decimals(INSTRUMENTS[inst]['tickSz']))),
            'lastSz': '0.01', 'bidPx': str(bid), 'bidSz': '12.4',
            'askPx': str(ask), 'askSz': '9.8',
            'open24h': str(round(self.open24[inst], 2)),
            'high24h': str(round(self.high24[inst], 2)),
            'low24h': str(round(self.low24[inst], 2)),
            'vol24h': str(round(18000 + self.vol24[inst], 2)),
            'volCcy24h': str(round((18000 + self.vol24[inst]) * mid, 0)),
            'ts': str(int(time.time() * 1000)),
        }

    def ticker(self, params):
        inst = params.get('instId')
        if inst not in INSTRUMENTS:
            return _refuse('51001', 'Instrument ID does not exist.',
                           code='51001', msg='Instrument ID does not exist.')
        return _ok([self._ticker_row(inst)])

    def tickers(self, params):
        kind = params.get('instType')
        return _ok([self._ticker_row(i) for i, s in INSTRUMENTS.items()
                    if not kind or s['instType'] == kind])

    def books(self, params):
        inst = params.get('instId')
        if inst not in INSTRUMENTS:
            return _refuse('51001', 'Instrument ID does not exist.',
                           code='51001')
        depth = int(params.get('sz') or 5)
        spec = INSTRUMENTS[inst]
        tick = float(spec['tickSz'])
        bid, ask = self._touch(inst)
        unit = 1.0 if spec['instType'] != 'SPOT' else 0.05
        rng = random.Random(int(time.time() * 4) ^ hash(inst))
        asks, bids = [], []
        for level in range(depth):
            size_a = round(unit * rng.uniform(4, 60), 4)
            size_b = round(unit * rng.uniform(4, 60), 4)
            asks.append([str(round(ask + level * tick, 6)), str(size_a),
                         '0', str(rng.randint(1, 9))])
            bids.append([str(round(bid - level * tick, 6)), str(size_b),
                         '0', str(rng.randint(1, 9))])
        return _ok([{'asks': asks, 'bids': bids,
                     'ts': str(int(time.time() * 1000))}])

    def candles(self, params):
        """Deterministic history consistent with the live price, newest
        first as OKX returns them: [ts, o, h, l, c, vol, volCcy,
        volCcyQuote, confirm]."""
        inst = params.get('instId')
        if inst not in INSTRUMENTS:
            return _refuse('51001', 'Instrument ID does not exist.',
                           code='51001')
        bar = params.get('bar', '1m')
        sec = BARS.get(bar, 60)
        limit = min(int(params.get('limit') or 100), 300)
        after = params.get('after')
        now_bucket = int(time.time() // sec) * sec
        end = (int(after) // 1000 - sec) if after else now_bucket
        spec = INSTRUMENTS[inst]
        rows = []
        for i in range(limit):
            start = end - i * sec
            closes = self._hist_mid(inst, start + sec, sec)
            opens = self._hist_mid(inst, start, sec)
            hi = max(opens, closes) * (1 + 0.0004)
            lo = min(opens, closes) * (1 - 0.0004)
            d = _decimals(spec['tickSz'])
            confirm = '0' if start == now_bucket else '1'
            rows.append([str(start * 1000), str(round(opens, d)),
                         str(round(hi, d)), str(round(lo, d)),
                         str(round(closes, d)), '120', '7800000', '7800000',
                         confirm])
        return _ok(rows)

    def _hist_mid(self, inst, t, sec):
        """The instrument's mid at time t: the live mid now, walked back
        by a deterministic path of the index and the basis."""
        spec = INSTRUMENTS[inst]
        now = time.time()
        steps = max(0, int((now - t) // sec))
        base_rng = random.Random(f"{spec['base']}:{sec}")
        basis_rng = random.Random(f'{inst}:{sec}')
        index = self.index[spec['base']]
        basis = self.basis[inst]
        vol = 0.55 / math.sqrt(365 * 86400) * math.sqrt(sec)
        # The basis path: an AR(1) around the mean, walked back.
        mean = spec['basis']
        noise = (0.00025 if spec['instType'] == 'SWAP'
                 else 0.0004 if spec['instType'] == 'FUTURES' else 0.0)
        for _ in range(steps):
            index /= math.exp(base_rng.gauss(0, vol))
            if noise:
                basis = mean + 0.85 * (basis - mean) + basis_rng.gauss(0, noise)
        return index * (1.0 + (basis if noise else 0.0))

    def funding_rate(self, params):
        inst = params.get('instId')
        if inst not in INSTRUMENTS or INSTRUMENTS[inst]['instType'] != 'SWAP':
            return _refuse('51001', 'Instrument ID does not exist.',
                           code='51001')
        now = int(time.time())
        nxt = (now // 28800 + 1) * 28800
        rate = 0.0001 if inst.startswith('BTC') else 0.00008
        return _ok([{'instId': inst, 'instType': 'SWAP',
                     'fundingRate': str(rate), 'nextFundingRate': '',
                     'fundingTime': str(nxt * 1000),
                     'nextFundingTime': str((nxt + 28800) * 1000),
                     'method': 'current_period'}])

    def mark_price(self, params):
        inst = params.get('instId')
        rows = []
        for i in ([inst] if inst else list(INSTRUMENTS)):
            if i in INSTRUMENTS:
                rows.append({'instId': i, 'markPx': str(round(self._mid(i), 2)),
                             'ts': str(int(time.time() * 1000))})
        return _ok(rows)

    # -- account -------------------------------------------------------

    def account_config(self, params):
        return _ok([{'uid': self.uid, 'acctLv': '3', 'posMode': self.pos_mode,
                     'perm': 'read_only,trade', 'level': self.vip,
                     'label': 'nexus-desk', 'autoLoan': True}])

    def _ct(self, inst):
        ct = INSTRUMENTS[inst]['ctVal']
        return float(ct) if ct else 1.0

    def _upl(self, inst, side, pos, avg):
        mid = self._mid(inst)
        sign = 1 if side == 'long' else -1
        return sign * (mid - avg) * pos * self._ct(inst)

    def _margin_numbers(self):
        imr = 0.0
        upl = 0.0
        for (inst, side), p in self.positions.items():
            if p['pos'] <= 0:
                continue
            notional = p['pos'] * self._ct(inst) * self._mid(inst)
            imr += notional / float(self.leverage[inst])
            upl += self._upl(inst, side, p['pos'], p['avgPx'])
        for ccy in ('BTC', 'ETH'):
            bal = self.balances.get(ccy, 0.0)
            if bal < 0:                    # a spot short is a borrow
                imr += abs(bal) * self.index[ccy] / 10.0
        equity = self.balances['USDT'] + upl
        for ccy in ('BTC', 'ETH'):
            equity += self.balances.get(ccy, 0.0) * self.index[ccy]
        mmr = imr * 0.4
        return equity, imr, mmr, upl

    def balance(self, params):
        equity, imr, mmr, upl = self._margin_numbers()
        details = []
        for ccy, bal in self.balances.items():
            px = 1.0 if ccy == 'USDT' else self.index[ccy]
            details.append({
                'ccy': ccy, 'eq': str(round(bal, 8)),
                'cashBal': str(round(bal, 8)),
                'availBal': str(round(max(bal, 0.0), 8)),
                'liab': str(round(-bal, 8)) if bal < 0 else '0',
                'eqUsd': str(round(bal * px, 2)),
                'upl': str(round(upl, 2)) if ccy == 'USDT' else '0',
                'interest': '0',
            })
        return _ok([{
            'totalEq': str(round(equity, 2)), 'adjEq': str(round(equity, 2)),
            'imr': str(round(imr, 2)), 'mmr': str(round(mmr, 2)),
            'mgnRatio': str(round(equity / mmr, 4)) if mmr else '',
            'notionalUsd': str(round(imr * 3, 2)), 'upl': str(round(upl, 2)),
            'availEq': str(round(equity - imr, 2)),
            'details': details, 'uTime': str(int(time.time() * 1000)),
        }])

    def account_positions(self, params):
        kind = params.get('instType')
        inst_filter = params.get('instId')
        rows = []
        equity, imr, mmr, _ = self._margin_numbers()
        for (inst, side), p in self.positions.items():
            if p['pos'] <= 0:
                continue
            if kind and INSTRUMENTS[inst]['instType'] != kind:
                continue
            if inst_filter and inst != inst_filter:
                continue
            mid = self._mid(inst)
            lever = float(self.leverage[inst])
            # Cross margin: liquidation where the whole account's equity
            # is gone to maintenance — approximated per position.
            room = (equity - mmr) / max(p['pos'] * self._ct(inst), 1e-12)
            liq = mid - room if side == 'long' else mid + room
            rows.append({
                'instId': inst, 'instType': INSTRUMENTS[inst]['instType'],
                'posSide': side, 'pos': str(p['pos']),
                'availPos': str(p['pos']), 'avgPx': str(round(p['avgPx'], 4)),
                'markPx': str(round(mid, 4)),
                'upl': str(round(self._upl(inst, side, p['pos'],
                                           p['avgPx']), 4)),
                'lever': str(lever), 'mgnMode': 'cross',
                'liqPx': str(round(max(liq, 0.0), 2)),
                'notionalUsd': str(round(p['pos'] * self._ct(inst) * mid, 2)),
                'imr': str(round(p['pos'] * self._ct(inst) * mid / lever, 2)),
                'ccy': 'USDT', 'cTime': str(p['cTime']),
                'uTime': str(int(time.time() * 1000)),
            })
        return _ok(rows)

    def trade_fee(self, params):
        kind = params.get('instType', 'SPOT')
        maker, taker = FEES.get(self.vip, FEES['Lv1']).get(kind, (0, 0))
        return _ok([{'instType': kind, 'level': self.vip,
                     'maker': str(maker), 'taker': str(taker),
                     'makerU': str(maker), 'takerU': str(taker),
                     'ts': str(int(time.time() * 1000))}])

    def leverage_info(self, params):
        inst = params.get('instId')
        rows = []
        for i in (inst.split(',') if inst else []):
            if i in INSTRUMENTS:
                rows.append({'instId': i, 'mgnMode': params.get('mgnMode',
                                                                'cross'),
                             'lever': self.leverage[i], 'posSide': ''})
        return _ok(rows)

    def set_leverage(self, body):
        inst = body.get('instId')
        if inst not in INSTRUMENTS:
            return _refuse('51001', 'Instrument ID does not exist.')
        self.leverage[inst] = str(body.get('lever', '3'))
        return _ok([{'instId': inst, 'lever': self.leverage[inst],
                     'mgnMode': body.get('mgnMode', 'cross')}])

    # -- trading -------------------------------------------------------

    def _order_row(self, o):
        return {k: (str(v) if not isinstance(v, str) else v)
                for k, v in o.items() if not k.startswith('_')}

    def place_order(self, body):
        inst = body.get('instId')
        if inst not in INSTRUMENTS:
            return _refuse('51001', 'Instrument ID does not exist.')
        spec = INSTRUMENTS[inst]
        side = (body.get('side') or '').lower()
        ord_type = (body.get('ordType') or 'market').lower()
        try:
            sz = float(body.get('sz'))
        except (TypeError, ValueError):
            return _refuse('51000', 'Parameter sz error')
        if sz < float(spec['minSz']) - 1e-12:
            return _refuse('51020', f"Order quantity must be greater than "
                                    f"or equal to {spec['minSz']}")
        pos_side = body.get('posSide') or ('net' if spec['instType'] == 'SPOT'
                                           else '')
        if spec['instType'] != 'SPOT' and pos_side not in ('long', 'short'):
            return _refuse('51000', 'Parameter posSide error')
        bid, ask = self._touch(inst)
        px = body.get('px')
        px = float(px) if px not in (None, '') else None
        if ord_type in ('limit', 'post_only', 'ioc', 'fok') and px is None:
            return _refuse('51000', 'Parameter px error')
        if ord_type == 'post_only' and px is not None:
            crosses = (side == 'buy' and px >= ask) or \
                      (side == 'sell' and px <= bid)
            if crosses:
                return _refuse('51124', 'The post-only order will take '
                               'liquidity on the order book, and the order '
                               'is canceled.')
        # Closing a perp/future with no position on that side.
        if spec['instType'] != 'SPOT':
            closing = (pos_side == 'long' and side == 'sell') or \
                      (pos_side == 'short' and side == 'buy')
            held = self.positions.get((inst, pos_side), {}).get('pos', 0.0)
            if closing and held < sz - 1e-9:
                return _refuse('51169', "Order failed because you don't have "
                               'any positions in this direction for this '
                               'contract.')
        ord_id = str(next(self._ord))
        now = int(time.time() * 1000)
        order = {
            'ordId': ord_id, 'clOrdId': body.get('clOrdId', ''),
            'tag': body.get('tag', ''), 'instId': inst,
            'instType': spec['instType'], 'side': side, 'posSide': pos_side,
            'ordType': ord_type, 'px': '' if px is None else px, 'sz': sz,
            'tdMode': body.get('tdMode', 'cross'),
            'reduceOnly': str(body.get('reduceOnly', 'false')).lower(),
            'state': 'live', 'accFillSz': 0.0, 'avgPx': '',
            'fillPx': '', 'fillSz': '', 'fee': 0.0,
            'feeCcy': 'USDT', 'pnl': 0.0, 'cTime': now, 'uTime': now,
            '_cost': 0.0,
        }
        self.orders[ord_id] = order
        if ord_type == 'market':
            self._fill(order, sz, ask if side == 'buy' else bid, taker=True)
        elif ord_type in ('ioc', 'fok', 'limit'):
            crosses = (side == 'buy' and px >= ask) or \
                      (side == 'sell' and px <= bid)
            if crosses:
                self._fill(order, sz, ask if side == 'buy' else bid,
                           taker=True)
            elif ord_type in ('ioc', 'fok'):
                order['state'] = 'canceled'
        return _ok([{'ordId': ord_id, 'clOrdId': order['clOrdId'],
                     'tag': order['tag'], 'sCode': '0',
                     'sMsg': 'Order placed', 'ts': str(now)}])

    def amend_order(self, body):
        order = self.orders.get(str(body.get('ordId')))
        if order is None or order['state'] not in ('live', 'partially_filled'):
            return _refuse('51503', 'Order modification failed as the order '
                           'has been completed.')
        if body.get('newPx') not in (None, ''):
            new_px = float(body['newPx'])
            bid, ask = self._touch(order['instId'])
            if order['ordType'] == 'post_only' and (
                    (order['side'] == 'buy' and new_px >= ask) or
                    (order['side'] == 'sell' and new_px <= bid)):
                order['state'] = 'canceled'
                return _refuse('51124', 'The post-only order will take '
                               'liquidity on the order book, and the order '
                               'is canceled.')
            order['px'] = new_px
        order['uTime'] = int(time.time() * 1000)
        return _ok([{'ordId': order['ordId'], 'clOrdId': order['clOrdId'],
                     'sCode': '0', 'sMsg': ''}])

    def cancel_order(self, body):
        order = self.orders.get(str(body.get('ordId')))
        if order is None:
            return _refuse('51400', 'Order cancellation failed as the order '
                           'does not exist.')
        if order['state'] not in ('live', 'partially_filled'):
            return _refuse('51401', 'Order cancellation failed as the order '
                           'has been completed.')
        order['state'] = 'canceled'
        order['uTime'] = int(time.time() * 1000)
        return _ok([{'ordId': order['ordId'], 'clOrdId': order['clOrdId'],
                     'sCode': '0', 'sMsg': ''}])

    def get_order(self, params):
        order = self.orders.get(str(params.get('ordId')))
        if order is None:
            return _refuse('51603', 'Order does not exist', code='51603',
                           msg='Order does not exist')
        return _ok([self._order_row(order)])

    def orders_pending(self, params):
        inst = params.get('instId')
        return _ok([self._order_row(o) for o in self.orders.values()
                    if o['state'] in ('live', 'partially_filled')
                    and (not inst or o['instId'] == inst)])

    def get_fills(self, params):
        inst = params.get('instId')
        kind = params.get('instType')
        rows = [f for f in reversed(self.fills)
                if (not inst or f['instId'] == inst)
                and (not kind or f['instType'] == kind)]
        return _ok(rows[:int(params.get('limit') or 100)])

    # -- matching ------------------------------------------------------

    def _match_resting(self):
        for order in list(self.orders.values()):
            if order['state'] not in ('live', 'partially_filled'):
                continue
            if order['ordType'] not in ('limit', 'post_only'):
                continue
            bid, ask = self._touch(order['instId'])
            px = float(order['px'])
            rest = order['sz'] - order['accFillSz']
            if rest <= 1e-12:
                continue
            through = (order['side'] == 'buy' and ask <= px) or \
                      (order['side'] == 'sell' and bid >= px)
            at_touch = (order['side'] == 'buy' and px >= bid) or \
                       (order['side'] == 'sell' and px <= ask)
            if through or (at_touch
                           and self.rng.random() < self.maker_fill_prob):
                self._fill(order, rest, px, taker=False)

    def _fill(self, order, qty, price, taker):
        inst = order['instId']
        spec = INSTRUMENTS[inst]
        kind = spec['instType']
        maker_rate, taker_rate = FEES.get(self.vip, FEES['Lv1'])[kind]
        rate = taker_rate if taker else maker_rate
        notional = qty * self._ct(inst) * price
        fee = notional * rate                         # negative = charged
        pnl = 0.0
        side, pos_side = order['side'], order['posSide']
        if kind == 'SPOT':
            base = spec['baseCcy']
            if side == 'buy':
                self.balances[base] = self.balances.get(base, 0.0) + qty
                self.balances['USDT'] -= notional
            else:
                self.balances[base] = self.balances.get(base, 0.0) - qty
                self.balances['USDT'] += notional
        else:
            key = (inst, pos_side)
            p = self.positions.setdefault(
                key, {'pos': 0.0, 'avgPx': 0.0,
                      'cTime': int(time.time() * 1000)})
            opening = (pos_side == 'long' and side == 'buy') or \
                      (pos_side == 'short' and side == 'sell')
            if opening:
                total = p['pos'] + qty
                p['avgPx'] = ((p['avgPx'] * p['pos'] + price * qty) / total
                              if total else price)
                p['pos'] = total
            else:
                qty = min(qty, p['pos'])
                sign = 1 if pos_side == 'long' else -1
                pnl = sign * (price - p['avgPx']) * qty * self._ct(inst)
                p['pos'] -= qty
                self.balances['USDT'] += pnl
                if p['pos'] <= 1e-12:
                    del self.positions[key]
        self.balances['USDT'] += fee
        order['_cost'] += price * qty
        order['accFillSz'] += qty
        order['avgPx'] = order['_cost'] / order['accFillSz']
        order['fillPx'] = price
        order['fillSz'] = qty
        order['fee'] += fee
        order['pnl'] += pnl
        order['state'] = ('filled' if order['accFillSz'] >= order['sz'] - 1e-12
                          else 'partially_filled')
        order['uTime'] = int(time.time() * 1000)
        self.fills.append({
            'instType': kind, 'instId': inst, 'tradeId': str(next(self._trade)),
            'ordId': order['ordId'], 'clOrdId': order['clOrdId'],
            'tag': order['tag'], 'billId': str(next(self._trade)),
            'fillPx': str(price), 'fillSz': str(qty), 'side': side,
            'posSide': pos_side, 'execType': 'T' if taker else 'M',
            'feeCcy': 'USDT', 'fee': str(round(fee, 8)),
            'fillPnl': str(round(pnl, 8)), 'ts': str(order['uTime']),
            'fillTime': str(order['uTime']),
        })


# -- HTTP ----------------------------------------------------------------


def _auth_refusal(sim, headers, method, path, raw):
    """OKX's own answer to a private request whose key is wrong, or
    None. With `sim.keys` unset any signed request passes (the demo);
    set to {api_key: (secret, passphrase)}, the key, passphrase and the
    HMAC signature are all checked as OKX checks them, and a request
    without the demo header is a live request (50101 on a demo key)."""
    if not headers.get('OK-ACCESS-SIGN'):
        return {'code': '50113', 'msg': 'Invalid Sign', 'data': []}
    keys = getattr(sim, 'keys', None)
    if not keys:
        return None
    key = headers.get('OK-ACCESS-KEY') or ''
    if key not in keys:
        return {'code': '50111', 'msg': 'Invalid OK-ACCESS-KEY', 'data': []}
    secret, passphrase = keys[key]
    if headers.get('x-simulated-trading') != '1':
        return {'code': '50101', 'msg': 'APIKey does not match current '
                                        'environment.', 'data': []}
    if headers.get('OK-ACCESS-PASSPHRASE') != passphrase:
        return {'code': '50105', 'msg': 'Invalid OK-ACCESS-PASSPHRASE',
                'data': []}
    import base64
    import hashlib
    import hmac
    message = (headers.get('OK-ACCESS-TIMESTAMP', '') + method + path
               + (raw.decode() if raw else ''))
    want = base64.b64encode(hmac.new(secret.encode(), message.encode(),
                                     hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(want, headers.get('OK-ACCESS-SIGN', '')):
        return {'code': '50113', 'msg': 'Invalid Sign', 'data': []}
    return None


def make_handler(sim):
    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method):
            url = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            body = None
            raw = b''
            if method == 'POST':
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length) if length else b''
                body = json.loads(raw or b'{}')
            private = (url.path.startswith('/api/v5/account')
                       or url.path.startswith('/api/v5/trade'))
            refusal = None
            if private:
                refusal = _auth_refusal(sim, self.headers, method,
                                        self.path, raw if method == 'POST'
                                        else b'')
            if refusal:
                reply = refusal
            else:
                reply = sim.handle(method, url.path, params, body)
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._answer('GET')

        def do_POST(self):
            self._answer('POST')

        def log_message(self, *args):
            pass

    return Handler


def serve(port=8771, host='127.0.0.1', **kwargs):
    sim = SimOKX(**kwargs)
    server = ThreadingHTTPServer((host, port), make_handler(sim))
    server.sim = sim
    return server


def main():
    parser = argparse.ArgumentParser(description='Simulated OKX v5 REST')
    parser.add_argument('--port', type=int, default=8771)
    parser.add_argument('--vip', default='Lv1')
    args = parser.parse_args()
    server = serve(args.port, vip=args.vip)
    print(f'simulated OKX on http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
