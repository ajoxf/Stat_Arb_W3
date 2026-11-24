"""THE CLIENT'S DASHBOARD: one managed account, as its owner sees it.

The balance and margin OKX holds, the money in and out, the P&L month by
month, the profit share and what is due, and nothing about what was
traded - no ladder, instrument, side, size or trade.

`gather` reads what is recorded (config.json, the mirror's record, the
trader's own entries); `render` draws it. The desk's preview and the
client's own login use both, so the two never disagree.
"""

import html
import json
import os
import time

from . import clientbook
from . import config as cfg

DASH = '—'


def _e(text):
    return html.escape(str(text))


def money(x, sign=False):
    if x is None:
        return DASH
    x = round(float(x), 2) + 0.0
    s = f'${abs(x):,.2f}'
    if sign:
        return ('+' if x > 0 else '−' if x < 0 else '') + s
    return ('−' if x < 0 else '') + s


def pct(x):
    if x is None:
        return DASH
    return ('+' if x > 0 else '−' if x < 0 else '') + f'{abs(x):.2f}%'


def tone(x):
    return '' if not x else ('up' if x > 0 else 'down')


def day(at):
    return time.strftime('%d %b %Y', time.gmtime(at)) if at else DASH


def follower_row(raw, name):
    return next((r for r in ((raw or {}).get('mirror') or {}).get(
        'followers') or () if str(r.get('name')) == name), None)


def read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def gather(name, config_path, state_path, now=None):
    """Everything the dashboard shows for one client, or None when there
    is no such managed account."""
    now = time.time() if now is None else now
    raw = cfg.load_raw(config_path)
    row = follower_row(raw, name)
    if row is None:
        return None
    state = read_json(state_path, {})
    cash = (state.get('cash') or {}).get(name) or {}
    flows = [dict(f, id=bill) for bill, f in (cash.get('flows') or {}).items()]
    # Money in and out: OKX's own funding ledger for this account -
    # nothing typed in, nothing assumed.
    closed = (state.get('closed') or {}).get(name) or []
    try:
        share = float(row.get('profit_share_pct') or 0.0)
    except (TypeError, ValueError):
        share = 0.0
    result = clientbook.book(flows, closed, share, cash.get('balance'), now)
    result.update(name=name, read_error=cash.get('error'),
                  read_at=cash.get('read_at'), now=now)
    return result


CSS = """
:root{--ink:#1f2933;--mute:#6b7280;--line:#dfe3e8;--band:#f4f6f9;--navy:#16324f;
--up:#127a3a;--down:#b42318;--gold:#b7791f}
*{box-sizing:border-box}body{margin:0;background:#eef1f5;color:var(--ink);
font:14px/1.45 "Segoe UI",system-ui,Arial,sans-serif}
.top{background:var(--navy);color:#fff;padding:14px 28px;display:flex;align-items:center;gap:14px}
.top .mark{width:34px;height:34px}.top h1{margin:0;font-size:19px;font-weight:600}
.top .who{margin-left:auto;text-align:right;font-size:13px;color:#cfd9e6}
.top .who b{color:#fff;font-size:15px}.top a{color:#cfd9e6;margin-left:14px}
.preview{background:#fff4d6;color:#7a5a00;text-align:center;padding:6px;font-size:12px}
main{max-width:1180px;margin:22px auto;padding:0 20px}
h2{font-size:15px;color:var(--navy);margin:26px 0 10px;text-transform:uppercase;letter-spacing:.06em}
h2 small{text-transform:none;letter-spacing:0;color:var(--mute);font-weight:400;font-size:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
.tile .v{font-size:24px;font-weight:650;margin:4px 0 2px;white-space:nowrap}
.tile .n{font-size:12px;color:var(--mute)}
.tile.hero{background:var(--navy);border-color:var(--navy);color:#fff}
.tile.hero .k,.tile.hero .n{color:#cfd9e6}
.up{color:var(--up)}.down{color:var(--down)}
.panel{background:#fff;border:1px solid var(--line);border-radius:8px;padding:6px 0;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);font-weight:600;
text-align:right;padding:9px 14px;border-bottom:1px solid var(--line);white-space:nowrap}
td{text-align:right;padding:9px 14px;border-bottom:1px solid #eef0f3;white-space:nowrap}
th.l,td.l{text-align:left}tr:last-child td{border-bottom:0}
tfoot td{font-weight:650;border-top:2px solid var(--line);background:var(--band)}
.pill{display:inline-block;padding:1px 9px;border-radius:10px;font-size:11px;font-weight:600}
.pill.due{background:#fff1db;color:#92400e}.pill.acc{background:#e6f0fb;color:#1e4f8a}
.pill.none{background:#eef1f4;color:var(--mute)}
.chart{padding:14px 16px 4px}.chart svg{width:100%;height:190px;display:block}
.notes{font-size:12px;color:var(--mute);margin:26px 0 40px;line-height:1.6}
.warn{background:#fff7f6;border:1px solid #f0b4ad;color:#8a1c12;padding:8px 12px;border-radius:6px;
margin-top:12px;font-size:12px}
@media print{body{background:#fff}.top a{display:none}}
@media(max-width:640px){.top{padding:12px 14px}.top h1{font-size:15px}.top .mark{width:26px;height:26px}
.top .who{font-size:11px}.top .who b{font-size:13px}main{padding:0 12px;margin-top:14px}
.tiles{grid-template-columns:1fr 1fr;gap:8px}.tile{padding:10px 11px}.tile .v{font-size:18px}
.tile.hero{grid-column:span 2}h2{font-size:13px;margin-top:20px}th,td{padding:7px 9px}}
"""

MARK = ('<svg class="mark" viewBox="0 0 16 16"><rect x="4" y="7.2" width="8" '
        'height="1.6" fill="#fff"/><circle cx="4.6" cy="8" r="2.6" '
        'fill="#5b9bd5"/><circle cx="11.4" cy="8" r="2.6" fill="#e06a63"/>'
        '</svg>')


def bars(months, width=1100, height=190):
    """Monthly P&L as bars, green up and red down, about a zero line."""
    if not months:
        return ''
    import math
    values = [m['pnl'] for m in months]
    # ROUND numbers on the axis - 1, 2, 2.5 or 5 x a power of ten.
    raw_top, raw_bottom = max([0.0] + values), min([0.0] + values)
    rough = ((raw_top - raw_bottom) or 1.0) / 4
    mag = 10 ** math.floor(math.log10(rough))
    step = next(k * mag for k in (1, 2, 2.5, 5, 10) if k * mag >= rough)
    top = math.ceil(raw_top / step) * step
    bottom = math.floor(raw_bottom / step) * step
    span = (top - bottom) or 1.0
    left, right, pad_t, pad_b = 70, 10, 12, 26
    w = (width - left - right) / len(values)
    y0 = pad_t + (top / span) * (height - pad_t - pad_b)
    out = []
    for k in range(int(round(span / step)) + 1):
        v = bottom + step * k
        y = pad_t + (top - v) / span * (height - pad_t - pad_b)
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" '
                   f'y2="{y:.1f}" stroke="#eef0f3"/><text x="{left - 8}" '
                   f'y="{y + 4:.1f}" font-size="11" fill="#6b7280" '
                   f'text-anchor="end">{_e(money(v)).replace(".00", "")}'
                   f'</text>')
    for i, (m, v) in enumerate(zip(months, values)):
        h = abs(v) / span * (height - pad_t - pad_b)
        x = left + i * w + w * 0.2
        y = y0 - h if v >= 0 else y0
        colour = '#2e8b57' if v >= 0 else '#c0392b'
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w * 0.6:.1f}" '
                   f'height="{max(h, 1):.1f}" rx="2" fill="{colour}">'
                   f'<title>{_e(m["label"])}: {_e(money(v, True))}</title>'
                   f'</rect><text x="{x + w * 0.3:.1f}" y="{height - 8}" '
                   f'font-size="11" fill="#6b7280" text-anchor="middle">'
                   f'{_e(m["label"])}</text>')
    out.append(f'<line x1="{left}" x2="{width - right}" y1="{y0:.1f}" '
               f'y2="{y0:.1f}" stroke="#9aa3af"/>')
    return (f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
            + ''.join(out) + '</svg>')


def render(data, preview=False, logout=True, desk_name='Nexus'):
    s = data['summary']
    months = data['months']
    share_pct = data.get('share_pct') or 0.0

    def tile(label, value, note='', cls='', extra=''):
        return (f'<div class="tile {extra}"><div class="k">{label}</div>'
                f'<div class="v {cls}">{value}</div>'
                f'<div class="n">{note}</div></div>')
    as_of = (time.strftime('%d %b %Y, %H:%M UTC', time.gmtime(
        s['balance_at'])) if s.get('balance_at') else 'not read yet')
    account = ''.join([
        tile('Current balance', money(s['balance']),
             f'held at OKX · as of {as_of}', extra='hero'),
        tile('Available margin', money(s.get('available')),
             'free to trade with'),
        tile('Margin in use', money(s.get('margin_used')),
             'tied up by open positions'),
        tile('Open P&amp;L', money(s.get('unrealized'), True),
             'positions still open', tone(s.get('unrealized')))])
    perf = ''.join([
        tile('Net deposited', money(s['net_invested']),
             f'{money(s["deposited"])} in · {money(s["withdrawn"])} '
             f'out'),
        tile('Total P&amp;L', money(s['pnl'], True), 'after trading fees',
             tone(s['pnl'])),
        tile('Return since start', pct(s['return_pct']), 'P&amp;L on '
             'money deposited', tone(s['return_pct'])),
        tile('This month', money(s['pnl_month'], True),
             pct(s['return_month_pct']), tone(s['pnl_month'])),
        tile(f'Manager payout · {share_pct:g}%', money(s['share_total']),
             f'{money(s["share_due"])} due · '
             f'{money(s["share_accruing"])} accruing this month'),
        tile('Your net P&amp;L', money(s['client_net_pnl'], True),
             'after the manager\u2019s payout', tone(s['client_net_pnl']))])
    # MONTHLY RETURNS.
    rows = ''.join(
        f'<tr><td class="l">{_e(m["label"])}</td><td>{money(m["opening"])}'
        f'</td><td>{money(m["deposits"]) if m["deposits"] else DASH}</td>'
        f'<td>{money(m["withdrawals"]) if m["withdrawals"] else DASH}</td>'
        f'<td class="{tone(m["pnl"])}">{money(m["pnl"], True)}</td>'
        f'<td class="{tone(m["return_pct"])}">{pct(m["return_pct"])}</td>'
        f'<td>{money(m["closing"])}</td></tr>'
        for m in reversed(months)) or (
        '<tr><td class="l" colspan="7">Nothing recorded yet.</td></tr>')
    monthly = (
        '<div class="panel"><table><thead><tr><th class="l">Month</th>'
        '<th>Opening</th><th>Deposits</th><th>Withdrawals</th><th>P&amp;L'
        '</th><th>Return</th><th>Closing</th></tr></thead><tbody>' + rows +
        f'</tbody><tfoot><tr><td class="l">Since start</td><td></td>'
        f'<td>{money(s["deposited"])}</td><td>{money(s["withdrawn"])}</td>'
        f'<td class="{tone(s["pnl"])}">{money(s["pnl"], True)}</td>'
        f'<td class="{tone(s["return_pct"])}">{pct(s["return_pct"])}</td>'
        f'<td>{money(s["book_value"])}</td></tr></tfoot></table></div>')
    chart = (f'<div class="panel chart">{bars(months)}</div>'
             if months else '')

    # PROFIT SHARE AND PAYOUT.
    def status(m):
        if m['status'] == 'due':
            return '<span class="pill due">due</span>'
        if m['status'] == 'accruing':
            return '<span class="pill acc">accruing</span>'
        return '<span class="pill none">none</span>'
    share_rows = ''.join(
        f'<tr><td class="l">{_e(m["label"])}</td><td class="{tone(m["pnl"])}">'
        f'{money(m["pnl"], True)}</td><td>{share_pct:g}%</td>'
        f'<td>{money(m["share"])}</td><td class="{tone(m["client_net"])}">'
        f'{money(m["client_net"], True)}</td><td>{status(m)}</td></tr>'
        for m in reversed(months)) or (
        '<tr><td class="l" colspan="6">Nothing recorded yet.</td></tr>')
    shares = (
        '<div class="panel"><table><thead><tr><th class="l">Month</th>'
        '<th>P&amp;L</th><th>Share</th><th>Payout to the fund manager</th>'
        '<th>Your net</th><th>Payout</th>'
        '</tr></thead><tbody>' + share_rows +
        f'</tbody><tfoot><tr><td class="l">Total</td>'
        f'<td class="{tone(s["pnl"])}">{money(s["pnl"], True)}</td>'
        f'<td></td>'
        f'<td>{money(s["share_total"])}</td>'
        f'<td class="{tone(s["client_net_pnl"])}">'
        f'{money(s["client_net_pnl"], True)}</td><td></td></tr></tfoot>'
        '</table></div>')
    # MONEY IN AND OUT.
    sources = {'OKX': 'OKX'}

    def flow_row(f):
        kind = f.get('kind') or ('deposit' if f['amount'] > 0
                                 else 'withdrawal')
        source = sources.get(f.get('source'), f.get('source') or '')
        if f.get('note'):
            source += ' \u00b7 ' + str(f['note'])
        return (f'<tr><td class="l">{day(f.get("at"))}</td><td class="l">'
                f'{_e(kind[:1].upper() + kind[1:])}</td>'
                f'<td class="{tone(f["amount"])}">{money(f["amount"], True)}'
                f'</td><td class="l">{_e(source)}</td></tr>')
    flow_rows = ''.join(flow_row(f) for f in data['flows']) or (
        '<tr><td class="l" colspan="4">No deposit or withdrawal recorded '
        'yet.</td></tr>')
    flows = ('<div class="panel"><table><thead><tr><th class="l">Date</th>'
             '<th class="l">Type</th><th>Amount</th><th class="l">Source</th>'
             '</tr></thead><tbody>' + flow_rows + '</tbody></table></div>')
    warn = (f'<div class="warn">The balance could not be read from OKX just '
            f'now ({_e(data["read_error"])}); the last good figures are '
            f'shown.</div>' if data.get('read_error') else '')
    now = data.get('now') or time.time()
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(desk_name)} · {_e(data['name'])}</title><style>{CSS}</style>
</head><body>
<div class="top">{MARK}<h1>{_e(desk_name)} · Client dashboard</h1>
<div class="who"><b>{_e(data['name'])}</b><br>{time.strftime(
        '%d %b %Y, %H:%M UTC', time.gmtime(now))}{
        '<a href="/logout">Log out</a>' if logout else ''}</div></div>
{'<div class="preview">Manager preview - this is what the client sees '
 'when they log in.</div>' if preview else ''}
<main>
<h2>Your account <small>- as OKX holds it</small></h2>
<div class="tiles">{account}</div>{warn}
<h2>Performance</h2><div class="tiles">{perf}</div>
<h2>Monthly returns</h2>{chart}{monthly}
<h2>Profit share &amp; payout <small>- {share_pct:g}% of each month's profit,
paid to your fund manager</small></h2>{shares}
<h2>Deposits &amp; withdrawals</h2>{flows}
<div class="notes"><b>P&amp;L</b> is from trades that have closed, after
trading fees; positions still open are in the balance and shown as Open
P&amp;L. <b>Return</b> for a month is its P&amp;L on what was in the
account at its start plus what came in during it. <b>Profit share</b> is
the fund manager's payout: {share_pct:g}% of each month's profit; a month
that loses pays none. A month's share is <b>due</b> once the month has ended and
<b>accruing</b> while it runs. Deposits and withdrawals are read from
your OKX account's own funding history. Past results do not promise
future ones.</div>
</main></body></html>"""


LOGIN_CSS = """
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;
justify-content:center;background:#16324f;font:14px "Segoe UI",system-ui,Arial,sans-serif}
form{background:#fff;padding:30px 32px;border-radius:10px;width:340px;box-shadow:0 8px 30px rgba(0,0,0,.25)}
h1{margin:0 0 4px;font-size:20px;color:#16324f}p{margin:0 0 18px;color:#6b7280;font-size:13px}
label{display:block;font-size:12px;color:#374151;margin:12px 0 4px}
input{width:100%;padding:9px 10px;border:1px solid #c9cfd8;border-radius:6px;font-size:14px}
button{margin-top:20px;width:100%;padding:10px;border:0;border-radius:6px;background:#16324f;
color:#fff;font-size:14px;font-weight:600;cursor:pointer}
.err{background:#fdecea;color:#b42318;padding:8px 10px;border-radius:6px;font-size:13px;margin-top:12px}
"""


def login_page(error=None, desk_name='Nexus'):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(desk_name)} · Client login</title><style>{LOGIN_CSS}</style>
</head><body><form method="post" action="/login" autocomplete="on">
<h1>{_e(desk_name)}</h1><p>Client dashboard - sign in to see your account.</p>
<label for="u">Username</label><input id="u" name="username" required
autocomplete="username"><label for="p">Password</label><input id="p"
name="password" type="password" required autocomplete="current-password">
{f'<div class="err">{_e(error)}</div>' if error else ''}
<button type="submit">Sign in</button></form></body></html>"""
