"""Rules about the money paths, checked mechanically rather than by eye.

Three separate bugs in this codebase were the SAME bug: a call that
says whether money moved, whose answer was thrown away.

  * The executor unwound a half-filled pair and ignored both answers,
    so a leg that would not come off left the trader naked with the
    screen showing a refusal.
  * The quoter did the same after a rejected hedge - on the RESTING
    order path, where the fill happens with nobody watching.
  * The session cutoff counted DAY orders it had ASKED to cancel, so a
    cancel that failed was published as one that worked, and an order
    left resting overnight was recorded as pulled.

Every one was found by reading code carefully, twice, after a trader
lost money. Reading carefully does not scale and I have already proved
it does not: fixes of mine introduced further bugs three times in this
project.

So the rule is enforced by a parser instead. This file is the method,
not a test of a feature.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Calls whose answer IS the money: ignoring one means not knowing
#: whether a position went on, came off, or is still sitting there.
MUST_READ = {
    '_unwind_leg', 'close_tickets', 'close_position_ticket', 'close_ticket',
    'cancel_order', 'cancel_pending', 'send_market_order',
    'place_limit', 'place_pending_limit', 'modify_order', 'modify_pending',
}

#: The two exceptions, and why each is safe. A list somebody has to
#: defend, not a switch that turns the rule off.
#:
#: Both are WRITES TO OUR OWN DATABASE wrapped in try/except that logs
#: at CRITICAL. What matters there is the exception, which IS handled;
#: the return value is a row count, and no money moves either way.
ALLOWED = {
    ('coordinator.py', 'remember_tickets'),
    ('coordinator.py', 'save_position'),
}


def _sources():
    for path in sorted(ROOT.glob('okxtrader/*.py')):
        yield path
    yield ROOT / 'start.py'


def discarded_calls():
    """(file, line, name, source) for every money call read by nobody."""
    out = []
    for path in _sources():
        text = path.read_text(encoding='utf-8')
        lines = text.split('\n')
        for node in ast.walk(ast.parse(text)):
            # A call standing alone as a STATEMENT throws its answer away.
            if not isinstance(node, ast.Expr):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            name = getattr(func, 'attr', None) or getattr(func, 'id', None)
            if name not in MUST_READ:
                continue
            if (path.name, name) in ALLOWED:
                continue
            out.append((path.name, node.lineno, name,
                        lines[node.lineno - 1].strip()))
    return out


def test_no_call_that_moves_money_has_its_answer_thrown_away():
    """The rule itself. A close, a cancel or an unwind that FAILED and
    was not read is a position the trader still owns and has not been
    told about."""
    found = discarded_calls()
    assert not found, '\n'.join(
        f'{f}:{line} {name}() -> {src}' for f, line, name, src in found)


def test_control_the_check_can_actually_see_one():
    """The control, and this file needs it more than most: a scanner
    that finds nothing because it is looking wrongly reads exactly like
    a clean codebase."""
    planted = ast.parse('def f(self):\n    self.close_tickets(1, 2)\n')
    hits = [n for n in ast.walk(planted)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and (getattr(n.value.func, 'attr', None) in MUST_READ)]
    assert len(hits) == 1


def test_control_a_call_whose_answer_is_read_is_not_flagged():
    """The other control: assigned, returned or tested must all pass,
    or the rule becomes noise and gets an allowlist entry per line."""
    for good in ('x = self.close_tickets(1)',
                 'return self.close_tickets(1)',
                 'if self.close_tickets(1).get("ok"): pass'):
        tree = ast.parse(f'def f(self):\n    {good}\n')
        bare = [n for n in ast.walk(tree)
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                and (getattr(n.value.func, 'attr', None) in MUST_READ)]
        assert not bare, good


def test_the_allowlist_stays_short_and_is_only_database_writes():
    """An allowlist that grows is the rule being switched off one line
    at a time."""
    assert len(ALLOWED) <= 2
    for _file, name in ALLOWED:
        assert name in ('remember_tickets', 'save_position'), name


@pytest.mark.parametrize('name', sorted(MUST_READ))
def test_every_named_call_still_exists_somewhere(name):
    """A rule about a function nobody calls any more is a rule that
    has quietly stopped protecting anything - the commonest way a
    check like this rots."""
    text = '\n'.join(p.read_text(encoding='utf-8') for p in _sources())
    assert name in text, f'{name} is in MUST_READ but appears nowhere'
