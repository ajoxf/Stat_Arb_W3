"""
MT5 ORDER TEST SUITE — porting scaffold
=======================================

Reference scaffold for re-implementing the crypto bot's "Full Order Test Suite"
(40 automated order scenarios with a live web progress panel) in a MetaTrader 5
environment.

It mirrors the original feature in app.py:
  - _SUITE_SCENARIOS          -> app.py:2304   (scenario list)
  - _test_suite_state         -> app.py:2295   (shared state streamed to UI)
  - run_test_suite            -> app.py:2791   (the runner loop)
  - run_single_scenario_task  -> app.py:3130   (single-case runner)
  - /api/test-suite/* routes  -> app.py:3071
  - UI panel (markup + JS)    -> templates/settings.html:548 and :1165

WHAT IS PORTABLE (copy almost verbatim):
  - SUITE_SCENARIOS structure
  - the runner loop control flow (open -> wait -> close/cancel -> pass/fail -> cooldown)
  - the `test_suite_update` WebSocket event contract
  - the entire UI panel (table + progress bar + buttons + socket listener)

WHAT YOU REWRITE FOR MT5 (the broker-specific layer):
  - open_order()            (was _suite_open_order,    app.py:2362)
  - close_position()        (was _suite_close_position, app.py:2454)
  - partial_fill_test()     (was _suite_partial_fill_test, app.py:2690)
  These three are the only functions that talk to the exchange. Everything else
  is transport/orchestration and carries over unchanged.

CONCEPT MAPPING — crypto spread  ->  MT5
  The crypto suite trades a spot+perp SPREAD, so each "order type" is one or two
  legs across two instruments:
      BUY_SPOT / SELL_SPOT       -> single leg on the "spot" symbol
      BUY_FUTURES / SELL_FUTURES -> single leg on the "futures" symbol
      LONG_SPREAD                -> BUY spot  + SELL futures   (2 legs)
      SHORT_SPREAD               -> SELL spot + BUY  futures   (2 legs)
  MT5 has no spot/perp split. Map the two legs onto two MT5 symbols of your
  choosing (a genuine pairs/spread trade), e.g.:
      LEG_A_SYMBOL = "EURUSD"     (was "spot")
      LEG_B_SYMBOL = "GBPUSD"     (was "futures")
  ...or collapse SPREAD scenarios to single-symbol tests if you only trade one
  instrument. The LIMIT/MARKET axis and the cancel / partial-fill axes port
  directly onto MT5 order types.

OKX ADAPTER CALL  ->  MetaTrader5 EQUIVALENT
  adapter.place_order(MARKET)   -> mt5.order_send(action=TRADE_ACTION_DEAL,
                                                  type=ORDER_TYPE_BUY|SELL)
  adapter.place_order(LIMIT)    -> mt5.order_send(action=TRADE_ACTION_PENDING,
                                                  type=ORDER_TYPE_BUY_LIMIT|SELL_LIMIT)
  adapter.cancel_order(oid)     -> mt5.order_send(action=TRADE_ACTION_REMOVE, order=ticket)
  adapter.get_order_status(oid) -> mt5.orders_get(ticket=)  (pending) /
                                   mt5.history_orders_get(ticket=) (done) /
                                   mt5.positions_get(ticket=) (open position)
  adapter.place_order(reduce_only) -> close a position via opposite ORDER_TYPE_*
                                   with `position=<ticket>` in the request
  engine.spot_tick.bid/ask/mid  -> mt5.symbol_info_tick(symbol).bid/.ask
  get_symbol_info().contract_val-> mt5.symbol_info(symbol).trade_contract_size /
                                   .volume_min / .volume_step

NOTE ON ASYNC: the original runs in an asyncio loop because the OKX adapter is
async. The official MetaTrader5 python package is SYNCHRONOUS and blocking. Two
options:
  (a) keep this async shape and wrap blocking mt5 calls in
      `await asyncio.to_thread(mt5.order_send, request)`  (recommended), or
  (b) run the whole suite in a plain background thread and drop async/await.
This scaffold keeps the async shape (option a) so the control flow matches the
original 1:1.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# --- transport stubs: wire these to your real Flask-SocketIO app -------------
# In the original these come from the app module. Replace with your instances.
def emit_update(state: Dict[str, Any]) -> None:
    """Push state to the UI. Real impl: socketio.emit('test_suite_update', state)."""
    raise NotImplementedError("wire to socketio.emit('test_suite_update', state)")


def log(msg: str, *args) -> None:
    print("[MT5 SUITE] " + (msg % args if args else msg))


# =============================================================================
# 1. SCENARIO LIST  — portable. (orig: app.py:2304 _SUITE_SCENARIOS)
# =============================================================================
# 40 cases: 18 LIMIT + 18 MARKET (forced) + 4 partial-fill recovery.
# Scenario keys:
#   id          unique short id (used by the single-run endpoint)
#   label       human label shown in the UI table
#   order_type  one of BUY_SPOT/SELL_SPOT/BUY_FUTURES/SELL_FUTURES/LONG_SPREAD/SHORT_SPREAD
#               (rename to your MT5 leg semantics; see CONCEPT MAPPING above)
#   cancel_test if True: LIMIT -> cancel the pending order; MARKET -> quick-close after 3s
#   forced_mode optional 'MARKET' to override the configured entry mode
#   partial_fail_test / filled_leg: place only `filled_leg`, skip the other, then close it

SUITE_SCENARIOS: List[Dict[str, Any]] = [
    {'id': '1a', 'label': 'BUY_SPOT #1',          'order_type': 'BUY_SPOT',      'cancel_test': False},
    {'id': '1b', 'label': 'BUY_SPOT #2',          'order_type': 'BUY_SPOT',      'cancel_test': False},
    {'id': '1c', 'label': 'BUY_SPOT #3 (cancel)', 'order_type': 'BUY_SPOT',      'cancel_test': True},
    {'id': '2a', 'label': 'SELL_FUTURES #1',      'order_type': 'SELL_FUTURES',  'cancel_test': False},
    {'id': '2b', 'label': 'SELL_FUTURES #2',      'order_type': 'SELL_FUTURES',  'cancel_test': False},
    {'id': '2c', 'label': 'SELL_FUTURES #3 (cancel)', 'order_type': 'SELL_FUTURES', 'cancel_test': True},
    {'id': '3a', 'label': 'BUY_FUTURES #1',       'order_type': 'BUY_FUTURES',   'cancel_test': False},
    {'id': '3b', 'label': 'BUY_FUTURES #2',       'order_type': 'BUY_FUTURES',   'cancel_test': False},
    {'id': '3c', 'label': 'BUY_FUTURES #3 (cancel)', 'order_type': 'BUY_FUTURES', 'cancel_test': True},
    {'id': '4a', 'label': 'SELL_SPOT #1',         'order_type': 'SELL_SPOT',     'cancel_test': False},
    {'id': '4b', 'label': 'SELL_SPOT #2',         'order_type': 'SELL_SPOT',     'cancel_test': False},
    {'id': '4c', 'label': 'SELL_SPOT #3 (cancel)','order_type': 'SELL_SPOT',     'cancel_test': True},
    {'id': '5a', 'label': 'LONG_SPREAD #1',       'order_type': 'LONG_SPREAD',   'cancel_test': False},
    {'id': '5b', 'label': 'LONG_SPREAD #2',       'order_type': 'LONG_SPREAD',   'cancel_test': False},
    {'id': '5c', 'label': 'LONG_SPREAD #3 (cancel)', 'order_type': 'LONG_SPREAD', 'cancel_test': True},
    {'id': '6a', 'label': 'SHORT_SPREAD #1',      'order_type': 'SHORT_SPREAD',  'cancel_test': False},
    {'id': '6b', 'label': 'SHORT_SPREAD #2',      'order_type': 'SHORT_SPREAD',  'cancel_test': False},
    {'id': '6c', 'label': 'SHORT_SPREAD #3 (cancel)', 'order_type': 'SHORT_SPREAD', 'cancel_test': True},
    # ── 18 MARKET-order scenarios (forced_mode overrides configured mode) ──────
    {'id': 'm1a', 'label': 'MKT BUY_SPOT #1',              'order_type': 'BUY_SPOT',      'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm1b', 'label': 'MKT BUY_SPOT #2',              'order_type': 'BUY_SPOT',      'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm1c', 'label': 'MKT BUY_SPOT #3 (quick-close)','order_type': 'BUY_SPOT',      'cancel_test': True,  'forced_mode': 'MARKET'},
    {'id': 'm2a', 'label': 'MKT SELL_FUTURES #1',          'order_type': 'SELL_FUTURES',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm2b', 'label': 'MKT SELL_FUTURES #2',          'order_type': 'SELL_FUTURES',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm2c', 'label': 'MKT SELL_FUTURES #3 (quick-close)', 'order_type': 'SELL_FUTURES', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm3a', 'label': 'MKT BUY_FUTURES #1',           'order_type': 'BUY_FUTURES',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm3b', 'label': 'MKT BUY_FUTURES #2',           'order_type': 'BUY_FUTURES',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm3c', 'label': 'MKT BUY_FUTURES #3 (quick-close)', 'order_type': 'BUY_FUTURES', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm4a', 'label': 'MKT SELL_SPOT #1',             'order_type': 'SELL_SPOT',     'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm4b', 'label': 'MKT SELL_SPOT #2',             'order_type': 'SELL_SPOT',     'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm4c', 'label': 'MKT SELL_SPOT #3 (quick-close)','order_type': 'SELL_SPOT',    'cancel_test': True,  'forced_mode': 'MARKET'},
    {'id': 'm5a', 'label': 'MKT LONG_SPREAD #1',           'order_type': 'LONG_SPREAD',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm5b', 'label': 'MKT LONG_SPREAD #2',           'order_type': 'LONG_SPREAD',   'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm5c', 'label': 'MKT LONG_SPREAD #3 (quick-close)', 'order_type': 'LONG_SPREAD', 'cancel_test': True, 'forced_mode': 'MARKET'},
    {'id': 'm6a', 'label': 'MKT SHORT_SPREAD #1',          'order_type': 'SHORT_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm6b', 'label': 'MKT SHORT_SPREAD #2',          'order_type': 'SHORT_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET'},
    {'id': 'm6c', 'label': 'MKT SHORT_SPREAD #3 (quick-close)', 'order_type': 'SHORT_SPREAD', 'cancel_test': True, 'forced_mode': 'MARKET'},
    # ── Partial-fill / leg-failure recovery (4 scenarios, always MARKET) ────────
    {'id': 'pf-1', 'label': 'LONG_SPREAD partial: A fills, B fails -> close A',
     'order_type': 'LONG_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'LEG_A'},
    {'id': 'pf-2', 'label': 'LONG_SPREAD partial: B fills, A fails -> close B',
     'order_type': 'LONG_SPREAD',  'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'LEG_B'},
    {'id': 'pf-3', 'label': 'SHORT_SPREAD partial: A fills, B fails -> close A',
     'order_type': 'SHORT_SPREAD', 'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'LEG_A'},
    {'id': 'pf-4', 'label': 'SHORT_SPREAD partial: B fills, A fails -> close B',
     'order_type': 'SHORT_SPREAD', 'cancel_test': False, 'forced_mode': 'MARKET',
     'partial_fail_test': True, 'filled_leg': 'LEG_B'},
]


# =============================================================================
# 2. SHARED STATE + EVENT CONTRACT  — portable. (orig: app.py:2295)
# =============================================================================
# The UI consumes exactly ONE websocket event: 'test_suite_update', whose
# payload is this dict. Emit it after EVERY state change and the table redraws.
#
#   {
#     'running':        bool,          # full suite in progress
#     'single_running': bool,          # a single scenario in progress
#     'current':        int,           # 1-based index of the active scenario
#     'total':          int,           # len(scenarios)
#     'pass':           int,
#     'fail':           int,
#     'start_time':     iso8601 str,
#     'order_mode':     str,           # default entry mode label
#     'scenarios': [ { ...scenario, 'status': pending|running|pass|fail|cancelled,
#                                    'detail': str, 'mode': 'LIMIT'|'MARKET' }, ... ]
#   }

_cancel = False
_running = False
_single_running = False
_state: Dict[str, Any] = {
    'running': False, 'single_running': False,
    'current': 0, 'total': len(SUITE_SCENARIOS),
    'pass': 0, 'fail': 0, 'scenarios': [], 'start_time': None, 'order_mode': '',
}

# Open test positions, keyed by an 8-char id. Mirrors the original `test_positions`.
test_positions: Dict[str, Dict[str, Any]] = {}


# =============================================================================
# 3. CONFIG  — adapt to your settings store.
# =============================================================================
@dataclass
class SuiteConfig:
    leg_a_symbol: str = "EURUSD"          # was config.spot_symbol
    leg_b_symbol: str = "GBPUSD"          # was config.futures_symbol
    entry_execution_mode: str = "LIMIT"   # 'LIMIT' or 'MARKET'
    exit_execution_mode: str = "LIMIT"
    limit_order_timeout_sec: int = 30     # how long a LIMIT order is given to fill
    limit_order_price_offset_bps: float = 1.0
    volume: float = 0.01                  # MT5 lots (was crypto `quantity`)


config = SuiteConfig()


# =============================================================================
# 4. BROKER LAYER — REWRITE THESE 3 FOR MT5.  (orig: app.py:2362 / 2454 / 2690)
# =============================================================================
# Return contract that the runner depends on:
#   open_order(...)        -> (legs: list[dict] | None, error: str | None)
#                             error is None on full success; on partial success
#                             return (legs_placed_so_far, error) so the runner
#                             can clean up the leg that did go through.
#   close_position(pos_id) -> (ok: bool, detail: str)
#   partial_fill_test(...) -> (ok: bool, detail: str)
#
# A "leg" dict needs at minimum: market/symbol, side, ticket/order_id, volume,
# entry_price, mode, target_price (limit px or None), plus any context you want
# to show in the Detail column.

def _legs_for(order_type: str) -> List[Tuple[str, str]]:
    """Map an order_type to its (symbol_role, side) legs. (orig single_leg dispatch)."""
    A, B = "LEG_A", "LEG_B"
    return {
        'BUY_SPOT':     [(A, "BUY")],
        'SELL_SPOT':    [(A, "SELL")],
        'BUY_FUTURES':  [(B, "BUY")],
        'SELL_FUTURES': [(B, "SELL")],
        'LONG_SPREAD':  [(A, "BUY"), (B, "SELL")],
        'SHORT_SPREAD': [(A, "SELL"), (B, "BUY")],
    }[order_type]


def _symbol_for(role: str) -> str:
    return config.leg_a_symbol if role == "LEG_A" else config.leg_b_symbol


async def open_order(order_type: str, volume: float,
                     forced_mode: Optional[str] = None
                     ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """
    Place the opening leg(s) for a scenario.  REWRITE BODY FOR MT5.

    MT5 sketch (synchronous mt5 calls wrapped via asyncio.to_thread):

        import MetaTrader5 as mt5
        mode = forced_mode or config.entry_execution_mode
        legs = []
        for role, side in _legs_for(order_type):
            symbol = _symbol_for(role)
            tick   = mt5.symbol_info_tick(symbol)
            if mode == "LIMIT":
                action  = mt5.TRADE_ACTION_PENDING
                otype   = mt5.ORDER_TYPE_BUY_LIMIT if side == "BUY" else mt5.ORDER_TYPE_SELL_LIMIT
                offset  = config.limit_order_price_offset_bps / 10000.0
                price   = tick.bid * (1 + offset) if side == "BUY" else tick.ask * (1 - offset)
            else:
                action  = mt5.TRADE_ACTION_DEAL
                otype   = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
                price   = tick.ask if side == "BUY" else tick.bid
            req = {
                "action": action, "symbol": symbol, "volume": volume,
                "type": otype, "price": price,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,   # or _FOK / _RETURN per broker
                "deviation": 20, "comment": "suite",
            }
            res = await asyncio.to_thread(mt5.order_send, req)
            if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                err = f"{mt5.last_error()} / retcode={getattr(res,'retcode',None)}"
                # return any legs already placed so the runner can clean them up
                return (legs or None), err
            legs.append({
                "symbol": symbol, "role": role, "side": side, "volume": volume,
                "order_id": res.order,          # ticket
                "entry_price": price, "mode": mode,
                "target_price": price if mode == "LIMIT" else None,
            })
        return legs, None
    """
    raise NotImplementedError("MT5: place leg(s) via mt5.order_send; see docstring")


async def close_position(pos_id: str) -> Tuple[bool, str]:
    """
    Cancel (if pending) or close (if filled) one test position. REWRITE FOR MT5.

    MT5 sketch:
        pos = test_positions.get(pos_id)
        if not pos: return False, "position not found"
        symbol, ticket = pos["symbol"], pos["order_id"]

        # 1) Still a pending order? -> remove it.
        pending = await asyncio.to_thread(mt5.orders_get, ticket=ticket)
        if pending:
            res = await asyncio.to_thread(mt5.order_send, {
                "action": mt5.TRADE_ACTION_REMOVE, "order": ticket,
            })
            test_positions.pop(pos_id, None)
            ok = res and res.retcode == mt5.TRADE_RETCODE_DONE
            return ok, "cancelled (was pending)" if ok else "cancel-failed"

        # 2) Otherwise it became a position -> close it (opposite deal w/ position=ticket).
        position = (await asyncio.to_thread(mt5.positions_get, ticket=ticket) or [None])[0]
        if position is None:
            test_positions.pop(pos_id, None)
            return True, "already closed / not found"
        tick  = await asyncio.to_thread(mt5.symbol_info_tick, symbol)
        close_side = "SELL" if pos["side"] == "BUY" else "BUY"
        otype = mt5.ORDER_TYPE_SELL if close_side == "SELL" else mt5.ORDER_TYPE_BUY
        price = tick.bid if close_side == "SELL" else tick.ask
        res = await asyncio.to_thread(mt5.order_send, {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": position.volume, "type": otype, "price": price,
            "position": position.ticket, "deviation": 20, "comment": "suite-close",
        })
        test_positions.pop(pos_id, None)
        ok = res and res.retcode == mt5.TRADE_RETCODE_DONE
        # Build a Detail string (fill px, P&L, fees) here for the UI column.
        return ok, f"closed @ {price}" if ok else f"close-failed: {mt5.last_error()}"
    """
    raise NotImplementedError("MT5: cancel pending or close position; see docstring")


async def partial_fill_test(order_type: str, volume: float, filled_leg: str
                            ) -> Tuple[bool, str]:
    """
    Recovery test: place ONLY `filled_leg` at MARKET (it fills), skip the other
    leg (simulated failure), then immediately MARKET-close the filled leg.
    Passes if the open AND the recovery close both succeed. REWRITE FOR MT5.
    (orig: app.py:2690)
    """
    raise NotImplementedError("MT5: open one leg at market, then market-close it")


# =============================================================================
# 5. RUNNER LOOP  — portable. (orig: app.py:2791 run_test_suite)
# =============================================================================
async def run_test_suite() -> None:
    global _cancel, _running, _state, test_positions
    _running, _cancel = True, False

    default_mode = config.entry_execution_mode
    limit_timeout = config.limit_order_timeout_sec

    scenarios = copy.deepcopy(SUITE_SCENARIOS)
    for s in scenarios:
        s['status'], s['detail'], s['mode'] = 'pending', '', default_mode

    _state = {
        'running': True, 'single_running': False,
        'current': 0, 'total': len(scenarios),
        'pass': 0, 'fail': 0, 'scenarios': scenarios,
        'start_time': datetime.now(timezone.utc).isoformat(),
        'order_mode': default_mode,
    }
    emit_update(_state)

    volume = config.volume

    for idx, scenario in enumerate(scenarios):
        if _cancel:
            scenario['status'], scenario['detail'] = 'cancelled', 'suite stopped'
            break

        scenario['status'] = 'running'
        _state['current'] = idx + 1
        order_type = scenario['order_type']
        cancel_test = scenario['cancel_test']
        scen_mode = scenario.get('forced_mode') or default_mode
        scenario['mode'] = scen_mode
        inter_pause = 5 if scen_mode == 'MARKET' else 20   # spacing between scenarios
        emit_update(_state)
        log("%d/%d  %s  [%s]", idx + 1, len(scenarios), scenario['label'], scen_mode)

        # ── PARTIAL-FILL RECOVERY ─────────────────────────────────────────
        if scenario.get('partial_fail_test'):
            scenario['detail'] = f"opening {scenario['filled_leg']} leg at MARKET…"
            emit_update(_state)
            try:
                ok, detail = await asyncio.wait_for(
                    partial_fill_test(order_type, volume, scenario['filled_leg']),
                    timeout=60.0)
            except asyncio.TimeoutError:
                ok, detail = False, "partial-fill test timed out (>60s)"
            except Exception as exc:
                ok, detail = False, f"unexpected error: {exc}"
            scenario['status'] = 'pass' if ok else 'fail'
            scenario['detail'] = detail
            _state['pass' if ok else 'fail'] += 1
            emit_update(_state)
            await _cooldown(scenario, idx, scenarios, inter_pause)
            continue

        # ── OPEN ──────────────────────────────────────────────────────────
        try:
            legs, open_err = await asyncio.wait_for(
                open_order(order_type, volume, forced_mode=scen_mode), timeout=30.0)
        except asyncio.TimeoutError:
            legs, open_err = None, "open timed out (>30s)"
        except Exception as exc:
            legs, open_err = None, str(exc)

        # full failure — nothing placed
        if open_err and not legs:
            scenario['status'], scenario['detail'] = 'fail', f"open failed: {open_err}"
            _state['fail'] += 1
            emit_update(_state)
            await asyncio.sleep(inter_pause)
            continue

        # register whatever legs were placed
        opened_ids = _register_legs(legs, scen_mode)
        scenario['detail'] = "  |  ".join(
            f"{p['market_type']} {p['side']} {p['open_mode']} "
            f"@ {p.get('target_price') or '~mkt'} [oid={str(p['order_id'])[:12]}…]"
            for p in (test_positions[i] for i in opened_ids))
        emit_update(_state)

        # partial open success — second leg failed: clean up + mark fail
        if open_err and legs:
            scenario['detail'] += f"  |  2nd leg FAILED: {open_err}"
            emit_update(_state)
            details = await _close_all(opened_ids)
            scenario['detail'] += "  |  " + "  |  ".join(details)
            scenario['status'] = 'fail'
            _state['fail'] += 1
            emit_update(_state)
            await asyncio.sleep(inter_pause)
            continue

        # ── WAIT ──────────────────────────────────────────────────────────
        if cancel_test:
            label = "cancel test" if scen_mode == "LIMIT" else "quick-close"
            scenario['detail'] += f"  |  {label} – closing in 3s"
            emit_update(_state)
            await asyncio.sleep(3)
        elif scen_mode == "LIMIT":
            scenario['detail'] += f"  |  waiting {limit_timeout}s for fill…"
            emit_update(_state)
            await asyncio.sleep(limit_timeout)
        else:
            await asyncio.sleep(4)  # market confirm

        if _cancel:
            scenario['status'] = 'cancelled'
            scenario['detail'] += '  |  suite stopped mid-scenario'
            break

        # ── CLOSE ───────────────────────────────────────────────────────────
        details = await _close_all(opened_ids)
        close_ok = all(not d.lower().startswith(("close-failed", "close timed out"))
                       for d in details)
        scenario['detail'] = "  |  ".join(details)
        scenario['status'] = 'pass' if close_ok else 'fail'
        _state['pass' if close_ok else 'fail'] += 1
        emit_update(_state)

        await _cooldown(scenario, idx, scenarios, inter_pause)

    _state['running'] = False
    _running = False
    emit_update(_state)
    log("Done – pass=%d fail=%d", _state['pass'], _state['fail'])


def _register_legs(legs: List[Dict[str, Any]], mode: str) -> List[str]:
    """Store opened legs in test_positions; return their ids. (orig: app.py:2963)."""
    ids = []
    for leg in legs:
        pid = str(uuid.uuid4())[:8]
        test_positions[pid] = {
            'id': pid,
            'market_type': leg.get('role', leg.get('symbol')),
            'symbol': leg['symbol'],
            'side': leg['side'],
            'quantity': leg['volume'],
            'entry_price': leg['entry_price'],
            'order_id': leg['order_id'],
            'entry_time': datetime.now(timezone.utc).isoformat(),
            'open_mode': mode,
            'target_price': leg.get('target_price'),
            'leg_label': f"{leg['symbol']} {leg['side']}",
        }
        ids.append(pid)
    return ids


async def _close_all(pos_ids: List[str]) -> List[str]:
    details = []
    for pid in pos_ids:
        try:
            _, d = await asyncio.wait_for(close_position(pid), timeout=30.0)
            details.append(d)
        except asyncio.TimeoutError:
            details.append("close timed out (>30s)")
        except Exception as exc:
            details.append(str(exc))
    return details


async def _cooldown(scenario, idx, scenarios, secs) -> None:
    """Inter-scenario pause, sliced so cancellation reacts quickly."""
    if idx < len(scenarios) - 1 and not _cancel:
        scenario['detail'] += f"  |  cooling {secs}s…"
        emit_update(_state)
        for _ in range(secs):
            if _cancel:
                break
            await asyncio.sleep(1)


# =============================================================================
# 6. SINGLE-SCENARIO RUNNER  — portable. (orig: app.py:3130)
# =============================================================================
async def run_single_scenario(scenario_id: str) -> None:
    global _single_running, _state
    base = next((s for s in SUITE_SCENARIOS if s['id'] == scenario_id), None)
    if base is None:
        return
    _single_running = True
    _state['single_running'] = True

    # ensure the table is populated so the one row can flip to running
    if not _state.get('scenarios'):
        _state['scenarios'] = [
            {**s, 'status': 'pending', 'detail': '',
             'mode': s.get('forced_mode', config.entry_execution_mode)}
            for s in SUITE_SCENARIOS]
    row = next(s for s in _state['scenarios'] if s['id'] == scenario_id)

    scen_mode = base.get('forced_mode') or config.entry_execution_mode
    row['status'], row['detail'], row['mode'] = 'running', 'starting…', scen_mode
    emit_update(_state)

    try:
        if base.get('partial_fail_test'):
            ok, detail = await partial_fill_test(base['order_type'], config.volume, base['filled_leg'])
        else:
            legs, err = await open_order(base['order_type'], config.volume, forced_mode=scen_mode)
            if err and not legs:
                ok, detail = False, f"open failed: {err}"
            else:
                ids = _register_legs(legs, scen_mode)
                await asyncio.sleep(3 if base['cancel_test'] else
                                    (config.limit_order_timeout_sec if scen_mode == "LIMIT" else 4))
                details = await _close_all(ids)
                ok = all(not d.lower().startswith(("close-failed", "close timed out")) for d in details)
                detail = "  |  ".join(details)
                if err:
                    ok, detail = False, f"2nd leg FAILED: {err}  |  {detail}"
    except Exception as exc:
        ok, detail = False, str(exc)

    row['status'] = 'pass' if ok else 'fail'
    row['detail'] = detail
    _state['pass' if ok else 'fail'] += 1
    _single_running = False
    _state['single_running'] = False
    emit_update(_state)


# =============================================================================
# 7. FLASK CONTROL ROUTES  — portable. (orig: app.py:3071)
# =============================================================================
# Register these on your Flask app. `loop` is the asyncio loop the suite runs in;
# `socketio` provides emit. Run coroutines with run_coroutine_threadsafe(coro, loop).
#
#   from flask import jsonify, request
#
#   @app.route('/api/test-suite/start', methods=['POST'])
#   def start_suite():
#       global _running
#       if _running and not _state.get('running'):
#           _running = False                      # clear stale flag from a crash
#       if _running:
#           return jsonify(success=False, error='Suite already running'), 400
#       asyncio.run_coroutine_threadsafe(run_test_suite(), loop)
#       return jsonify(success=True, message='Test suite started')
#
#   @app.route('/api/test-suite/stop', methods=['POST'])
#   def stop_suite():
#       global _cancel
#       _cancel = True                            # current scenario finishes first
#       return jsonify(success=True)
#
#   @app.route('/api/test-suite/reset', methods=['POST'])
#   def reset_suite():
#       global _running, _single_running, _cancel
#       _running = _single_running = _cancel = False
#       _state['running'] = _state['single_running'] = False
#       socketio.emit('test_suite_update', _state)
#       return jsonify(success=True)
#
#   @app.route('/api/test-suite/status', methods=['GET'])
#   def suite_status():
#       state = dict(_state)
#       if not state.get('scenarios'):            # pre-populate so Run buttons render
#           state['scenarios'] = [
#               {**s, 'status': 'pending', 'detail': '',
#                'mode': s.get('forced_mode', config.entry_execution_mode)}
#               for s in SUITE_SCENARIOS]
#       return jsonify(state)
#
#   @app.route('/api/test-suite/run-scenario', methods=['POST'])
#   def run_scenario():
#       sid = (request.get_json() or {}).get('scenario_id')
#       if _running or _single_running:
#           return jsonify(success=False, error='Another run is active'), 400
#       asyncio.run_coroutine_threadsafe(run_single_scenario(sid), loop)
#       return jsonify(success=True)


# =============================================================================
# 8. UI PANEL  — copy verbatim from templates/settings.html
# =============================================================================
# The web panel is broker-agnostic: it only reads the `test_suite_update` event
# and POSTs to the routes above. Lift it directly:
#   - markup (buttons + progress bar + table):  templates/settings.html:548-611
#   - JS (renderSuiteTable / applySuiteState / start|stop|reset|runSingle +
#         socket.on('test_suite_update')):       templates/settings.html:1165-1328
# Only cosmetic edits needed: change column copy from spot/futures to your MT5
# leg names, and the default "0 / 40" counter already matches this 40-case list.


if __name__ == "__main__":
    # Smoke check: the portable orchestration runs end-to-end once the three
    # broker functions are implemented. With the stubs it will mark every
    # scenario 'fail' (NotImplementedError), which still exercises the loop,
    # the state machine, and the event contract.
    async def _demo():
        global emit_update
        emit_update = lambda st: log("emit: %d/%d pass=%d fail=%d",
                                     st['current'], st['total'], st['pass'], st['fail'])
        await run_test_suite()
    asyncio.run(_demo())
