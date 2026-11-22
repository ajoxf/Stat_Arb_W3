"""BTC.D - Bitcoin's share of the whole crypto market's capitalisation.

Shown on the Algo panel and in Telegram's status as CONTEXT for the
ETH vs BTC spread: money moving into Bitcoin (dominance rising) is the
backdrop against which ETH tends to lag BTC, and the reverse. It is a
reading only - nothing is filtered, sized or sent from it.

OKX does not publish it, so it comes from CoinGecko's free public API
(no key): `/global` gives the share now and the whole market's 24 h
change in capitalisation; `/coins/markets?ids=bitcoin` gives Bitcoin's
own. The share 24 h ago follows from the two:

    D_then = D_now x (1 + total change) / (1 + bitcoin change)

CoinGecko counts every coin it lists, so its figure sits a little apart
from TradingView's BTC.D (which counts the top 125); the MOVES agree.

Read on a thread of its own, every few minutes: the free API allows a
few dozen calls a minute, and a slow answer must never hold up a poll.
A failed read keeps the last good one and says why.
"""

import logging
import threading
import time

GLOBAL_URL = 'https://api.coingecko.com/api/v3/global'
BITCOIN_URL = ('https://api.coingecko.com/api/v3/coins/markets'
               '?vs_currency=usd&ids=bitcoin')
SOURCE = 'CoinGecko'


def _http_get(url, timeout=10.0):
    import requests
    response = requests.get(url, timeout=timeout,
                            headers={'Accept': 'application/json'})
    response.raise_for_status()
    return response.json()


def parse(global_body, bitcoin_body):
    """The reading from CoinGecko's two answers: dominance now (%), its
    change over 24 h in percentage POINTS (None when either 24 h change
    is missing), and CoinGecko's own time of it."""
    data = (global_body or {}).get('data') or {}
    share = (data.get('market_cap_percentage') or {}).get('btc')
    if share is None:
        raise ValueError('CoinGecko gave no Bitcoin share')
    share = float(share)
    change = None
    total = data.get('market_cap_change_percentage_24h_usd')
    rows = bitcoin_body if isinstance(bitcoin_body, list) else []
    own = rows[0].get('market_cap_change_percentage_24h') if rows else None
    if total is not None and own is not None and float(own) > -100.0:
        then = share * (1.0 + float(total) / 100.0) / (1.0 + float(own) / 100.0)
        change = share - then
    return {'pct': round(share, 3),
            'change_24h': None if change is None else round(change, 3),
            'at': data.get('updated_at')}


class Dominance:
    """The latest BTC.D reading, refreshed in the background."""

    def __init__(self, every_sec=300.0, fetch=None, clock=time.time,
                 background=True):
        self.every_sec = float(every_sec or 0.0)
        #: None: this module's `_http_get`, looked up at each read (so a
        #: test can stand in for the network).
        self.fetch = fetch
        self.clock = clock
        self.background = background
        self._lock = threading.Lock()
        self._busy = False
        self._last_try = None
        self._reading = None
        self._error = None

    def due(self, now=None):
        if self.every_sec <= 0 or self._busy:
            return False
        now = self.clock() if now is None else now
        return self._last_try is None or now - self._last_try >= self.every_sec

    def refresh_if_due(self):
        """Start a read when one is due. Never waits for it."""
        if not self.due():
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._last_try = self.clock()
        if self.background:
            threading.Thread(target=self._read, daemon=True,
                             name='btc-dominance').start()
        else:
            self._read()
        return True

    def _read(self):
        try:
            fetch = self.fetch or _http_get
            reading = parse(fetch(GLOBAL_URL), fetch(BITCOIN_URL))
            reading['fetched_at'] = self.clock()
            with self._lock:
                self._reading, self._error = reading, None
        except Exception as e:                # a reading, never a fault
            with self._lock:
                self._error = f'{SOURCE} did not answer: {e}'[:200]
            logging.info('BTC.D: %s', self._error)
        finally:
            with self._lock:
                self._busy = False

    def snapshot(self):
        """For the status file: the last good reading (or none), its age,
        and the error of the last try when it failed."""
        if self.every_sec <= 0:
            return None
        with self._lock:
            reading = dict(self._reading) if self._reading else {}
            error = self._error
        if reading.get('fetched_at') is not None:
            reading['age_sec'] = round(self.clock() - reading['fetched_at'])
        reading['source'] = SOURCE
        reading['error'] = error
        return reading
