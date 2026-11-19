"""An account's MetaTrader 5 is chosen on the Exchanges page, never in a
file.

A desk would not start because the path had been pasted from Windows'
"Copy as path", quotes and all: the file was exactly where the path said
and Windows was asked for one whose name began with a quote. The person
at that desk does not open config files. So the path is cleaned wherever
it is read, the installed terminals are offered to PICK, a wrong path
opens the screen and holds the engine rather than stopping both, and the
screen says what is wrong and where to fix it.
"""

import json
import os

import pytest

from okxtrader import config as cfg
from okxtrader.webapp import create_app


def exe(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / cfg.TERMINAL_EXE
    path.write_text('', encoding='utf-8')
    return str(path)


# --- the path as typed ----------------------------------------------------

@pytest.mark.parametrize('typed,meant', [
    ('"C:\\MT5\\terminal64.exe"', 'C:\\MT5\\terminal64.exe'),
    ("'C:\\MT5\\terminal64.exe'", 'C:\\MT5\\terminal64.exe'),
    ('  "C:\\MT5\\terminal64.exe"  ', 'C:\\MT5\\terminal64.exe'),
    ('" C:\\MT5\\terminal64.exe "', 'C:\\MT5\\terminal64.exe'),
    ('C:\\MT5\\terminal64.exe', 'C:\\MT5\\terminal64.exe'),
    ('', ''),
    (None, ''),
])
def test_quotes_and_spaces_around_a_path_come_off(typed, meant):
    assert cfg.clean_terminal_path(typed) == meant


def test_control_a_quote_inside_the_path_is_left_alone():
    """Only a MATCHING pair around the whole path is packaging."""
    assert cfg.clean_terminal_path('"C:\\MT5') == '"C:\\MT5'


def test_a_folder_means_the_terminal_inside_it(tmp_path):
    path = exe(tmp_path / 'MT5')
    assert cfg.terminal_file(str(tmp_path / 'MT5')) == path
    assert cfg.terminal_file(f'"{tmp_path / "MT5"}"') == path


def test_control_a_folder_without_a_terminal_is_not_made_up(tmp_path):
    (tmp_path / 'empty').mkdir()
    assert cfg.terminal_file(str(tmp_path / 'empty')) == \
        str(tmp_path / 'empty')


def test_a_missing_terminal_is_named_with_what_to_do(tmp_path):
    gone = str(tmp_path / 'gone' / cfg.TERMINAL_EXE)
    words = cfg.terminal_missing(gone)
    assert 'no MetaTrader 5 at' in words and gone in words
    assert 'Pick' in words
    (tmp_path / 'empty').mkdir()
    assert 'folder with no terminal64.exe' in \
        cfg.terminal_missing(str(tmp_path / 'empty'))


def test_control_a_terminal_that_is_there_is_not_missing(tmp_path):
    path = exe(tmp_path / 'MT5')
    assert cfg.terminal_missing(path) is None
    assert cfg.terminal_missing(f'"{path}"') is None
    # Blank is a choice - attach to the one already open - not a fault.
    assert cfg.terminal_missing('') is None
    assert cfg.terminal_missing(None) is None


def test_a_quoted_path_loads_as_the_path(tmp_path):
    path = exe(tmp_path / 'MT5')
    account = cfg.AccountConfig.from_dict('A', {'terminal_path': f'"{path}"'})
    assert account.terminal_path == path


def test_one_terminal_quoted_and_unquoted_is_still_one_terminal(tmp_path):
    path = exe(tmp_path / 'MT5')
    raw = {'accounts': {'a': {'terminal_path': f'"{path}"'},
                        'b': {'terminal_path': path}}}
    assert cfg.terminal_clash(raw, 'b', path)
    assert cfg.engine_hold_reasons(raw)


def test_control_two_terminals_hold_nothing(tmp_path):
    raw = {'accounts': {'a': {'terminal_path': exe(tmp_path / 'A')},
                        'b': {'terminal_path': exe(tmp_path / 'B')}}}
    assert cfg.engine_hold_reasons(raw) == []


# --- the installed terminals, to pick from --------------------------------

def test_the_installed_terminals_are_found_and_who_uses_each(tmp_path):
    root = tmp_path / 'Program Files'
    cfi = exe(root / 'CFI2 MetaTrader 5 Terminal')
    fxpro = exe(root / 'FxPro - MetaTrader 5')
    (root / 'Not a terminal').mkdir()
    portable = exe(tmp_path / 'drive' / 'MT5-A')
    raw = {'accounts': {'CFI': {'terminal_path': f'"{cfi}"'}}}

    found = cfg.find_terminals(raw, roots=[str(root), str(tmp_path / 'drive'),
                                           str(tmp_path / 'nowhere')])
    by_path = {t['path']: t for t in found}
    assert set(by_path) == {cfi, fxpro, portable}
    assert by_path[cfi]['used_by'] == 'CFI'
    assert by_path[fxpro]['used_by'] is None
    assert by_path[cfi]['folder'] == 'CFI2 MetaTrader 5 Terminal'


def test_one_root_listed_twice_lists_each_terminal_once(tmp_path):
    exe(tmp_path / 'MT5')
    assert len(cfg.find_terminals({}, roots=[str(tmp_path)] * 2)) == 1


# --- the web screen -------------------------------------------------------

@pytest.fixture
def paths(tmp_path):
    return {'status': str(tmp_path / 'status.json'),
            'commands': str(tmp_path / 'commands.jsonl'),
            'results': str(tmp_path / 'results.json'),
            'config': str(tmp_path / 'config.json')}


@pytest.fixture
def client(paths):
    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'])
    app.config.update(TESTING=True)
    return app.test_client()


def test_saving_a_pasted_path_stores_the_path_without_quotes(client, paths,
                                                             tmp_path):
    path = exe(tmp_path / 'CFI2 MetaTrader 5 Terminal')
    cfg.save_raw(paths['config'], {'accounts': {'CFI': {}}, 'pairs': {}})
    body = client.post('/api/accounts/CFI',
                       json={'terminal_path': f'"{path}"'}).get_json()
    assert body['ok'], body
    assert body['terminal_path'] == path
    assert body['terminal_missing'] is None
    stored = json.load(open(paths['config'], encoding='utf-8'))
    assert stored['accounts']['CFI']['terminal_path'] == path


def test_saving_a_folder_stores_the_terminal_inside_it(client, paths,
                                                       tmp_path):
    path = exe(tmp_path / 'MT5')
    cfg.save_raw(paths['config'], {'accounts': {'CFI': {}}, 'pairs': {}})
    body = client.post('/api/accounts/CFI',
                       json={'terminal_path': str(tmp_path / 'MT5')}).get_json()
    assert body['terminal_path'] == path


def test_saving_a_wrong_path_saves_it_and_says_what_is_wrong(client, paths,
                                                             tmp_path):
    gone = str(tmp_path / 'gone' / cfg.TERMINAL_EXE)
    cfg.save_raw(paths['config'], {'accounts': {'CFI': {}}, 'pairs': {}})
    body = client.post('/api/accounts/CFI',
                       json={'terminal_path': gone}).get_json()
    assert body['ok']
    assert 'no MetaTrader 5 at' in body['terminal_missing']


def test_the_row_says_its_terminal_is_missing(client, paths, tmp_path):
    there = exe(tmp_path / 'A')
    cfg.save_raw(paths['config'], {'accounts': {
        'CFI': {'terminal_path': f'"{tmp_path / "gone" / "terminal64.exe"}"'},
        'FxPro': {'terminal_path': there}}, 'pairs': {}})
    rows = {r['name']: r for r in
            client.get('/api/accounts').get_json()['accounts']}
    assert 'no MetaTrader 5 at' in rows['CFI']['terminal_missing']
    assert rows['FxPro']['terminal_missing'] is None          # control


def test_the_page_lists_the_installed_terminals(client, paths, tmp_path,
                                                monkeypatch):
    path = exe(tmp_path / 'Program Files' / 'CFI2 MetaTrader 5 Terminal')
    monkeypatch.setattr(cfg, 'terminal_roots',
                        lambda: [str(tmp_path / 'Program Files')])
    cfg.save_raw(paths['config'], {'accounts': {'CFI': {'terminal_path': path}},
                                   'pairs': {}})
    body = client.get('/api/terminals').get_json()
    assert body['ok']
    assert body['terminals'] == [{'path': path,
                                  'folder': 'CFI2 MetaTrader 5 Terminal',
                                  'used_by': 'CFI'}]


def test_the_screen_says_the_engine_is_waiting_and_where_to_fix_it(
        client, paths, tmp_path):
    cfg.save_raw(paths['config'], {'accounts': {
        'CFI': {'terminal_path': str(tmp_path / 'gone' / 'terminal64.exe')}},
        'pairs': {}})
    body = client.get('/api/status').get_json()
    assert body['engine'] == 'down'
    assert body['engine_held']
    assert body['engine_note'].startswith(
        'the engine is waiting — open Exchanges')
    assert "account 'CFI'" in body['engine_note']
    assert 'Exchanges' in body['engine_note']


def test_control_a_good_setup_shows_no_waiting_note(client, paths, tmp_path):
    cfg.save_raw(paths['config'], {'accounts': {
        'CFI': {'terminal_path': exe(tmp_path / 'MT5')}}, 'pairs': {}})
    body = client.get('/api/status').get_json()
    assert body['engine'] == 'down'
    assert not body.get('engine_held')
    assert 'engine is waiting' not in body['engine_note']
