"""The CLIENT PORTAL: its own web server, read-only, one client's own
dashboard after their own sign-in - and nothing else."""

import calendar
import json

import pytest

from okxtrader import client_portal as cp
from okxtrader import clientdash


def at(year, month, day=10):
    return float(calendar.timegm((year, month, day, 12, 0, 0)))


@pytest.fixture
def desk(tmp_path):
    """Two managed accounts, each with its money from OKX and its closed
    copies, and a login for each."""
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'accounts': {}, 'pairs': {}, 'mirror': {
        'enabled': True, 'followers': [
            {'name': 'client_a', 'multiple': 1, 'profit_share_pct': 20},
            {'name': 'client_b', 'multiple': 2, 'profit_share_pct': 25}]}}))
    (tmp_path / 'status.json').write_text('{}')
    (tmp_path / 'mirror_state.json').write_text(json.dumps({
        'cash': {
            'client_a': {'balance': {'equity': 10912.4, 'available': 9300.0,
                                     'margin_used': 1612.4, 'at': at(2026, 11, 24)},
                         'flows': {'b1': {'at': at(2026, 9, 1), 'amount': 10000.0,
                                          'kind': 'transfer in from the main account',
                                          'source': 'OKX'}}},
            'client_b': {'balance': {'equity': 51234.0, 'at': at(2026, 11, 24)},
                         'flows': {'b9': {'at': at(2026, 9, 2), 'amount': 50000.0,
                                          'kind': 'deposit', 'source': 'OKX'}}}},
        'closed': {
            'client_a': [{'closed_at': at(2026, 9, 20), 'pnl': 512.3},
                         {'closed_at': at(2026, 10, 18), 'pnl': -130.0},
                         {'closed_at': at(2026, 11, 12), 'pnl': 530.1}],
            'client_b': [{'closed_at': at(2026, 10, 3), 'pnl': 777.77}]}}))
    cp.set_login(str(config), 'client_a', 'Client.A', 'correct horse 1')
    cp.set_login(str(config), 'client_b', 'client.b', 'battery staple 2')
    clock = {'now': at(2026, 11, 24)}
    app = cp.create_portal(str(config), str(tmp_path / 'status.json'),
                           clock=lambda: clock['now'])
    app.config.update(TESTING=True)
    return tmp_path, app.test_client(), clock


def sign_in(client, username, password):
    return client.post('/login', data={'username': username,
                                       'password': password})


def test_a_client_signs_in_and_sees_their_own_dashboard(desk):
    folder, client, _clock = desk
    assert client.get('/').headers['Location'].endswith('/login')
    r = sign_in(client, 'client.a', 'correct horse 1')   # any case
    assert r.status_code == 302
    page = client.get('/').get_data(as_text=True)
    assert 'client_a' in page and 'Client dashboard' in page
    assert '$10,912.40' in page and '$9,300.00' in page    # balance, margin
    assert 'Monthly returns' in page and 'Profit payout' in page
    assert 'Profit share' not in page
    assert 'Transfer in from the main account' in page
    # Nothing about what was traded.
    for word in ('SWAP', 'BTC', 'ETH', 'ladder', 'Leg'):
        assert word not in page
    # ...and never the other client's.
    assert 'client_b' not in page and '51,234' not in page


def test_a_wrong_password_and_an_unknown_name_get_the_same_answer(desk):
    _folder, client, _clock = desk
    wrong = sign_in(client, 'client.a', 'nope nope nope')
    unknown = sign_in(client, 'nobody', 'nope nope nope')
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_data(as_text=True) == unknown.get_data(as_text=True)
    assert client.get('/').status_code == 302               # not signed in


def test_five_wrong_tries_lock_that_username_for_a_while(desk):
    _folder, client, clock = desk
    for _ in range(5):
        sign_in(client, 'client.b', 'wrong password!')
    locked = sign_in(client, 'client.b', 'battery staple 2')
    assert locked.status_code == 429
    clock['now'] += cp.LOCK_SEC + 1
    assert sign_in(client, 'client.b', 'battery staple 2').status_code == 302


def test_the_account_comes_from_the_login_never_the_address(desk):
    _folder, client, _clock = desk
    sign_in(client, 'client.a', 'correct horse 1')
    for path in ('/?account=client_b', '/client_b', '/mirror/client/client_b',
                 '/api/command', '/api/status', '/report'):
        page = client.get(path)
        assert '51,234' not in page.get_data(as_text=True), path
    # The portal cannot trade: there is no command route at all.
    assert client.post('/api/command', json={'kind': 'kill'}).status_code \
        in (404, 405)


def test_a_removed_login_signs_the_session_out(desk):
    folder, client, _clock = desk
    sign_in(client, 'client.a', 'correct horse 1')
    cp.remove_login(str(folder / 'config.json'), 'client_a')
    assert client.get('/').headers['Location'].endswith('/login')


def test_passwords_are_only_ever_kept_hashed(desk):
    folder, _client, _clock = desk
    text = (folder / cp.LOGINS_FILE).read_text()
    assert 'correct horse' not in text and 'battery staple' not in text
    row = json.loads(text)['client_a']
    assert set(row) >= {'salt', 'hash', 'iterations', 'username'}
    assert row['iterations'] >= 100_000


def test_weak_logins_and_taken_usernames_are_refused(desk):
    folder, _client, _clock = desk
    config = str(folder / 'config.json')
    with pytest.raises(ValueError, match='10 characters'):
        cp.set_login(config, 'client_a', 'client.a', 'short')
    with pytest.raises(ValueError, match='already'):
        cp.set_login(config, 'client_a', 'client.b', 'long enough pw 1')


def test_the_desk_preview_is_the_same_page_the_client_sees(desk):
    folder, _client, _clock = desk
    data = clientdash.gather('client_a', str(folder / 'config.json'),
                             str(folder / 'mirror_state.json'),
                             now=at(2026, 11, 24))
    page = clientdash.render(data, preview=True, logout=False)
    assert 'Manager preview' in page and 'Log out' not in page
    s = data['summary']
    assert s['pnl'] == pytest.approx(912.4)
    assert s['balance'] == 10912.4 and s['available'] == 9300.0
    assert s['share_pct'] == 20.0
