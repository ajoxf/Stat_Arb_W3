"""Config: the refusals that happen at SAVE time, and the write that
cannot truncate.
"""

import json
import os

import pytest

from okxtrader import config as cfg


def raw_with(**accounts):
    return {'accounts': accounts, 'pairs': {'P': {}}}












def test_a_save_that_would_drop_the_accounts_is_refused(tmp_path):
    path = str(tmp_path / 'config.json')
    cfg.save_raw(path, raw_with(a={'login': 1}))

    with pytest.raises(RuntimeError, match='partial read'):
        cfg.save_raw(path, {'pairs': {'P': {}}})

    # The good copy is untouched.
    assert cfg.load_raw(path)['accounts'] == {'a': {'login': 1}}


def test_deleting_an_account_is_allowed_when_it_is_meant(tmp_path):
    path = str(tmp_path / 'config.json')
    cfg.save_raw(path, raw_with(a={'login': 1}))
    cfg.save_raw(path, {'accounts': {}, 'pairs': {'P': {}}},
                 allow_shrink=True)
    assert cfg.load_raw(path)['accounts'] == {}


def test_a_missing_config_is_empty_but_a_broken_one_is_not(tmp_path):
    path = str(tmp_path / 'config.json')
    assert cfg.load_raw(path) == {}             # first run

    with open(path, 'w', encoding='utf-8') as f:
        f.write('{not json')
    with pytest.raises(RuntimeError, match='no usable backup'):
        cfg.load_raw(path)


def test_a_broken_config_falls_back_to_the_backup(tmp_path):
    path = str(tmp_path / 'config.json')
    cfg.save_raw(path, raw_with(a={'login': 1}))
    cfg.save_raw(path, raw_with(a={'login': 2}))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('half a fi')

    assert cfg.load_raw(path)['accounts']['a']['login'] == 1


def test_the_config_is_never_written_in_place(tmp_path):
    """A reader in the truncation window saw half a config — and, in
    front of a read-modify-write save, wrote an empty one back."""
    path = str(tmp_path / 'config.json')
    cfg.save_raw(path, raw_with(a={'login': 1}))
    seen = []

    real_replace = os.replace

    def watching(src, dst):
        if dst == path:
            with open(dst, 'r', encoding='utf-8') as f:
                seen.append(json.load(f))       # readable throughout
        real_replace(src, dst)

    cfg.os.replace = watching
    try:
        cfg.save_raw(path, raw_with(a={'login': 2}))
    finally:
        cfg.os.replace = real_replace
    assert seen and seen[-1]['accounts']['a']['login'] == 1




def test_a_password_with_spaces_or_a_hash_survives_the_env_file(tmp_path):
    path = str(tmp_path / '.env')
    cfg.write_env_value(path, 'MT5_PASSWORD_A', 'two words #1')
    cfg.write_env_value(path, 'MT5_PASSWORD_B', 'plain')
    cfg.write_env_value(path, 'MT5_PASSWORD_A', 'changed "quoted"')

    body = open(path, encoding='utf-8').read()
    assert 'MT5_PASSWORD_A="changed \\"quoted\\""' in body
    assert body.count('MT5_PASSWORD_A') == 1        # replaced, not appended
    assert 'MT5_PASSWORD_B="plain"' in body




def test_structural_changes_say_restart_and_comfort_ones_do_not():
    """Crying "restart" on every save teaches the operator to ignore the
    line that matters."""
    first = cfg.TraderConfig.from_raw({
        'accounts': {'a': {'exchange': 'okx', 'demo': True}},
        'pairs': {'P': {'leg_a': {'account': 'a', 'symbol': 'X'},
                        'hedge_ratio': 1.0}},
        'settings': {'MARKET_PROTECTION_TICKS': 3.0}})

    same_but_comfort = cfg.TraderConfig.from_raw({
        'accounts': {'a': {'exchange': 'okx', 'demo': True}},
        'pairs': {'P': {'leg_a': {'account': 'a', 'symbol': 'X'},
                        'hedge_ratio': 1.0}},
        'settings': {'MARKET_PROTECTION_TICKS': 5.0}})
    assert first.restart_required(same_but_comfort) == []

    symbol_changed = cfg.TraderConfig.from_raw({
        'accounts': {'a': {'exchange': 'okx', 'demo': True}},
        'pairs': {'P': {'leg_a': {'account': 'a', 'symbol': 'Y'},
                        'hedge_ratio': 1.0}}})
    assert first.restart_required(symbol_changed) == ['pair P']


def test_the_example_config_does_not_drift_from_the_defaults():
    """A number in the example OVERRIDES the default, silently.

    README says `cp config.example.json config.json`, and settings load
    as `dict(DEFAULT_SETTINGS)` updated with the file — so a value the
    example repeats is pinned there for the life of that deployment.
    MAX_QUOTE_AGE_SEC was raised to 15s because 5s was tuned on a feed
    that ticks constantly and fires all day on a CFD account; the
    example kept saying 5.0, so every config made from it went on
    calling a quiet-but-healthy leg stale.

    Repeating a default is only safe while it still IS the default.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'config.example.json')
    with open(path, encoding='utf-8') as handle:
        example = json.load(handle)

    drifted = {key: (value, cfg.DEFAULT_SETTINGS.get(key))
               for key, value in (example.get('settings') or {}).items()
               if key in cfg.DEFAULT_SETTINGS
               and value != cfg.DEFAULT_SETTINGS[key]}
    assert not drifted, (
        f'config.example.json disagrees with DEFAULT_SETTINGS: {drifted}. '
        f'Either update the example or drop the key from it — a copied '
        f'example pins the stale value into every new deployment.')


def test_a_config_still_carrying_AutoRouting_loads_and_ignores_it():
    """AutoRouting has been removed. A desk's config.json written before
    that still says `auto_route: true` on its pairs and
    `AUTO_ROUTE_ENABLED: true` in its settings — and must still load,
    with nothing armed by either."""
    from okxtrader.config import PairConfig
    pair = PairConfig.from_dict('A|B', {'name': 'A vs B', 'auto_route': True})
    assert not hasattr(pair, 'auto_route')
    assert 'auto_route' not in pair.to_dict()
