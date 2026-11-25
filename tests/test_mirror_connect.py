"""Adding a managed account from its API key alone: the desk reads the
rest from OKX - what the account is, demo or live, whether it can
trade, its equity - and the multiple is capital-proportional."""

import json
import time

from okxtrader.okx_api import OKXError
from okxtrader.webapp import create_app


class FakeOKX:
    """A key that works on ONE of live and demo, as an OKX key does."""

    def __init__(self, demo_ok, config, equity):
        self.demo_ok, self.config, self.equity = demo_ok, config, equity
        self.asked = []

    def __call__(self, key, secret, passphrase, demo):
        outer = self

        class Client:
            def get(self, path, params=None, private=False):
                outer.asked.append((demo, path))
                if demo != outer.demo_ok:
                    raise OKXError('APIKey does not match current '
                                   'environment', code='50101')
                if path.endswith('/config'):
                    return [outer.config]
                return [{'totalEq': str(outer.equity)}]
        return Client()


def app_with(tmp_path, okx, followers=()):
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({
        'at': time.time(),
        'accounts': {'okx': {'equity': 3744.0}},
        'pairs': {'A|B': {'leg_a_account': 'okx', 'leg_b_account': 'okx'}}}))
    (tmp_path / 'config.json').write_text(json.dumps({
        'accounts': {}, 'pairs': {},
        'mirror': {'followers': [{'name': n, 'multiple': 1}
                                 for n in followers]}}))
    app = create_app(str(status), str(tmp_path / 'c.jsonl'),
                     str(tmp_path / 'r.json'), str(tmp_path / 'config.json'),
                     db_path=str(tmp_path / 'x.db'))
    app.config['OKX_CLIENT_FACTORY'] = okx
    return app.test_client()


KEY = {'api_key': 'k-123', 'secret': 's-456', 'passphrase': 'p-789'}
MANAGED = {'uid': '448812345678', 'label': 'Client Alpha!', 'type': '2',
           'perm': 'read_only,trade', 'posMode': 'long_short_mode'}


def test_the_key_alone_finds_the_account_and_its_multiple(tmp_path):
    okx = FakeOKX(demo_ok=True, config=MANAGED, equity=7488.0)
    body = app_with(tmp_path, okx).post('/api/mirror/discover',
                                        json=KEY).get_json()
    assert body['ok'] is True
    # Tried live first; the key works on demo.
    assert okx.asked[0] == (False, '/api/v5/account/config')
    assert body['demo'] is True
    assert body['managed'] is True and body['can_trade'] is True
    assert body['kind'] == 'managed trading sub-account'
    assert body['uid'] == '448812345678'
    # Twice your equity: twice your size.
    assert body['equity'] == 7488.0 and body['master_equity'] == 3744.0
    assert body['multiple'] == 2.0
    assert body['name'] == 'Client Alpha'
    assert body['warnings'] == []
    # Nothing saved: the key is not in .env until the account is added.
    assert not (tmp_path / '.env').exists()


def test_a_key_that_cannot_trade_or_is_not_managed_says_so(tmp_path):
    okx = FakeOKX(demo_ok=False, config=dict(MANAGED, type='0',
                                              perm='read_only', label=''),
                  equity=1872.0)
    body = app_with(tmp_path, okx, followers=['client_5678']).post(
        '/api/mirror/discover', json=KEY).get_json()
    assert body['demo'] is False and body['multiple'] == 0.5
    assert body['can_trade'] is False and body['managed'] is False
    assert any('cannot TRADE' in w for w in body['warnings'])
    assert any('main account' in w for w in body['warnings'])
    # No label: named from the UID, and never over an existing name.
    assert body['name'] == 'client_5678_2'


def test_a_key_okx_refuses_everywhere_is_refused(tmp_path):
    okx = FakeOKX(demo_ok=None, config=MANAGED, equity=0)
    answer = app_with(tmp_path, okx).post('/api/mirror/discover', json=KEY)
    assert answer.status_code == 400
    assert 'live or demo' in answer.get_json()['error']
    assert app_with(tmp_path, okx).post('/api/mirror/discover', json={
        'api_key': 'k'}).status_code == 400


def test_no_equity_leaves_the_multiple_to_the_trader(tmp_path):
    okx = FakeOKX(demo_ok=True, config=MANAGED, equity=0)
    body = app_with(tmp_path, okx).post('/api/mirror/discover',
                                        json=KEY).get_json()
    assert body['multiple'] is None
    assert any('set it by hand' in w for w in body['warnings'])
