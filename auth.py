"""
Authentication blueprint for the SaaS trading platform.

Provides:
- /auth/login    POST  JSON login
- /auth/register POST  JSON registration
- /auth/logout   POST  Logout current user
- /login         GET   Login page
- /register      GET   Register page
"""

import logging
from datetime import datetime
from flask import (
    Blueprint, render_template, request, jsonify,
    redirect, url_for, flash
)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

# db is injected from app.py after blueprint registration
_db = None


def init_auth(db_manager):
    """Inject the DatabaseManager instance into the auth module."""
    global _db
    _db = db_manager


# ─────────────────────────────────────────────────────────────────────────────
# Page routes
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route('/login', methods=['GET'])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('auth/login.html')


@auth_bp.route('/register', methods=['GET'])
def register_page():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    # Only allow registration if there are no users yet (first admin),
    # OR if registrations are open (can be controlled by env var later)
    user_count = _db.count_users() if _db else 0
    return render_template('auth/register.html', is_first_user=(user_count == 0))


# ─────────────────────────────────────────────────────────────────────────────
# API routes (JSON)
# ─────────────────────────────────────────────────────────────────────────────

@auth_bp.route('/login', methods=['POST'])
def login():
    """Authenticate user and create session."""
    data = request.json or {}
    email_or_username = (data.get('email') or data.get('username') or '').strip().lower()
    password = data.get('password', '')

    if not email_or_username or not password:
        return jsonify({'success': False, 'error': 'Email/username and password are required'}), 400

    # Look up by email first, then username
    user = _db.get_user_by_email(email_or_username)
    if not user:
        user = _db.get_user_by_username(email_or_username)

    if not user or not check_password_hash(user.password_hash, password):
        logger.warning("Failed login attempt for: %s", email_or_username)
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

    if not user.is_active:
        return jsonify({'success': False, 'error': 'Account is disabled. Contact support.'}), 403

    login_user(user, remember=data.get('remember', False))
    _db.update_last_login(user.id)
    _db.audit(user.id, 'LOGIN', ip=request.remote_addr)
    logger.info("User logged in: id=%d email=%s", user.id, user.email)

    return jsonify({
        'success': True,
        'redirect': url_for('dashboard'),
        'user': {
            'id': user.id,
            'email': user.email,
            'username': user.username,
            'is_admin': user.is_admin,
        }
    })


@auth_bp.route('/register', methods=['POST'])
def register():
    """Create a new user account."""
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    username = (data.get('username') or '').strip()
    password = data.get('password', '')
    confirm = data.get('confirm_password', '')

    # Validation
    if not email or not username or not password:
        return jsonify({'success': False, 'error': 'All fields are required'}), 400

    if len(password) < 8:
        return jsonify({'success': False, 'error': 'Password must be at least 8 characters'}), 400

    if password != confirm:
        return jsonify({'success': False, 'error': 'Passwords do not match'}), 400

    if '@' not in email:
        return jsonify({'success': False, 'error': 'Invalid email address'}), 400

    if len(username) < 3 or len(username) > 32:
        return jsonify({'success': False, 'error': 'Username must be 3-32 characters'}), 400

    # Check uniqueness
    if _db.get_user_by_email(email):
        return jsonify({'success': False, 'error': 'Email already registered'}), 409

    if _db.get_user_by_username(username):
        return jsonify({'success': False, 'error': 'Username already taken'}), 409

    # First registered user becomes admin
    is_first_user = (_db.count_users() == 0)
    is_admin = is_first_user  # First user is admin

    password_hash = generate_password_hash(password)
    user_id = _db.create_user(email, username, password_hash, is_admin=is_admin)

    user = _db.get_user_by_id(user_id)
    login_user(user)
    _db.update_last_login(user_id)
    _db.audit(user_id, 'REGISTER', ip=request.remote_addr)
    logger.info("New user registered: id=%d email=%s is_admin=%s", user_id, email, is_admin)

    return jsonify({
        'success': True,
        'redirect': url_for('dashboard'),
        'user': {
            'id': user.id,
            'email': user.email,
            'username': user.username,
            'is_admin': user.is_admin,
        }
    })


@auth_bp.route('/logout', methods=['POST', 'GET'])
@login_required
def logout():
    """Log out the current user."""
    if current_user.is_authenticated:
        _db.audit(current_user.id, 'LOGOUT', ip=request.remote_addr)
        logger.info("User logged out: id=%d", current_user.id)
    logout_user()
    return redirect(url_for('auth.login_page'))
