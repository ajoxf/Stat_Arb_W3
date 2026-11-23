"""The printable trading report: the Analysis tab's record as one
document - the logo, the headline returns, the equity curve, the split
by ladder and by who traded, and every closed trade - laid out for A4
landscape and the browser's Print / Save as PDF.

Built from `analysis.report`, the same numbers the tab shows. It works
out nothing of its own beyond sums of those rows.

THE LOGO: a file named `report_logo.png` (or .svg, .jpg, .jpeg, .webp)
beside config.json, embedded in the page; without one, the Nexus mark.
"""

import base64
import html
import os
import time

LOGO_NAMES = ('report_logo.svg', 'report_logo.png', 'report_logo.jpg',
              'report_logo.jpeg', 'report_logo.webp')
LOGO_TYPES = {'.svg': 'image/svg+xml', '.png': 'image/png',
              '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
              '.webp': 'image/webp'}

NEXUS_MARK = (
    '<svg class="mark" viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">'
    '<rect x="4" y="7.2" width="8" height="1.6" fill="#1a1a1a"/>'
    '<circle cx="4.6" cy="8" r="2.6" fill="#2f6fb0"/>'
    '<circle cx="11.4" cy="8" r="2.6" fill="#c0342f"/></svg>')

DASH = '—'
MASK = '•••••'

#: How the money a trade made is shown: in full, masked (the column is
#: there, the dollars are dots) or hidden (the columns are left out).
#: The percentages - on margin, on capital, the win rate - stay either
#: way: a report can be shared without saying how much was traded.
PNL_MODES = ('show', 'mask', 'hide')


def logo_html(folder):
    """The trader's own logo, embedded, or the Nexus mark."""
    for name in LOGO_NAMES:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            try:
                with open(path, 'rb') as f:
                    data = base64.b64encode(f.read()).decode('ascii')
            except OSError:
                continue
            kind = LOGO_TYPES[os.path.splitext(name)[1].lower()]
            return (f'<img class="logo" alt="logo" '
                    f'src="data:{kind};base64,{data}">')
    return NEXUS_MARK


def _e(text):
    return html.escape(str(text))


def money(x, sign=True):
    if x is None:
        return DASH
    x = round(x, 2) + 0.0
    s = f'${abs(x):,.2f}'
    if not sign:
        return s
    return ('+' if x > 0 else ('−' if x < 0 else '')) + s


def pct(x, places=2):
    if x is None:
        return DASH
    return ('+' if x > 0 else ('−' if x < 0 else '')) + \
        f'{abs(x):.{places}f}%'


def tone(x):
    return '' if not x else ('up' if x > 0 else 'down')


def spread(x):
    if x is None:
        return DASH
    text = f'{x:.4f}'.rstrip('0')
    whole, _, frac = text.partition('.')
    return whole + '.' + frac.ljust(2, '0')


def held(seconds):
    if seconds is None:
        return DASH
    s = max(0, int(round(seconds)))
    h, m = s // 3600, (s % 3600) // 60
    return f'{h}h {m}m' if h else (f'{m}m {s % 60}s' if m else f'{s}s')


def clock(at, with_date=True):
    if not at:
        return DASH
    t = time.localtime(at)
    return time.strftime('%d %b %H:%M' if with_date else '%H:%M', t)


def size(value):
    if value is None:
        return DASH
    return f'{value:,.2f}'.rstrip('0').rstrip('.')


def side(value):
    return {'BUY': 'Long', 'SELL': 'Short'}.get(value, value or DASH)


def period_text(days):
    return {0: 'Whole record', 1: 'Last day', 7: 'Last 7 days',
            30: 'Last 30 days'}.get(int(days) if days == int(days) else -1,
                                     f'Last {days:g} days')


def equity_svg(trips, width=1000, height=190, cash=None):
    """The running net P&L, trade by trade, oldest first."""
    cash = cash or money
    points = [t['cum_pnl'] for t in trips if t.get('cum_pnl') is not None]
    if len(points) < 1:
        return ''
    values = [0.0] + points
    lo, hi = min(values), max(values)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    pad = (hi - lo) * 0.1
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 64, 12, 10, 24
    w, h = width - left - right, height - top - bottom

    def x(i):
        return left + w * i / max(1, len(values) - 1)

    def y(v):
        return top + h * (hi - v) / (hi - lo)

    line = ' '.join(f'{x(i):.1f},{y(v):.1f}' for i, v in enumerate(values))
    area = (f'{x(0):.1f},{y(0):.1f} ' + line +
            f' {x(len(values) - 1):.1f},{y(0):.1f}')
    ticks = []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        ticks.append(f'<line x1="{left}" x2="{width - right}" '
                     f'y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                     f'<text x="{left - 6}" y="{y(v) + 3:.1f}" '
                     f'class="ax" text-anchor="end">'
                     f'{_e(cash(v))}</text>')
    labels = []
    step = max(1, len(points) // 12)
    for i in range(1, len(values)):
        if i % step == 0 or i == len(values) - 1:
            labels.append(f'<text x="{x(i):.1f}" y="{height - 6}" '
                          f'class="ax" text-anchor="middle">{i}</text>')
    dots = ''.join(
        f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.2" class="dot"/>'
        for i, v in enumerate(values) if i)
    return (f'<svg class="equity" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
            + ''.join(ticks) +
            f'<line x1="{left}" x2="{width - right}" y1="{y(0):.1f}" '
            f'y2="{y(0):.1f}" class="zero"/>'
            f'<polygon points="{area}" class="area"/>'
            f'<polyline points="{line}" class="line"/>' + dots +
            ''.join(labels) + '</svg>')


def _group(trips, key):
    out = {}
    for t in trips:
        if t.get('pnl') is None:
            continue
        row = out.setdefault(key(t), {'n': 0, 'wins': 0, 'net': 0.0,
                                      'fees': 0.0, 'held': 0.0})
        row['n'] += 1
        row['wins'] += 1 if t['pnl'] > 0 else 0
        row['net'] += t['pnl']
        row['fees'] += t.get('fees') or 0.0
        row['held'] += t.get('held_sec') or 0.0
    return out


def split_table(title, groups, cash=None, hide=False):
    if not groups:
        return ''
    cash = cash or money

    def money_cells(g):
        if hide:
            return ''
        return (f'<td class="{tone(g["net"])}">{cash(g["net"])}</td>'
                f'<td class="{tone(g["net"])}">{cash(g["net"] / g["n"])}</td>'
                f'<td>{cash(g["fees"], sign=False)}</td>')
    rows = ''.join(
        f'<tr><td class="l">{_e(name)}</td><td>{g["n"]}</td>'
        f'<td>{g["wins"]} / {g["n"] - g["wins"]}</td>'
        f'<td>{100.0 * g["wins"] / g["n"]:.1f}%</td>'
        + money_cells(g) +
        f'<td>{held(g["held"] / g["n"])}</td></tr>'
        for name, g in sorted(groups.items()))
    return (f'<table class="split"><thead><tr><th class="l">{_e(title)}</th>'
            '<th>Trades</th><th>Won / lost</th><th>Win rate</th>' +
            ('' if hide else '<th>Net P&amp;L</th><th>Per trade</th>'
                             '<th>Fees</th>') +
            '<th>Avg held</th></tr></thead><tbody>' + rows +
            '</tbody></table>')


def render(body, names=None, days=7.0, pair_key=None, now=None,
           logo='', desk_name='Nexus', pnl='show', link=None):
    """The whole report as one HTML page. `pnl`: one of PNL_MODES.
    `link(mode)`: this report's address in that mode, for the toolbar."""
    pnl = pnl if pnl in PNL_MODES else 'show'
    hide = pnl == 'hide'

    def cash(x, sign=True):
        return money(x, sign) if pnl == 'show' else MASK
    names = names or {}
    now = time.time() if now is None else now
    s = body.get('summary') or {}
    d = body.get('drawdown') or {}
    trips = list(reversed(body.get('journal') or []))      # oldest first
    priced = [t for t in trips if t.get('pnl') is not None]
    gross = sum(t['gross_pnl'] for t in priced
                if t.get('gross_pnl') is not None)
    fees = sum(t['fees'] for t in priced if t.get('fees') is not None)
    wins = [t['pnl'] for t in priced if t['pnl'] > 0]
    losses = [t['pnl'] for t in priced if t['pnl'] <= 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    factor = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 \
        else None
    span = ''
    if trips:
        first = min(t['opened_at'] or t['closed_at'] for t in trips)
        span = (time.strftime('%d %b %Y', time.localtime(first)) + ' – ' +
                time.strftime('%d %b %Y',
                              time.localtime(trips[-1]['closed_at'])))

    def name(key):
        return names.get(key) or key or DASH

    def tile(label, value, cls='', note=''):
        return (f'<div class="tile"><div class="tl">{label}</div>'
                f'<div class="tv {cls}">{value}</div>'
                f'<div class="tn">{note}</div></div>')

    capital = s.get('capital')
    tiles = ''.join([
        '' if hide else
        tile('Net P&amp;L', cash(s.get('net_pnl')), tone(s.get('net_pnl')),
             'after every fee'),
        tile('Return on capital', pct(s.get('return_on_capital')),
             tone(s.get('return_on_capital')),
             (f'on {money(capital, sign=False)} managed' if pnl == 'show'
              else 'on the capital managed') if capital
             else 'capital not set'),
        tile('Return on margin', pct(s.get('return_on_margin')),
             tone(s.get('return_on_margin')),
             f'{s.get("margin_trades") or 0} trades with margin priced'),
        tile('Win rate', '' if s.get('win_rate') is None
             else f'{s["win_rate"]:.1f}%', '',
             f'{s.get("wins") or 0} won · {s.get("losses") or 0} lost'),
        tile('Trades closed', str(s.get('closed') or 0), '',
             f'{s.get("open") or 0} still open'),
        tile('Max drawdown', DASH if d.get('max') is None
             else (pct(-d['max'] / capital * 100.0) if hide and capital
                   else cash(-d['max'])), 'down' if d.get('max') else '',
             'worst fall from a peak' + (', of capital' if hide and capital
                                         else '')),
        tile('Profit factor', f'{factor:.2f}' if factor else DASH, '',
             'won ÷ lost, in money') if hide else
        tile('Avg win / loss', f'{cash(avg_win)} / {cash(avg_loss)}', '',
             'profit factor ' + (f'{factor:.2f}' if factor else DASH)),
        '' if hide else
        tile('Gross → fees', f'{cash(gross)}', '',
             f'fees {cash(fees, sign=False)}'),
    ])

    rows = []
    for i, t in enumerate(trips, 1):
        rows.append(
            f'<tr><td>{i}</td><td>{clock(t.get("opened_at"))}</td>'
            f'<td>{clock(t.get("closed_at"))}</td>'
            f'<td class="l">{_e(name(t.get("pair_key")))}</td>'
            f'<td>{side(t.get("side"))}</td>'
            f'<td>{"Algo" if t.get("source") == "ALGO" else "Manual"}</td>'
            f'<td>{size(t.get("quantity"))}</td>'
            f'<td>{spread(t.get("entry_spread"))} → '
            f'{spread(t.get("exit_spread"))}</td>'
            f'<td>{held(t.get("held_sec"))}</td>'
            f'<td class="l">{_e(t.get("exit_reason") or DASH)}</td>'
            + ('' if hide else
               f'<td>{cash(t.get("gross_pnl"))}</td>'
               f'<td>{cash(t.get("fees"), sign=False)}</td>'
               f'<td class="{tone(t.get("pnl"))}"><b>{cash(t.get("pnl"))}</b>'
               '</td>') +
            f'<td class="{tone(t.get("pct_margin"))}">'
            f'{pct(t.get("pct_margin"))}</td>'
            f'<td class="{tone(t.get("pct_capital"))}">'
            f'{pct(t.get("pct_capital"), 3)}</td>'
            + ('' if hide else
               f'<td class="{tone(t.get("cum_pnl"))}">{cash(t.get("cum_pnl"))}'
               '</td>') +
            '</tr>')
    journal = ('<table class="journal"><thead><tr><th>#</th><th>Opened</th>'
               '<th>Closed</th><th class="l">Ladder</th><th>Side</th>'
               '<th>Who</th><th>Size</th><th>Spread in → out</th>'
               '<th>Held</th><th class="l">Exit</th>' +
               ('' if hide else '<th>Gross</th><th>Fees</th>'
                                '<th>Net P&amp;L</th>') +
               '<th>On margin</th><th>On capital</th>' +
               ('' if hide else '<th>Running</th>') +
               '</tr></thead><tbody>' +
               (''.join(rows) or '<tr><td colspan="16">No closed trade in '
                                 'this period.</td></tr>') +
               '</tbody></table>')

    if pnl == 'show':
        axis = money
    elif capital:
        def axis(v):
            return pct(v / capital * 100.0)
    else:
        def axis(v):
            return ''
    curve = equity_svg(trips, cash=axis)
    by_pair = split_table('Ladder', _group(
        trips, lambda t: name(t.get('pair_key'))), cash=cash, hide=hide)
    by_who = split_table('Who traded', _group(
        trips, lambda t: 'Algo' if t.get('source') == 'ALGO' else 'Manual'),
        cash=cash, hide=hide)
    extremes = '' if hide else (
        f'<div><span>Best trade</span><b class="up">'
        f'{cash(d.get("best_trade"))}</b></div>'
        f'<div><span>Worst trade</span><b class="down">'
        f'{cash(d.get("worst_trade"))}</b></div>')
    modes = ''.join(
        f'<a class="{"on" if m == pnl else ""}" href="{_e(link(m))}">'
        f'{m.title()}</a>' for m in PNL_MODES) if link else ''

    meta = [('Period', period_text(days) + (f' · {span}' if span
                                            else '')),
            ('Ladders', name(pair_key) if pair_key else 'Every ladder'),
            ('Capital managed', (money(capital, sign=False)
                                 if pnl == 'show' else 'not shown')
             if capital else 'not set'),
            ('Prepared', time.strftime('%d %b %Y, %H:%M',
                                       time.localtime(now)))]
    meta_html = ''.join(f'<div><span>{k}</span>{_e(v)}</div>'
                        for k, v in meta)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(desk_name)} — Trading Report</title>
<style>{CSS}</style></head>
<body>
<div class="toolbar"><button onclick="window.print()">Print / Save as PDF</button>
{f'<span class="modes">Net P&amp;L: {modes}</span>' if modes else ''}
<span>A4 landscape. In the print dialog, turn on “Background graphics”
for the colours.</span></div>
<main>
<header class="head">
  <div class="brand">{logo}<div><div class="desk">{_e(desk_name)}</div>
  <h1>Trading Report</h1><div class="sub">ETH vs BTC spread desk · OKX</div></div></div>
  <div class="meta">{meta_html}</div>
</header>
<section class="tiles">{tiles}</section>
<section class="two">
  <div class="panel"><h2>Equity curve <small>running net {'' if pnl == 'show'
  else ('as a share of capital ' if capital else '')}after each closed
  trade</small></h2>{curve or '<p class="none">No closed trade yet.</p>'}</div>
  <div class="panel"><h2>Breakdown</h2>{by_pair}{by_who}
  <div class="kv">{extremes}
  <div><span>Capital used / trade</span><b>{'' if s.get('capital_used_avg') is None else f"{s['capital_used_avg']:.1f}% avg · {s.get('capital_used_max') or 0:.1f}% max"}</b></div></div></div>
</section>
<section class="panel jr"><h2>Trade journal <small>{len(trips)} closed
trade(s), oldest first</small></h2>{journal}</section>
<footer class="notes"><b>Net P&amp;L</b> is after every fee. <b>On margin</b>:
net ÷ the margin both legs tied up at entry. <b>On capital</b>: net ÷
the capital managed. <b>Max drawdown</b>: the worst fall of the running
net from a peak, closed trades only. Funding is not included. Built from
the desk’s own record of fills; past results do not promise future ones.
</footer>
</main></body></html>"""


CSS = """
@page { size: A4 landscape; margin: 9mm 9mm 11mm;
  @bottom-left { content: "Nexus \\2014  Trading Report"; font: 8px Segoe UI, Arial, sans-serif; color: #6b7280; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font: 8px Segoe UI, Arial, sans-serif; color: #6b7280; } }
:root { --ink:#1f2933; --mute:#6b7280; --line:#d5dae1; --band:#f3f5f8;
  --navy:#16324f; --up:#127a3a; --down:#b42318; }
* { box-sizing: border-box; }
body { margin: 0; background: #e9ecf0; color: var(--ink);
  font: 12px/1.35 "Segoe UI", Arial, sans-serif;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.toolbar { position: sticky; top: 0; z-index: 2; display: flex; gap: 12px;
  align-items: center; padding: 8px 16px; background: var(--navy); color: #dbe4ee; }
.toolbar .modes a { color: #dbe4ee; margin: 0 2px; padding: 3px 9px;
  border: 1px solid #5b7590; border-radius: 3px; text-decoration: none; }
.toolbar .modes a.on { background: #fff; color: var(--navy); border-color: #fff; }
.toolbar button { font: 600 12px inherit; padding: 6px 14px; border: 0;
  border-radius: 4px; background: #fff; color: var(--navy); cursor: pointer; }
main { max-width: 1120px; margin: 16px auto; background: #fff; padding: 22px 26px;
  box-shadow: 0 1px 4px rgba(0,0,0,.12); }
.head { display: flex; justify-content: space-between; align-items: flex-end;
  border-bottom: 3px solid var(--navy); padding-bottom: 10px; gap: 20px; }
.brand { display: flex; align-items: center; gap: 14px; }
.mark, .logo { width: 56px; height: 56px; object-fit: contain; }
.logo { width: auto; max-width: 180px; }
.desk { font-weight: 700; letter-spacing: .18em; text-transform: uppercase;
  font-size: 11px; color: var(--mute); }
h1 { margin: 0; font-size: 24px; color: var(--navy); letter-spacing: -.01em; }
.sub { color: var(--mute); font-size: 11px; }
.meta { display: grid; grid-template-columns: auto auto; gap: 2px 18px;
  font-size: 11px; text-align: right; }
.meta span { color: var(--mute); margin-right: 6px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 8px; margin: 14px 0; }
.tile { border: 1px solid var(--line); border-top: 3px solid var(--navy);
  border-radius: 3px; padding: 7px 8px; background: var(--band); }
.tl { font-size: 9.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--mute); }
.tv { font-size: 16px; font-weight: 700; margin: 2px 0; white-space: nowrap; }
.tn { font-size: 9.5px; color: var(--mute); }
.two { display: grid; grid-template-columns: 1.35fr 1fr; gap: 12px; }
.panel { border: 1px solid var(--line); border-radius: 3px; padding: 8px 10px; }
h2 { margin: 0 0 6px; font-size: 12.5px; color: var(--navy); text-transform: uppercase;
  letter-spacing: .05em; }
h2 small { text-transform: none; letter-spacing: 0; color: var(--mute); font-weight: 400; }
.equity { width: 100%; height: 190px; display: block; }
.equity .grid { stroke: #e6e9ee; } .equity .zero { stroke: #8a94a3; stroke-dasharray: 3 3; }
.equity .area { fill: rgba(22,50,79,.08); }
.equity .line { fill: none; stroke: var(--navy); stroke-width: 2; vector-effect: non-scaling-stroke; }
.equity .dot { fill: var(--navy); } .equity .ax { font-size: 10px; fill: var(--mute); }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { border: 1px solid var(--line); padding: 3px 5px; text-align: center; white-space: nowrap; }
th { background: var(--navy); color: #fff; font-weight: 600; font-size: 10.5px; }
td.l, th.l { text-align: left; }
.split { margin-bottom: 8px; font-size: 11px; }
.journal { font-size: 10.5px; }
.journal tbody tr:nth-child(even) td { background: var(--band); }
.journal thead { display: table-header-group; }
.journal tr { break-inside: avoid; }
.jr { margin-top: 12px; }
.kv { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; font-size: 11px; }
.kv span { display: block; color: var(--mute); font-size: 9.5px; text-transform: uppercase; }
.up { color: var(--up); } .down { color: var(--down); }
.none { color: var(--mute); }
.notes { margin-top: 10px; font-size: 9.5px; color: var(--mute); border-top: 1px solid var(--line);
  padding-top: 6px; }
@media print {
  body { background: #fff; } .toolbar { display: none; }
  main { margin: 0; padding: 0; box-shadow: none; max-width: none; }
  .two { break-inside: avoid; }
}
"""
