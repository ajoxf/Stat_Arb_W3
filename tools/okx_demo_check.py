"""End-to-end check of the desk against YOUR OKX DEMO account.

    python tools/okx_demo_check.py

Runs the desk's own OKX code - the broker, the W3 maker fill and maker
close, the close guards and the fills journal - against the OKX demo
account saved on the Exchanges page (account "okx"; `--account NAME`
for another). It uses the smallest sizes OKX accepts, cleans up after
itself, and writes `okx_demo_check_report.txt` beside config.json. No
key, secret or passphrase is ever printed or written.

It REFUSES a LIVE account: this places real orders.

Stop the desk (close its black window) while this runs, so the two do
not both work the same account.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from okxtrader import diagnostics                          # noqa: E402
from okxtrader.config import PairConfig, TraderConfig      # noqa: E402
from okxtrader.executor import PairExecutor                # noqa: E402
from okxtrader.legs import LocalLeg                        # noqa: E402
from okxtrader.maker import MakerCloses, MakerFills        # noqa: E402
from okxtrader.models import OrderSide, SpreadSide         # noqa: E402
from okxtrader.okx_broker import OKXBroker                 # noqa: E402
from okxtrader.spread import compute_spread                # noqa: E402

PERP_A, PERP_B, SPOT = 'BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'BTC-USDT'
TAG = 'LADDER-CHECK'


class Report:
    def __init__(self):
        self.rows = []

    def add(self, status, name, detail=''):
        self.rows.append((status, name, str(detail)))
        print(f'{status:5s} {name}  {detail}', flush=True)
        return status == 'PASS'

    def run(self, name, fn):
        try:
            detail = fn()
            return self.add('PASS', name, detail or '')
        except Skip as s:
            return self.add('SKIP', name, str(s))
        except AssertionError as e:
            return self.add('FAIL', name, str(e))
        except Exception as e:
            return self.add('FAIL', name, f'{type(e).__name__}: {e} | '
                            + traceback.format_exc(limit=2).replace('\n', ' '))

    def text(self):
        counts = {s: sum(1 for r in self.rows if r[0] == s)
                  for s in ('PASS', 'FAIL', 'WARN', 'SKIP')}
        width = max((len(r[1]) for r in self.rows), default=10)
        lines = [f'OKX demo check - {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}',
                 ', '.join(f'{v} {k.lower()}' for k, v in counts.items()), '']
        lines += [f'{s:5s} {n.ljust(width)}  {d}' for s, n, d in self.rows]
        return '\n'.join(lines) + '\n'


class Skip(Exception):
    pass


def held(broker, symbol, pos_side):
    broker._pos_cache = None
    table = broker._exchange_positions()
    assert table is not None, 'OKX positions could not be read'
    return table.get((symbol, pos_side), 0.0)


def wait(seconds, until):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if until():
            return True
        time.sleep(0.5)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', default=os.path.join(ROOT, 'config.json'))
    parser.add_argument('--account', default='okx')
    args = parser.parse_args()

    config = TraderConfig.from_file(args.config)       # loads .env too
    account = config.accounts.get(args.account)
    report = Report()
    out = os.path.join(os.path.dirname(os.path.abspath(args.config)),
                       'okx_demo_check_report.txt')

    def finish():
        with open(out, 'w', encoding='utf-8') as f:
            f.write(report.text())
        print(f'\nReport written to {out}')

    if account is None:
        report.add('FAIL', 'account', f"no account '{args.account}' in "
                   f"{args.config} - add it on the Exchanges page")
        return finish()
    if not account.demo:
        report.add('FAIL', 'account is DEMO', 'this account is set to LIVE. '
                   'This check places real orders, so it only runs on Demo '
                   'trading. Switch the row to Demo trading (with a demo key) '
                   'on the Exchanges page.')
        return finish()

    broker = OKXBroker(account, ledger_path=tempfile.mkdtemp(prefix='okxcheck-'),
                       stream=True)
    leg = LocalLeg(broker)
    tickets = []                       # every ticket this run opened

    # -- 1. the account ---------------------------------------------------
    def account_checklist():
        assert broker.initialize(), broker.last_error
        c = diagnostics.Checklist()
        diagnostics.check_okx_account(c, args.account, broker,
                                      has_keys=account.has_api_key
                                      or bool(account.base_url))
        bad = [f"{x['name']}: {x['message']}" for x in c.checks
               if x['status'] == 'FAIL']
        assert not bad, ' | '.join(bad)
        return ' | '.join(f"{x['name']}: {x['message']}" for x in c.checks
                          if x['status'] in ('PASS', 'INFO'))
    if not report.run('account checklist (as Test runs it)',
                      account_checklist):
        report.add('SKIP', 'trading checks', 'fix the account first - the '
                   'checklist above says what and where')
        return finish()

    # -- 2. prices ---------------------------------------------------------
    def feed():
        for sym in (PERP_A, PERP_B, SPOT):
            broker.symbol_tick(sym)
        if broker.stream is None:
            raise Skip('WebSocket feed off (websocket-client missing or '
                       'OKX_WEBSOCKET=0) - prices come from REST')
        ok = wait(20, lambda: all(broker.stream.row(s) for s in
                                  (PERP_A, PERP_B, SPOT)))
        why = broker.stream.last_error or 'no error'
        assert ok, (f'no pushed prices within 20 s ({why}) - the desk '
                    f'falls back to REST')
        t0 = time.time(); seen = set()
        while time.time() - t0 < 5:
            r = broker.stream.row(PERP_A)
            seen.add(r.get('ts')); time.sleep(0.1)
        return f'WebSocket connected; {len(seen)} BTC-USDT-SWAP updates in 5 s'
    report.run('live price feed (WebSocket)', feed)

    specs = {}

    def instruments():
        for sym in (PERP_A, PERP_B, SPOT):
            info = broker.symbol_info(sym)
            assert info is not None, broker.not_found_reason(sym)
            tick = broker.symbol_tick(sym)
            assert tick and tick.bid > 0 and tick.ask >= tick.bid, \
                f'{sym}: no price'
            specs[sym] = info
        return ', '.join(f'{s} min {specs[s].volume_min:g} @ '
                         f'{broker.symbol_tick(s).bid:g}' for s in specs)
    if not report.run('instruments and prices', instruments):
        return finish()
    base = {(PERP_A, 'long'): held(broker, PERP_A, 'long'),
            (PERP_A, 'short'): held(broker, PERP_A, 'short'),
            (PERP_B, 'long'): held(broker, PERP_B, 'long'),
            (PERP_B, 'short'): held(broker, PERP_B, 'short')}

    # -- 3. an order on, and off by ticket ---------------------------------
    lot = specs[PERP_A].volume_min
    state = {}

    def ioc_open():
        r = broker.send_market_order(PERP_A, OrderSide.BUY, lot, comment=TAG)
        assert r.success and r.ticket, r.error
        tickets.append(r.ticket)
        state['ticket'] = r.ticket
        grew = held(broker, PERP_A, 'long') - base[(PERP_A, 'long')]
        assert abs(grew - lot) < 1e-9, f'OKX long changed by {grew}, not {lot}'
        return f'BUY {lot:g} {PERP_A} filled at {r.executed_price}, ticket {r.ticket}'
    report.run('market order opens a position (IOC, capped)', ioc_open)

    def close_by_ticket():
        if 'ticket' not in state:
            raise Skip('nothing was opened')
        r = broker.close_position_ticket(PERP_A, state['ticket'], lot,
                                         OrderSide.BUY, comment=TAG)
        assert r.success, r.error
        assert abs(held(broker, PERP_A, 'long')
                   - base[(PERP_A, 'long')]) < 1e-9, 'long not back where it was'
        assert abs(held(broker, PERP_A, 'short')
                   - base[(PERP_A, 'short')]) < 1e-9, \
            'a SHORT appeared - the close opened the opposite side'
        return f'closed ticket {state["ticket"]} at {r.executed_price}; no short opened'
    report.run('close by ticket reduces, never opens the other side',
               close_by_ticket)

    def second_close():
        if 'ticket' not in state:
            raise Skip('nothing was opened')
        r = broker.close_position_ticket(PERP_A, state['ticket'], lot,
                                         OrderSide.BUY, comment=TAG)
        assert not r.success, 'a second close went through'
        assert abs(held(broker, PERP_A, 'short')
                   - base[(PERP_A, 'short')]) < 1e-9, 'a short appeared'
        return f'refused: {r.error}'
    report.run('closing it again sends nothing', second_close)

    # -- 4. resting orders --------------------------------------------------
    def post_only_cross():
        tick = broker.symbol_tick(PERP_A)
        r = broker.place_pending_limit(PERP_A, OrderSide.BUY, lot,
                                       tick.ask * 1.002, comment=TAG)
        if r.get('ok'):
            tickets.append(r['ticket'])
            s = broker.order_fill_state(r['ticket'])
            assert not s.get('filled_volume'), 'a post-only order TOOK'
            if s.get('still_open'):
                broker.cancel_pending(r['ticket'])
            return 'accepted and cancelled by OKX without taking'
        return f"refused: {r.get('error')}"
    report.run('post-only order never takes liquidity', post_only_cross)

    def rest_amend_cancel():
        tick = broker.symbol_tick(PERP_A)
        r = broker.place_pending_limit(PERP_A, OrderSide.BUY, lot,
                                       tick.bid * 0.98, comment=TAG)
        assert r.get('ok'), r.get('error')
        tickets.append(r['ticket'])
        s = broker.order_fill_state(r['ticket'])
        assert s.get('still_open'), f'not resting: {s}'
        new = broker.legal_limit_price(PERP_A, OrderSide.BUY, tick.bid * 0.97)
        a = broker.modify_pending(r['ticket'], new)
        assert a.get('ok'), a.get('error')
        c = broker.cancel_pending(r['ticket'])
        assert c.get('cancelled') and not c.get('filled_volume'), c
        return f'rested at {r["price"]:g}, amended to {new:g}, cancelled'
    report.run('rest, re-peg by amend, cancel', rest_amend_cancel)

    # -- 5. the W3 maker fill and maker close on a real spread ----------------
    def maker_round_trip():
        a_px = broker.symbol_tick(PERP_A).bid
        b_px = broker.symbol_tick(PERP_B).bid
        beta = b_px / a_px
        clip_b = max(specs[PERP_B].volume_min,
                     round(lot * specs[PERP_A].contract_size * a_px
                           / (specs[PERP_B].contract_size * b_px)
                           / specs[PERP_B].volume_step)
                     * specs[PERP_B].volume_step)
        pair = PairConfig(f'{PERP_A}|{PERP_B}', name='check',
                          leg_a={'account': 'okx', 'symbol': PERP_A},
                          leg_b={'account': 'okx', 'symbol': PERP_B},
                          hedge_ratio=round(beta, 6),
                          hedge_ratio_for=f'{PERP_A}|{PERP_B}',
                          increment=0.0001, clip_lots_a=lot,
                          clip_lots_b=clip_b, pair_type='RELATED')
        for side, sym in (('a', PERP_A), ('b', PERP_B)):
            i = specs[sym]
            setattr(pair, f'meta_{side}', {
                'contract_size': i.contract_size, 'volume_min': i.volume_min,
                'volume_step': i.volume_step, 'volume_max': i.volume_max})
        cfg = TraderConfig(pairs={pair.key: pair})
        cfg.settings.update({'FILL_STYLE': 'maker',
                             'MARKET_PROTECTION_TICKS': 0})
        legs = {'okx': leg}
        executor = PairExecutor(cfg, legs)
        fills, closes = MakerFills(cfg, legs, executor), \
            MakerCloses(cfg, legs, executor)
        done = []
        fills.on_done = lambda job, result: done.append(result)
        closes.on_done = lambda job, answer: done.append(answer)

        def md():
            return compute_spread(pair, leg.tick(PERP_A), leg.tick(PERP_B),
                                  pair.hedge_ratio)
        started = fills.start(pair, SpreadSide.BUY, md(), 1)
        assert started.ok, started.reason
        t0 = time.time()
        while not done and time.time() - t0 < 150:
            fills.step(pair, md())
            time.sleep(0.5)
        assert done, 'the maker fill did not end within 150 s'
        result = done.pop()
        took = time.time() - t0
        if not result.ok:
            # A clean refusal (nothing filled) or an unwind is the W3
            # fill behaving; what matters is nothing left behind.
            for (sym, ps), was in base.items():
                assert abs(held(broker, sym, ps) - was) < 1e-9, \
                    f'{sym} {ps} left changed after: {result.reason}'
            return (f'not filled as maker in {took:.0f} s - "{result.reason}" '
                    f'- and nothing left on at OKX (demo books are thin; '
                    f'run again to see a fill)')
        position = result.position
        for f in (position.leg_a, position.leg_b):
            tickets.extend(f.position_tickets)
        entry = (f'BUY spread on as maker in {took:.1f} s at '
                 f'{position.entry_spread:.6g} (A {position.leg_a.volume:g} '
                 f'{PERP_A}, B {position.leg_b.volume:g} {PERP_B})')
        answer = closes.start(pair, position, md(), 'demo check')
        assert answer.get('ok'), answer
        t0 = time.time()
        while not done and time.time() - t0 < 90:
            closes.step(pair, md())
            time.sleep(0.5)
        assert done and done[-1].get('ok'), f'close did not settle: {done}'
        for (sym, ps), was in base.items():
            assert abs(held(broker, sym, ps) - was) < 1e-9, \
                f'{sym} {ps} not back where it was after the close'
        return (f'{entry}; closed as maker/market in {time.time()-t0:.1f} s, '
                f'OKX back where it started, no opposite position')
    report.run('W3 maker spread fill and maker close', maker_round_trip)

    # -- 6. spot ---------------------------------------------------------------
    def spot_round_trip():
        tick = broker.symbol_tick(SPOT)
        size = max(specs[SPOT].volume_min, round(2.0 / tick.ask, 6))
        r = broker.send_market_order(SPOT, OrderSide.BUY, size, comment=TAG)
        assert r.success, r.error
        tickets.append(r.ticket)
        c = broker.close_position_ticket(SPOT, r.ticket, r.volume,
                                         OrderSide.BUY, comment=TAG)
        assert c.success or c.volume, c.error
        again = broker.close_position_ticket(SPOT, r.ticket, r.volume,
                                             OrderSide.BUY, comment=TAG)
        assert not again.success, 'a second spot close went through'
        return (f'bought {r.volume:g} BTC, sold it back; a second close '
                f'was refused ({again.error}) - no borrow')
    report.run('spot buy and close; no short by mistake', spot_round_trip)

    # -- 7. the journal -------------------------------------------------------
    def journal():
        time.sleep(2)
        rows = broker.order_log(1)
        ours = [r for r in rows if str(r.get('position_id')) in
                {str(t) for t in tickets}]
        assert ours, 'none of this run\'s fills are in OKX\'s fill history yet'
        assert all(r['inst_type'] == 'DEAL' for r in ours)
        fee = sum(r['commission'] for r in ours)
        return (f'{len(ours)} fills of this run, each with its ticket; '
                f'commission {fee:.4f} USDT '
                f'({sum(1 for r in ours if r["order_type"] == "maker")} maker)')
    report.run('fills journal (OKX fill history, by ticket)', journal)

    # -- 8. clean-up -----------------------------------------------------------
    def cleanup():
        left = broker.pending_orders_by_magic() or []
        for o in left:
            broker.cancel_pending(o['ticket'])
        for p in broker.positions_by_magic() or []:
            broker.close_position_ticket(p['symbol'], p['ticket'], p['volume'],
                                         OrderSide(p['side']), comment=TAG)
        for (sym, ps), was in base.items():
            assert abs(held(broker, sym, ps) - was) < 1e-9, \
                f'{sym} {ps} differs from before the check'
        return (f'cancelled {len(left)} resting order(s); OKX positions are '
                f'exactly as before the check')
    report.run('clean-up: nothing of the check left on OKX', cleanup)
    broker.shutdown()
    finish()


if __name__ == '__main__':
    main()
