"""Newline-delimited JSON over TCP — the wire between coordinator and
leg runners. Localhost only; no credentials ever cross this link."""

import json
import socket


class JsonLineSocket:
    def __init__(self, sock):
        self.sock = sock
        self.file = sock.makefile('rwb')

    def send(self, obj):
        self.file.write(json.dumps(obj).encode('utf-8') + b'\n')
        self.file.flush()

    def recv(self):
        line = self.file.readline()
        if not line:
            return None
        return json.loads(line.decode('utf-8'))

    def request(self, obj):
        self.send(obj)
        return self.recv()

    def close(self):
        try:
            self.file.close()
        finally:
            self.sock.close()


def connect(host, port, timeout=5.0):
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return JsonLineSocket(sock)


def parse_endpoint(endpoint):
    """'127.0.0.1:9101' -> ('127.0.0.1', 9101).

    Forgiving about how the operator typed it, because a wrong
    separator here takes the whole system down at startup:
    '127.0.0.1.9101' (dot instead of colon) and a bare '9101' are
    accepted, and anything genuinely unusable raises a message that
    says what to type instead of an int() traceback."""
    text = str(endpoint or '').strip().strip('"\'')
    if not text:
        raise ValueError(
            "Empty leg runner endpoint. Use host:port — e.g. "
            "127.0.0.1:9101 for the first account and 127.0.0.1:9102 "
            "for the second. Leave it blank ONLY when both legs share "
            "one account.")

    if text.isdigit():                       # just a port
        return '127.0.0.1', int(text)

    host, colon, port = text.rpartition(':')
    if not colon:
        # A dot where the colon should be: 127.0.0.1.9101 has five
        # dot-separated parts, one more than an IPv4 address.
        parts = text.split('.')
        if len(parts) == 5 and all(p.isdigit() for p in parts):
            host, port = '.'.join(parts[:4]), parts[-1]
        else:
            raise ValueError(
                f"Leg runner endpoint {endpoint!r} has no port. Use "
                f"host:port with a COLON — e.g. 127.0.0.1:9101.")

    host = host.strip() or '127.0.0.1'
    port = port.strip()
    if not port.isdigit():
        raise ValueError(
            f"Leg runner endpoint {endpoint!r} does not end in a port "
            f"number. Use host:port — e.g. 127.0.0.1:9101.")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError(
            f"Leg runner endpoint {endpoint!r}: {port} is not a valid "
            f"port. Use something in 1024-65535 — e.g. 127.0.0.1:9101.")
    return host, port
