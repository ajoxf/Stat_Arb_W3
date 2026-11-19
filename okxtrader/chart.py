"""The pair chart's series: each ladder's spread and the Algo's z, as
CANDLES.

Observes only. Fed from the poll the signal journal already runs - the
Algo's OWN z, read off the same candles it trades on - so the chart is
the number the Algo decides on, not a second reckoning of it.
Arrow-statarb computed its chart z against the window's own mean; here
that would draw a z the Algo never saw.

Every poll goes into a 10-second candle - open, high, low, close of the
spread and of z - so a high or a low the poll saw is never thinned
away. The screen merges these into the candle size its range wants.

Written to a file of its own beside the status file, every couple of
seconds: the status file is read several times a second and carrying
two hours of candles in it would make every one of those reads heavy.
"""

import collections
import json
import os

from . import atomicfile

#: The finest candle kept. The screen's candles are whole multiples of
#: it (20 s, 30 s, 1 min, 2 min).
BUCKET_SEC = 10
#: How much is kept, and how much the screen can ask for.
KEEP_SEC = 2 * 60 * 60
#: How often the file is rewritten.
WRITE_EVERY_SEC = 2.0


def chart_path(status_path):
    """Beside the status file, so the web process finds it the same
    way."""
    return os.path.join(os.path.dirname(os.path.abspath(status_path)),
                        'chart.json')


def history_path(status_path):
    """The 15m / 1h / 4h candle histories, beside the status file:
    written only when one is fetched from MT5, read by the web process
    when the chart window asks."""
    return os.path.join(os.path.dirname(os.path.abspath(status_path)),
                        'chart_history.json')


def _round(value, places=6):
    return None if value is None else round(float(value), places)


def _ohlc(candle, prefix, value):
    """Fold one reading into a candle's open/high/low/close."""
    if value is None:
        return
    if candle.get(prefix + 'o') is None:
        candle[prefix + 'o'] = candle[prefix + 'h'] = \
            candle[prefix + 'l'] = value
    else:
        candle[prefix + 'h'] = max(candle[prefix + 'h'], value)
        candle[prefix + 'l'] = min(candle[prefix + 'l'], value)
    candle[prefix + 'c'] = value


class SpreadTape:
    def __init__(self, keep_sec=KEEP_SEC, bucket_sec=BUCKET_SEC):
        self.keep_sec = keep_sec
        self.bucket_sec = bucket_sec
        self._candles = {}           # key -> deque of candle dicts
        self._lines = {}             # key -> the Algo's levels now
        self._written = None

    def observe(self, key, now, md, body, params):
        """One poll's reading. `body` is what the Algo's own code made
        of it (`shadow.ShadowAlgo.step`); `params` its settings.

        A price the jump guard is holding back is not drawn: the candles
        the Algo trades on do not take it either, and a wick that was
        never a price is what makes a chart lie."""
        if not md or md.get('mid_spread') is None or md.get('jump_reason'):
            return
        body = body or {}
        params = params or {}
        self._lines[key] = {
            'entry_z': params.get('entry_z'),
            'stop_z': params.get('stop_z') if params.get('stop_z_on')
            else None,
            'mean': _round(body.get('mean')),
            'sigma': _round(body.get('sigma')),
            'ready': bool(body.get('ready')),
            'count': body.get('count'), 'needed': body.get('needed'),
        }
        start = float(now) - float(now) % self.bucket_sec
        candles = self._candles.setdefault(key, collections.deque())
        if not candles or candles[-1]['t'] != start:
            candles.append({'t': start})
        candle = candles[-1]
        _ohlc(candle, '', float(md['mid_spread']))
        z = body.get('z_mid')
        _ohlc(candle, 'z', None if z is None else float(z))
        while candles and now - candles[0]['t'] > self.keep_sec:
            candles.popleft()

    def forget(self, key):
        self._candles.pop(key, None)
        self._lines.pop(key, None)

    def series(self, key):
        candles = [{name: (_round(value, 6) if name != 't' else value)
                    for name, value in candle.items()}
                   for candle in self._candles.get(key) or ()]
        out = dict(self._lines.get(key) or {})
        out.update({
            'bucket_sec': self.bucket_sec,
            'candles': candles,
            'spread_min': min((c['l'] for c in candles), default=None),
            'spread_max': max((c['h'] for c in candles), default=None),
        })
        return out

    def snapshot(self, now, keys=None):
        keys = self._candles.keys() if keys is None else keys
        return {'at': now, 'keep_sec': self.keep_sec,
                'pairs': {key: self.series(key) for key in keys}}

    def write(self, path, now, keys=None, force=False):
        """Rewrite the file if it is due. A failure is the previous
        file left standing - the chart a couple of seconds old - never a
        stopped poll."""
        if not force and self._written is not None and \
                now - self._written < WRITE_EVERY_SEC:
            return False
        self._written = now
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.snapshot(now, keys), f)
        return atomicfile.replace(tmp, path)
