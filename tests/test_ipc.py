"""The wire between the coordinator and the leg runners.

A typo in an endpoint takes the whole system down at startup, so it is
parsed forgivingly and refused with a message that says what to type —
never an int() traceback.
"""

import threading

import pytest

from okxtrader import ipc
from okxtrader.leg_runner import LegServer


@pytest.mark.parametrize('typed,expected', [
    ('127.0.0.1:9101', ('127.0.0.1', 9101)),
    ('  127.0.0.1:9101  ', ('127.0.0.1', 9101)),
    ('"127.0.0.1:9101"', ('127.0.0.1', 9101)),
    ('127.0.0.1.9101', ('127.0.0.1', 9101)),     # dot for the colon
    ('9101', ('127.0.0.1', 9101)),               # a bare port
    (':9101', ('127.0.0.1', 9101)),
    ('localhost:9102', ('localhost', 9102)),
])
def test_an_endpoint_is_read_the_way_it_was_typed(typed, expected):
    assert ipc.parse_endpoint(typed) == expected


@pytest.mark.parametrize('typed,says', [
    ('', 'host:port'),
    ('127.0.0.1', 'COLON'),
    ('127.0.0.1:abc', 'port number'),
    ('127.0.0.1:99999', 'not a valid'),
])
def test_an_unusable_endpoint_says_what_to_type_instead(typed, says):
    with pytest.raises(ValueError, match=says):
        ipc.parse_endpoint(typed)


def test_the_ui_and_the_coordinator_can_both_attach_at_once(legs):
    """Symbol setup must work with the coordinator DOWN, and while it is
    UP. With a single-client accept loop the UI timed out whenever the
    coordinator was connected."""
    server = LegServer(legs['acct_a'].broker, '127.0.0.1', 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first = ipc.connect('127.0.0.1', server.port)
        second = ipc.connect('127.0.0.1', server.port)
        assert first.request({'cmd': 'ping'})['ok']
        assert second.request({'cmd': 'tick', 'symbol': 'XAUUSD_'})['ok']
        # And the first connection still works after the second attached.
        assert first.request({'cmd': 'ping'})['ok']
        first.close()
        second.close()
    finally:
        server.stop()


def test_an_unknown_command_is_an_error_not_a_crash(legs):
    server = LegServer(legs['acct_a'].broker, '127.0.0.1', 0)
    try:
        reply = server.handle({'cmd': 'nonsense'})
        assert reply == {'ok': False, 'error': 'Unknown command: nonsense'}
    finally:
        server.stop()


def test_the_runner_serves_the_commands_the_coordinator_needs(legs):
    server = LegServer(legs['acct_a'].broker, '127.0.0.1', 0)
    try:
        assert server.handle({'cmd': 'tick', 'symbol': 'XAUUSD_'})['ok']
        assert server.handle({'cmd': 'symbol_report',
                              'symbol': 'XAUUSD_'})['report']['found']
        assert server.handle({'cmd': 'positions'})['positions'] == []
        assert server.handle({'cmd': 'find_symbols', 'pattern': 'XAU'})[
            'symbols'][0]['symbol'] == 'XAUUSD_'
    finally:
        server.stop()
