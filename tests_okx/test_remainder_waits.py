"""A reducing click is ONE instruction, and the remainder goes LAST.

Live 2026-09-03, from the desk's own tickets:

    05:48  SELL 10 spread   A 2110 / B 2109   (MARKET)
    06:27  click BUY 12 at one level, in LIMIT mode
    06:27  BUY 2 spread OPENED   A 2127 / B 2126
           ...and SELL 10 still on.

The reduce arithmetic was right — 12 covers the 10, remainder 2, and
2 is exactly what opened. What was wrong is that the two halves of one
click ran on DIFFERENT ENGINES:

  - the remainder rested as an ordinary pending AT THE BROKER, and
    filled on the broker's own tick stream;
  - the close was a level watched HERE, at poll rate, about three
    times a second.

The broker's fills do not wait for our polling. The pending filled on a
tick we never sampled, the close did not fire, and the trader ended up
+2 AND still -10 — the one net position they certainly did not ask for.

So the remainder now rides on the close and opens only after it.
"""

import pytest

from okxtrader.quoter import QuoteGroup, closing_trigger_reached
from okxtrader.models import SpreadSide


def group(side=SpreadSide.BUY, level=46.16, position_id='POS-OLD'):
    return QuoteGroup('XAUUSD|GCZ6', side, level, 'b',
                      position_id=position_id)


def test_a_fresh_group_carries_no_remainder():
    """The CONTROL. An ordinary reducing click that covers exactly must
    not open anything afterwards."""
    assert group().open_after == 0.0


def test_the_remainder_is_recorded_on_the_close_not_rested():
    g = group()
    g.open_after = 2.0
    assert g.open_after == pytest.approx(2.0)


def test_the_click_hands_the_remainder_over_instead_of_opening_it():
    import inspect
    from okxtrader.coordinator import Coordinator

    source = inspect.getsource(Coordinator._click)
    # Both resting paths.
    assert source.count('carry_remainder') == 2, (
        'one of the two resting paths still rests the remainder itself')
    for chunk in source.split('carry_remainder')[1:2]:
        # Whatever follows must RETURN, not fall through to add_order.
        head = chunk[:400]
        assert 'return' in head


def test_the_remainder_opens_only_after_a_FULL_close():
    """Not on a partial. Half a close with the remainder already on is
    the same wrong net position in a smaller size."""
    import inspect
    from okxtrader.quoter import Quoter

    source = inspect.getsource(Quoter._work_closing)
    partial = source.index("action': 'closed_part_at_level'")
    # The LAST one: the first is the drop branch for a position that
    # went elsewhere, which is a different thing.
    opens = source.rindex('group.open_after > 0')
    assert opens > partial, 'the remainder opens on a partial close'
    # And after the position is settled and the group popped.
    assert source.index('self.groups.pop(key, None)', partial) < opens


def test_a_position_closed_elsewhere_DROPS_the_remainder():
    """The click said "cover this and open the rest". If something else
    covered it — a manual flatten, the overnight rule, the reconciler —
    the rest is no longer the trade that was asked for, and putting a
    position on by itself minutes later is the last thing anyone wants
    from a ladder."""
    import inspect
    from okxtrader.quoter import Quoter

    source = inspect.getsource(Quoter._work_closing)
    gone = source.index('or not position.is_open')
    dropped = source.index('are dropped')
    # The drop happens in the position-is-gone branch, long before the
    # branch that actually opens a remainder.
    assert gone < dropped < source.rindex('group.open_after > 0')


def test_the_trigger_still_reads_the_executable_side():
    """Untouched by this change, and the thing the close depends on."""
    assert closing_trigger_reached(
        SpreadSide.BUY, 46.16, {'long_spread': 46.10}) is True
    assert closing_trigger_reached(
        SpreadSide.BUY, 46.16, {'long_spread': 46.20}) is False
    assert closing_trigger_reached(
        SpreadSide.SELL, 45.64, {'short_spread': 45.70}) is True
    assert closing_trigger_reached(
        SpreadSide.SELL, 45.64, {'short_spread': 45.60}) is False
    # A price nobody has NEVER triggers.
    assert closing_trigger_reached(
        SpreadSide.BUY, 46.16, {'long_spread': None}) is False
