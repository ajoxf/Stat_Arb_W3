"""Telegram: watch the desk and drive the Algo from a phone.

A bot that lives in the WEB process and talks to the desk through the
web app itself, the same requests the browser makes. So every refusal,
every check and every "LIVE needs confirming" is the one the screen
already gets; there is no second way in with rules of its own.

What it may do, and how:

- **Who.** Only the Telegram user ids listed in `TELEGRAM_ALLOWED_USERS`
  get an answer. Anyone else is ignored — not told "no", ignored — and
  their id is logged once, so adding a colleague is a matter of reading
  the log. The bot token lives in `.env` and nowhere else, and never in
  a log line: Telegram puts it in the URL, so no URL is ever logged.
- **What.** A Dashboard (everything to watch, one screen), Settings
  (per ladder, in sections: Algo mode Off / Signals / LIVE, entry,
  filters, exits, daily limits, costs and session), CLOSE ALL on a
  ladder, KILL ALL. Plain text, no icons. Every one of those asks
  Confirm / Cancel first, and LIVE asks twice. It can never place a NEW
  order — there is no click, no level, no quantity in it — and a test
  holds it to the commands it is allowed to send.
- **Buttons expire.** A button is a short token, good for ten minutes.
  A confirm pressed on yesterday's menu does nothing but say so.
- **Alerts** are pushed to every allowed user, and only for what
  needs a person: a trade opened or closed, a failure (the engine
  stalling, an account dropping out, an order failed or refused, a
  stale or jumping price), a day's limit hit, a ladder going into or
  out of LIVE, and a summary once a day at each ladder's close. A
  signal - taken or held back - is not news: the trade is, and the
  desk shows the rest. `/alerts` turns them off and on per chat.

The desk's screen lock does not apply here: the lock guards the desk
PC, and the bot has its own guard — the allow-list. It passes the web
app's lock with a secret that is made fresh when the process starts and
never leaves it.
"""

import html
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = 'https://api.telegram.org'

#: The header that carries the in-process secret past the screen lock.
BOT_HEADER = 'X-MT5Trader-Bot'

#: How long a button stays good.
TOKEN_TTL_SEC = 600.0

#: How long a "send me the new value" prompt waits for the reply.
INPUT_TTL_SEC = 300.0

#: The commands the bot may send to the engine, and nothing else. No
#: `click`, no `close_at_limit`: it never opens a position.
ALLOWED_COMMANDS = frozenset({'set_algo', 'set_pair', 'flatten_pair', 'kill'})


#: A price problem has to last this long before it is worth a message:
#: a feed that is stale for one poll and back the next is not one.
PROBLEM_SETTLE_SEC = 20.0

DIRECTIONS = [('BOTH', 'Both'), ('H_TO_L', 'H to L only'),
              ('L_TO_H', 'L to H only')]
LEVEL_MODES = [('MARGIN', '% of margin'), ('ATR', 'ATR')]
#: A side's own sizing: SAME is "the ladder's own", saved as blank.
SIDE_MODES = [('SAME', 'Same as ladder'), ('MARGIN', '% of margin'),
              ('ATR', 'ATR')]
#: The words that clear a side's own number back to the ladder's.
SAME_WORDS = ('', 'same', 'none', 'clear', 'off', '-')
OVERNIGHT = [('ALLOW', 'Hold overnight'),
             ('EXIT_IF_PROFIT', 'Exit if in profit'),
             ('EXIT_ALWAYS', 'Exit anyway')]

#: What can be changed from the phone: the name, what the trader calls
#: it, where it is kept (the Algo's own numbers, or the pair's), and how
#: it is typed.
FIELDS = [
    # Entry
    ('entry_z', 'Entry z', 'algo', 'number'),
    ('direction', 'Direction', 'algo', DIRECTIONS),
    ('algo_qty', 'Algo qty (spreads)', 'algo', 'number'),
    ('reentry_on', 'Wait for re-entry', 'algo', 'bool'),
    ('reentry_back', 'Re-entry back (sigma)', 'algo', 'number'),
    ('reentry_window_pct', 'Re-entry window (%)', 'algo', 'number'),
    ('max_entry_z', 'Max entry z', 'algo', 'number'),
    ('warmup_min', 'Warm-up (min)', 'algo', 'number'),
    ('cooldown_min', 'Cooldown (min)', 'algo', 'number'),
    ('cutoff_buffer_min', 'No entries before cutoff (min)', 'algo', 'number'),
    # Filters
    ('edge_on', 'Edge filter', 'algo', 'bool'),
    ('edge_multiple', 'Edge required (x)', 'algo', 'number'),
    ('regime_on', 'Regime filter', 'algo', 'bool'),
    ('trend_on', 'Trend filter', 'algo', 'bool'),
    ('trend_sigma', 'Trend limit (sigma)', 'algo', 'number'),
    ('trend_lookback_min', 'Trend lookback (min)', 'algo', 'number'),
    # Exits
    ('tp_target_pct_of_margin', 'Take profit (% of margin)', 'pair',
     'number'),
    ('stop_loss_on', 'Stop loss', 'algo', 'bool'),
    ('stop_loss_pct', 'Stop loss (% of margin)', 'algo', 'number'),
    ('stop_mode', 'Stop sized by', 'algo', LEVEL_MODES),
    ('target_mode', 'Target sized by', 'algo', LEVEL_MODES),
    ('atr_period', 'ATR period (candles)', 'algo', 'number'),
    ('atr_stop_mult', 'Stop x ATR', 'algo', 'number'),
    ('atr_target_mult', 'Target x ATR', 'algo', 'number'),
    ('reversion_on', 'Back to mean', 'algo', 'bool'),
    ('stop_z_on', 'Z-stop', 'algo', 'bool'),
    ('stop_z', 'Z-stop at |z|', 'algo', 'number'),
    ('time_stop_on', 'Time stop', 'algo', 'bool'),
    ('time_stop_candles', 'Time stop (candles)', 'algo', 'number'),
    # Daily limits
    ('max_trades_day', 'Max trades a day', 'algo', 'number'),
    ('max_losses_row', 'Max losses in a row', 'algo', 'number'),
    ('daily_loss_limit', 'Daily loss limit', 'algo', 'number'),
    # Costs and session
    ('commission_per_lot_a', 'Commission per lot, leg A (per side)', 'pair',
     'number'),
    ('commission_per_lot_b', 'Commission per lot, leg B (per side)', 'pair',
     'number'),
    ('overnight', 'Overnight', 'pair', OVERNIGHT),
    # This ladder's hours, HH:MM broker time
    ('session_open', 'Session open', 'pair', 'time'),
    ('session_close', 'Session close', 'pair', 'time'),
    ('break', 'Daily break', 'pair', 'range'),
]
# Each side's own exits: blank (SAME) is the ladder's own value.
for _suffix, _side in (('sell', 'H to L'), ('buy', 'L to H')):
    FIELDS += [
        (f'stop_mode_{_suffix}', f'{_side}: stop sized by', 'algo',
         SIDE_MODES),
        (f'target_mode_{_suffix}', f'{_side}: target sized by', 'algo',
         SIDE_MODES),
        (f'stop_loss_pct_{_suffix}', f'{_side}: stop loss (% margin)',
         'algo', 'side'),
        (f'tp_pct_{_suffix}', f'{_side}: take profit (% margin)', 'algo',
         'side'),
        (f'atr_stop_mult_{_suffix}', f'{_side}: stop x ATR', 'algo', 'side'),
        (f'atr_target_mult_{_suffix}', f'{_side}: target x ATR', 'algo',
         'side')]
FIELD = {f[0]: f for f in FIELDS}

#: The settings screen's sections, in order, and what is in each.
SECTIONS = {
    'entry': ('Entry', ['entry_z', 'direction', 'algo_qty', 'reentry_on',
                        'reentry_back', 'reentry_window_pct', 'max_entry_z',
                        'warmup_min',
                        'cooldown_min', 'cutoff_buffer_min']),
    'filters': ('Filters', ['edge_on', 'edge_multiple', 'regime_on',
                            'trend_on', 'trend_sigma',
                            'trend_lookback_min']),
    'exits': ('Exits', ['tp_target_pct_of_margin', 'stop_loss_on',
                        'stop_loss_pct', 'stop_mode', 'target_mode',
                        'atr_period', 'atr_stop_mult', 'atr_target_mult', 'reversion_on', 'stop_z_on',
                        'stop_z', 'time_stop_on', 'time_stop_candles']),
    'sides': ('Exits per side', [
        f'{key}_{suffix}' for suffix in ('sell', 'buy')
        for key in ('stop_mode', 'target_mode', 'stop_loss_pct', 'tp_pct',
                    'atr_stop_mult', 'atr_target_mult')]),
    'limits': ('Daily limits', ['max_trades_day', 'max_losses_row',
                                'daily_loss_limit']),
    'costs': ('Session and costs', ['session_open', 'session_close',
                                    'break', 'overnight',
                                    'commission_per_lot_a',
                                    'commission_per_lot_b']),
}
SECTION_OF = {field: section for section, (_, fields) in SECTIONS.items()
              for field in fields}

#: The settings table's labels, where the button's own is longer than
#: the label column.
SHORT_LABELS = {
    'reentry_on': 'Re-entry', 'reentry_back': 'Re-entry back',
    'reentry_window_pct': 'Re-entry window',
    'warmup_min': 'Warm-up min', 'cooldown_min': 'Cooldown min',
    'cutoff_buffer_min': 'Cutoff min', 'algo_qty': 'Algo qty',
    'edge_multiple': 'Edge required', 'trend_sigma': 'Trend limit',
    'trend_lookback_min': 'Trend lookback', 'edge_on': 'Edge',
    'regime_on': 'Regime', 'trend_on': 'Trend',
    'tp_target_pct_of_margin': 'Take profit %', 'stop_loss_on': 'Stop loss',
    'stop_loss_pct': 'Stop loss %', 'reversion_on': 'Back to mean',
    'stop_z_on': 'Z-stop', 'stop_z': 'Z-stop at |z|',
    'time_stop_on': 'Time stop', 'time_stop_candles': 'Time stop bars',
    'max_trades_day': 'Trades a day', 'max_losses_row': 'Losses in row',
    'daily_loss_limit': 'Loss limit', 'commission_per_lot_a': 'Comm A /lot',
    'commission_per_lot_b': 'Comm B /lot', 'max_entry_z': 'Max entry z',
}

#: What each setting is counted in, for the settings list.
UNITS = {
    'algo_qty': ' spreads', 'reentry_back': ' sigma',
    'reentry_window_pct': ' %', 'warmup_min': ' min',
    'cooldown_min': ' min', 'cutoff_buffer_min': ' min',
    'edge_multiple': ' x', 'trend_sigma': ' sigma',
    'trend_lookback_min': ' min', 'tp_target_pct_of_margin': ' %',
    'stop_loss_pct': ' %', 'stop_z': ' sigma', 'time_stop_candles': ' bars',
    'atr_period': ' candles', 'atr_stop_mult': ' x ATR',
    'atr_target_mult': ' x ATR',
    'max_trades_day': ' trades', 'max_losses_row': ' losses',
    'daily_loss_limit': ' USD', 'commission_per_lot_a': ' USD/lot',
    'commission_per_lot_b': ' USD/lot',
}

#: How a ladder's Algo mode is written.
MODE_WORDS = {'OFF': 'Off', 'DRY': 'Signals', 'LIVE': 'LIVE'}


def esc(value):
    return html.escape('' if value is None else str(value), quote=False)


def num(value, digits=2, signed=False):
    """A number for a message; unmeasured is a dash, never 0."""
    if value is None:
        return '—'
    try:
        value = float(value)
    except (TypeError, ValueError):
        return esc(value)
    return f'{value:+.{digits}f}' if signed else f'{value:.{digits}f}'


def money(value):
    if value is None:
        return '—'
    return f'${float(value):+,.2f}'


def mode_word(row):
    if not row.get('algo_on'):
        return 'OFF'
    return 'LIVE' if row.get('algo_mode') == 'LIVE' else 'DRY'


def effective_params(row):
    """The Algo settings IN FORCE, defaults included — what the desk's
    settings pane shows, not only what someone typed."""
    from . import algo as algo_module
    return algo_module.clean_params(row.get('algo_params'))


def side_words(side):
    return {'SELL': 'H to L', 'BUY': 'L to H'}.get(side, side or '?')


def table(rows):
    """Rows of (label, value) the way Stat_Arb_W3 writes them: a bold
    label, two spaces, the value in code type - which Telegram draws
    larger and clearer than a monospace block, each number in its own
    box. `None` is a blank line between groups. Values are raw text,
    escaped here once."""
    lines = []
    for row in rows:
        if row is None:
            lines.append('')
            continue
        label, value = row
        text = '—' if value is None or value == '' else str(value)
        lines.append(f'<b>{esc(str(label).strip())}</b>  '
                     f'<code>{esc(text)}</code>')
    return '\n'.join(lines)


def title(text, now=None):
    """A message's heading: what it is, and - for a screen - when (UTC).
    A trade message carries its own times in its rows instead."""
    if now is None:
        return f'<b>{esc(text)}</b>'
    return (f'<b>{esc(text)}  ·  '
            f'{time.strftime("%H:%M:%S", time.gmtime(now))} UTC</b>')


#: The commands, in the menu. Ordinary text, so Telegram makes each one
#: tappable - and no monospace block anywhere in the bot.
COMMANDS = [('/status', 'Engine & algo state, every ladder'),
            ('/positions', 'Open positions: live P&L + exit levels'),
            ('/trades', 'Recent closed trades'),
            ('/pnl', 'P&L summary'),
            ('/balance', 'Account balances'),
            ('/settings', 'Every setting, with its key'),
            ('/set', 'Change a setting: /set <key> <value>'),
            ('/alerts', 'Alerts on / off'),
            ('/ping', 'Alive check'),
            ('/start', 'Menu')]


def command_list():
    return '\n'.join(f'{name}  -  {esc(what)}' for name, what in COMMANDS)


def plain(value, digits=2, signed=False):
    """`num`, unescaped: values go into a `table`, which escapes once."""
    if value is None:
        return '—'
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f'{value:+.{digits}f}' if signed else f'{value:.{digits}f}'


# -- the trade messages ----------------------------------------------------------


def utc(at, date=True):
    if not at:
        return '—'
    return time.strftime('%Y-%m-%d %H:%M:%S UTC' if date else '%H:%M:%S UTC',
                         time.gmtime(float(at)))


def duration(seconds):
    if seconds is None:
        return '—'
    s = max(0, int(seconds))
    if s < 3600:
        return f'{s // 60}m {s % 60}s'
    if s < 86400:
        return f'{s // 3600}h {(s % 3600) // 60}m'
    return f'{s // 86400}d {(s % 86400) // 3600}h'


def w3(value, digits=2):
    """Money the way Stat_Arb_W3 writes a P&L: the sign after the
    dollar - `$+3.85`, `$-9.60`."""
    if value is None:
        return '—'
    return f'${float(value):+,.{digits}f}'


def w3fee(value, digits=2):
    """A cost, as W3 writes one: `-$0.40`."""
    if value is None:
        return '—'
    return f'-${abs(float(value)):,.{digits}f}'


def price(value, digits=4):
    """A leg price, as W3 writes one: `$4,131.0300`."""
    if value is None:
        return '—'
    try:
        return f'${float(value):,.{digits}f}'
    except (TypeError, ValueError):
        return '—'


def stamp_ms(at):
    """`HH:MM:SS.mmm UTC`, as W3 times an order."""
    if at is None:
        return '—'
    at = float(at)
    return (time.strftime('%H:%M:%S', time.gmtime(at))
            + f'.{int(round((at % 1) * 1000)) % 1000:03d} UTC')


def bps(spread, leg_a_price):
    """The spread in basis points of leg A, as W3 shows it."""
    try:
        return f'  ({float(spread) / float(leg_a_price) * 10000:+.2f} bps)'
    except (TypeError, ValueError, ZeroDivisionError):
        return ''


def usd(value, signed=True, digits=2):
    if value is None:
        return '—'
    value = float(value)
    if signed:
        return w3(value, digits)         # `$+3.85`, as W3 writes it
    return f'${value:,.{digits}f}'


def _z(value):
    return '' if value is None else f'  (Z: {plain(value, 4, True)})'


def _timing(opened, latency_ms):
    """W3's three timing rows: when the orders went, when the position
    was on, and the time between - from the desk's own clock."""
    placed = None if opened is None or latency_ms is None \
        else float(opened) - float(latency_ms) / 1000.0
    return [('Orders at', stamp_ms(placed)),
            ('Filled at', stamp_ms(opened)),
            ('Latency', '—' if latency_ms is None
             else f'{float(latency_ms):.0f} ms')]


def net_words(row):
    """The ladder's net position from its OPEN positions, in the desk's
    words: `flat`, `H to L 0.50`."""
    net = 0.0
    for position in row.get('positions') or ():
        net += _sign(position.get('side')) * float(position.get('quantity')
                                                   or 0.0)
    if abs(net) < 1e-9:
        return 'flat'
    return f'{side_words("SELL" if net < 0 else "BUY")} {plain(abs(net))}'


def _signal_rows(levels, block):
    """W3's signal rows - the z, the band's SD and mean, the half-life,
    the regime - from what the Algo measured on this ladder."""
    block = block or {}
    filters = block.get('filters') or {}
    regime = filters.get('regime') or {}
    rows = [('Z-score', plain((levels or {}).get('entry_z'), 4, True))]
    if block.get('sigma') is not None:
        rows.append(('Spread SD', plain(block.get('sigma'), 6)))
    if block.get('mean') is not None:
        rows.append(('Spread Mean', plain(block.get('mean'), 4, True)))
    candles = filters.get('half_life_candles')
    if candles is not None:
        minutes = filters.get('half_life_minutes')
        rows.append(('Half-Life', f'{float(candles):.1f} periods'
                     + (f'  ({float(minutes):,.0f} min)'
                        if minutes is not None else '')))
    if regime.get('state'):
        rows.append(('Regime', str(regime['state']).lower()
                     .replace('_', ' ')))
    return rows


def _fees_rows(position, levels):
    """W3's Est. Fees and Breakeven: what the round trip costs, and the
    spread move that pays for it - break-even less entry, times k."""
    be = (levels or {}).get('break_even')
    if be is None:
        be = (position.get('exit') or {}).get('break_even')
    entry = position.get('entry_spread')
    units = position.get('spread_units')
    if be is None or entry is None:
        return [('Est. Fees', '—'), ('Breakeven', '—')]
    move = (float(be) - float(entry)) * _sign(position.get('side'))
    if abs(move) < 5e-5:
        move = 0.0                       # never "-0.0000"
    fees = abs(move) * float(units) if units else None
    return [('Est. Fees', f'{w3fee(fees, 4)}  (round trip, both legs)'
             if fees is not None else '—'),
            ('Breakeven', f'{move:+.4f} spread move')]


def _legs(position):
    out = []
    for leg in ('leg_a', 'leg_b'):
        fill = position.get(leg) or {}
        out.append((leg[-1].upper(), fill))
    return out


def notional(position):
    """Both legs' face value at entry, or None if any part is unknown."""
    total = 0.0
    for _, fill in _legs(position):
        try:
            total += (float(fill['volume']) * float(fill['contract_size'])
                      * float(fill['price']))
        except (KeyError, TypeError, ValueError):
            return None
    return total


def pnl_shares(pnl, position):
    """Net P&L as a share of the NOTIONAL (both legs' face value) and of
    the MARGIN it tied up - the base the TP % and SL % are set on, so a
    target hit reads as about its TP %. An unmeasured base is '—'."""
    if pnl is None:
        return ''
    face = notional(position)
    margin = position.get('entry_margin')
    if margin:
        margin = float(margin) * float(position.get('quantity') or 1.0)

    def share(base, digits):
        return f'{pnl / base * 100:+.{digits}f}%' if base else '—'
    return (f'  ({share(face, 4)} of notional \u00b7 '
            f'{share(margin, 2)} of margin)')


def margin_text(position):
    margin = position.get('entry_margin')
    if not margin:
        return '—'
    margin = float(margin) * float(position.get('quantity') or 1.0)
    face = notional(position)
    if face and margin:
        return f'{usd(margin, False)}  ({face / margin:.0f}x)'
    return usd(margin, False)


def _sign(side):
    return 1.0 if side == 'BUY' else -1.0


def level_money(position, level):
    """Gross money at a spread level, for the whole position."""
    entry = position.get('entry_spread')
    units = position.get('spread_units')
    if level is None or entry is None or not units:
        return None
    return (float(level) - float(entry)) * _sign(position.get('side')) \
        * float(units)


def leg_now(position, market, leg):
    """The price this leg would CLOSE at now: a bought leg on the bid,
    a sold one at the offer."""
    fill = position.get('leg_' + leg) or {}
    side = fill.get('side')
    return (market or {}).get(f'leg_{leg}_bid' if side == 'BUY'
                              else f'leg_{leg}_ask')


def _pct(now, then):
    try:
        return f'  ({(float(now) / float(then) - 1.0) * 100:+.2f}%)'
    except (TypeError, ValueError, ZeroDivisionError):
        return ''


def trade_entry_text(position, ladder, levels=None, block=None):
    """TRADE ENTRY, row for row as Stat_Arb_W3 sends it - then the exits
    this desk sets, which W3 has no rows for."""
    levels = levels or {}
    legs = _legs(position)
    face = notional(position)
    fill_a = (position.get('leg_a') or {})
    rows = [('ID', f'#{position.get("position_id") or "pending"}'),
            ('Entry Time', utc(position.get('opened_at'))), None]
    for name, fill in legs:
        rows.append((f'Leg {name} Lots', f'{fill.get("side") or ""} '
                                         f'{plain(fill.get("volume"))} '
                                         f'{fill.get("symbol") or ""}'))
    rows += [('Notional', price(face, 2) if face else '—'),
             ('Margin Req', margin_text(position)), None]
    for name, fill in legs:
        rows.append((f'Leg {name} Entry', price(fill.get('price'))))
    rows.append(('Spread', plain(position.get('entry_spread'), 4, True)
                 + bps(position.get('entry_spread'), fill_a.get('price'))))
    rows.append(None)
    rows += _signal_rows(levels, block)
    rows.append(None)
    rows += _fees_rows(position, levels)
    rows.append(None)
    rows += _timing(position.get('opened_at'),
                    position.get('click_to_on_ms'))
    # What this desk adds: where it comes off, and who put it on.
    rows += [None, ('Source', (position.get('source') or 'MANUAL').title())]
    if levels.get('entry_atr') is not None:
        rows.append(('ATR at entry', plain(levels.get('entry_atr'), 4)
                     + f'  (TP {_basis(levels, "target")}, '
                       f'SL {_basis(levels, "stop")})'))
    for label, key in (('Take Profit', 'tp'), ('Stop Loss', 'sl')):
        if key in levels:
            value = levels.get(key)
            rows.append((label, plain(value, 4) + (
                f'  ({w3(levels.get(key + "_money"))} net)'
                if levels.get(key + '_money') is not None else
                f'  ({w3(level_money(position, value))} gross)'
                if level_money(position, value) is not None else '')))
    rows.append(('Slippage', plain(position.get('entry_slippage'), 4, True)
                 + ' spread'))
    return (title(f'TRADE ENTRY  ·  {side_words(position.get("side"))} '
                  f'{ladder}') + '\n' + table(rows))


def trade_exit_text(record, ladder):
    """TRADE EXIT, row for row as Stat_Arb_W3 sends it, then ANALYSIS."""
    position = record.get('position') or {}
    exits = record.get('exit_prices') or {}
    pnl = position.get('realized_pnl')
    result = '—' if pnl is None else ('PROFIT' if pnl >= 0 else 'LOSS')
    opened, closed = position.get('opened_at'), position.get('closed_at')
    held = None if not opened or not closed else closed - opened
    entry, exit_ = position.get('entry_spread'), position.get('exit_spread')
    change = None if entry is None or exit_ is None else \
        (exit_ - entry) * _sign(position.get('side'))
    gross = level_money(position, exit_) if exit_ is not None else None
    fees = None if gross is None or pnl is None else gross - pnl
    rows = [('Reason', position.get('close_reason')),
            ('Duration', duration(held)),
            ('Exit Time', utc(closed)), None]
    for name, fill in _legs(position):
        rows.append((f'Leg {name} Entry', price(fill.get('price'))))
        # The broker's closing deals first; failing those, the close
        # price the leg itself recorded when it was filled.
        closed_at = exits.get('leg_' + name.lower())
        if closed_at is None:
            closed_at = fill.get('exit_price')
        rows.append((f'Leg {name} Exit', price(closed_at)))
    rows += [None,
             ('Entry Spread', plain(entry, 4, True) + _z(record.get('entry_z'))),
             ('Exit Spread', plain(exit_, 4, True) + _z(record.get('exit_z'))),
             ('Spread Chg', plain(change, 4, True)
              + ('' if change is None else
                 ('  (with)' if change >= 0 else '  (against)'))), None,
             # The close's own clock: when it was on is all the desk
             # keeps for an exit, so the send time is not invented.
             ('Orders at', '—'),
             ('Filled at', stamp_ms(closed)),
             ('Latency', '—'), None,
             ('Gross PnL', w3(gross, 4)),
             ('Est. Fees', f'{w3fee(fees, 4)}  (commission, both legs)'
              if fees is not None else '—'),
             ('Net PnL', w3(pnl, 4) + pnl_shares(pnl, position)),
             ('Slippage', f'in {plain(position.get("entry_slippage"), 4, True)}'
                          f' / out '
                          f'{plain(position.get("exit_slippage"), 4, True)}')]
    peak, trough = position.get('peak_pnl'), position.get('trough_pnl')
    analysis_rows = [('Outcome', _outcome(position.get('close_reason'),
                                          pnl))]
    if peak is not None or trough is not None:
        analysis_rows.append((
            'Peak/Trough',
            f'{w3(peak)} ({plain(position.get("peak_min"), 0)}m) / '
            f'{w3(trough)} ({plain(position.get("trough_min"), 0)}m)'))
        if peak and peak > 0 and pnl is not None:
            analysis_rows.append(('Capture', f'{w3(pnl)} of {w3(peak)} '
                                             f'best ({pnl / peak * 100:+.0f}%)'))
    analysis_rows.append(('Hold', duration(held)))
    if record.get('entry_z') is not None or record.get('exit_z') is not None:
        analysis_rows.append(('Z path',
                              f'{plain(record.get("entry_z"), 4, True)} -> '
                              f'{plain(record.get("exit_z"), 4, True)}'))
    return (title(f'TRADE EXIT  ·  {side_words(position.get("side"))} '
                  f'{ladder}  ·  {result}') + '\n' + table(rows)
            + '\n\n' + title('ANALYSIS') + '\n' + table(analysis_rows))


def _basis(levels, which):
    return 'by ATR' if levels.get(f'{which}_mode') == 'ATR' else '% margin'


def _outcome(reason, pnl):
    reason = str(reason or '').lower()
    if 'profit target' in reason:
        return 'TARGET HIT - banked at the take-profit'
    if 'stop loss' in reason:
        return 'STOPPED - the stop loss took it off'
    if 'back to the mean' in reason:
        return 'BACK TO THE MEAN - taken at the mean, in profit'
    if 'overnight' in reason or 'cutoff' in reason:
        return 'SESSION CUTOFF - closed by the overnight rule'
    if 'kill' in reason:
        return 'KILL ALL'
    if pnl is None:
        return 'CLOSED - P&L not measured'
    return 'CLOSED BY HAND - ' + ('in profit' if pnl >= 0 else 'at a loss')


def position_rows(position, row, levels=None, now=None):
    """OPEN POSITIONS, one position: W3's rows in W3's order, then the
    P&L and the exits this desk holds for it."""
    levels = levels or {}
    market = row.get('market') or {}
    block = row.get('algo_block') or {}
    legs = _legs(position)
    rows = [('Position', f'{side_words(position.get("side"))} '
                         f'{plain(position.get("quantity"))} spread(s)  ·  '
                         f'{(position.get("source") or "MANUAL").lower()}'),
            None]
    for name, fill in legs:
        rows.append((f'Leg {name} Lots', f'{fill.get("side") or ""} '
                                         f'{plain(fill.get("volume"))} '
                                         f'{fill.get("symbol") or ""}'))
    face = notional(position)
    rows += [('Notional', price(face, 2) if face else '—'),
             ('Margin Req', margin_text(position)),
             ('Entry Time', utc(position.get('opened_at'))), None]
    for name, fill in legs:
        rows.append((f'Leg {name} Entry', price(fill.get('price'))))
    rows.append(('Entry Spread', plain(position.get('entry_spread'), 4, True)
                 + _z(levels.get('entry_z'))))
    rows.append(None)
    for name, fill in legs:
        now_price = leg_now(position, market, name.lower())
        rows.append((f'Leg {name} Now', price(now_price)
                     + _pct(now_price, fill.get('price'))))
    closing = position.get('closing_spread')
    z_now = block.get('z_buy' if position.get('side') == 'SELL'
                      else 'z_sell')
    rows.append(('Spread Now', plain(closing, 4, True) + _z(z_now)))
    rows.append(None)
    rows += _timing(position.get('opened_at'),
                    position.get('click_to_on_ms'))
    # What this desk adds.
    entry = position.get('entry_spread')
    delta = None if closing is None or entry is None else closing - entry
    good = None if delta is None else delta * _sign(position.get('side'))
    rows += [None,
             ('Spread Chg', plain(delta, 4, True) + (
                 '' if good is None else
                 ('  (with)' if good >= 0 else '  (against)'))),
             ('Net PnL', w3(position.get('net_pnl'))
              + pnl_shares(position.get('net_pnl'), position))]
    if levels:
        for label, key in (('Break-even', 'break_even'),
                           ('Take Profit', 'tp'), ('Stop Loss', 'sl')):
            value = levels.get(key)
            worth = levels.get(key + '_money')
            if worth is None and key != 'break_even':
                gross = level_money(position, value)
                extra = f'  ({w3(gross)} gross)' if gross is not None else ''
            else:
                extra = f'  ({w3(worth)} net)' if worth is not None else ''
            rows.append((label, plain(value, 4) + extra))
        if levels.get('entry_atr') is not None:
            rows.append(('ATR at entry', plain(levels.get('entry_atr'), 4)
                         + f'  (TP {_basis(levels, "target")}, '
                           f'SL {_basis(levels, "stop")})'))
    opened = position.get('opened_at')
    if opened and now:
        rows.append(('Age', duration(now - opened)))
    rows.append(('Slippage', plain(position.get('entry_slippage'), 4, True)
                 + ' spread'))
    return rows


class TelegramError(Exception):
    pass


class TelegramAPI:
    """The Bot API over HTTPS, with the standard library only.

    The token is in every URL Telegram takes, so no URL — and no
    exception that might carry one — is ever logged or raised from
    here: only Telegram's own description of what went wrong.
    """

    def __init__(self, token, opener=None, timeout=40.0):
        self._token = token
        self._open = opener or urllib.request.urlopen
        self.timeout = timeout

    def call(self, method, **params):
        url = f'{API_ROOT}/bot{self._token}/{method}'
        data = json.dumps({k: v for k, v in params.items()
                           if v is not None}).encode('utf-8')
        request = urllib.request.Request(
            url, data=data, headers={'Content-Type': 'application/json'})
        try:
            with self._open(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode('utf-8'))
            except Exception:
                body = {}
            raise TelegramError(f'{method}: HTTP {e.code} '
                                f'{body.get("description") or ""}'.strip())
        except urllib.error.URLError as e:
            raise TelegramError(f'{method}: cannot reach Telegram '
                                f'({type(e.reason).__name__})')
        except (OSError, ValueError) as e:
            raise TelegramError(f'{method}: {type(e).__name__}')
        if not body.get('ok'):
            raise TelegramError(f'{method}: {body.get("description")}')
        return body.get('result')


class WebDesk:
    """The desk, through the web app's own routes.

    In process, with Flask's test client: nothing goes over the network,
    and every request meets the checks a browser's would.
    """

    def __init__(self, app, secret, sleep=time.sleep, clock=time.time):
        self.client = app.test_client()
        self.headers = {BOT_HEADER: secret}
        self.sleep = sleep
        self.clock = clock

    def _get(self, path):
        response = self.client.get(path, headers=self.headers)
        return response.get_json(silent=True) or {}

    def _post(self, path, body):
        response = self.client.post(path, json=body, headers=self.headers)
        answer = response.get_json(silent=True) or {}
        if response.status_code >= 400 and 'ok' not in answer:
            answer['ok'] = False
        return answer

    def status(self):
        return self._get('/api/status')

    def config_pair(self, key):
        return ((self._get('/api/config').get('pairs') or {}).get(key)
                or {})

    def analysis(self, days=0):
        """The Analysis tab's report: closed trades, summary, journal."""
        return self._get(f'/api/analysis?days={float(days)}')

    def position(self, position_id):
        """One position as recorded, with its closing prices and z."""
        return self._get('/api/position/'
                         + urllib.parse.quote(str(position_id), safe=''))

    def events(self, limit=50):
        return self._get(f'/api/events?limit={int(limit)}').get('events') \
            or []

    def save_pair(self, key, payload):
        return self._post('/api/pairs/' + urllib.parse.quote(key, safe=''),
                          payload)

    def command(self, kind, payload, wait=10.0):
        """Send one engine command and wait for its answer.

        `{'ok': True/False/None, 'reason': ..., 'data': ...}` — None
        when it was queued and the engine has not answered yet.
        """
        if kind not in ALLOWED_COMMANDS:
            raise ValueError(f'the Telegram bot does not send {kind!r}')
        sent = self._post('/api/command', {'kind': kind, 'payload': payload})
        if not sent.get('ok'):
            return {'ok': False, 'reason': sent.get('error') or 'refused'}
        deadline = self.clock() + wait
        while self.clock() < deadline:
            result = self._get(f'/api/result/{sent["id"]}')
            if not result.get('pending'):
                data = result.get('data')
                detail = data if isinstance(data, dict) else {}
                # The runner's ok is "the command ran"; the engine's own
                # answer inside it can still be a refusal.
                ok = bool(result.get('ok')) and detail.get('ok') is not False
                return {'ok': ok, 'data': data,
                        'reason': result.get('error') or detail.get('reason')}
            self.sleep(0.25)
        return {'ok': None, 'reason': 'sent — the engine has not answered '
                                      'yet; check the status in a moment'}


def allowed_users(raw):
    """`TELEGRAM_ALLOWED_USERS`: numeric ids, comma or space separated."""
    ids = set()
    for part in (raw or '').replace(',', ' ').split():
        try:
            ids.add(int(part))
        except ValueError:
            logging.warning('[telegram] %r in TELEGRAM_ALLOWED_USERS is not '
                            'a numeric user id — ignored', part)
    return ids


class Bot:
    """The menu, the confirms and the alerts."""

    def __init__(self, api, desk, allowed, clock=time.time):
        self.api = api
        self.desk = desk
        self.allowed = set(allowed)
        self.clock = clock
        self.offset = None
        self._tokens = {}
        self._waiting = {}          # chat -> (expires, pair key, field)
        self._ignored = set()
        self.alerts_off = set()     # chats that turned alerts off
        self.watch = AlertWatch(clock)
        self.lock = threading.Lock()

    # -- the transport --------------------------------------------------------

    def poll_once(self, timeout=25):
        updates = self.api.call('getUpdates', offset=self.offset,
                                timeout=timeout,
                                allowed_updates=['message',
                                                 'callback_query']) or []
        for update in updates:
            self.offset = update['update_id'] + 1
            try:
                with self.lock:
                    self.handle(update)
            except Exception as e:          # one bad update never stops it
                logging.exception('[telegram] update failed: %s', e)
        return len(updates)

    def send(self, chat, text, buttons=None):
        return self.api.call('sendMessage', chat_id=chat, text=text,
                             parse_mode='HTML',
                             disable_web_page_preview=True,
                             reply_markup=self._markup(buttons))

    def show(self, chat, message_id, text, buttons=None):
        """Replace a menu in place, rather than stacking up messages."""
        if message_id is None:
            return self.send(chat, text, buttons)
        try:
            return self.api.call('editMessageText', chat_id=chat,
                                 message_id=message_id, text=text,
                                 parse_mode='HTML',
                                 disable_web_page_preview=True,
                                 reply_markup=self._markup(buttons))
        except TelegramError as e:
            if 'not modified' in str(e):
                return None
            return self.send(chat, text, buttons)

    def _markup(self, buttons):
        if not buttons:
            return None
        return {'inline_keyboard': [
            [{'text': label, 'callback_data': self._token(action, args)}
             for label, action, args in row] for row in buttons]}

    def _token(self, action, args):
        now = self.clock()
        for token in [t for t, v in self._tokens.items() if v[0] < now]:
            del self._tokens[token]
        token = secrets.token_urlsafe(9)
        self._tokens[token] = (now + TOKEN_TTL_SEC, action, args)
        return token

    # -- one update -------------------------------------------------------------

    def handle(self, update):
        if 'callback_query' in update:
            query = update['callback_query']
            user = (query.get('from') or {}).get('id')
            if not self._may(user):
                return
            message = query.get('message') or {}
            chat = (message.get('chat') or {}).get('id', user)
            try:
                self.api.call('answerCallbackQuery',
                              callback_query_id=query.get('id'))
            except TelegramError:
                pass
            entry = self._tokens.pop(query.get('data'), None)
            if entry is None or entry[0] < self.clock():
                self.show(chat, message.get('message_id'),
                          'That button has expired. Here is the menu again.',
                          self._main_buttons(chat))
                return
            _, action, args = entry
            getattr(self, 'on_' + action)(chat, message.get('message_id'),
                                          *args)
            return
        message = update.get('message') or {}
        user = (message.get('from') or {}).get('id')
        if not self._may(user):
            return
        chat = (message.get('chat') or {}).get('id', user)
        text = (message.get('text') or '').strip()
        waiting = self._waiting.pop(chat, None)
        if waiting and waiting[0] >= self.clock() \
                and not text.startswith('/'):
            return self._typed_value(chat, waiting[1], waiting[2], text)
        command = text.split()[0].split('@')[0].lower() if text else ''
        if command == '/alerts':
            if chat in self.alerts_off:
                self.alerts_off.discard(chat)
                return self.send(chat, 'Alerts are <b>on</b> for this chat.')
            self.alerts_off.add(chat)
            return self.send(chat, 'Alerts are <b>off</b> for this chat. '
                                   'Send /alerts to turn them back on.')
        if command == '/ping':
            return self.send(chat, f'pong  ·  '
                             f'{time.strftime("%H:%M:%S", time.gmtime(self.clock()))}'
                             f' UTC')
        if command == '/positions':
            return self.on_positions(chat, None)
        if command == '/balance':
            return self.on_balance(chat, None)
        if command == '/trades':
            return self.on_trades(chat, None)
        if command == '/pnl':
            return self.on_pnl(chat, None)
        if command in ('/dashboard', '/status', '/algo'):
            return self.on_dashboard(chat, None)
        if command == '/settings':
            return self.on_settings_all(chat)
        if command == '/set':
            return self._set_command(chat, text.split()[1:])
        return self.on_main(chat, None)

    def _may(self, user):
        if user in self.allowed:
            return True
        if user not in self._ignored:
            self._ignored.add(user)
            logging.warning('[telegram] ignored a message from user id %s — '
                            'add it to TELEGRAM_ALLOWED_USERS in .env to '
                            'allow it', user)
        return False

    # -- reading the desk ------------------------------------------------------

    def _snapshot(self):
        snapshot = self.desk.status() or {}
        return snapshot, snapshot.get('pairs') or {}

    def _pair_name(self, pairs, key):
        return esc((pairs.get(key) or {}).get('name') or key)

    def _main_buttons(self, chat):
        alerts = 'OFF' if chat in self.alerts_off else 'ON'
        return [[('Status', 'dashboard', ()),
                 ('Positions', 'positions', ())],
                [('Trades', 'trades', ()), ('P&L', 'pnl', ())],
                [('Balance', 'balance', ()),
                 ('Settings', 'settings_list', ())],
                [(f'Alerts: {alerts}', 'toggle_alerts', ()),
                 ('KILL ALL', 'kill_ask', ())]]

    # -- the main menu -------------------------------------------------------------

    def on_main(self, chat, message_id):
        snapshot, pairs = self._snapshot()
        rows = [('Engine', self._engine_plain(snapshot)),
                ('Alerts', 'OFF' if chat in self.alerts_off else 'ON')]
        for key, row in sorted(pairs.items()):
            pnl = row.get('open_pnl')
            rows.append(None)
            rows.append((row.get('name') or key,
                         f'{MODE_WORDS[mode_word(row)]}  |  net '
                         f'{plain(row.get("net_position"), 2, True)}'
                         + (f'  |  {money(pnl)}' if pnl is not None else '')))
        text = (title('NEXUS SPREAD DESK', self.clock()) + '\n'
                + table(rows) + '\n\n<b>COMMANDS</b>\n' + command_list())
        if not pairs:
            text += '\nNo ladders are configured.'
        self.show(chat, message_id, text, self._main_buttons(chat))

    def on_toggle_alerts(self, chat, message_id):
        if chat in self.alerts_off:
            self.alerts_off.discard(chat)
        else:
            self.alerts_off.add(chat)
        self.on_main(chat, message_id)

    # -- the dashboard: everything there is to watch, on one screen ---------------

    @staticmethod
    def _engine_plain(snapshot):
        state = snapshot.get('engine') or 'down'
        if state == 'up':
            return 'Running'
        note = snapshot.get('engine_note')
        return state.upper() + (f' - {note}' if note else '')

    def on_dashboard(self, chat, message_id):
        snapshot, pairs = self._snapshot()
        rows = [('Engine', self._engine_plain(snapshot))]
        clock = snapshot.get('broker_clock') or {}
        if clock.get('broker_time'):
            rows.append(('Broker time', clock['broker_time']))
            rows.append(('Cutoff', clock.get('cutoff')))
        for name, info in sorted((snapshot.get('accounts') or {}).items()):
            if not info:
                rows.append((name, 'NOT ANSWERING'))
                continue
            equity = info.get('equity')
            level = info.get('margin_level')
            rows.append((name, ('—' if equity is None else
                                f'{float(equity):,.2f}') + ' '
                         + (info.get('currency') or '')))
            rows.append(('Margin level', f'{float(level):,.0f}%'
                         if level else '—'))
        for name in snapshot.get('dark_accounts') or []:
            rows.append((name, 'leg runner NOT ANSWERING'))
        parts = [title('SYSTEM STATUS', self.clock()) + '\n' + table(rows)]
        buttons = []
        for key, row in sorted(pairs.items()):
            parts.append(f'<b>{self._pair_name(pairs, key)}</b>\n'
                         + table(self._ladder_rows(row)))
            if row.get('positions'):
                buttons.append([(f'Close all: {row.get("name") or key}',
                                 'flatten_ask', (key,))])
        if not pairs:
            parts.append('No ladders are configured.')
        check = snapshot.get('pnl_check') or {}
        if check.get('ours') is not None:
            parts.append('<b>Total</b>\n'
                         + table([('Open P&L', money(check.get('ours'))),
                                  ('MT5 says', money(check.get('theirs')))]))
        buttons.append([('Refresh', 'dashboard', ()),
                        ('Main menu', 'main', ())])
        self.show(chat, message_id, '\n\n'.join(parts), buttons)

    def _ladder_rows(self, row):
        block = row.get('algo_block') or {}
        params = effective_params(row)
        problem = self._price_problem(row)
        mode = mode_word(row)
        filters_now = block.get('filters') or {}
        regime_now = filters_now.get('regime') or {}
        rows = [('Algo', 'Disabled' if mode == 'OFF' else 'Enabled'),
                ('Mode', {'OFF': '—', 'DRY': 'Signals',
                          'LIVE': 'Live'}[mode]),
                ('Position', net_words(row)),
                ('Z-score', plain(block.get('z_mid'), 4, True)),
                ('Regime', (regime_now.get('state') or '—').lower()
                 .replace('_', ' ') if regime_now.get('state') else '—')]
        candles = filters_now.get('half_life_candles')
        if candles is not None:
            minutes = filters_now.get('half_life_minutes')
            rows.append(('Half-Life', f'{float(candles):.1f} periods'
                         + (f'  ({float(minutes):,.0f} min)'
                            if minutes is not None else '')))
        rows += [None,
                 ('Feed', f'PROBLEM: {problem}' if problem else 'OK'),
                 ('Session', self._session_words(row))]
        on = row.get('algo_on')
        if on:
            rows.append(('State', (block.get('state') or 'WATCHING')
                         .lower().replace('_', ' ')))
            warmup = block.get('warmup') or {}
            # The warm-up has its own row; saying it twice is noise.
            if block.get('blocked') and not str(block['blocked']) \
                    .startswith('warming up'):
                rows.append(('Held because', block['blocked']))
            if warmup.get('need_sec'):
                rows.append(('Warm-up', 'complete' if warmup.get('done') else
                             f'{int((warmup.get("sec") or 0) // 60)} of '
                             f'{round(warmup["need_sec"] / 60)} min'))
        rows.append(None)
        rows.append(('H to L', plain(row.get('short_spread'), 3)
                     + (f'   z {plain(block.get("z_sell"), 2, True)}'
                        if on else '')))
        rows.append(('L to H', plain(row.get('long_spread'), 3)
                     + (f'   z {plain(block.get("z_buy"), 2, True)}'
                        if on else '')))
        if on:
            entry = params.get('entry_z')
            if params.get('reentry_on'):
                back = max(0.0, (entry or 0) - (params.get('reentry_back')
                                                or 0))
                edge = back * (1.0 - (params.get('reentry_window_pct')
                                      or 100.0) / 100.0)
                rows.append(('Arms at', f'z +/-{plain(entry)}'))
                # The whole window: past its far edge the side disarms.
                rows.append(('Enters at', f'z +/-{plain(back)} to '
                                          f'+/-{plain(edge)} (way back)'))
                armed = block.get('armed') or {}
                rows.append(('Armed', ', '.join(
                    side_words(s) for s in ('SELL', 'BUY') if armed.get(s))
                    or 'no'))
            else:
                rows.append(('Enters at', f'z +/-{plain(entry)}'))
                rows.append(('Band', f'{plain(block.get("lower"), 3)} to '
                                     f'{plain(block.get("upper"), 3)}'))
            filters = block.get('filters') or {}
            edge = filters.get('edge') or {}
            regime = filters.get('regime') or {}
            trend = filters.get('trend') or {}
            rows.append(None)
            rows.append(('Edge', 'off' if not edge.get('on') else
                         ('pass' if edge.get('ok') else 'FAIL')
                         + f' {plain(edge.get("ratio"))}x of '
                           f'{plain(edge.get("required"), 1)}x'))
            rows.append(('Regime filter', 'off' if not regime.get('on') else
                         (regime.get('state') or '—').lower()
                         .replace('_', ' ')))
            rows.append(('Trend', 'off' if not trend.get('on') else
                         (trend.get('state') or 'waiting').lower()))
            rows.append(('Ready', 'yes' if filters.get('ready') else 'no'))
            atr_modes = 'ATR' in (params.get('stop_mode'),
                                  params.get('target_mode'))
            rows.append((f'ATR({block.get("atr_period") or params.get("atr_period")})',
                         plain(block.get('atr'), 4)
                         + (f'  (stop {plain(params.get("atr_stop_mult"), 1)}x'
                            f', target {plain(params.get("atr_target_mult"), 1)}x)'
                            if atr_modes else '')))
        levels = {p.get('position_id'): p
                  for p in (block.get('positions') or [])}
        positions = row.get('positions') or []
        if positions:
            rows.append(None)
        for position in positions:
            level = levels.get(position.get('position_id')) or {}
            rows.append(('Holding', f'{side_words(position.get("side"))} '
                         f'{plain(position.get("quantity"))} '
                         f'({(position.get("source") or "MANUAL").lower()})'))
            rows.append(('Entry', plain(position.get('entry_spread'), 3)))
            rows.append(('Now', plain(position.get('closing_spread'), 3)))
            rows.append(('P&L', money(position.get('net_pnl'))))
            if level:
                rows.append(('Take profit', plain(level.get('tp'), 3)))
                rows.append(('Stop loss', plain(level.get('sl'), 3)))
                rows.append(('Break-even', plain(level.get('break_even'),
                                                 3)))
        if on:
            day = block.get('day') or {}
            rows.append(None)
            rows.append(('Trades today', f'{day.get("trades") or 0} of '
                         f'{params.get("max_trades_day") or "unlimited"}'))
            rows.append(('Losses in row', day.get('losses_row') or 0))
            rows.append(('P&L today', money(day.get('pnl'))))
            last = block.get('last_blocked')
            if last:
                rows.append(('Last blocked', f'{side_words(last.get("side"))}'
                             f' at z {plain(last.get("z"), 2, True)}: '
                             f'{last.get("reason")}'))
        return rows

    def on_status(self, chat, message_id):
        self.on_dashboard(chat, message_id)

    def on_positions(self, chat, message_id):
        snapshot, pairs = self._snapshot()
        now = self.clock()
        parts = [title('OPEN POSITIONS', now)]
        buttons = []
        for key, row in sorted(pairs.items()):
            positions = row.get('positions') or []
            if not positions:
                continue
            block = row.get('algo_block') or {}
            levels = {p.get('position_id'): p
                      for p in (block.get('positions') or [])}
            for position in positions:
                level = dict(levels.get(position.get('position_id')) or {})
                if not level and position.get('exit'):
                    level = {'break_even': position['exit'].get(
                        'break_even'), 'tp': position['exit'].get('tp')}
                parts.append(f'<b>{self._pair_name(pairs, key)}</b>\n'
                             + table(position_rows(position, row, level,
                                                   now)))
            buttons.append([(f'Close all: {row.get("name") or key}',
                             'flatten_ask', (key,))])
        if len(parts) == 1:
            parts.append('No open positions.')
        buttons.append([('Refresh', 'positions', ()),
                        ('Main menu', 'main', ())])
        self.show(chat, message_id, '\n\n'.join(parts), buttons)

    def _report_of(self, days=0):
        report = getattr(self.desk, 'analysis', None)
        try:
            return (report(days) if report else {}) or {}
        except Exception as e:
            logging.warning('[telegram] the trade record: %s', e)
            return {}

    def on_trades(self, chat, message_id):
        """The five most recent closed trades, each in full."""
        report = self._report_of(0)
        trips = (report.get('journal') or [])[:5]
        names = report.get('names') or {}
        parts = [title('RECENT TRADES', self.clock())]
        if not report.get('ok'):
            parts.append('The trade record cannot be read right now.')
        elif not trips:
            parts.append('No closed trades yet.')
        lookup = getattr(self.desk, 'position', None)
        for trip in trips:
            pnl = trip.get('pnl')
            result = '—' if pnl is None else ('PROFIT' if pnl >= 0
                                              else 'LOSS')
            entry, exit_ = trip.get('entry_spread'), trip.get('exit_spread')
            # Each leg's closing price, from the broker's own deals.
            exits = {}
            if lookup is not None and trip.get('position_id'):
                try:
                    exits = (lookup(trip['position_id']) or {}) \
                        .get('exit_prices') or {}
                except Exception as e:
                    logging.warning('[telegram] trade %s: %s',
                                    trip.get('position_id'), e)
            fill_a = trip.get('leg_a') or {}
            rows = [('Exit', trip.get('exit_reason')),
                    ('Duration', duration(trip.get('held_sec'))), None]
            for leg in ('a', 'b'):
                fill = trip.get('leg_' + leg) or {}
                closed_at = exits.get('leg_' + leg)
                if closed_at is None:
                    closed_at = fill.get('exit')
                rows.append((f'Leg {leg.upper()} Entry',
                             price(fill.get('entry'))))
                rows.append((f'Leg {leg.upper()} Exit', price(closed_at)))
            rows += [None,
                     ('Entry Spread', plain(entry, 4, True)
                      + bps(entry, fill_a.get('entry'))),
                     ('Exit Spread', plain(exit_, 4, True)),
                     ('Entry Z', plain(trip.get('entry_z'), 4, True)),
                     ('Exit Z', plain(trip.get('exit_z'), 4, True)),
                     None,
                     ('Gross PnL', w3(trip.get('gross_pnl'))),
                     ('Est. Fees', w3fee(trip.get('fees'))),
                     ('Net PnL', f'{w3(pnl)}  {result}'),
                     # What this desk adds.
                     None,
                     ('Closed', utc(trip.get('closed_at'))),
                     ('Source', (trip.get('source') or 'MANUAL').title()),
                     ('Size', f'{plain(trip.get("quantity"))} spread(s)'),
                     ('Running', w3(trip.get('cum_pnl')))]
            ladder = names.get(trip.get('pair_key')) or trip.get('pair_key')
            parts.append(f'<b>#{esc(trip.get("position_id"))}  '
                         f'{esc(side_words(trip.get("side")))} '
                         f'{esc(ladder)}  {result}</b>\n' + table(rows))
        self.show(chat, message_id, '\n\n'.join(parts),
                  [[('Refresh', 'trades', ()), ('Main menu', 'main', ())]])

    def on_pnl(self, chat, message_id):
        """The P&L on one sheet: all-time, today, and what is open."""
        snapshot, _ = self._snapshot()
        report = self._report_of(0)
        trips = [t for t in report.get('journal') or ()
                 if t.get('pnl') is not None]
        now = self.clock()
        today = time.strftime('%Y-%m-%d', time.gmtime(now))
        todays = [t for t in trips if time.strftime(
            '%Y-%m-%d', time.gmtime(t.get('closed_at') or 0)) == today]
        wins = [t['pnl'] for t in trips if t['pnl'] > 0]
        losses = [t['pnl'] for t in trips if t['pnl'] <= 0]
        check = snapshot.get('pnl_check') or {}

        def total(field, among):
            values = [t.get(field) for t in among]
            if not values or any(v is None for v in values):
                return None              # unmeasured is not zero
            return sum(values)
        drawdown = (report.get('drawdown') or {}).get('max')
        rows = [('Closed Trades', len(trips)),
                ('Win Rate', '—' if not trips else
                 f'{len(wins) / len(trips) * 100:.1f}%  '
                 f'({len(wins)}W / {len(losses)}L net)'),
                ('Avg Win (net)', w3(sum(wins) / len(wins)) if wins else '—'),
                ('Avg Loss (net)', w3(sum(losses) / len(losses))
                 if losses else '—'),
                None,
                ('Today Gross', w3(total('gross_pnl', todays))
                 + f'  ({len(todays)} trades)'),
                ('Today Fees', w3fee(total('fees', todays))),
                ('Today Net', w3(sum(t['pnl'] for t in todays))),
                None,
                ('All-time Gross', w3(total('gross_pnl', trips))),
                ('All-time Fees', w3fee(total('fees', trips))),
                ('All-time Net', w3(sum(t['pnl'] for t in trips))
                 if trips else '—'),
                ('Unrealized', w3(check.get('ours'))),
                # What this desk adds.
                None,
                ('Best / Worst', '—' if not trips else
                 f'{w3(max(t["pnl"] for t in trips))}  /  '
                 f'{w3(min(t["pnl"] for t in trips))}'),
                ('Max Drawdown', w3(-drawdown) if drawdown is not None
                 else '—')]
        text = title('P&L SUMMARY', now) + '\n' + table(rows)
        if not report.get('ok'):
            text += '\n\nThe trade record cannot be read right now.'
        self.show(chat, message_id, text,
                  [[('Refresh', 'pnl', ()), ('Main menu', 'main', ())]])

    def register_commands(self):
        """Telegram's own command menu - the list that opens on "/" -
        with a line saying what each one does."""
        self.api.call('setMyCommands', commands=[
            {'command': name.lstrip('/'), 'description': what}
            for name, what in COMMANDS])

    def on_balance(self, chat, message_id):
        snapshot, _ = self._snapshot()
        parts = [title('ACCOUNT BALANCE', self.clock())]
        for name, info in sorted((snapshot.get('accounts') or {}).items()):
            if not info:
                parts.append(f'<b>{esc(name)}</b>\n'
                             + table([('Status', 'NOT ANSWERING')]))
                continue
            currency = info.get('currency') or ''

            def amount(field):
                value = info.get(field)
                return '—' if value is None else \
                    f'{float(value):,.2f} {currency}'.strip()
            level = info.get('margin_level')
            # The broker's OWN margin-call and stop-out levels, not ours.
            health = 'OK'
            if level:
                if info.get('margin_so_so') and level <= info['margin_so_so']:
                    health = 'STOP OUT'
                elif info.get('margin_so_call') and \
                        level <= info['margin_so_call']:
                    health = 'MARGIN CALL'
            account = ' · '.join(str(v) for v in (info.get('login'),
                                                  info.get('server')) if v)
            parts.append(f'<b>{esc(name)}</b>\n' + table([
                ('Account', f'{account}  (MT5)' if account else 'MT5'),
                ('Equity', amount('equity')),
                ('Available', amount('margin_free')),
                ('Used', amount('margin')),
                ('Margin', f'{float(level):,.1f}%  [{health}]' if level
                 else '—  (no margin in use)'),
                ('Unrealized', w3(info.get('profit'))),
                None,
                ('Balance', amount('balance'))]))
        if len(parts) == 1:
            parts.append('No accounts are reporting.')
        self.show(chat, message_id, '\n\n'.join(parts),
                  [[('Refresh', 'balance', ()), ('Main menu', 'main', ())]])

    # -- the Algo mode (in Settings) -------------------------------------------------

    def on_algo_pair(self, chat, message_id, key):
        _, pairs = self._snapshot()
        row = pairs.get(key)
        if row is None:
            return self.show(chat, message_id, f'No ladder {esc(key)}.',
                             [[('Main menu', 'main', ())]])
        current = mode_word(row)
        text = (title('SETTINGS  ·  ALGO MODE', self.clock()) + '\n'
                f'<b>{self._pair_name(pairs, key)}</b>\n'
                + table([('Current', MODE_WORDS[current]), None,
                         ('Off', 'nothing is watched or sent'),
                         ('Signals', 'the Algo signals, you trade; '
                                     'it sends nothing'),
                         ('LIVE', 'trades both accounts on this '
                                  'ladder; manual orders on it are off')]))

        def label(mode, word):
            return word + (' (current)' if current == mode else '')
        buttons = [[(label('OFF', 'Off'), 'algo_ask', (key, 'NONE')),
                    (label('DRY', 'Signals'), 'algo_ask', (key, 'ALGO')),
                    (label('LIVE', 'LIVE'), 'algo_ask', (key, 'ALGO_LIVE'))],
                   [('Back', 'settings_pair', (key,)),
                    ('Main menu', 'main', ())]]
        self.show(chat, message_id, text, buttons)

    def on_algo_list(self, chat, message_id):
        self.on_dashboard(chat, message_id)

    def on_algo_ask(self, chat, message_id, key, choice):
        _, pairs = self._snapshot()
        name = self._pair_name(pairs, key)
        words = {'NONE': 'Off', 'ALGO': 'Signals (the Algo signals, you '
                 'trade; it sends nothing)', 'ALGO_LIVE': 'LIVE'}[choice]
        if choice == 'ALGO_LIVE':
            text = (f'<b>Switch {name} to LIVE?</b>\n\nThe Algo will send '
                    f'REAL orders to both accounts on this ladder: MARKET in '
                    f'when the spread stretches past its band, out at the '
                    f'take-profit or the stop loss. Manual orders on this '
                    f'ladder are off while it runs.')
            return self.show(chat, message_id, text, [
                [('Continue', 'algo_live_again', (key,)),
                 ('Cancel', 'algo_pair', (key,))]])
        self.show(chat, message_id,
                  f'Set the Algo on <b>{name}</b> to <b>{words}</b>?',
                  [[('Confirm', 'algo_do', (key, choice, None)),
                    ('Cancel', 'algo_pair', (key,))]])

    def on_algo_live_again(self, chat, message_id, key):
        _, pairs = self._snapshot()
        self.show(chat, message_id,
                  f'<b>Second confirmation: LIVE on '
                  f'{self._pair_name(pairs, key)}.</b>\nReal orders, '
                  f'real money.',
                  [[('Confirm LIVE', 'algo_do', (key, 'ALGO_LIVE', None)),
                    ('Cancel', 'algo_pair', (key,))]])

    def on_algo_do(self, chat, message_id, key, choice, off_action):
        live = choice == 'ALGO_LIVE'
        payload = {'pair': key, 'algo': 'ALGO' if live else choice,
                   'mode': 'LIVE' if live else 'DRY_RUN'}
        if live:
            payload['confirmed'] = True
        if off_action:
            payload['off_action'] = off_action
        answer = self.desk.command('set_algo', payload)
        data = answer.get('data') if isinstance(answer.get('data'),
                                                dict) else {}
        if data.get('choose'):
            count = len(data.get('positions') or []) or 1
            return self.show(
                chat, message_id,
                f'The Algo holds {count} position(s) on this ladder. Close '
                f'them now at market, by ticket, or hand them to manual?',
                [[('Close now', 'algo_do', (key, choice, 'close')),
                  ('Hand to manual', 'algo_do', (key, choice, 'manual'))],
                 [('Cancel', 'algo_pair', (key,))]])
        self._report(chat, answer, 'Algo mode changed.')
        self.on_algo_pair(chat, None, key)

    def _report(self, chat, answer, done):
        if answer.get('ok'):
            self.send(chat, f'<b>Done.</b> {done}')
        elif answer.get('ok') is None:
            self.send(chat, f'<b>Pending.</b> {esc(answer.get("reason"))}')
        else:
            self.send(chat, f'<b>Refused.</b> {esc(answer.get("reason"))}')

    # -- closing ----------------------------------------------------------------------

    def on_flatten_ask(self, chat, message_id, key):
        _, pairs = self._snapshot()
        row = pairs.get(key) or {}
        self.show(chat, message_id,
                  f'<b>Close every position on '
                  f'{self._pair_name(pairs, key)}?</b>\n'
                  f'Net {num(row.get("net_position"), 2, True)} spreads, '
                  f'closed at market, by ticket. This cannot be undone.',
                  [[('Close all', 'flatten_do', (key,)),
                    ('Cancel', 'dashboard', ())]])

    def on_flatten_do(self, chat, message_id, key):
        answer = self.desk.command('flatten_pair', {'pair': key})
        data = answer.get('data') or {}
        failed = data.get('failed') if isinstance(data, dict) else None
        if answer.get('ok') and failed:
            answer = {'ok': False, 'reason': '; '.join(
                str(f.get('reason')) for f in failed)}
        self._report(chat, answer, f'Closed {data.get("closed", 0)} '
                                   f'position(s).' if isinstance(data, dict)
                     else 'Closed.')
        self.on_dashboard(chat, None)

    def on_kill_ask(self, chat, message_id):
        self.show(chat, message_id,
                  '<b>KILL ALL?</b>\nCancels every working order on every '
                  'ladder and closes every open position at market, by '
                  'ticket. This cannot be undone.',
                  [[('Confirm KILL ALL', 'kill_do', ()),
                    ('Cancel', 'main', ())]])

    def on_kill_do(self, chat, message_id):
        answer = self.desk.command('kill', {'flatten': True})
        self._report(chat, answer, 'Every order cancelled and every '
                                   'position closed.')
        self.on_main(chat, None)

    # -- settings: a ladder, then a section, then a setting ---------------------------

    def _ladders(self, pairs):
        return sorted(pairs.items())

    def on_settings_all(self, chat):
        """Every setting of every ladder, with the key `/set` takes - one
        message per ladder, the way Stat_Arb_W3 lists its own."""
        _, pairs = self._snapshot()
        ladders = self._ladders(pairs)
        if not ladders:
            return self.send(chat, 'No ladders are configured.')
        for number, (key, row) in enumerate(ladders, 1):
            saved = self.desk.config_pair(key)
            lines = [title(f'SETTINGS  ·  {number}. '
                           f'{row.get("name") or key}', self.clock()),
                     f'<b>Algo mode</b>  <code>'
                     f'{MODE_WORDS[mode_word(row)]}</code>  mode']
            for heading, fields in SECTIONS.values():
                lines.append('')
                lines.append(f'<b>{esc(heading.upper())}</b>')
                for field in fields:
                    value = self._shown(field, self._current(row, saved,
                                                              field))
                    unit = UNITS.get(field, '') \
                        if isinstance(value, (int, float)) else ''
                    # The unit follows the value, so the label drops its
                    # own "(min)", "(spreads)" and the like.
                    label = FIELD[field][1].split(' (')[0]
                    lines.append(f'<b>{esc(label)}</b>  '
                                 f'<code>{esc(value)}{esc(unit)}</code>  '
                                 f'{esc(field)}')
            self.send(chat, '\n'.join(lines),
                      [[(f'Change: {row.get("name") or key}',
                         'settings_pair', (key,))]])
        if len(ladders) == 1:
            how = ('To change: /set &lt;key&gt; &lt;value&gt;\n'
                   'Example: /set entry_z 2.5\n'
                   'Algo mode: /set mode off | dry | live')
        else:
            how = ('To change: /set &lt;ladder&gt; &lt;key&gt; &lt;value&gt;'
                   '\nExample: /set 1 entry_z 2.5\n'
                   'Algo mode: /set 1 mode off | dry | live\n'
                   + '\n'.join(f'Ladder {n}: {esc(r.get("name") or k)}'
                                for n, (k, r) in enumerate(ladders, 1)))
        self.send(chat, how + '\nEvery change asks you to confirm first.',
                  [[('Main menu', 'main', ())]])

    def _set_command(self, chat, words):
        """`/set [ladder] <key> <value>` - parsed, checked, and then put
        to the same Confirm / Cancel a button would."""
        _, pairs = self._snapshot()
        ladders = self._ladders(pairs)
        usage = ('Use: /set ' + ('' if len(ladders) == 1 else '<ladder> ')
                 + '&lt;key&gt; &lt;value&gt; - send /settings for the keys.')
        if len(words) == 2 and len(ladders) == 1:
            key = ladders[0][0]
            field, raw = words
        elif len(words) == 3:
            pick, field, raw = words
            key = None
            if pick.isdigit() and 1 <= int(pick) <= len(ladders):
                key = ladders[int(pick) - 1][0]
            else:
                for k, r in ladders:
                    if pick.lower() in (k.lower(),
                                        str(r.get('name') or '').lower()):
                        key = k
            if key is None:
                return self.send(chat, f'<b>Not changed.</b> No ladder '
                                       f'{esc(pick)}. {usage}')
        else:
            return self.send(chat, f'<b>Not changed.</b> {usage}')
        field = field.lower()
        if field == 'mode':
            choice = {'off': 'NONE', 'signals': 'ALGO', 'signal': 'ALGO',
                      'dry': 'ALGO', 'dryrun': 'ALGO', 'dry_run': 'ALGO',
                      'live': 'ALGO_LIVE'} \
                .get(raw.lower())
            if choice is None:
                return self.send(chat, '<b>Not changed.</b> mode is off, '
                                       'signals or live.')
            return self.on_algo_ask(chat, None, key, choice)
        if field not in FIELD:
            return self.send(chat, f'<b>Not changed.</b> No setting '
                                   f'{esc(field)}. {usage}')
        kind = FIELD[field][3]
        if kind == 'bool':
            value = {'on': True, 'true': True, 'yes': True, '1': True,
                     'off': False, 'false': False, 'no': False,
                     '0': False}.get(raw.lower())
            if value is None:
                return self.send(chat, f'<b>Not changed.</b> '
                                       f'{esc(field)} is on or off.')
            return self.on_set_ask(chat, None, key, field, value)
        if isinstance(kind, list):
            for code, word in kind:
                if raw.lower() in (code.lower(), word.lower()):
                    return self.on_set_ask(chat, None, key, field, code)
            return self.send(chat, f'<b>Not changed.</b> {esc(field)} is '
                                   + ' | '.join(code for code, _ in kind)
                                   + '.')
        return self._typed_value(chat, key, field, raw)

    def on_settings_list(self, chat, message_id):
        _, pairs = self._snapshot()
        buttons = [[(row.get('name') or key, 'settings_pair', (key,))]
                   for key, row in sorted(pairs.items())]
        buttons.append([('Main menu', 'main', ())])
        self.show(chat, message_id, title('SETTINGS', self.clock())
                  + '\nSelect a ladder.',
                  buttons)

    def on_settings_pair(self, chat, message_id, key):
        _, pairs = self._snapshot()
        row = pairs.get(key) or {}
        buttons = [[(f'Algo mode: {MODE_WORDS[mode_word(row)]}',
                     'algo_pair', (key,))]]
        for section, (heading, _) in SECTIONS.items():
            buttons.append([(heading, 'settings_section', (key, section))])
        buttons.append([('Back', 'settings_list', ()),
                        ('Main menu', 'main', ())])
        self.show(chat, message_id,
                  title('SETTINGS', self.clock()) + '\n'
                  f'<b>{self._pair_name(pairs, key)}</b>\n'
                  f'Select a section.', buttons)

    def on_settings_section(self, chat, message_id, key, section):
        _, pairs = self._snapshot()
        row = pairs.get(key) or {}
        saved = self.desk.config_pair(key)
        heading, fields = SECTIONS[section]
        rows, buttons = [], []
        for field in fields:
            label = FIELD[field][1]
            value = self._shown(field, self._current(row, saved, field))
            rows.append((SHORT_LABELS.get(field, label), value))
            buttons.append([(f'{label}: {value}', 'edit', (key, field))])
        buttons.append([('Back', 'settings_pair', (key,)),
                        ('Main menu', 'main', ())])
        self.show(chat, message_id,
                  title(f'SETTINGS  ·  {heading.upper()}', self.clock())
                  + f'\n<b>{self._pair_name(pairs, key)}</b>\n' + table(rows)
                  + '\nSelect a setting to change it.', buttons)

    def _back_to(self, key, field):
        return ('settings_section', (key, SECTION_OF[field]))

    def _current(self, row, saved, field):
        name, _, where, _ = FIELD[field]
        if where == 'algo':
            return effective_params(row).get(name)
        if name == 'overnight':
            return row.get('overnight') or saved.get(name)
        hours = row.get('session') or {}
        if name == 'session_open':
            return saved.get(name) or 'any'
        if name == 'session_close':
            return saved.get(name) or f'desk {hours.get("close") or "cutoff"}'
        if name == 'break':
            if saved.get('break_start') and saved.get('break_end'):
                return f'{saved["break_start"]}-{saved["break_end"]}'
            return 'none'
        value = saved.get(name)
        return 'desk default' if value is None else value

    @staticmethod
    def _shown(field, value):
        kind = FIELD[field][3]
        if (kind == 'side' or kind is SIDE_MODES) and \
                value in (None, 'SAME'):
            return 'same as ladder'
        if kind == 'bool':
            return 'ON' if value else 'OFF'
        if isinstance(kind, list):
            return dict(kind).get(value, value)
        if kind in ('time', 'range') and value is None:
            return 'none'
        return value

    def on_edit(self, chat, message_id, key, field):
        _, label, _, kind = FIELD[field]
        _, pairs = self._snapshot()
        row = pairs.get(key) or {}
        now = self._shown(field, self._current(row, self.desk.config_pair(
            key), field))
        back, args = self._back_to(key, field)
        if kind == 'bool':
            options = [(True, 'ON'), (False, 'OFF')]
        elif isinstance(kind, list):
            options = kind
        else:
            self._waiting[chat] = (self.clock() + INPUT_TTL_SEC, key, field)
            how = {'side': 'Send the new value - or "same" for the '
                            'ladder\'s own.',
                   'time': 'Send HH:MM, broker time - or "none" to clear.',
                   'range': 'Send HH:MM-HH:MM, broker time - or "none".'} \
                .get(kind, 'Send the new value as a message.')
            return self.show(chat, message_id,
                             f'<b>{esc(label)}</b>\nCurrent value: '
                             f'<b>{esc(now)}</b>\n\n{how}',
                             [[('Cancel', back, args)]])
        self.show(chat, message_id,
                  f'<b>{esc(label)}</b>\nCurrent value: <b>{esc(now)}</b>'
                  f'\n\nChange to:',
                  [[(word, 'set_ask', (key, field, value))
                    for value, word in options],
                   [('Cancel', back, args)]])

    def _typed_value(self, chat, key, field, text):
        from . import algo as algo_module
        from . import session as session_module
        _, label, where, kind = FIELD[field]
        if kind in ('time', 'range'):
            raw = text.strip()
            if raw.lower() in ('', 'none', 'clear', 'off', '-'):
                return self.on_set_ask(chat, None, key, field, None)
            try:
                if kind == 'time':
                    value = session_module.clean_hhmm(
                        raw) if session_module.parse_hhmm(raw) else None
                else:
                    start, end = [p.strip() for p in raw.split('-', 1)]
                    session_module.parse_hhmm(start)
                    session_module.parse_hhmm(end)
                    value = (f'{session_module.clean_hhmm(start)}-'
                             f'{session_module.clean_hhmm(end)}')
            except ValueError as e:
                return self.send(chat, f'<b>Not changed.</b> {esc(e)}.')
            return self.on_set_ask(chat, None, key, field, value)
        if kind == 'side' and text.strip().lower() in SAME_WORDS:
            return self.on_set_ask(chat, None, key, field, None)
        try:
            value = float(text.replace(',', '.'))
        except ValueError:
            return self.send(chat, f'<b>Not changed.</b> {esc(text)!s} is '
                                   f'not a number.')
        if field in algo_module._INTS:
            value = int(value)
        problems = (algo_module.check_params({field: value})
                    if where == 'algo'
                    else ([] if value >= 0 else
                          [f'{label} cannot be negative']))
        if problems:
            return self.send(chat, '<b>Not changed.</b> '
                             + esc('; '.join(problems)) + '.')
        self.on_set_ask(chat, None, key, field, value)

    def on_set_ask(self, chat, message_id, key, field, value):
        _, pairs = self._snapshot()
        label = FIELD[field][1]
        back, args = self._back_to(key, field)
        self.show(chat, message_id,
                  f'<b>{self._pair_name(pairs, key)}</b>\n'
                  f'Set {esc(label)} to <b>{esc(self._shown(field, value))}'
                  f'</b>?',
                  [[('Confirm', 'set_do', (key, field, value)),
                    ('Cancel', back, args)]])

    def on_set_do(self, chat, message_id, key, field, value):
        """Save it the way the settings pane does: written to the pair,
        then sent to the running engine so it applies now."""
        where = FIELD[field][2]
        if where == 'algo':
            typed = dict(self.desk.config_pair(key).get('algo_params') or {})
            # SAME is the ladder's own: saved as blank, never a value.
            typed[field] = None if value == 'SAME' else value
            payload = {'algo_params': typed}
        elif field == 'break':
            start, end = (value.split('-', 1) if value else (None, None))
            payload = {'break_start': start, 'break_end': end}
        else:
            payload = {field: value}
        saved = self.desk.save_pair(key, payload)
        if not saved.get('ok'):
            self.send(chat, f'<b>Not saved.</b> {esc(saved.get("error"))}')
            return self.on_settings_section(chat, None, key,
                                            SECTION_OF[field])
        answer = self.desk.command('set_pair', {'pair': key,
                                                'fields': payload})
        for note in saved.get('notes') or []:
            self.send(chat, f'<b>Note.</b> {esc(note)}')
        self._report(chat, answer, f'{FIELD[field][1]} is now '
                                   f'{self._shown(field, value)}.')
        self.on_settings_section(chat, None, key, SECTION_OF[field])

    # -- alerts -----------------------------------------------------------------------

    def _price_problem(self, row):
        # A feed that is silent because this ladder's market is shut -
        # its break, or outside the hours set for it - is not news.
        if (row.get('session') or {}).get('quiet'):
            return None
        market = row.get('market') or {}
        return (row.get('dark_reason') or market.get('stale_reason')
                or market.get('jump_reason') or market.get('guard_reason'))

    @staticmethod
    def _session_words(row):
        hours = row.get('session') or {}
        if not hours:
            return '—'
        state = (hours.get('state') or 'unknown').lower()
        span = f'{hours.get("open") or "any"} to {hours.get("close")}'
        if hours.get('break'):
            span += f', break {hours["break"]}'
        return f'{state}  ({span} broker time)'

    def alert_once(self):
        """Look once, and push what is new to every allowed user."""
        snapshot = self.desk.status() or {}
        events = self.desk.events(limit=50)
        messages = self.watch.check(snapshot, events, self._price_problem,
                                    getattr(self.desk, 'position', None))
        for text in messages:
            for user in sorted(self.allowed):
                if user in self.alerts_off:
                    continue
                try:
                    self.send(user, text)
                except TelegramError as e:
                    logging.warning('[telegram] alert to %s not sent: %s',
                                    user, e)
        return messages


class AlertWatch:
    """What has changed since the last look, as messages.

    The first look only takes its bearings: history already in the
    journal, positions already open and problems already on screen when
    the bot starts are not news.
    """

    def __init__(self, clock=time.time):
        self.clock = clock
        self.started = False
        self.last_event = 0
        self.engine = None
        self.dark = set()
        self.problems = {}          # pair -> (problem, since, told)
        self.errors = {}
        self.halts = {}
        self.live = {}              # pair -> is it LIVE
        self.signals = {}           # pair -> the last signal alert's id
        self.positions = {}         # id -> pair key
        self.summary_sent = {}      # pair -> the day its summary went

    def check(self, snapshot, events, price_problem, lookup=None):
        out = []
        now = self.clock()
        pairs = snapshot.get('pairs') or {}
        new_events = sorted((e for e in events or ()
                             if (e.get('id') or 0) > self.last_event),
                            key=lambda e: e.get('id') or 0)
        if new_events:
            self.last_event = new_events[-1].get('id') or self.last_event
        first = not self.started
        self.started = True

        def name_of(key):
            return (pairs.get(key) or {}).get('name') or key or ''

        # The engine itself.
        engine = snapshot.get('engine') or 'down'
        if not first and engine != self.engine:
            if engine == 'up':
                out.append(title('SYSTEM', now) + '\n'
                           + table([('Engine', 'Running again')]))
            else:
                out.append(title('SYSTEM ERROR', now) + '\n' + table([
                    ('Engine', engine.upper()),
                    ('Detail', snapshot.get('engine_note'))]))
        self.engine = engine
        dark = set(snapshot.get('dark_accounts') or ())
        if not first:
            for name in sorted(dark - self.dark):
                out.append(title('SYSTEM ERROR', now) + '\n' + table([
                    ('Account', name), ('Status', 'NOT ANSWERING')]))
            for name in sorted(self.dark - dark):
                out.append(title('SYSTEM', now) + '\n' + table([
                    ('Account', name), ('Status', 'answering again')]))
        self.dark = dark

        # Positions that opened, and positions that closed.
        current = {}
        for key, row in pairs.items():
            block = row.get('algo_block') or {}
            levels = {p.get('position_id'): p
                      for p in (block.get('positions') or [])}
            for position in row.get('positions') or []:
                pid = position.get('position_id')
                if not pid:
                    continue
                current[pid] = key
                if not first and pid not in self.positions:
                    out.append(trade_entry_text(position, name_of(key),
                                                levels.get(pid), block))
        if not first:
            for pid, key in self.positions.items():
                if pid in current:
                    continue
                record = {}
                if lookup is not None:
                    try:
                        record = lookup(pid) or {}
                    except Exception as e:
                        logging.warning('[telegram] position %s: %s', pid, e)
                if record.get('ok') and (record.get('position') or {}) \
                        .get('closed_at'):
                    out.append(trade_exit_text(record, name_of(key)))
                else:
                    out.append(title('TRADE EXIT', now) + '\n' + table([
                        ('ID', pid), ('Ladder', name_of(key)),
                        ('Detail', 'closed; the record is not readable '
                                   'yet - see the desk')]))
        self.positions = current

        for key, row in pairs.items():
            name = name_of(key)
            # A price problem, once it has lasted.
            problem = price_problem(row)
            was = self.problems.get(key)
            if problem:
                if was is None or was[0] != problem:
                    self.problems[key] = (problem, now, first)
                elif not was[2] and now - was[1] >= PROBLEM_SETTLE_SEC:
                    out.append(title('FEED WARNING', now) + '\n' + table([
                        ('Ladder', name), ('Problem', problem)]))
                    self.problems[key] = (problem, was[1], True)
            elif was is not None:
                if was[2] and not first:
                    out.append(title('FEED RESTORED', now) + '\n' + table([
                        ('Ladder', name), ('Prices', 'back to normal')]))
                del self.problems[key]
            # Engine errors on the pair.
            errors = set(row.get('errors') or ())
            if not first:
                for error in sorted(errors - self.errors.get(key, set())):
                    out.append(title('ERROR', now) + '\n' + table([
                        ('Ladder', name), ('Error', error)]))
            self.errors[key] = errors
            block = row.get('algo_block') or {}
            # A day's limit reached.
            halt = block.get('halt')
            if halt and halt != self.halts.get(key) and not first:
                out.append(title('DAILY LIMIT', now) + '\n' + table([
                    ('Ladder', name),
                    ('Entries', 'stopped for the rest of the day'),
                    ('Reason', halt),
                    ('Exits', 'still managed')]))
            self.halts[key] = halt
            # Into or out of LIVE - from the desk, the other Telegram
            # user, or a restart (LIVE is OFF after every restart).
            # Signals and off are not money, so not news.
            live = mode_word(row) == 'LIVE'
            if key in self.live and live != self.live[key] and not first:
                out.append(title('ALGO MODE', now) + '\n' + table([
                    ('Ladder', name),
                    ('Mode', 'LIVE - the Algo sends orders' if live else
                     MODE_WORDS[mode_word(row)] + ' - LIVE is off')]))
            self.live[key] = live
            # SIGNALS: the trade is the trader's to make - an entry the
            # Algo would take, or their position at its exit. Said once,
            # by its id; in LIVE the Algo trades it and TRADE ENTRY /
            # EXIT says so.
            alert = block.get('signal_alert') or {}
            if alert.get('id') and alert['id'] != self.signals.get(key) \
                    and mode_word(row) == 'DRY' and not first:
                out.append(self._signal(name, alert, now))
            if alert.get('id'):
                self.signals[key] = alert['id']

        for event in new_events if not first else ():
            text = self._event(event, pairs, now)
            if text:
                out.append(text)

        # Once a day per ladder, at THAT ladder's close on the broker's
        # clock - an oil future and a gold CFD do not end the same day.
        clock = snapshot.get('broker_clock') or {}
        broker_time = clock.get('broker_time')
        today = time.strftime('%Y-%m-%d', time.localtime(now))
        for key, row in sorted(pairs.items()):
            close = (row.get('session') or {}).get('close') \
                or clock.get('cutoff')
            if not broker_time or not close or broker_time[:5] < close \
                    or self.summary_sent.get(key) == today:
                continue
            if not first:
                out.append(self._summary(
                    dict(snapshot, pairs={key: row}), now))
            self.summary_sent[key] = today
        return out

    @staticmethod
    def _signal(name, alert, now):
        entry = alert.get('kind') == 'ENTRY'
        return (title('SIGNAL  ·  ' + ('ENTRY' if entry else
                                       'EXIT YOUR POSITION'), now)
                + '\n' + table([
                    ('Ladder', name),
                    ('Side', side_words(alert.get('side'))),
                    ('Do', alert.get('text')),
                    ('Spread', plain(alert.get('spread'), 4)),
                    ('Z-score', plain(alert.get('z'), 2, True)), None,
                    ('Sent', 'nothing - Signals mode, you trade')]))

    @staticmethod
    def _event(event, pairs, now):
        kind = event.get('kind')
        detail = event.get('detail') or {}
        key = event.get('pair_key')
        name = (pairs.get(key) or {}).get('name') or key or ''
        # A signal is not news: in LIVE the TRADE ENTRY / EXIT says it,
        # in Signals the SIGNAL alert says it. The desk keeps every one.
        if kind == 'algo_order':
            if detail.get('ok'):
                return None          # the TRADE ENTRY / EXIT says it
            return (title(f'ORDER FAILED  ·  '
                          f'{detail.get("action") or ""}', now)
                    + '\n' + table([
                        ('Ladder', name),
                        ('Side', side_words(detail.get('side'))),
                        ('Reason', detail.get('reason'))]))
        if kind == 'refused':
            return title('ORDER REFUSED', now) + '\n' + table([
                ('Ladder', name), ('Reason', detail.get('reason'))])
        if kind in ('reconcile', 'unclaimed_closed', 'recovery', 'adopted'):
            summary = ', '.join(f'{k}={v}' for k, v in list(detail.items())[:4])
            return title('RECONCILER', now) + '\n' + table([
                ('Event', kind), ('Ladder', name or '—'),
                ('Detail', summary)])
        return None

    @staticmethod
    def _summary(snapshot, now=None):
        """END OF DAY, as Stat_Arb_W3 sends it, one block per ladder."""
        at = time.strftime('%Y-%m-%d %H:%M UTC',
                           time.gmtime(time.time() if now is None else now))
        parts = [f'<b>END OF DAY  ·  {at}</b>']
        accounts = snapshot.get('accounts') or {}
        for key, row in sorted((snapshot.get('pairs') or {}).items()):
            block = row.get('algo_block') or {}
            day = block.get('day') or {}
            regime = (block.get('filters') or {}).get('regime') or {}
            equity = []
            for account in (row.get('leg_a_account'),
                            row.get('leg_b_account')):
                info = accounts.get(account) or {}
                if account and info.get('equity') is not None:
                    equity.append(f'{account} {float(info["equity"]):,.2f}')
            parts.append(f'<b>{esc(row.get("name") or key)}</b>\n' + table([
                ('Trades', f'{day.get("trades") or 0}  '
                           f'({day.get("wins") or 0} wins)'
                 if 'wins' in day else day.get('trades') or 0),
                ('PnL', w3(day.get('pnl'))),
                None,
                ('Equity', '  ·  '.join(equity) if equity else '—'),
                ('Unrealized', w3(row.get('open_pnl'))),
                None,
                ('Position', net_words(row)),
                ('Z-score', plain(block.get('z_mid'), 4, True)),
                ('Regime', (regime.get('state') or '—').lower()
                 .replace('_', ' ') if regime.get('state') else '—'),
                # What this desk adds.
                None,
                ('Algo', MODE_WORDS[mode_word(row)]),
                ('Losses in row', day.get('losses_row') or 0)]))
        return '\n\n'.join(parts)


def start(app, env=None, api=None):
    """Start the bot beside the web app, if `.env` asks for one.

    Returns the Bot, or None when there is no token. Two daemon threads:
    one long-polls Telegram for button presses and messages, one looks
    for alerts every few seconds. Neither can stop the web app.
    """
    env = os.environ if env is None else env
    token = (env.get('TELEGRAM_BOT_TOKEN') or '').strip()
    if not token:
        # Said, not silent: "I set it up and nothing happens" has to be
        # answerable from the console.
        logging.info('[telegram] no bot: TELEGRAM_BOT_TOKEN is not set in '
                     '.env (beside config.json)')
        return None
    users = allowed_users(env.get('TELEGRAM_ALLOWED_USERS'))
    if not users:
        logging.warning('[telegram] TELEGRAM_BOT_TOKEN is set but '
                        'TELEGRAM_ALLOWED_USERS is empty — the bot will '
                        'answer nobody')
    secret = secrets.token_urlsafe(24)
    app.config['BOT_SECRET'] = secret
    bot = Bot(api or TelegramAPI(token), WebDesk(app, secret), users)
    try:
        bot.register_commands()
    except TelegramError as e:
        logging.warning('[telegram] the command menu was not set: %s', e)

    def loop(work, pause, name):
        backoff = 5.0
        while True:
            try:
                work()
                backoff = 5.0
            except Exception as e:
                # Telegram down, the network out: wait longer each time,
                # up to a minute, and say so once per failure.
                logging.warning('[telegram] %s: %s — retrying in %.0fs',
                                name, e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            if pause:
                time.sleep(pause)

    threading.Thread(target=loop, args=(bot.poll_once, 0, 'updates'),
                     name='telegram-updates', daemon=True).start()

    def alerts():
        with bot.lock:
            bot.alert_once()

    threading.Thread(target=loop, args=(alerts, 5.0, 'alerts'),
                     name='telegram-alerts', daemon=True).start()
    logging.info('[telegram] bot started for %d allowed user(s)', len(users))
    return bot
