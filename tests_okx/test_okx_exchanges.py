"""The Exchanges page for OKX, against the simulated OKX over real HTTP
with real signatures: save a key (to .env, never the config), Test it,
search instruments, and derive a pair."""

import json
import os
import socket
import threading

import pytest

from okxtrader import okx_sim
from okxtrader.webapp import create_app

KEY, SECRET, PASS = 'k-123', 's-456', 'p-789'


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def okx():
    port = _free_port()
    server = okx_sim.serve(port, autostart=False)
    server.sim.keys = {KEY: (SECRET, PASS)}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, f'http://127.0.0.1:{port}'
    server.shutdown()
    server.sim.stop()


@pytest.fixture
def web(tmp_path, okx, monkeypatch):
    _server, url = okx
    for key in list(os.environ):
        if key.startswith('OKX_'):
            monkeypatch.delenv(key, raising=False)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'accounts': {}, 'pairs': {}}))
    app = create_app(status_path=str(tmp_path / 'status.json'),
                     command_path=str(tmp_path / 'commands.jsonl'),
                     results_path=str(tmp_path / 'results.json'),
                     config_path=str(config),
                     db_path=str(tmp_path / 'okxtrader.db'))
    client = app.test_client()
    client.url = url
    client.tmp = tmp_path
    yield client
    for key in list(os.environ):
        if key.startswith('OKX_'):
            os.environ.pop(key, None)


def _save(web, name='desk', **fields):
    body = {'exchange': 'okx', 'demo': True, 'base_url': web.url}
    body.update(fields)
    return web.post(f'/api/accounts/{name}', json=body)


def _checks(result):
    return {c['name']: c['status'] for c in result['checks']}


def test_a_key_goes_to_env_and_never_to_the_config(web):
    answer = _save(web, api_key=KEY, secret=SECRET, passphrase=PASS)
    assert answer.get_json()['ok']
    config = (web.tmp / 'config.json').read_text()
    assert KEY not in config and SECRET not in config and PASS not in config
    env = (web.tmp / '.env').read_text()
    assert 'OKX_API_KEY_DESK' in env and SECRET in env
    row = web.get('/api/accounts').get_json()['accounts'][0]
    assert row['exchange'] == 'okx' and row['demo'] is True
    assert row['key_set'] and row['secret_set'] and row['passphrase_set']
    assert SECRET not in json.dumps(row)


def test_test_passes_on_a_good_demo_key(web):
    _save(web, api_key=KEY, secret=SECRET, passphrase=PASS)
    result = web.get('/api/accounts/desk/test').get_json()
    checks = _checks(result)
    assert result['ok'], result
    assert checks['API key'] == 'PASS'
    assert checks['Trade permission'] == 'PASS'
    assert checks['Position mode'] == 'PASS'
    assert checks['Fee tier'] == 'INFO'


@pytest.mark.parametrize('fields,code,words', [
    ({'secret': 'wrong'}, '50113', 'SECRET KEY is wrong'),
    ({'passphrase': 'wrong'}, '50105', 'passphrase is wrong'),
    ({'api_key': 'nope'}, '50111', 'does not know this API key'),
    ({'demo': False}, '50101', 'OTHER environment'),
])
def test_a_bad_key_says_which_part_is_wrong(web, fields, code, words):
    good = {'api_key': KEY, 'secret': SECRET, 'passphrase': PASS}
    good.update(fields)
    _save(web, **good)
    result = web.get('/api/accounts/desk/test').get_json()
    key = next(c for c in result['checks'] if c['name'] == 'API key')
    assert key['status'] == 'FAIL' and code in key['message']
    assert any(words in step for step in key['fix'])


def test_no_key_saved_is_a_plain_instruction(web, okx):
    server, _url = okx
    server.sim.keys = None
    web.post('/api/accounts/desk', json={'exchange': 'okx', 'demo': True})
    result = web.get('/api/accounts/desk/test').get_json()
    assert _checks(result)['OKX reachable'] in ('PASS', 'FAIL')
    assert not result['ok']


def test_net_mode_is_caught_before_the_first_trade(web, okx):
    server, _url = okx
    server.sim.pos_mode = 'net_mode'
    _save(web, api_key=KEY, secret=SECRET, passphrase=PASS)
    result = web.get('/api/accounts/desk/test').get_json()
    assert _checks(result)['Position mode'] == 'FAIL'


def test_instrument_search_covers_spot_perps_and_futures(web):
    _save(web, api_key=KEY, secret=SECRET, passphrase=PASS)
    found = web.get('/api/accounts/desk/symbols?q=BTC').get_json()
    kinds = {s['symbol']: s['inst_type'] for s in found['symbols']}
    assert kinds['BTC-USDT'] == 'SPOT'
    assert kinds['BTC-USDT-SWAP'] == 'SWAP'
    assert kinds['BTC-USDT-261225'] == 'FUTURES'
    assert 'ETH-USDT' not in kinds


def test_a_pair_is_derived_from_okx_specs(web):
    _save(web, api_key=KEY, secret=SECRET, passphrase=PASS)
    answer = web.post('/api/pairs/BTC-USDT-SWAP|BTC-USDT-261225/derive',
                      json={'leg_a': {'account': 'desk',
                                      'symbol': 'BTC-USDT-SWAP'},
                            'leg_b': {'account': 'desk',
                                      'symbol': 'BTC-USDT-261225'},
                            'pair_type': 'SPOT_FUTURE'}).get_json()
    assert answer['ok'], answer
    assert answer['specs']['a']['contract_size'] == 0.01
    assert answer['specs']['b']['volume_step'] == 0.1
    assert answer['increment'] == pytest.approx(0.1)
    assert answer['spread_now'] is not None


@pytest.mark.parametrize('a,b,kind', [
    ('BTC-USDT', 'BTC-USDT-SWAP', 'SPOT_FUTURE'),
    ('BTC-USDT-SWAP', 'BTC-USDT-261225', 'SPOT_FUTURE'),
    ('BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'RELATED'),
])
def test_the_pair_type_follows_the_instruments(web, a, b, kind):
    _save(web, api_key=KEY, secret=SECRET, passphrase=PASS)
    answer = web.post(f'/api/pairs/{a}|{b}/derive',
                      json={'leg_a': {'account': 'desk', 'symbol': a},
                            'leg_b': {'account': 'desk', 'symbol': b}}
                      ).get_json()
    assert answer['pair_type_suggestion'] == kind
    assert answer['fees_bps']['b']['maker'] is not None
