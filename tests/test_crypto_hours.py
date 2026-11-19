"""Crypto trades 24/7: no desk-wide cutoff unless a ladder sets hours."""

import types
from datetime import datetime

from okxtrader.session import PairSession

LATE = datetime(2026, 11, 21, 23, 30)          # a Saturday, late


def _pair(**hours):
    return types.SimpleNamespace(**hours)


def test_24_7_has_no_cutoff_and_is_always_open():
    hours = PairSession(_pair(), {'MARKET_HOURS': '24/7'})
    assert hours.cutoff is None
    assert hours.state(LATE) == 'OPEN'
    assert hours.minutes_to_close(LATE) is None
    assert hours.entry_block(LATE) is None


def test_a_ladder_with_its_own_hours_keeps_them():
    hours = PairSession(_pair(session_close='22:00'), {'MARKET_HOURS': '24/7'})
    assert hours.cutoff == (22, 0) and hours.state(LATE) == 'CLOSED'


def test_session_mode_brings_back_mirrors_cutoff():
    hours = PairSession(_pair(), {'MARKET_HOURS': 'SESSION',
                                  'OVERNIGHT_CLOSE_HOUR': 16,
                                  'OVERNIGHT_CLOSE_MINUTE': 55})
    assert hours.cutoff == (16, 55) and hours.state(LATE) == 'CLOSED'
