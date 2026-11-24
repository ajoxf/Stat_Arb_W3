"""THE CLIENT PORTAL: a web server of its own, for clients only.

It is NOT the desk. The desk's web server can trade; this one cannot - it
has four routes (log in, log out, the dashboard, a health check) and it
only READS: config.json, the mirror's record and the trader's own
entries. So it is the only thing a client - or the internet - ever
reaches, and nothing that can place an order is behind it.

Each client signs in with the username and password the trader set on
the Managed Accounts screen and sees THEIR dashboard: which one is taken
from the login, never from the address, so one client can never ask for
another's. Passwords are kept only as salted PBKDF2 hashes
(`client_logins.json`); five wrong tries lock that username for five
minutes.

Run it with `python run_client_portal.py` - on this PC only until it is
deliberately opened (see that file).
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from flask import Flask, redirect, request, session

from . import clientdash

LOGINS_FILE = 'client_logins.json'
SECRET_FILE = 'client_portal.secret'
ITERATIONS = 200_000
MAX_FAILURES = 5
LOCK_SEC = 300.0
SESSION_HOURS = 12


# -- passwords -----------------------------------------------------------------

def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                 bytes.fromhex(salt), iterations).hex()
    return {'salt': salt, 'hash': digest, 'iterations': iterations}


def check_password(password, stored):
    if not stored or not password:
        return False
    digest = hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), bytes.fromhex(stored['salt']),
        int(stored.get('iterations') or ITERATIONS)).hex()
    return hmac.compare_digest(digest, stored['hash'])


def logins_path(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)),
                        LOGINS_FILE)


def read_logins(config_path):
    return clientdash.read_json(logins_path(config_path), {})


def set_login(config_path, account, username, password):
    """Give one managed account's owner a username and password (the
    Managed Accounts screen). A username belongs to one account only."""
    username = (username or '').strip().lower()
    if len(username) < 3 or not username.replace('_', '').replace(
            '-', '').replace('.', '').replace('@', '').isalnum():
        raise ValueError('a username is 3 or more letters, digits, '
                         '., -, _ or @')
    if len(password or '') < 10:
        raise ValueError('a password is 10 characters or more')
    logins = read_logins(config_path)
    for other, row in logins.items():
        if other != account and row.get('username') == username:
            raise ValueError(f'the username {username} is already '
                             f'{other}’s')
    logins[account] = dict(hash_password(password), username=username,
                           set_at=time.time())
    path = logins_path(config_path)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(logins, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def remove_login(config_path, account):
    logins = read_logins(config_path)
    if logins.pop(account, None) is not None:
        path = logins_path(config_path)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(logins, f)
        os.replace(tmp, path)


def secret_key(config_path):
    """The key the portal signs its session cookies with - made once and
    kept beside config.json, so a restart does not sign everyone out."""
    path = os.path.join(os.path.dirname(os.path.abspath(config_path)),
                        SECRET_FILE)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            key = f.read().strip()
            if len(key) >= 32:
                return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


# -- the app -------------------------------------------------------------------

def create_portal(config_path='config.json', status_path='status.json',
                  clock=time.time):
    app = Flask(__name__)
    app.secret_key = secret_key(config_path)
    app.config.update(SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Strict',
                      PERMANENT_SESSION_LIFETIME=SESSION_HOURS * 3600)
    state_path = os.path.join(
        os.path.dirname(os.path.abspath(status_path)), 'mirror_state.json')
    failures = {}
    lock = threading.Lock()

    def html(page, status=200):
        response = app.make_response((page, status))
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    def account_for(username):
        for account, row in read_logins(config_path).items():
            if row.get('username') == username:
                return account, row
        return None, None

    @app.get('/login')
    def login_form():
        return html(clientdash.login_page())

    @app.post('/login')
    def login():
        username = (request.form.get('username') or '').strip().lower()
        password = request.form.get('password') or ''
        now = clock()
        with lock:
            count, until = failures.get(username, (0, 0.0))
            if until > now:
                return html(clientdash.login_page(
                    'Too many tries - wait a few minutes and try again.'),
                    429)
        account, row = account_for(username)
        if account is None or not check_password(password, row):
            with lock:
                count += 1
                failures[username] = (
                    0 if count >= MAX_FAILURES else count,
                    now + LOCK_SEC if count >= MAX_FAILURES else 0.0)
            # The same answer for an unknown name and a wrong password:
            # which usernames exist is not something to give away.
            return html(clientdash.login_page(
                'That username and password do not match.'), 401)
        with lock:
            failures.pop(username, None)
        session.clear()
        session.permanent = True
        session['account'] = account
        session['username'] = username
        return redirect('/')

    @app.get('/logout')
    def logout():
        session.clear()
        return redirect('/login')

    @app.get('/')
    def dashboard():
        account = session.get('account')
        if not account:
            return redirect('/login')
        # Still this account's login? A login removed or given to someone
        # else signs the old session out.
        row = read_logins(config_path).get(account)
        if not row or row.get('username') != session.get('username'):
            session.clear()
            return redirect('/login')
        data = clientdash.gather(account, config_path, state_path,
                                 now=clock())
        if data is None:
            session.clear()
            return html(clientdash.login_page(
                'This account is not set up any more.'), 404)
        return html(clientdash.render(data))

    @app.get('/health')
    def health():
        return {'ok': True}

    return app
