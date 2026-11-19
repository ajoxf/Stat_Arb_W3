"""Why did the Algo trade — or not? Read from the journal, per ladder.

    python tools/algo_report.py                 # the last 24 hours
    python tools/algo_report.py --hours 72
    python tools/algo_report.py --db path\\to\\okxtrader.db

Safe to run while the program is running: it only reads.

For each ladder it prints:

- when the Algo was switched (off / dry run / LIVE);
- every signal it acted on, and in LIVE what became of the order — done,
  or refused in the refusal's own words;
- every signal it HELD BACK, and why (the cutoff, the warm-up, a filter,
  an Algo qty the broker cannot trade...), with a count per reason;

and ends with the most common reason nothing was entered. Held-back
signals are journalled from this version on; older ones are not there.
"""

import argparse
import datetime
import os
import re
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from okxtrader.database import Store                          # noqa: E402

KINDS = ('algo_switch', 'algo_signal', 'algo_order', 'algo_blocked',
         'refused')
SIDES = {'SELL': 'H to L', 'BUY': 'L to H'}


def when(at):
    return datetime.datetime.fromtimestamp(at).strftime('%m-%d %H:%M:%S')


def z_text(z):
    return '' if z is None else f' z {float(z):+.2f}'


def reason_kind(reason):
    """A reason without its numbers, so 'warming up: 12 of 90' and '13 of
    90' are counted as one."""
    return re.sub(r'[-+]?\d[\d.,:]*', '#', reason or '?').strip()


def line(event):
    kind, d = event['kind'], event['detail'] or {}
    side = SIDES.get(d.get('side'), d.get('side') or '')
    if kind == 'algo_switch':
        return f"switched → {d.get('algo')} {d.get('mode') or ''}".rstrip()
    if kind == 'algo_signal':
        mode = 'LIVE' if d.get('mode') == 'LIVE' else 'DRY'
        if d.get('action') == 'ENTER':
            return (f"[{mode}] ENTER {side}{z_text(d.get('z'))} at "
                    f"{d.get('spread')}")
        return f"[{mode}] EXIT {side} — {d.get('reason') or ''}"
    if kind == 'algo_order':
        return (f"LIVE {d.get('action')} {side}: "
                + ('done' if d.get('ok') else
                   f"REFUSED — {d.get('reason')}"))
    if kind == 'algo_blocked':
        return f"held back {side}{z_text(d.get('z'))}: {d.get('reason')}"
    if kind == 'refused':
        return f"refused: {d.get('reason')}"
    return kind


def report(store, hours, out=sys.stdout, now=None):
    now = time.time() if now is None else now
    since = now - hours * 3600
    events = []
    for kind in KINDS:
        for event in store.events(kind, limit=5000):
            if event['at'] < since:
                continue
            if kind == 'refused' and not (event['detail'] or {}).get('algo'):
                continue                  # a manual click's, not the Algo's
            events.append(event)
    events.sort(key=lambda e: e['at'])
    by_pair = defaultdict(list)
    for event in events:
        by_pair[event.get('pair_key') or '?'].append(event)
    print(f'Algo report — the last {hours:g} hours', file=out)
    if not by_pair:
        print('\nNothing from the Algo in the journal for this period: it '
              'was off, or saw no stretch to act on.', file=out)
        return
    for key, rows in sorted(by_pair.items()):
        print(f'\n=== {key} ===', file=out)
        for event in rows:
            print(f"  {when(event['at'])}  {line(event)}", file=out)
        entered = sum(1 for e in rows if e['kind'] == 'algo_order'
                      and (e['detail'] or {}).get('action') == 'ENTER'
                      and (e['detail'] or {}).get('ok'))
        refused = [e for e in rows if e['kind'] == 'algo_order'
                   and not (e['detail'] or {}).get('ok')]
        held = Counter(reason_kind((e['detail'] or {}).get('reason'))
                       for e in rows if e['kind'] == 'algo_blocked')
        print(f'  -- LIVE entries done: {entered} · orders refused: '
              f'{len(refused)} · signals held back: {sum(held.values())}',
              file=out)
        for reason, count in held.most_common():
            print(f'     held back {count}x: {reason}', file=out)
        if refused:
            print(f"     last refusal: "
                  f"{(refused[-1]['detail'] or {}).get('reason')}", file=out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--db', default='okxtrader.db')
    parser.add_argument('--hours', type=float, default=24.0)
    args = parser.parse_args(argv)
    if not os.path.exists(args.db):
        sys.exit(f'no journal at {args.db} — run this from the program '
                 f'folder, or pass --db')
    report(Store(args.db), args.hours)


if __name__ == '__main__':
    main()
