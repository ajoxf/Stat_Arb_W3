"""An unanswered shutdown prompt means NO."""

import io

from okxtrader.models import LegFill, OrderSide, SpreadPosition, SpreadSide
from okxtrader.shutdown import prompt_text, should_close


def a_position():
    leg_a = LegFill('acct_a', 'XAUUSD_', OrderSide.BUY, 0.1, 4292.2)
    leg_b = LegFill('acct_b', 'GC1226', OrderSide.SELL, 0.1, 4351.0)
    return SpreadPosition('XAUUSD_|GC1226', SpreadSide.SELL, 1.0, leg_a,
                          leg_b, 58.8, 'MARKET', 10.0)


def test_silence_leaves_the_positions_open():
    """Closing at market is irreversible; a position left open is
    recovered on the next start."""
    out = io.StringIO()
    assert should_close([a_position()], 'ask', timeout=0.05, stream=out,
                        reader=lambda: _never()) is False
    assert 'SHUTTING DOWN with 1 OPEN POSITION' in out.getvalue()


def _never():
    import threading
    threading.Event().wait()        # never returns; the join times out


def test_only_an_explicit_yes_closes():
    out = io.StringIO()
    assert should_close([a_position()], 'ask', 1.0, out, lambda: 'y\n') is True
    assert should_close([a_position()], 'ask', 1.0, out, lambda: '\n') is False
    assert should_close([a_position()], 'ask', 1.0, out, lambda: 'n\n') is False


def test_the_setting_can_answer_without_asking():
    out = io.StringIO()
    assert should_close([a_position()], 'always', stream=out) is True
    assert should_close([a_position()], 'never', stream=out) is False
    assert out.getvalue() == ''             # neither mode prompts


def test_nothing_open_is_not_a_question():
    assert should_close([], 'always') is False


def test_the_prompt_names_what_is_at_stake():
    text = prompt_text([a_position()])
    assert 'XAUUSD_|GC1226' in text
    assert 'close them now, at market' in text
    assert 'no engine, until you start up again' in text
