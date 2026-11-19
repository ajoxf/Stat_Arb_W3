"""OKX v5 REST client — signing, the demo header, and errors in OKX's words.

Synchronous on purpose: the engine is a threaded poll loop, and every
caller wants an answer before it moves on. Only `okx_broker.py` uses
this module.

A request is `(method, path, params, body)`. With a `transport`
injected (the simulated exchange, in tests and demos) the same request
is answered in-process and nothing is signed or sent — so the code
path the broker runs is the one it runs against the real exchange,
down to the response envelope.
"""

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

LIVE_URL = 'https://www.okx.com'


class OKXError(Exception):
    """A request OKX refused. `code`/`s_code` are OKX's own; the message
    is OKX's own words, never "check the log"."""

    def __init__(self, message, code=None, s_code=None, data=None):
        super().__init__(message)
        self.code = code
        self.s_code = s_code
        self.data = data


def _iso_now():
    return datetime.now(timezone.utc).isoformat(
        timespec='milliseconds').replace('+00:00', 'Z')


class OKXClient:
    """One API key on one OKX account (demo or live)."""

    def __init__(self, api_key='', secret='', passphrase='', demo=True,
                 base_url=LIVE_URL, transport=None, timeout=10.0):
        self.api_key = api_key or ''
        self.secret = secret or ''
        self.passphrase = passphrase or ''
        #: Demo trading is the same host with one header: every private
        #: AND public call must carry it, or a demo key reads as invalid.
        self.demo = bool(demo)
        self.base_url = base_url.rstrip('/')
        self.transport = transport
        self.timeout = timeout
        self._session = None
        self._lock = threading.Lock()
        #: OKX's own clock minus ours, in seconds — refreshed from
        #: /public/time. A signature older than 30 s is refused, so a
        #: drifting box clock reads as "Invalid OK-ACCESS-TIMESTAMP"
        #: rather than as anything about the order.
        self.clock_offset = 0.0

    @property
    def has_credentials(self):
        return bool(self.api_key and self.secret and self.passphrase)

    # -- signing ------------------------------------------------------------

    def _sign(self, timestamp, method, request_path, body):
        message = f'{timestamp}{method.upper()}{request_path}{body}'
        digest = hmac.new(self.secret.encode(), message.encode(),
                          hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, method, request_path, body, private):
        headers = {'Content-Type': 'application/json'}
        if self.demo:
            headers['x-simulated-trading'] = '1'
        if private:
            ts = datetime.fromtimestamp(time.time() + self.clock_offset,
                                        timezone.utc).isoformat(
                timespec='milliseconds').replace('+00:00', 'Z')
            headers.update({
                'OK-ACCESS-KEY': self.api_key,
                'OK-ACCESS-SIGN': self._sign(ts, method, request_path, body),
                'OK-ACCESS-TIMESTAMP': ts,
                'OK-ACCESS-PASSPHRASE': self.passphrase,
            })
        return headers

    # -- requests -------------------------------------------------------------

    def request(self, method, path, params=None, body=None, private=True):
        """The response's `data` list. Raises OKXError with OKX's words
        on any refusal — the envelope's code, or the first row's sCode
        for a per-order rejection."""
        method = method.upper()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if self.transport is not None:
            raw = self.transport(method, path, params, body)
        else:
            raw = self._http(method, path, params, body, private)
        return self._unwrap(raw)

    def get(self, path, params=None, private=False):
        return self.request('GET', path, params=params, private=private)

    def post(self, path, body, private=True):
        return self.request('POST', path, body=body, private=private)

    def _http(self, method, path, params, body, private):
        import requests                       # only needed against OKX itself
        if private and not self.has_credentials:
            raise OKXError('no OKX API key saved for this account — enter '
                           'the key, secret and passphrase on the '
                           'Exchanges page')
        query = ('?' + urlencode(params)) if params else ''
        request_path = path + query
        payload = json.dumps(body) if body is not None else ''
        headers = self._headers(method, request_path, payload, private)
        if self._session is None:
            with self._lock:
                if self._session is None:
                    self._session = requests.Session()
        try:
            response = self._session.request(
                method, self.base_url + request_path, headers=headers,
                data=payload or None, timeout=self.timeout)
        except requests.RequestException as exc:
            raise OKXError(f'OKX did not answer: {exc}') from exc
        try:
            return response.json()
        except ValueError:
            raise OKXError(f'OKX answered HTTP {response.status_code} with '
                           f'no JSON: {response.text[:200]}')

    @staticmethod
    def _unwrap(raw):
        if not isinstance(raw, dict):
            raise OKXError(f'unexpected OKX response: {raw!r}'[:300])
        code = str(raw.get('code', ''))
        data = raw.get('data') or []
        if code != '0':
            # A batch/order refusal carries the real reason per row.
            first = data[0] if data and isinstance(data[0], dict) else {}
            s_code = str(first.get('sCode') or '') or None
            s_msg = first.get('sMsg') or ''
            message = raw.get('msg') or ''
            text = (f'OKX {s_code}: {s_msg}' if s_code and s_msg
                    else f'OKX {code}: {message or s_msg or "refused"}')
            raise OKXError(text, code=code, s_code=s_code, data=data)
        return data

    def sync_clock(self):
        """Measure OKX's clock against ours; returns the offset in s."""
        before = time.time()
        rows = self.get('/api/v5/public/time')
        after = time.time()
        if rows:
            server = int(rows[0]['ts']) / 1000.0
            self.clock_offset = server - (before + after) / 2.0
        return self.clock_offset
