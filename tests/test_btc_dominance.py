"""BTC.D on the Algo panel and in Telegram: Bitcoin's share of the crypto
market, from CoinGecko, as context. A reading only - nothing is decided
from it, and a failed read never stops a poll."""

import pytest

from okxtrader import dominance
from okxtrader.coordinator import Coordinator

GLOBAL = {'data': {'market_cap_percentage': {'btc': 57.0, 'eth': 9.1},
                   'market_cap_change_percentage_24h_usd': -2.0,
                   'updated_at': 1795300000}}
BITCOIN = [{'id': 'bitcoin', 'market_cap_change_percentage_24h': 1.0}]


def answers(url, timeout=10.0):
    return GLOBAL if url == dominance.GLOBAL_URL else BITCOIN


def test_the_share_now_and_its_24h_move_in_points():
    reading = dominance.parse(GLOBAL, BITCOIN)
    assert reading['pct'] == pytest.approx(57.0)
    # 24 h ago: 57 x 0.98 / 1.01 = 55.307..., so up about 1.69 points -
    # Bitcoin grew while the market shrank.
    assert reading['change_24h'] == pytest.approx(57.0 - 57.0 * 0.98 / 1.01,
                                                  abs=1e-3)
    assert reading['at'] == 1795300000


def test_no_24h_figure_is_no_change_not_zero():
    reading = dominance.parse(GLOBAL, [])
    assert reading['pct'] == pytest.approx(57.0)
    assert reading['change_24h'] is None


def test_read_when_due_and_not_before():
    now = [1000.0]
    calls = []

    def fetch(url, timeout=10.0):
        calls.append(url)
        return answers(url)
    d = dominance.Dominance(every_sec=300, fetch=fetch,
                            clock=lambda: now[0], background=False)
    assert d.refresh_if_due() is True
    assert len(calls) == 2
    snap = d.snapshot()
    assert snap['pct'] == pytest.approx(57.0) and snap['error'] is None
    assert snap['source'] == 'CoinGecko'
    now[0] += 299
    assert d.refresh_if_due() is False
    now[0] += 2
    assert d.refresh_if_due() is True and len(calls) == 4


def test_a_failed_read_keeps_the_last_good_one_and_says_why():
    now = [1000.0]
    state = {'up': True}

    def fetch(url, timeout=10.0):
        if not state['up']:
            raise ConnectionError('timed out')
        return answers(url)
    d = dominance.Dominance(every_sec=60, fetch=fetch,
                            clock=lambda: now[0], background=False)
    d.refresh_if_due()
    state['up'] = False
    now[0] += 120
    d.refresh_if_due()
    snap = d.snapshot()
    assert snap['pct'] == pytest.approx(57.0)          # the last good one
    assert snap['age_sec'] == 120
    assert 'timed out' in snap['error']


def test_off_is_nothing_at_all():
    d = dominance.Dominance(every_sec=0, fetch=answers, background=False)
    assert d.refresh_if_due() is False
    assert d.snapshot() is None


def test_the_desk_publishes_it_and_a_dead_source_never_stops_a_poll(
        config, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.dominance = dominance.Dominance(
        every_sec=300, fetch=answers, clock=coordinator.clock,
        background=False)
    coordinator.start()
    coordinator.poll_once()
    snap = coordinator.snapshot()['btc_dominance']
    assert snap['pct'] == pytest.approx(57.0)
    # The real reader, with no network (as in every test): the poll and
    # the snapshot carry on; the reading says why it has nothing.
    coordinator.dominance = dominance.Dominance(
        every_sec=300, clock=coordinator.clock, background=False)
    coordinator.poll_once()
    snap = coordinator.snapshot()['btc_dominance']
    assert snap.get('pct') is None and 'CoinGecko' in snap['error']


def test_telegram_says_it_in_one_line():
    from okxtrader.telegram import btc_dominance_text
    assert btc_dominance_text({'pct': 57.123, 'change_24h': 0.314}) == \
        '57.12% (24h +0.31)'
    assert btc_dominance_text({'pct': 57.0, 'change_24h': -1.2,
                               'error': 'x'}) == '57.00% (24h -1.20) (old)'
    assert btc_dominance_text(None) is None
    assert btc_dominance_text({'pct': None, 'error': 'down'}) is None
