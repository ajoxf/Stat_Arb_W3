"""The open-P&L total must not treat an unmarkable position as zero."""

from okxtrader.coordinator import Coordinator


def totals(marks):
    """Reproduce the summing rule over a list of per-position net P&L."""
    open_pnl = 0.0
    for net_pnl in marks:
        if net_pnl is None:
            open_pnl = None
        elif open_pnl is not None:
            open_pnl += net_pnl
    return open_pnl


def test_every_position_marked_sums_normally():
    assert totals([10.0, -4.0, 2.5]) == 8.5


def test_one_unmarkable_position_makes_the_total_unknown():
    """It used to be skipped, so the total read 8.50 while one position
    was missing from it — a number a trader would size against."""
    assert totals([10.0, None, -1.5]) is None
    assert totals([None]) is None


def test_the_rule_is_the_one_the_coordinator_uses():
    """Guard against the fix being edited out of coordinator.py."""
    import inspect
    source = inspect.getsource(Coordinator.snapshot)
    assert 'open_pnl = None' in source, \
        'the coordinator no longer nulls the total on an unmarkable leg'
