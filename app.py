"""Papyrus: a self-hosted pastebin with threaded Markdown discussions.

    python3 app.py            run the server (the first run sets itself up)
    python3 app.py -d         run it in the background; stop / restart / status manage it
    python3 app.py admin      set the admin password and enable /admin
    python3 app.py --help     all options

On first run it creates .venv, installs Papyrus into it (`pip install -e .`, which also gives
you a `.venv/bin/papyrus` command) and writes config.json with a fresh random secret_key.
Behind a reverse proxy, set server.trusted_proxies in config.json.
"""
import argparse
import datetime
import functools
import getpass
import hmac
import json
import multiprocessing
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

# ── First-run setup ───────────────────────────────────────────────────────────
# Standard library only: this has to work before Flask is importable.

_BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
_VENV_DIR       = os.path.join(_BASE_DIR, '.venv')
_SECRET_PLACEHOLDER = '"secret_key": "change-this-to-a-long-random-secret"'


def ensure_config(base_dir=_BASE_DIR):
    """Create config.json from config.example.json with a fresh random secret_key.

    Returns True if a file was written, False if config.json already existed.
    """
    config  = os.path.join(base_dir, 'config.json')
    example = os.path.join(base_dir, 'config.example.json')
    if os.path.exists(config):
        return False
    with open(example, encoding='utf-8') as f:
        text = f.read()
    if _SECRET_PLACEHOLDER not in text:
        raise RuntimeError('config.example.json has no placeholder secret_key to replace')
    text = text.replace(_SECRET_PLACEHOLDER, f'"secret_key": "{secrets.token_hex(32)}"')
    # O_EXCL: never overwrite a config someone else created in the meantime.
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    return True


def _have_dependencies():
    try:
        import flask, gunicorn, mistune, nh3  # noqa: F401
    except ImportError:
        return False
    return True


def _python_has_dependencies(python):
    probe = 'import flask, gunicorn, mistune, nh3'
    return subprocess.run([python, '-c', probe], capture_output=True).returncode == 0


def ensure_environment():
    """Install the dependencies into ./.venv if needed and re-run under it."""
    if _have_dependencies():
        return
    if os.environ.get('PAPYRUS_BOOTSTRAPPED'):
        sys.exit('Dependencies are still missing after `pip install -e .`.')
    in_venv = os.path.realpath(sys.prefix) == os.path.realpath(_VENV_DIR)
    python = sys.executable if in_venv else os.path.join(_VENV_DIR, 'bin', 'python')
    try:
        if not os.path.exists(python):
            print('Creating .venv ...', flush=True)
            import venv
            venv.create(_VENV_DIR, with_pip=True)
        if not _python_has_dependencies(python):
            print('Installing Papyrus and its dependencies ...', flush=True)
            subprocess.check_call([python, '-m', 'pip', 'install', '--quiet', '-e', _BASE_DIR])
    except (ImportError, OSError, subprocess.CalledProcessError) as e:
        sys.exit(f'Could not set up the environment ({e}).\n'
                 f'Install it yourself: python3 -m venv .venv && .venv/bin/pip install -e .')
    os.environ['PAPYRUS_BOOTSTRAPPED'] = '1'
    os.execv(python, [python] + sys.argv)


if __name__ == '__main__':
    ensure_environment()

try:
    if ensure_config():
        print('Created config.json with a new random secret_key '
              '(the admin panel stays off until you run `python3 app.py admin`).', file=sys.stderr)
except OSError:
    pass  # read-only checkout (e.g. a container): fall back to config.example.json + SECRET_KEY

import mistune
import nh3
from flask import Flask, render_template, request, jsonify, abort, Response, g, session, redirect, url_for
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

_UTC = datetime.timezone.utc


# ── Load configuration ────────────────────────────────────────────────────────

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
if not os.path.exists(_CONFIG_PATH):  # fresh checkout: run on the example defaults
    _CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.example.json')

def _load_config():
    with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

try:
    CFG = _load_config()
except (FileNotFoundError, json.JSONDecodeError) as e:
    raise RuntimeError(f"Failed to load config.json: {e}") from e

_site   = CFG['site']
_server = CFG['server']
_db_cfg = CFG['database']
_pastes = CFG['pastes']
_theme  = CFG['theme']
_feat   = CFG['features']
_ui     = CFG['ui']

# ── Flask app setup ───────────────────────────────────────────────────────────

app = Flask(__name__)

# Prefer SECRET_KEY from the environment; fall back to config.json.
# Refuse to start if the key is still the default placeholder.
_secret_key = os.environ.get('SECRET_KEY') or _server.get('secret_key', '')
_DEFAULT_KEYS = {'', 'change-me', 'change-this-to-a-long-random-secret'}
if _secret_key in _DEFAULT_KEYS:
    raise RuntimeError(
        'SECRET_KEY is not set or is still the default placeholder. '
        'Set a long random value in the SECRET_KEY environment variable '
        'or in config.json before starting the server.'
    )
app.config['SECRET_KEY'] = _secret_key

# When running behind N reverse proxies, trust their X-Forwarded-* headers so
# request.remote_addr (used for rate limiting) is the real client address.
_trusted_proxies = int(_server.get('trusted_proxies', 0))
if _trusted_proxies > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_trusted_proxies,
                            x_proto=_trusted_proxies, x_host=_trusted_proxies)

# ── Session hardening ─────────────────────────────────────────────────────────
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE']   = _server.get('use_https', False)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

if _server.get('debug', False):
    print('WARNING: debug=true is set in config.json — never use debug mode in production!',
          file=sys.stderr, flush=True)

_raw_db_path        = _db_cfg.get('path', 'papyrus.db')
DATABASE            = os.path.realpath(os.path.join(_BASE_DIR, _raw_db_path))
if not DATABASE.startswith(_BASE_DIR):
    raise RuntimeError(f"Database path '{DATABASE}' must be within the project directory.")
MAX_PASTE_BYTES     = _pastes.get('max_size_bytes', 2 * 1024 * 1024)
MAX_TITLE_BYTES     = 200
MIN_ID_LENGTH       = 12  # 48 bits; shorter IDs are enumerable
_LANGUAGE_RE        = re.compile(r'^[a-z0-9_+-]{1,32}$')
_PASTE_ID_RE        = re.compile(r'^[0-9a-f]{4,32}$')
_CONTROL_CHARS_RE   = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')

# Reject oversized request bodies before they are read into memory.
app.config['MAX_CONTENT_LENGTH'] = MAX_PASTE_BYTES + 64 * 1024

# ── Security headers ─────────────────────────────────────────────────────────

@app.before_request
def _generate_csp_nonce():
    """Generate a unique nonce per request for the Content-Security-Policy."""
    g.csp_nonce = secrets.token_urlsafe(16)

@app.after_request
def set_security_headers(response):
    nonce = getattr(g, 'csp_nonce', '')
    response.headers['X-Frame-Options']        = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy']        = 'same-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        f"script-src 'self' https://cdnjs.cloudflare.com 'nonce-{nonce}'; "
        "style-src 'self' https://cdnjs.cloudflare.com 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none';"
    )
    return response

# ── Markdown ──────────────────────────────────────────────────────────────────

_MD_TAGS = {
    'p', 'br', 'hr', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
    'ul', 'ol', 'li', 'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
}
_MD_CODE_CLASS_RE = re.compile(r'^language-[a-z0-9_+-]{1,32}$')


class _SafeRenderer(mistune.HTMLRenderer):
    """Raw HTML is escaped, and images become plain links.

    Remote images would let a commenter learn the IP address of everyone who
    views the paste, so they are never embedded.
    """
    def image(self, text, url, title=None):
        return self.link(text or url, url, title)


_markdown = mistune.create_markdown(
    renderer=_SafeRenderer(escape=True),
    plugins=['strikethrough', 'table', 'url'],
)

def _md_attribute_filter(tag, attr, value):
    if (tag, attr) == ('code', 'class'):
        return value if _MD_CODE_CLASS_RE.match(value) else None
    return value

@functools.lru_cache(maxsize=2048)
def render_markdown(text):
    """Render untrusted Markdown to sanitised HTML (cached by source text)."""
    html = _markdown(text)
    return nh3.clean(
        html,
        tags=_MD_TAGS,
        attributes={'a': {'href', 'title'}, 'code': {'class'}},
        url_schemes={'http', 'https', 'mailto'},
        link_rel='noopener noreferrer nofollow',
        attribute_filter=_md_attribute_filter,
    )

# ── Jinja helpers ─────────────────────────────────────────────────────────────

@app.context_processor
def inject_config():
    return {'cfg': CFG, 'csp_nonce': getattr(g, 'csp_nonce', '')}

@app.template_filter('markdown')
def markdown_filter(text):
    return Markup(render_markdown(text or ''))

@app.template_filter('utc_iso')
def utc_iso_filter(value):
    """'2026-10-09 17:57:55' (sqlite CURRENT_TIMESTAMP, UTC) -> '2026-10-09T17:57:55Z'."""
    return (value or '').replace(' ', 'T') + 'Z'

# ── Database ──────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

@contextmanager
def _db():
    """Yield a connection that is always closed; commit explicitly where needed."""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()

_db_init_lock = threading.Lock()
_db_initialized = False

def _columns(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}

def _create_tables(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pastes (
            id                  TEXT PRIMARY KEY,
            title               TEXT NOT NULL DEFAULT 'Untitled',
            content             TEXT NOT NULL,
            language            TEXT NOT NULL DEFAULT 'text',
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at          TEXT,
            views               INTEGER DEFAULT 0,
            deletion_token      TEXT,
            discussions_enabled INTEGER DEFAULT 0
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pastes_created ON pastes(created_at)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id          TEXT PRIMARY KEY,
            paste_id    TEXT NOT NULL,
            parent_id   TEXT,
            nick        TEXT,
            content     TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    if 'parent_id' not in _columns(conn, 'comments'):  # created before replies existed
        conn.execute('ALTER TABLE comments ADD COLUMN parent_id TEXT')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_comments_paste ON comments(paste_id)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS rate_limits (
            ip_address    TEXT,
            timestamp     REAL,
            PRIMARY KEY (ip_address, timestamp)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_ts ON rate_limits(timestamp)')

def _migrate_encrypted_schema(conn):
    """Move an old end-to-end-encrypted database to the plaintext schema.

    Pastes that were stored unencrypted are carried over. Encrypted ones cannot
    be read by the server, so they stay untouched in `pastes_encrypted_legacy`.
    """
    conn.execute('ALTER TABLE pastes RENAME TO pastes_encrypted_legacy')
    old_comments = conn.execute("SELECT name FROM sqlite_master WHERE name='comments'").fetchone()
    if old_comments and 'nick' not in _columns(conn, 'comments'):
        conn.execute('ALTER TABLE comments RENAME TO comments_encrypted_legacy')
    _create_tables(conn)

    kept = skipped = 0
    for row in conn.execute('SELECT * FROM pastes_encrypted_legacy').fetchall():
        try:
            data = json.loads(row['encrypted_data'])
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, TypeError):
            skipped += 1
            continue
        conn.execute(
            'INSERT INTO pastes (id, title, content, language, created_at, expires_at, views,'
            ' deletion_token, discussions_enabled) VALUES (?,?,?,?,?,?,?,?,?)',
            (row['id'], _clean_title(data.get('title')), str(data.get('content') or ''),
             _clean_language(data.get('language')), row['created_at'], row['expires_at'],
             row['views'], row['deletion_token'], row['discussions_enabled']))
        kept += 1
        # Old comments held a JSON blob ({content, nick}), or ciphertext we can't read.
        legacy = conn.execute("SELECT name FROM sqlite_master WHERE name='comments_encrypted_legacy'").fetchone()
        if legacy:
            for c in conn.execute('SELECT * FROM comments_encrypted_legacy WHERE paste_id = ?',
                                  (row['id'],)).fetchall():
                try:
                    blob = json.loads(c['content'])
                    conn.execute('INSERT INTO comments (id, paste_id, nick, content, created_at) VALUES (?,?,?,?,?)',
                                 (c['id'], row['id'], str(blob.get('nick') or '')[:32],
                                  str(blob['content']), c['created_at']))
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
    print(f'[Migration] Plaintext schema: kept {kept} paste(s); {skipped} encrypted paste(s) '
          f'remain in pastes_encrypted_legacy.', file=sys.stderr, flush=True)

def init_db():
    """Initialise the database. Thread-safe and idempotent: runs DDL only once per process."""
    global _db_initialized
    with _db_init_lock:
        if _db_initialized:
            return
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                if 'encrypted_data' in _columns(conn, 'pastes'):
                    _migrate_encrypted_schema(conn)
                _create_tables(conn)
                # Older rows stored 'YYYY-MM-DD HH:MM:SS+00:00' (space); expiry comparisons are
                # string-based, so normalise to the ISO 'T' form.
                conn.execute(
                    "UPDATE pastes SET expires_at = replace(expires_at, ' ', 'T') "
                    "WHERE expires_at LIKE '____-__-__ %'"
                )
        finally:
            conn.close()
        _db_initialized = True
        _start_cleanup_task()

_cleanup_running = False

def _purge_expired():
    now = datetime.datetime.now(_UTC).isoformat()
    with _db() as conn:
        with conn:
            expired = [r[0] for r in conn.execute(
                'SELECT id FROM pastes WHERE expires_at < ?', (now,))]
            for paste_id in expired:
                _delete_paste(conn, paste_id)
            purged_ips = conn.execute(
                'DELETE FROM rate_limits WHERE timestamp < ?', (time.time() - 3600,)).rowcount
    if expired or purged_ips:
        print(f"[Cleanup] Purged {len(expired)} expired pastes and {purged_ips} rate limit entries.",
              flush=True)

def _start_cleanup_task():
    """Start a background thread to purge expired pastes every hour."""
    global _cleanup_running
    if _cleanup_running:
        return
    _cleanup_running = True
    def run():
        while True:
            # Sleep first to avoid running immediately on startup
            time.sleep(3600)
            try:
                _purge_expired()
            except Exception as e:
                print(f"Error in background cleanup: {e}", file=sys.stderr)

    threading.Thread(target=run, daemon=True).start()

# ── Helpers ───────────────────────────────────────────────────────────────────

def generate_paste_id():
    length = min(max(int(_pastes.get('id_length', MIN_ID_LENGTH)), MIN_ID_LENGTH), 32)
    return secrets.token_hex(16)[:length]

def _delete_paste(conn, paste_id):
    conn.execute('DELETE FROM comments WHERE paste_id = ?', (paste_id,))
    conn.execute('DELETE FROM pastes WHERE id = ?', (paste_id,))

def _is_same_origin():
    """Verify that the request is from the same origin to prevent CSRF."""
    def _origin(url):
        parts = urlsplit(url)
        return (parts.scheme, parts.netloc) if parts.scheme and parts.netloc else None

    expected = _origin(request.host_url)
    header = request.headers.get('Origin') or request.headers.get('Referer')
    if not header:
        return False
    return _origin(header) == expected

def _parse_expiry(value):
    """Parse a stored expires_at value; naive timestamps are treated as UTC."""
    dt = datetime.datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=_UTC)

def _is_expired(value):
    return bool(value) and _parse_expiry(value) < datetime.datetime.now(_UTC)

def _ensure_rate_limits_table(conn):
    """Create the rate_limits table if it doesn't exist (migration safety net)."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS rate_limits (
            ip_address    TEXT,
            timestamp     REAL,
            PRIMARY KEY (ip_address, timestamp)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_ts ON rate_limits(timestamp)')
    conn.commit()

def _rate_limit_count(conn, key, window):
    try:
        return conn.execute(
            'SELECT COUNT(*) FROM rate_limits WHERE ip_address = ? AND timestamp > ?',
            (key, time.time() - window)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        # Table missing (pre-migration DB) — create it and proceed.
        _ensure_rate_limits_table(conn)
        return 0

def _is_rate_limited(remote_ip, key_prefix, window, limit):
    """Read-only check: has this IP already used up its budget?"""
    with _db() as conn:
        return _rate_limit_count(conn, f"{key_prefix}:{remote_ip}", window) >= limit

def _record_hit(remote_ip, key_prefix):
    with _db() as conn:
        _ensure_rate_limits_table(conn)
        # INSERT OR IGNORE: the primary key is (ip, timestamp)
        conn.execute('INSERT OR IGNORE INTO rate_limits (ip_address, timestamp) VALUES (?, ?)',
                     (f"{key_prefix}:{remote_ip}", time.time()))
        conn.commit()

def _check_rate_limit(remote_ip, key_prefix='rl', window=600, limit=10):
    """Count this request against the budget; False if the budget is exhausted."""
    if _is_rate_limited(remote_ip, key_prefix, window, limit):
        return False
    _record_hit(remote_ip, key_prefix)
    return True

def _get_paste_or_abort(paste_id):
    if not _PASTE_ID_RE.match(paste_id):
        abort(404)
    with _db() as conn:
        paste = conn.execute('SELECT * FROM pastes WHERE id = ?', (paste_id,)).fetchone()
        if not paste:
            abort(404)
        if _is_expired(paste['expires_at']):
            _delete_paste(conn, paste_id)
            conn.commit()
            abort(410)
    return paste

def _clean_title(title):
    if not isinstance(title, str):
        return 'Untitled'
    title = _CONTROL_CHARS_RE.sub('', title).strip()
    return title.encode('utf-8')[:MAX_TITLE_BYTES].decode('utf-8', 'ignore') or 'Untitled'

def _clean_language(language):
    if isinstance(language, str) and _LANGUAGE_RE.match(language):
        return language
    return _pastes.get('default_language', 'text')

_EXPIRY_DELTAS = {
    '1hour':  datetime.timedelta(hours=1),
    '1day':   datetime.timedelta(days=1),
    '1week':  datetime.timedelta(weeks=1),
    '1month': datetime.timedelta(days=30),
    '1year':  datetime.timedelta(days=365),
}

def _expiry_from_choice(choice):
    """Return an ISO expires_at string for an expiry option, or None for 'never'."""
    allowed = set(_pastes.get('allow_expiry_options', ['1year']))
    if choice not in allowed:
        choice = _pastes.get('default_expiry', 'never')
    delta = _EXPIRY_DELTAS.get(choice)
    if delta is None:  # 'never', or an option the config lists but we don't know
        return None
    return (datetime.datetime.now(_UTC) + delta).isoformat()

# Prism language ids differ from ours for a few languages; False = no highlighting.
_PRISM_LANG_MAP = {'text': False, 'html': 'markup', 'xml': 'markup'}

def _prism_language(language):
    return _PRISM_LANG_MAP.get(language, language)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/create', methods=['POST'])
def create_paste():
    # 10 pastes per 10 minutes per IP address (SQLite backed for worker safety)
    if not _check_rate_limit(request.remote_addr, key_prefix='create', limit=10):
        return jsonify({'error': 'Rate limit exceeded. Please wait a few minutes.'}), 429

    # Two ways in: a JSON body (the web UI) or a plain-text body for curl:
    #   curl --data-binary @file 'https://host/create?title=x&language=python&expires_in=1day'
    plain_text = request.mimetype == 'text/plain'
    if plain_text:
        data = {
            'content':     request.get_data(as_text=True),
            'title':       request.args.get('title'),
            'language':    request.args.get('language'),
            'expires_in':  request.args.get('expires_in', _pastes.get('default_expiry', 'never')),
            'discussions': request.args.get('discussions') in ('1', 'true', 'yes'),
        }
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'JSON body required'}), 400

    content = data.get('content')
    if not content or not isinstance(content, str) or not content.strip():
        return jsonify({'error': 'content must be a non-empty string'}), 400
    if len(content.encode('utf-8')) > MAX_PASTE_BYTES:
        return jsonify({'error': 'Paste too large'}), 413

    title = _clean_title(data.get('title'))
    language = _clean_language(data.get('language'))
    discussions = bool(data.get('discussions', False))
    expires_at = _expiry_from_choice(data.get('expires_in', _pastes.get('default_expiry', 'never')))

    deletion_token = secrets.token_urlsafe(32)
    with _db() as conn:
        for _ in range(5):
            paste_id = generate_paste_id()
            try:
                conn.execute(
                    'INSERT INTO pastes (id, title, content, language, expires_at, deletion_token,'
                    ' discussions_enabled) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (paste_id, title, content, language, expires_at, deletion_token, int(discussions)))
                conn.commit()
                break
            except sqlite3.IntegrityError:
                continue
        else:
            return jsonify({'error': 'Service temporarily unavailable'}), 503

    if plain_text:
        resp = Response(request.host_url + paste_id + '\n', mimetype='text/plain')
        resp.headers['X-Deletion-Token'] = deletion_token
        return resp
    return jsonify({'paste_id': paste_id, 'url': f'/{paste_id}', 'deletion_token': deletion_token})

@app.route('/api/delete/<string:paste_id>', methods=['POST'])
def delete_paste(paste_id):
    if not _PASTE_ID_RE.match(paste_id):
        abort(404)
    if not _is_same_origin():
        return jsonify({'error': 'Cross-Origin request blocked'}), 403

    data = request.get_json(silent=True) or {}
    token = data.get('token')
    if not token:
        return jsonify({'error': 'Deletion token required'}), 400

    with _db() as conn:
        paste = conn.execute('SELECT deletion_token FROM pastes WHERE id = ?', (paste_id,)).fetchone()
        if not paste:
            return jsonify({'error': 'Paste not found'}), 404
        if not isinstance(token, str) or not hmac.compare_digest(paste['deletion_token'] or '', token):
            return jsonify({'error': 'Invalid deletion token'}), 403
        _delete_paste(conn, paste_id)
        conn.commit()
    return jsonify({'success': True, 'message': 'Paste deleted successfully'})

@app.route('/<string:paste_id>')
def view_paste(paste_id):
    paste = _get_paste_or_abort(paste_id)
    rows = []
    with _db() as conn:
        conn.execute('UPDATE pastes SET views = views + 1 WHERE id = ?', (paste_id,))
        if paste['discussions_enabled']:
            rows = conn.execute(
                'SELECT id, parent_id, nick, content, created_at FROM comments WHERE paste_id = ?'
                ' ORDER BY created_at ASC, rowid ASC', (paste_id,)).fetchall()
        conn.commit()
    return render_template('view.html', paste=paste, comments=_comment_tree(rows),
                           comment_count=len(rows), prism_lang=_prism_language(paste['language']))

@app.route('/raw/<string:paste_id>')
def view_paste_raw(paste_id):
    paste = _get_paste_or_abort(paste_id)
    return Response(paste['content'], mimetype='text/plain; charset=utf-8')

@app.route('/api/languages')
def get_languages():
    return jsonify(CFG.get('languages', []))

@app.route('/api/config')
def get_client_config():
    """Expose a whitelisted subset of config to the browser. Never forward entire blobs."""
    return jsonify({
        'site': {
            'name':        _site.get('name', 'Papyrus'),
            'tagline':     _site.get('tagline', ''),
            'brand_icon':  _site.get('brand_icon', ''),
            'footer_text': _site.get('footer_text', ''),
        },
        'theme': {
            'default':          _theme.get('default', 'auto'),
            'allow_user_toggle': _theme.get('allow_user_toggle', True),
            'light':            _theme.get('light', {}),
            'dark':             _theme.get('dark', {}),
        },
        'features': {
            'show_recent':      _feat.get('show_recent', False),
            'show_view_count':  _feat.get('show_view_count', True),
            'auto_save_draft':  _feat.get('auto_save_draft', True),
            'draft_max_age_days': _feat.get('draft_max_age_days', 7),
            'keyboard_shortcuts': _feat.get('keyboard_shortcuts', True),
        },
        'ui': {
            'code_font_family': _ui.get('code_font_family', 'monospace'),
            'code_font_size':   _ui.get('code_font_size', '0.875rem'),
            'code_line_height': _ui.get('code_line_height', '1.6'),
            'textarea_rows':    _ui.get('textarea_rows', 20),
            'border_radius':    _ui.get('border_radius', '8px'),
            'animation_speed':  _ui.get('animation_speed', '0.2s'),
        },
        'pastes': {
            'default_language':     _pastes.get('default_language', 'text'),
            'default_expiry':       _pastes.get('default_expiry', 'never'),
            'allow_expiry_options': _pastes.get('allow_expiry_options', []),
            'expiry_labels':        _pastes.get('expiry_labels', {}),
        },
    })

@app.route('/recent')
def recent_pastes():
    """Show recently created pastes (only available when show_recent is enabled in config)."""
    if not _feat.get('show_recent', False):
        abort(404)
    limit = int(_pastes.get('recent_limit', 50))
    now   = datetime.datetime.now(_UTC).isoformat()
    with _db() as conn:
        pastes = conn.execute(
            '''
            SELECT id, title, language, created_at, expires_at, views FROM pastes
            WHERE expires_at IS NULL OR expires_at > ?
            ORDER BY created_at DESC LIMIT ?
            ''',
            (now, limit)
        ).fetchall()
    return render_template('recent.html', pastes=pastes)

# ── Comments ──────────────────────────────────────────────────────────────────

_MAX_COMMENT_BYTES      = 10240
_MAX_NICK_LENGTH        = 32
_MAX_COMMENTS_PER_PASTE = 500
_MAX_REPLY_DEPTH        = 6   # top-level comments are depth 0

def _comment_tree(rows):
    """Nest flat comment rows (oldest first) into [{..., 'children': [...]}, ...]."""
    nodes = {r['id']: {**dict(r), 'children': []} for r in rows}
    roots = []
    for r in rows:
        parent = nodes.get(r['parent_id'])
        (parent['children'] if parent else roots).append(nodes[r['id']])
    return roots

def _comment_depth(conn, comment_id):
    """Depth of an existing comment: 0 for top level."""
    depth = 0
    while comment_id and depth <= _MAX_REPLY_DEPTH + 1:
        row = conn.execute('SELECT parent_id FROM comments WHERE id = ?', (comment_id,)).fetchone()
        if row is None or row['parent_id'] is None:
            break
        comment_id = row['parent_id']
        depth += 1
    return depth

@app.route('/<string:paste_id>/comment', methods=['POST'])
def post_comment(paste_id):
    paste = _get_paste_or_abort(paste_id)
    if not _is_same_origin():
        abort(403, 'Cross-origin request blocked.')
    if not paste['discussions_enabled']:
        abort(403, 'Discussions are not enabled for this paste.')
    if not _check_rate_limit(request.remote_addr, key_prefix='comment', window=600, limit=20):
        abort(429, 'Rate limit exceeded. Please wait a few minutes.')

    content = (request.form.get('content') or '').strip()
    if not content:
        abort(400, 'A comment cannot be empty.')
    if len(content.encode('utf-8')) > _MAX_COMMENT_BYTES:
        abort(413, 'Comment too large.')
    nick = _CONTROL_CHARS_RE.sub('', request.form.get('nick') or '').strip()[:_MAX_NICK_LENGTH]

    parent_id = (request.form.get('parent') or '').strip() or None
    comment_id = secrets.token_hex(8)
    with _db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM comments WHERE paste_id = ?', (paste_id,)).fetchone()[0]
        if count >= _MAX_COMMENTS_PER_PASTE:
            abort(429, 'Comment limit reached for this paste.')
        if parent_id:
            parent = conn.execute('SELECT id, parent_id FROM comments WHERE id = ? AND paste_id = ?',
                                  (parent_id, paste_id)).fetchone()
            if parent is None:
                abort(400, 'The comment you are replying to does not exist.')
            # Very deep chains are flattened: reply to the parent's parent instead.
            if _comment_depth(conn, parent_id) + 1 > _MAX_REPLY_DEPTH:
                parent_id = parent['parent_id']
        conn.execute('INSERT INTO comments (id, paste_id, parent_id, nick, content) VALUES (?, ?, ?, ?, ?)',
                     (comment_id, paste_id, parent_id, nick, content))
        conn.commit()
    return redirect(url_for('view_paste', paste_id=paste_id) + f'#c-{comment_id}', code=303)

@app.route('/api/preview', methods=['POST'])
def preview_markdown():
    """Render Markdown for the comment form's Preview tab."""
    if not _is_same_origin():
        return jsonify({'error': 'Cross-Origin request blocked'}), 403
    if not _check_rate_limit(request.remote_addr, key_prefix='preview', window=600, limit=120):
        return jsonify({'error': 'Rate limit exceeded.'}), 429
    data = request.get_json(silent=True) or {}
    text = data.get('text')
    if not isinstance(text, str) or len(text.encode('utf-8')) > _MAX_COMMENT_BYTES:
        return jsonify({'error': 'text must be a string under 10 KB'}), 400
    return jsonify({'html': render_markdown(text)})

# ── Admin Panel ───────────────────────────────────────────────────────────────

def is_admin():
    return CFG.get('admin', {}).get('enabled', False) and session.get('admin_logged_in') is True

_ADMIN_LOGIN_WINDOW = 3600
_ADMIN_LOGIN_LIMIT  = 5

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    admin_cfg = CFG.get('admin', {})
    if not admin_cfg.get('enabled', False) or not admin_cfg.get('pass'):
        abort(404)
    error = None
    if request.method == 'POST':
        if not _is_same_origin():
            return "CSRF Blocked", 403
        # Only failed attempts count against the budget.
        if _is_rate_limited(request.remote_addr, 'admin_login', _ADMIN_LOGIN_WINDOW, _ADMIN_LOGIN_LIMIT):
            return render_template('admin_login.html', error='Too many login attempts. Try again later.'), 429

        user = request.form.get('username') or ''
        pw   = request.form.get('password') or ''
        # Evaluate both checks so timing doesn't reveal which one failed.
        user_ok = hmac.compare_digest(user.encode(), str(admin_cfg.get('user', '')).encode())
        pass_ok = check_password_hash(admin_cfg['pass'], pw)
        if user_ok and pass_ok:
            session.clear()
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        _record_hit(request.remote_addr, 'admin_login')
        error = 'Invalid credentials'
    return render_template('admin_login.html', error=error)

@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    if not _is_same_origin():
        abort(403)
    session.pop('admin_logged_in', None)
    return redirect(url_for('index'))

@app.route('/admin')
def admin_dashboard():
    if not is_admin():
        return redirect(url_for('admin_login'))
    with _db() as conn:
        pastes = conn.execute(
            'SELECT id, created_at, expires_at, views FROM pastes ORDER BY created_at DESC'
        ).fetchall()
    return render_template('admin_dashboard.html', pastes=pastes)

@app.route('/admin/delete/<string:paste_id>', methods=['POST'])
def admin_delete_paste(paste_id):
    if not is_admin():
        abort(403)
    if not _is_same_origin():
        abort(403)
    with _db() as conn:
        _delete_paste(conn, paste_id)
        conn.commit()
    return redirect(url_for('admin_dashboard'))

# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

@app.errorhandler(410)
def gone(error):
    return render_template('410.html'), 410

# ── Command line ──────────────────────────────────────────────────────────────

_PID_FILE = os.path.join(_BASE_DIR, 'papyrus.pid')
_LOG_FILE = os.path.join(_BASE_DIR, 'papyrus.log')


def _post_fork(server, worker):
    """Threads don't survive Gunicorn's fork, so each worker restarts the cleanup task."""
    global _cleanup_running
    _cleanup_running = False
    _start_cleanup_task()


def _control_socket_path(port):
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR') or tempfile.gettempdir()
    return os.path.join(runtime_dir, f'papyrus-{port}.ctl')


def _server_options(host, port, workers, pidfile=None):
    options = {
        'bind':               f'{host}:{port}',
        'workers':            workers,
        # SQLite (WAL) serialises writes; threads add concurrency without more processes.
        'worker_class':       'gthread',
        'threads':            4,
        'max_requests':       1000,   # recycle workers to bound memory growth
        'max_requests_jitter': 100,
        'timeout':            60,
        'keepalive':          5,
        'accesslog':          '-',
        'errorlog':           '-',
        'loglevel':           'info',
        'post_fork':          _post_fork,
        # Newer Gunicorn opens a control socket at one path shared by every instance of
        # the same user; give each Papyrus its own (0600, per port) so they can't collide.
        'control_socket':     _control_socket_path(port),
    }
    if pidfile:
        options['pidfile'] = pidfile
    return options


def _default_workers():
    return int(_server.get('workers') or min(8, max(2, multiprocessing.cpu_count() + 1)))


def _run_dev_server(host, port):
    print('Using the Flask development server; not for production.', file=sys.stderr)
    app.run(debug=_server.get('debug', False), host=host, port=port)


def _run_gunicorn(host, port, workers, pidfile=None):
    from gunicorn.app.base import BaseApplication

    class Server(BaseApplication):
        def __init__(self, application, options):
            self.application, self.options = application, options
            super().__init__()

        def load_config(self):
            for key, value in self.options.items():
                if key in self.cfg.settings:  # skip options an older Gunicorn doesn't know
                    self.cfg.set(key, value)

        def load(self):
            return self.application

    print(f'Papyrus listening on http://{host}:{port}  ({workers} workers)', flush=True)
    Server(app, _server_options(host, port, workers, pidfile)).run()


def _port_in_use(host, port):
    probe = '127.0.0.1' if host in ('0.0.0.0', '') else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((probe, port)) == 0


def _read_pid():
    try:
        with open(_PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cmd_start(args):
    host    = args.host or _server.get('host', '127.0.0.1')
    port    = args.port or int(_server.get('port', 5000))
    workers = args.workers or _default_workers()

    if args.daemon:
        return _start_in_background(host, port, workers)
    if _port_in_use(host, port):
        sys.exit(f'Port {port} is already in use (is Papyrus already running? try `python3 app.py status`).')
    if args.dev or _server.get('debug', False):
        return _run_dev_server(host, port)
    try:
        import gunicorn.app.base  # noqa: F401
    except ImportError:  # e.g. Windows
        print('Gunicorn is not available on this platform.', file=sys.stderr)
        return _run_dev_server(host, port)
    _run_gunicorn(host, port, workers, args.pid)


def _start_in_background(host, port, workers):
    pid = _read_pid()
    if pid and _alive(pid):
        sys.exit(f'Already running (PID {pid}). Use `restart` to reload.')
    if _port_in_use(host, port):
        sys.exit(f'Port {port} is already in use.')
    command = [sys.executable, os.path.abspath(__file__), 'start', '--host', host,
               '--port', str(port), '--workers', str(workers), '--pid', _PID_FILE]
    with open(_LOG_FILE, 'a') as log:
        proc = subprocess.Popen(command, stdout=log, stderr=log, cwd=_BASE_DIR, start_new_session=True)
    time.sleep(1.5)
    if proc.poll() is not None:
        sys.exit(f'Papyrus exited immediately; see {_LOG_FILE}')
    print(f'Started (PID {proc.pid}) at http://{host}:{port}\nLog: {_LOG_FILE}')


def cmd_stop():
    pid = _read_pid()
    if not pid or not _alive(pid):
        print('Not running')
        if pid:
            os.remove(_PID_FILE)
        return
    os.kill(pid, signal.SIGTERM)  # Gunicorn treats SIGTERM as a graceful shutdown
    for _ in range(100):
        time.sleep(0.1)
        if not _alive(pid):
            break
    else:
        sys.exit(f'PID {pid} did not exit within 10 seconds; stop it manually (kill -9 {pid}).')
    try:
        os.remove(_PID_FILE)
    except FileNotFoundError:
        pass
    print('Stopped')


def cmd_status():
    pid = _read_pid()
    print(f'Running (PID {pid})' if pid and _alive(pid) else 'Not running')


def set_admin_password(config_path, user, password):
    """Store a password hash for the admin panel and enable it."""
    with open(config_path, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg.setdefault('admin', {}).update(
        {'enabled': True, 'user': user, 'pass': generate_password_hash(password)})
    tmp = config_path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, config_path)


def cmd_admin(args):
    password = getpass.getpass('New admin password: ')
    if len(password) < 8:
        sys.exit('Use at least 8 characters.')
    if password != getpass.getpass('Repeat it: '):
        sys.exit('The passwords did not match.')
    set_admin_password(os.path.join(_BASE_DIR, 'config.json'), args.user, password)
    print(f'Admin panel enabled for "{args.user}". Restart Papyrus to apply '
          f'(python3 app.py restart), then open /admin.')


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(prog='app.py', description='Run and manage Papyrus.')
    parser.add_argument('command', nargs='?', default='start',
                        choices=['start', 'stop', 'restart', 'status', 'admin'],
                        help='start (default), stop, restart, status, or admin (set the admin password)')
    parser.add_argument('--host', help='address to bind (default: server.host in config.json)')
    parser.add_argument('--port', type=int, help='port to bind (default: server.port)')
    parser.add_argument('--workers', type=int, help='worker processes (default: server.workers or CPUs+1)')
    parser.add_argument('-d', '--daemon', action='store_true', help='start in the background')
    parser.add_argument('--dev', action='store_true', help='use the Flask development server')
    parser.add_argument('--user', default='admin', help='admin username (with the admin command)')
    parser.add_argument('--pid', help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    if args.command == 'start':
        cmd_start(args)
    elif args.command == 'stop':
        cmd_stop()
    elif args.command == 'restart':
        cmd_stop()
        args.daemon = True
        cmd_start(args)
    elif args.command == 'status':
        cmd_status()
    elif args.command == 'admin':
        cmd_admin(args)


# ── Entry point ───────────────────────────────────────────────────────────────

# Always initialise the DB when the module is imported (works under Gunicorn
# and other WSGI servers that import app directly, not just via main()).
init_db()

if __name__ == '__main__':
    main()
