"""One line per click, in the log file, always.

A successful placement used to write NOTHING. The ladder showed it, so
nobody added a line — and then "which cell did he click, and was it
LIMIT or MARKET?" could only be answered by opening the database and
doing arithmetic on the recorded slippage. The trader who was told his
order had filled where he never clicked waited a day for that answer.

The line carries what was ASKED FOR — pair, side, mode, size, level —
and what came of it, on every path including a refusal. A refusal is
exactly the case where nothing else records the intent at all.
"""

import logging

import pytest

from okxtrader.coordinator import Coordinator
from okxtrader.models import SpreadSide


@pytest.fixture
def engine(config, pair, legs):
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    coordinator.start()
    coordinator.poll_once()
    return coordinator


def clicks(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith('CLICK ')]


def test_a_limit_click_is_logged_with_its_level_and_its_mode(engine, pair,
                                                             legs, caplog):
    coordinator = engine
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] - 0.05, 10)

    with caplog.at_level(logging.INFO):
        coordinator.click(pair.key, SpreadSide.SELL, level)

    said = clicks(caplog)
    assert len(said) == 1
    line = said[0]
    assert pair.key in line
    assert 'SELL' in line
    assert 'LIMIT' in line and 'MARKET' not in line
    assert f'{level:g}' in line, 'the clicked level is not in the line'
    assert 'taken' in line


def test_a_market_click_says_MARKET_in_the_same_place(engine, pair, legs,
                                                      caplog):
    """The control that matters for the question this was built to
    answer: the two modes must be told apart from the line alone."""
    coordinator = engine
    pair.order_type = pair.order_type.__class__('MARKET')
    md = coordinator.market[pair.key]

    with caplog.at_level(logging.INFO):
        coordinator.click(pair.key, SpreadSide.BUY, md['long_spread'])

    line = clicks(caplog)[0]
    assert 'MARKET' in line and 'LIMIT' not in line
    assert 'BUY' in line


def test_a_REFUSED_click_is_logged_too_with_the_reason(engine, pair, legs,
                                                       caplog):
    """The path where nothing else records anything. A refused click
    leaves no position, no order and no fill — so if this line is not
    written, the fact that the trader asked at all is simply gone."""
    coordinator = engine
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] - 0.05, 10)
    legs['acct_a'].tick = lambda symbol: None      # the pair goes dark
    coordinator.poll_once()

    with caplog.at_level(logging.INFO):
        answer = coordinator.click(pair.key, SpreadSide.SELL, level)

    assert answer['refused']
    line = clicks(caplog)[0]
    assert 'refused' in line
    assert 'no price' in line, 'the reason did not reach the log'
    assert f'{level:g}' in line


def test_the_size_on_the_line_is_the_one_that_was_used(engine, pair, legs,
                                                       caplog):
    """A click with no size takes the pair's default. Logging the empty
    request rather than the resolved size would put a blank against
    every ordinary click."""
    coordinator = engine
    pair.default_quantity = 3.0
    md = coordinator.market[pair.key]

    with caplog.at_level(logging.INFO):
        coordinator.click(pair.key, SpreadSide.SELL,
                          round(md['short_spread'] - 0.05, 10))

    assert 'qty 3' in clicks(caplog)[0]


def test_three_clicks_write_three_lines(engine, pair, legs, caplog):
    """Three clicks at one level aggregate into ONE pending at the
    broker, so the broker's own record cannot tell you there were
    three. This is the only place that can."""
    coordinator = engine
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] - 0.05, 10)

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            coordinator.click(pair.key, SpreadSide.SELL, level)

    assert len(clicks(caplog)) == 3


def test_a_broken_log_line_never_costs_the_click(engine, pair, legs, caplog):
    """The rule this addition could most easily have broken: a click is
    an order. It does not fail because of its own audit trail."""
    coordinator = engine
    md = coordinator.market[pair.key]
    level = round(md['short_spread'] - 0.05, 10)

    def explode(*args, **kwargs):
        raise RuntimeError('the log handler fell over')

    original, logging.info = logging.info, explode
    try:
        answer = coordinator.click(pair.key, SpreadSide.SELL, level)
    finally:
        logging.info = original

    assert answer['ok'], answer          # the ORDER still happened
    assert len(coordinator.book.orders(pair.key)) == 1
