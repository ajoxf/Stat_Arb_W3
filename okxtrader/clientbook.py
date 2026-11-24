"""THE CLIENT'S BOOK: what a managed account's owner is shown.

Money in and out (deposits, withdrawals - from OKX's own funding ledger
for the sub-account), the balance OKX holds now, the P&L of the copies,
month by month, and the PROFIT SHARE: the fund manager's agreed
percentage of each month's profit - the manager's payout. A month that
lost earns no share, and nothing is carried from one month to the next.

Pure arithmetic on recorded rows: nothing here reads OKX or a file, so
the desk's view of a client and the client's own dashboard are the same
numbers by construction.

P&L is the copies' CLOSED P&L, after fees. A copy still open is in the
OKX balance, not in the months - said on the page.
"""

import calendar
import time


def month_key(at):
    t = time.gmtime(at)
    return f'{t.tm_year:04d}-{t.tm_mon:02d}'


def month_label(key):
    year, month = key.split('-')
    return f'{calendar.month_abbr[int(month)]} {year}'


def _months(first, last):
    """Every month key from `first` to `last`, inclusive."""
    y, m = map(int, first.split('-'))
    out = []
    while True:
        key = f'{y:04d}-{m:02d}'
        out.append(key)
        if key >= last:
            return out
        m += 1
        if m > 12:
            y, m = y + 1, 1


def book(flows=(), closed=(), share_pct=0.0, balance=None, now=None):
    """The client's whole book.

    `flows`: [{'at', 'amount' (+ in, - out), 'kind', 'note', 'source'}].
    `closed`: the copies, [{'closed_at', 'pnl'}].
    `share_pct`: the manager's profit share, e.g. 20 for 20 %.
    `balance`: {'equity', 'at'} as OKX last said, or None.

    Returns {'summary', 'months' (oldest first), 'flows' (newest
    first), 'share_pct'}. Each month: opening (net money in plus the P&L
    before it), deposits, withdrawals, P&L, return %, the manager's profit
    share (the payout - share_pct of the month's profit, nothing on a
    month that lost), the client's net, and whether the payout is due (a
    finished month) or still accruing (this month).
    """
    now = time.time() if now is None else now
    share = max(0.0, float(share_pct or 0.0)) / 100.0
    flows = sorted((f for f in flows or () if f.get('at') is not None),
                   key=lambda f: f['at'])
    closed = sorted((c for c in closed or ()
                     if c.get('closed_at') is not None
                     and c.get('pnl') is not None),
                    key=lambda c: c['closed_at'])
    stamps = [f['at'] for f in flows] + [c['closed_at'] for c in closed]
    this_month = month_key(now)
    months = []
    if stamps:
        keys = _months(month_key(min(stamps)), max(this_month,
                                                   month_key(max(stamps))))
        invested = cum_pnl = 0.0
        for key in keys:
            dep = sum(f['amount'] for f in flows
                      if month_key(f['at']) == key and f['amount'] > 0)
            wd = -sum(f['amount'] for f in flows
                      if month_key(f['at']) == key and f['amount'] < 0)
            pnl = sum(c['pnl'] for c in closed
                      if month_key(c['closed_at']) == key)
            opening = invested + cum_pnl
            # What was at work this month: what was there at its start
            # and what came in during it.
            base = opening + dep
            cum_pnl += pnl
            invested += dep - wd
            # THE PAYOUT: the manager's share of THIS month's profit; a
            # month that lost pays none.
            fee = round(max(0.0, pnl) * share, 2)
            months.append({
                'month': key, 'label': month_label(key),
                'opening': round(opening, 2), 'deposits': round(dep, 2),
                'withdrawals': round(wd, 2), 'pnl': round(pnl, 2),
                'return_pct': (round(100.0 * pnl / base, 3)
                               if base > 0 else None),
                'share': fee, 'client_net': round(pnl - fee, 2),
                'closing': round(invested + cum_pnl, 2),
                'status': ('accruing' if key == this_month else
                           'due' if fee > 0 else '-')})
    deposited = sum(f['amount'] for f in flows if f['amount'] > 0)
    withdrawn = -sum(f['amount'] for f in flows if f['amount'] < 0)
    total_pnl = sum(c['pnl'] for c in closed)
    shares = sum(m['share'] for m in months)
    due = sum(m['share'] for m in months if m['status'] == 'due')
    accruing = sum(m['share'] for m in months if m['status'] == 'accruing')
    net_in = deposited - withdrawn
    current = months[-1] if months and months[-1]['month'] == this_month \
        else None
    summary = {
        'deposited': round(deposited, 2), 'withdrawn': round(withdrawn, 2),
        'net_invested': round(net_in, 2),
        'balance': (round(float(balance['equity']), 2)
                    if balance and balance.get('equity') is not None
                    else None),
        'balance_at': (balance or {}).get('at'),
        'available': (balance or {}).get('available'),
        'margin_used': (balance or {}).get('margin_used'),
        'unrealized': (balance or {}).get('unrealized'),
        # What the book says the account holds, from its own rows - beside
        # OKX's figure, the difference is open copies and anything the
        # rows do not know about.
        'book_value': round(net_in + total_pnl, 2),
        'pnl': round(total_pnl, 2),
        'return_pct': (round(100.0 * total_pnl / deposited, 3)
                       if deposited > 0 else None),
        'pnl_month': current['pnl'] if current else 0.0,
        'return_month_pct': current['return_pct'] if current else None,
        'share_pct': round(share * 100.0, 4),
        'share_total': round(shares, 2), 'share_due': round(due, 2),
        'share_accruing': round(accruing, 2),
        'client_net_pnl': round(total_pnl - shares, 2)}
    return {'summary': summary, 'months': months,
            'flows': [dict(f) for f in reversed(flows)],
            'share_pct': summary['share_pct']}
