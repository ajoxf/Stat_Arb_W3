"""The two ways the engine went silent while the screen looked fine.

Live on 2026-08-31 the ladders showed the PREVIOUS DAY's prices, ages
reading twelve seconds, a loop time of 315ms — and no engine behind any
of it. The web process was serving the last `status.json` written; the
launcher had died at startup on a `TypeError` from one retired field
left in `config.json`, and before that the coordinator had been exiting
whenever no leg runner answered, so the launcher restarted it into the
same exit, forever.

Nothing here is cosmetic. A screen showing a market that is not there
is the one failure that gets money committed on numbers that no longer
exist.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import start                                                    # noqa: E402
from okxtrader.config import PairConfig, TraderConfig            # noqa: E402
from okxtrader.coordinator import Coordinator                    # noqa: E402


# -- a field this version no longer reads ---------------------------------

def test_a_retired_field_does_not_stop_the_config_loading():
    """`swap_per_day` was removed between releases. A config still
    carrying it took the whole launcher down."""
    pair = PairConfig.from_dict('XAUUSD_|GC1226', {
        'name': 'Gold basis',
        'leg_a': {'account': 'a', 'symbol': 'XAUUSD_'},
        'leg_b': {'account': 'b', 'symbol': 'GC1226'},
        'swap_per_day': 0.35,               # retired
        'bogus_field': 'whatever',          # never existed
    })
    assert pair.name == 'Gold basis'
    assert not hasattr(pair, 'swap_per_day')


def test_a_retired_field_is_gone_from_the_file_at_the_next_save():
    raw = {'pairs': {'K': {'leg_a': {}, 'leg_b': {}, 'swap_per_day': 1.0}}}
    config = TraderConfig.from_raw(raw)
    assert 'swap_per_day' not in config.to_raw()['pairs']['K']


def test_a_field_that_IS_read_still_lands(tmp_path):
    """The control: dropping unknown fields must not drop known ones."""
    pair = PairConfig.from_dict('K', {'leg_a': {}, 'leg_b': {},
                                      'carry_rate_pct': 4.25})
    assert pair.carry_rate_pct == 4.25


# -- the launcher never dies on the config --------------------------------

def _write(tmp_path, raw):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    return str(path)


def test_an_unloadable_config_is_a_printed_problem_not_a_traceback(tmp_path):
    """check_config is what keeps the launcher alive: it reports, the
    engine stays down, the screen stays up and the loop tries again."""
    path = _write(tmp_path, {'pairs': {'K': {'order_type': 'NOT_AN_ORDER'}}})
    problems = start.check_config(path)
    assert problems and 'could not be loaded' in problems[0]
    # ...and it says which pair, which field and what the choices are.
    assert "pair 'K'" in problems[0] and 'LIMIT, MARKET' in problems[0]


def test_a_null_where_a_default_exists_is_the_DEFAULT(tmp_path):
    """`order_type: null` raised `OrderType(None)` inside the launcher
    and killed it at startup. A field with a default has one for
    exactly this case — a config written by an older UI, or a value
    cleared by hand."""
    path = _write(tmp_path, {'pairs': {'K': {
        'leg_a': {}, 'leg_b': {}, 'order_type': None, 'time_in_force': None,
        'overnight': None, 'rows': None, 'pair_type': None,
        'enabled': None, 'default_quantity': None}}})
    assert start.check_config(path) == []

    pair = TraderConfig.from_file(path).pairs['K']
    assert pair.order_type.value == 'LIMIT'
    assert pair.time_in_force.value == 'DAY'
    assert pair.overnight.value == 'ALLOW'
    assert pair.rows == 30 and pair.pair_type == 'SPOT_FUTURE'
    assert pair.enabled is True and pair.default_quantity == 1.0


def test_a_value_that_is_NOT_a_choice_is_still_refused(tmp_path):
    """The control: taking a blank as the default must not turn a
    wrong value into a silent one."""
    import pytest
    with pytest.raises(ValueError) as raised:
        PairConfig.from_dict('K', {'time_in_force': 'FOREVER'})
    assert 'FOREVER' in str(raised.value) and 'DAY' in str(raised.value)


def test_a_hot_apply_of_a_null_keeps_what_the_ladder_is_running(tmp_path):
    """The same null arriving through the live path. It used to raise
    inside the poll, where the only sign was a line in the log."""
    pair = PairConfig.from_dict('K', {'leg_a': {}, 'leg_b': {},
                                      'order_type': 'MARKET'})
    pair.apply_hot({'order_type': None, 'overnight': None})
    assert pair.order_type.value == 'MARKET'


def test_a_config_that_is_not_json_is_reported_too(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{not json', encoding='utf-8')
    problems = start.check_config(str(path))
    assert problems and 'not valid JSON' in problems[0]


def test_a_good_config_has_no_problems(tmp_path):
    """The control: the checker must not refuse a config that is fine."""
    path = _write(tmp_path, {
        'accounts': {'a': {'endpoint': '127.0.0.1:9101'}},
        'pairs': {'K': {'leg_a': {'account': 'a', 'symbol': 'X'},
                        'leg_b': {'account': 'a', 'symbol': 'Y'}}}})
    assert start.check_config(path) == []


# -- a pair key, and what kind of pair it is -------------------------------

def test_a_key_typed_with_spaces_saves_as_the_tidy_one(tmp_path):
    """The server tidies it too, so a pair created from an older page —
    or by hand — lands under the key the ladder looks for."""
    from okxtrader.webapp import _tidy_key
    assert _tidy_key(' XAUUSD.f | GCZ6.f ') == 'XAUUSD.f|GCZ6.f'
    assert _tidy_key('XAUUSD.f|GCZ6.f') == 'XAUUSD.f|GCZ6.f'


def test_the_pair_type_DIFFERENT_reads_as_RELATED(tmp_path):
    """The Exchanges page wrote DIFFERENT; the ladder offers RELATED.
    They are the same statement — two instruments with no carry between
    them — and a pair must not fall between the two names."""
    assert PairConfig.from_dict('K', {'pair_type': 'DIFFERENT'}) \
        .pair_type == 'RELATED'
    # The control: a type that IS one of the three is kept.
    assert PairConfig.from_dict('K', {'pair_type': 'FUTURE_FUTURE'}) \
        .pair_type == 'FUTURE_FUTURE'
    # ...and an unrecognised one reads as RELATED: no fair value is the
    # safe way to be wrong.
    assert PairConfig.from_dict('K', {'pair_type': 'wat'}) \
        .pair_type == 'RELATED'


# -- an account with no leg runner ----------------------------------------

def _with_accounts(config, *names):
    from okxtrader.config import AccountConfig
    for i, name in enumerate(names):
        config.accounts[name] = AccountConfig(
            name, endpoint=f'127.0.0.1:{9101 + i}')
    return config


def test_a_dark_account_is_named_on_the_snapshot(config, legs, tmp_path):
    """An absent account looked exactly like a quiet one."""
    _with_accounts(config, 'acct_a', 'acct_b')
    coordinator = Coordinator(config, {'acct_a': legs['acct_a']},
                              status_path=str(tmp_path / 'status.json'),
                              sleep=lambda s: None)
    assert coordinator.dark_accounts() == ['acct_b']
    assert coordinator.snapshot()['dark_accounts'] == ['acct_b']


def test_both_accounts_up_are_not_reported_dark(config, legs):
    """The control."""
    _with_accounts(config, 'acct_a', 'acct_b')
    coordinator = Coordinator(config, legs, sleep=lambda s: None)
    assert coordinator.dark_accounts() == []
    assert coordinator.snapshot()['dark_accounts'] == []


def test_recovery_is_incomplete_while_an_account_is_dark(config, legs,
                                                         tmp_path):
    """UNKNOWN is not flat. With one account unreadable the book cannot
    be called complete, and the reconciler must not auto-close anything
    on the account it CAN read."""
    from okxtrader.database import Store
    _with_accounts(config, 'acct_a', 'acct_b')
    coordinator = Coordinator(config, {'acct_a': legs['acct_a']},
                              status_path=str(tmp_path / 'status.json'),
                              store=Store(str(tmp_path / 'db.sqlite')),
                              sleep=lambda s: None)
    report = coordinator.recover()
    assert report['complete'] is False
    assert 'acct_b' in report['error']
    assert coordinator.reconciler.book_complete is False


def test_recovery_completes_when_every_account_answers(config, legs,
                                                       tmp_path):
    """The control: the same path with both runners up."""
    from okxtrader.database import Store
    _with_accounts(config, 'acct_a', 'acct_b')
    coordinator = Coordinator(config, legs,
                              status_path=str(tmp_path / 'status.json'),
                              store=Store(str(tmp_path / 'db.sqlite')),
                              sleep=lambda s: None)
    assert coordinator.recover()['complete'] is True


# -- a leg runner started late joins by itself -----------------------------

def test_a_late_leg_runner_joins_without_a_restart(config, legs, tmp_path):
    """The coordinator used to EXIT when no runner answered; the
    launcher restarted it into the same exit. Now it runs, retries, and
    the account joins — swept, resolved, and recovered against."""
    _with_accounts(config, 'acct_a', 'acct_b')
    coordinator = Coordinator(
        config, {'acct_a': legs['acct_a']},
        status_path=str(tmp_path / 'status.json'), sleep=lambda s: None,
        leg_factory=lambda name: legs.get(name))
    coordinator.resolve_symbols()
    assert coordinator.errors[list(config.pairs)[0]]    # leg B unresolvable

    assert coordinator.retry_dark_legs() == ['acct_b']
    assert coordinator.dark_accounts() == []
    assert coordinator.errors[list(config.pairs)[0]] == []


def test_the_retry_is_on_a_slow_clock(config, legs, tmp_path):
    """One connection attempt per account per LEG_RETRY_SEC, not one
    per poll three times a second."""
    tries = []
    _with_accounts(config, 'acct_a', 'acct_b')
    now = [1000.0]
    coordinator = Coordinator(
        config, {'acct_a': legs['acct_a']},
        status_path=str(tmp_path / 'status.json'), sleep=lambda s: None,
        clock=lambda: now[0],
        leg_factory=lambda name: tries.append(name) or None)
    coordinator.retry_dark_legs()
    coordinator.retry_dark_legs()
    now[0] += 0.3
    coordinator.retry_dark_legs()
    assert tries == ['acct_b']
    now[0] += 10.0
    coordinator.retry_dark_legs()
    assert tries == ['acct_b', 'acct_b']


def test_without_a_factory_nothing_is_retried(config, legs, tmp_path):
    """The control: a coordinator driving its own legs (the tests, and
    anything embedding it) is left alone."""
    _with_accounts(config, 'acct_a', 'acct_b')
    coordinator = Coordinator(config, {'acct_a': legs['acct_a']},
                              status_path=str(tmp_path / 'status.json'),
                              sleep=lambda s: None)
    assert coordinator.retry_dark_legs() == []
    assert coordinator.dark_accounts() == ['acct_b']
