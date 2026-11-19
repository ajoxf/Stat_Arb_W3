"""OKX's public WebSocket price feed: the ladders' prices and books, pushed.

Polling REST costs one round trip per instrument per pass (about 250 ms
each from a distant box). OKX's `tickers` channel instead pushes every
change of the best bid and ask as it happens, so the poll reads prices
from memory and makes no network call for them at all.

It is a FEED, never the only one. `row(symbol)` answers only while the
connection is healthy - something heard from OKX within
`STALE_SEC`, pongs included, so a quiet instrument on a live connection
still counts - and otherwise None, and the broker falls back to REST by
itself. The connection keeps itself alive with OKX's own text ping, and
comes back on its own with a growing back-off, re-subscribing to
everything it was watching.

Public data only: no key is ever sent on this connection.
"""

import json
import logging
import threading
import time

LIVE_URL = 'wss://ws.okx.com:8443/ws/v5/public'
DEMO_URL = 'wss://wspap.okx.com:8443/ws/v5/public'

#: OKX closes a connection that has heard nothing for 30 s, and answers
#: the text 'ping' with 'pong'.
PING_SEC = 20.0
#: Nothing heard for this long and the feed is not believed: REST.
STALE_SEC = 30.0
BACKOFF_START_SEC = 1.0
BACKOFF_MAX_SEC = 30.0


class PriceStream:
    """One public WebSocket to OKX, subscribed to `tickers` for every
    instrument the desk watches."""

    def __init__(self, url, clock=time.time, app_factory=None):
        self.url = url
        self.clock = clock
        self._app_factory = app_factory          # tests swap the socket
        self.rows = {}                           # instId -> ticker row
        self.books = {}                          # instId -> books5 row
        self.wanted = set()                      # instIds to subscribe
        self.subscribed = set()                  # instIds with tickers
        self.book_subscribed = set()             # instIds with books5
        self.connected = False
        self.last_heard = 0.0
        self.last_error = ''
        self.connects = 0
        self._app = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # -- what the broker asks -------------------------------------------------

    @property
    def healthy(self):
        return (self.connected
                and self.clock() - self.last_heard < STALE_SEC)

    def row(self, symbol):
        """The latest ticker row for `symbol`, or None when the feed is
        not to be believed (not connected, gone quiet, or not yet
        subscribed) - then the caller reads REST instead."""
        if not self.healthy or symbol not in self.subscribed:
            return None
        return self.rows.get(symbol)

    def book(self, symbol):
        """The latest five-level book for `symbol`, or None (-> REST)."""
        if not self.healthy or symbol not in self.book_subscribed:
            return None
        return self.books.get(symbol)

    def watch(self, symbols):
        """Subscribe to these too (idempotent)."""
        new = set(symbols) - self.wanted
        if not new:
            return
        with self._lock:
            self.wanted |= new
        if self.connected:
            self._subscribe(new)

    # -- life cycle -------------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='okx-price-stream')
        self._thread.start()
        threading.Thread(target=self._pinger, daemon=True,
                         name='okx-price-ping').start()

    def stop(self):
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()
            except Exception:
                pass

    def _make_app(self):
        if self._app_factory is not None:
            return self._app_factory(self)
        import websocket                      # websocket-client
        return websocket.WebSocketApp(
            self.url, on_open=lambda ws: self.on_open(),
            on_message=lambda ws, msg: self.on_message(msg),
            on_error=lambda ws, err: self.on_error(err),
            on_close=lambda ws, code, msg: self.on_close())

    def _run(self):
        backoff = BACKOFF_START_SEC
        while not self._stop.is_set():
            started = self.clock()
            try:
                self._app = self._make_app()
                self._app.run_forever()
            except Exception as exc:              # never kill the feed
                self.on_error(exc)
            self.on_close()
            if self._stop.is_set():
                break
            # A connection that lasted a while starts the back-off over.
            if self.clock() - started > 60:
                backoff = BACKOFF_START_SEC
            self._stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SEC)

    def _pinger(self):
        while not self._stop.wait(PING_SEC):
            if self.connected:
                self._send('ping')

    # -- the socket's events ----------------------------------------------------

    def on_open(self):
        self.connected = True
        self.connects += 1
        self.last_heard = self.clock()
        self.subscribed = set()
        self.book_subscribed = set()
        logging.info('OKX price feed connected (%s)', self.url)
        if self.wanted:
            self._subscribe(set(self.wanted))

    def on_close(self):
        if self.connected:
            logging.warning('OKX price feed closed - prices from REST until '
                            'it is back')
        self.connected = False
        self.subscribed = set()
        self.book_subscribed = set()

    def on_error(self, error):
        self.last_error = str(error)
        logging.debug('OKX price feed: %s', error)

    def on_message(self, message):
        self.last_heard = self.clock()
        if message == 'pong':
            return
        try:
            body = json.loads(message)
        except ValueError:
            return
        event = body.get('event')
        arg = body.get('arg') or {}
        if event == 'subscribe':
            if arg.get('instId'):
                (self.book_subscribed if arg.get('channel') == 'books5'
                 else self.subscribed).add(arg['instId'])
            return
        if event == 'error':
            self.last_error = f"OKX {body.get('code')}: {body.get('msg')}"
            logging.warning('OKX price feed refused a subscription: %s',
                            self.last_error)
            return
        channel = arg.get('channel')
        if channel not in ('tickers', 'books5'):
            return
        for row in body.get('data') or ():
            inst = row.get('instId') or arg.get('instId')
            if not inst:
                continue
            # A row arriving is as good as the confirmation.
            if channel == 'tickers':
                self.rows[inst] = row
                self.subscribed.add(inst)
            else:
                self.books[inst] = row
                self.book_subscribed.add(inst)

    def _subscribe(self, symbols):
        args = [{'channel': channel, 'instId': s} for s in sorted(symbols)
                for channel in ('tickers', 'books5')]
        for i in range(0, len(args), 20):
            self._send(json.dumps({'op': 'subscribe', 'args': args[i:i + 20]}))

    def _send(self, text):
        app = self._app
        if app is None:
            return
        try:
            app.send(text)
        except Exception as exc:
            self.on_error(exc)
