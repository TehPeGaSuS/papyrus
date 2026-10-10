"""Papyrus: a self-hosted pastebin with threaded Markdown discussions.

    python3 app.py            run the server (the first run sets itself up)
    python3 app.py -d         run it in the background; stop / restart / status manage it
    python3 app.py admin      set the admin password and enable /admin
    python3 app.py --help     all options

On first run it creates .venv, installs Papyrus into it (`pip install -e .`, which also gives
you a `.venv/bin/papyrus` command) and writes config.json with a fresh random secret_key.
Behind a reverse proxy, have the proxy set X-Forwarded-For and X-Forwarded-Proto (see the README).
"""
import argparse
import datetime
import functools
import getpass
import hashlib
import hmac
import ipaddress
import json
import multiprocessing
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote, urlsplit

__version__ = '1.1.1'   # the one place the version lives; pyproject.toml reads it from here

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


def _venv_support_hint():
    version = f'{sys.version_info.major}.{sys.version_info.minor}'
    return (f'Python cannot create virtual environments here. On Debian/Ubuntu install it with:\n'
            f'    sudo apt install python3-venv     (or python{version}-venv)\n'
            f'Other distributions normally ship venv with Python itself; install their '
            f'"python3-venv" / "ensurepip" package if they split it out.')


def _venv_support_available():
    import importlib.util
    return importlib.util.find_spec('venv') is not None and importlib.util.find_spec('ensurepip') is not None


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
    set_up = False
    try:
        if not os.path.exists(python):
            if not _venv_support_available():
                sys.exit(_venv_support_hint())
            print('Creating .venv ...', flush=True)
            set_up = True
            import venv
            try:
                venv.create(_VENV_DIR, with_pip=True)
            except (OSError, subprocess.CalledProcessError) as e:
                # A half-made venv would look usable on the next run; remove it.
                shutil.rmtree(_VENV_DIR, ignore_errors=True)
                sys.exit(f'Could not create .venv ({e}).\n{_venv_support_hint()}')
        if not _python_has_dependencies(python):
            print('Installing Papyrus and its dependencies ...', flush=True)
            set_up = True
            subprocess.check_call([python, '-m', 'pip', 'install', '--quiet', '-e', _BASE_DIR])
    except (ImportError, OSError, subprocess.CalledProcessError) as e:
        sys.exit(f'Could not set up the environment ({e}).\n'
                 f'Install it yourself: python3 -m venv .venv && .venv/bin/pip install -e .')
    os.environ['PAPYRUS_BOOTSTRAPPED'] = '1'
    if set_up:
        os.environ['PAPYRUS_FIRST_RUN'] = '1'      # tells the re-run that this was only the bootstrap
    os.execv(python, [python] + sys.argv)


if __name__ == '__main__':
    ensure_environment()

_FIRST_RUN = bool(os.environ.pop('PAPYRUS_FIRST_RUN', ''))   # True when this run installed the environment...
try:
    if ensure_config():
        _FIRST_RUN = True                                         # ...or wrote the config
        print('Created config.json with a new random secret_key.', file=sys.stderr)
except OSError:
    pass  # read-only checkout (e.g. a container): fall back to config.example.json + SECRET_KEY

import mistune
import nh3
from flask import Flask, render_template, request, jsonify, abort, Response, g, session, redirect, url_for
from markupsafe import Markup
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
    _where = os.path.basename(_CONFIG_PATH)
    sys.exit(
        f'Papyrus will not start: server.secret_key in {_where} is empty or still the placeholder, and the '
        f'SECRET_KEY environment variable is not set.\n'
        f'The simplest fix: delete config.json and start again. Papyrus then writes a new one with a random key '
        f'(copying config.example.json by hand keeps the placeholder).\n'
        f'Or put your own long random value in config.json, for example the output of\n'
        f'    python3 -c "import secrets; print(secrets.token_hex(32))"\n'
        f'or pass it in the SECRET_KEY environment variable.'
    )
app.config['SECRET_KEY'] = _secret_key

# Reverse proxies: the proxy is expected to set X-Forwarded-For (the client's address) and
# X-Forwarded-Proto. Papyrus only believes them when the connection itself comes from one of
# these addresses (default: this machine), so a forged header can't dodge the rate limits
# if the app is ever reachable directly.
_TRUSTED_PEERS = [ipaddress.ip_network(str(entry), strict=False)
                  for entry in _server.get('trusted_proxy_ips', ['127.0.0.1', '::1'])]

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
# Werkzeug caps non-file form fields at 500 kB by default; `curl -F 'raw=<-'` sends the paste as one.
app.config['MAX_FORM_MEMORY_SIZE'] = MAX_PASTE_BYTES + 64 * 1024

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
        "img-src 'self'; "  # our own logo/favicon only; no user-supplied image can load
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

def _render_markdown_uncached(text):
    html = _markdown(text)
    return nh3.clean(
        html,
        tags=_MD_TAGS,
        attributes={'a': {'href', 'title'}, 'code': {'class'}},
        url_schemes={'http', 'https', 'mailto'},
        link_rel='noopener noreferrer nofollow',
        attribute_filter=_md_attribute_filter,
    )

_render_markdown_cached = functools.lru_cache(maxsize=1024)(_render_markdown_uncached)
_MARKDOWN_CACHE_LIMIT = 16 * 1024      # comments, notices and short pastes are cached; big documents are not

def render_markdown(text):
    """Render untrusted Markdown to sanitised HTML. Short texts are cached by their source, so a
    thousand of them cost about 16 MB at most; long ones are rendered each time and not kept."""
    if len(text) <= _MARKDOWN_CACHE_LIMIT:
        return _render_markdown_cached(text)
    return _render_markdown_uncached(text)

# Pastes in the Markdown language can be shown rendered, up to this size (same sanitising as comments).
MAX_PREVIEW_BYTES = 256 * 1024

# ── Jinja helpers ─────────────────────────────────────────────────────────────

def _reports_enabled():
    """Reports need somebody to read them, so they exist only while the admin panel is on."""
    return bool(_feat.get('reports', True)) and bool(CFG.get('admin', {}).get('enabled', False))

@app.context_processor
def inject_config():
    return {'cfg': CFG, 'csp_nonce': getattr(g, 'csp_nonce', ''),
            'reports_enabled': _reports_enabled(), 'is_admin': is_admin(),
            'palettes': _PALETTES, 'default_palette': _DEFAULT_PALETTE, 'theme_css_version': _THEME_CSS_VERSION,
            'site_notice': _SITE_NOTICE, 'site_footer': _SITE_FOOTER, 'source_url': SOURCE_URL,
            'version': __version__}

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

def _create_tables(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pastes (
            id                  TEXT PRIMARY KEY,
            title               TEXT NOT NULL DEFAULT 'Untitled',
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at          TEXT,
            views               INTEGER DEFAULT 0,
            removal_hash        TEXT NOT NULL DEFAULT '',   -- sha256 of the removal token, never the token
            discussions_enabled INTEGER DEFAULT 0
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS files (
            paste_id    TEXT NOT NULL,
            position    INTEGER NOT NULL,                 -- 0 is the first file
            filename    TEXT NOT NULL DEFAULT '',         -- unique within the paste (numbered if repeated)
            language    TEXT NOT NULL DEFAULT 'text',
            content     TEXT NOT NULL,
            PRIMARY KEY (paste_id, position)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pastes_removal ON pastes(removal_hash)')
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
    conn.execute('CREATE INDEX IF NOT EXISTS idx_comments_paste ON comments(paste_id)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id          TEXT PRIMARY KEY,
            paste_id    TEXT NOT NULL,
            comment_id  TEXT NOT NULL DEFAULT '',   -- '' = the paste itself (NULL would defeat UNIQUE)
            reason      TEXT NOT NULL,
            note        TEXT NOT NULL DEFAULT '',
            reporter    TEXT NOT NULL,              -- keyed hash of the reporter's address, never the address
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (paste_id, comment_id, reporter)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_reports_target ON reports(paste_id, comment_id)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS rate_limits (
            ip_address    TEXT,
            timestamp     REAL,
            PRIMARY KEY (ip_address, timestamp)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_ts ON rate_limits(timestamp)')

def _hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def _new_removal_token():
    return secrets.token_urlsafe(24)

SCHEMA_VERSION = 2   # stored in the database (PRAGMA user_version); bump it when the layout changes

def init_db():
    """Initialise the database. Thread-safe and idempotent: runs DDL only once per process."""
    global _db_initialized
    with _db_init_lock:
        if _db_initialized:
            return
        conn = sqlite3.connect(DATABASE)
        try:
            with conn:
                existing = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pastes'").fetchone()
                if existing and conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                    raise RuntimeError(
                        f'{DATABASE} was created by an earlier development version of Papyrus and its layout '
                        f'is out of date. There is no migration: delete or move that file and Papyrus will '
                        f'create a fresh one.')
                _create_tables(conn)
                conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
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
    conn.execute('DELETE FROM files WHERE paste_id = ?', (paste_id,))
    conn.execute('DELETE FROM reports WHERE paste_id = ?', (paste_id,))
    conn.execute('DELETE FROM comments WHERE paste_id = ?', (paste_id,))
    conn.execute('DELETE FROM pastes WHERE id = ?', (paste_id,))

def _delete_comment(conn, comment_id):
    """Remove one comment. Its replies are kept and move up to its parent."""
    row = conn.execute('SELECT parent_id FROM comments WHERE id = ?', (comment_id,)).fetchone()
    if row is None:
        return False
    conn.execute('UPDATE comments SET parent_id = ? WHERE parent_id = ?', (row['parent_id'], comment_id))
    conn.execute('DELETE FROM reports WHERE comment_id = ?', (comment_id,))
    conn.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    return True

def _is_trusted_peer(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in network for network in _TRUSTED_PEERS)

def _client_ip():
    """The visitor's address: X-Forwarded-For from a trusted proxy, else the socket peer.

    The last entry is used: it is the one the proxy itself added or set, so anything a
    visitor put in front of it is ignored.
    """
    return _resolve_client_ip(request.remote_addr or '', request.headers.get('X-Forwarded-For', ''))

def _resolve_client_ip(peer, forwarded_for):
    """The rule behind _client_ip(), on plain strings (the access log uses it too)."""
    if not _is_trusted_peer(peer):
        return peer
    last = forwarded_for.rsplit(',', 1)[-1].strip()
    try:
        return str(ipaddress.ip_address(last))
    except ValueError:
        return peer

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

def _rate_limit_count(conn, key, window):
    return conn.execute(
        'SELECT COUNT(*) FROM rate_limits WHERE ip_address = ? AND timestamp > ?',
        (key, time.time() - window)
    ).fetchone()[0]

def _is_rate_limited(remote_ip, key_prefix, window, limit):
    """Read-only check: has this IP already used up its budget?"""
    with _db() as conn:
        return _rate_limit_count(conn, f"{key_prefix}:{remote_ip}", window) >= limit

def _record_hit(remote_ip, key_prefix):
    with _db() as conn:
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

def _paste_files(paste_id):
    """The files of a paste, in order."""
    with _db() as conn:
        return conn.execute('SELECT position, filename, language, content FROM files WHERE paste_id = ?'
                            ' ORDER BY position', (paste_id,)).fetchall()

def _first_content(conn, paste_id):
    row = conn.execute('SELECT content FROM files WHERE paste_id = ? ORDER BY position LIMIT 1', (paste_id,)).fetchone()
    return row['content'] if row else ''

def _clean_title(title):
    if not isinstance(title, str):
        return 'Untitled'
    title = _CONTROL_CHARS_RE.sub('', title).strip()
    return title.encode('utf-8')[:MAX_TITLE_BYTES].decode('utf-8', 'ignore') or 'Untitled'

def _clean_language(language):
    if isinstance(language, str) and _LANGUAGE_RE.match(language):
        return language
    return _pastes.get('default_language', 'text')

MAX_FILENAME_LENGTH = 100

def _clean_filename(value):
    """A display name like 'deploy.sh': no directories, no control characters."""
    if not isinstance(value, str):
        return ''
    name = _CONTROL_CHARS_RE.sub('', value).replace('\\', '/').rsplit('/', 1)[-1].strip()
    return name[:MAX_FILENAME_LENGTH]

# File extension -> language. Only languages listed in config.json are ever returned.
_EXTENSION_LANGUAGES = {
    'js': 'javascript', 'mjs': 'javascript', 'cjs': 'javascript', 'jsx': 'javascript',
    'ts': 'typescript', 'tsx': 'typescript', 'py': 'python', 'pyw': 'python', 'java': 'java',
    'c': 'c', 'h': 'c', 'cpp': 'cpp', 'cc': 'cpp', 'cxx': 'cpp', 'hpp': 'cpp', 'hh': 'cpp',
    'cs': 'csharp', 'html': 'html', 'htm': 'html', 'css': 'css', 'scss': 'scss', 'sql': 'sql',
    'json': 'json', 'yml': 'yaml', 'yaml': 'yaml', 'xml': 'xml', 'svg': 'xml',
    'sh': 'bash', 'bash': 'bash', 'zsh': 'bash', 'ps1': 'powershell', 'php': 'php', 'rb': 'ruby',
    'go': 'go', 'rs': 'rust', 'swift': 'swift', 'kt': 'kotlin', 'kts': 'kotlin',
    'md': 'markdown', 'markdown': 'markdown', 'diff': 'diff', 'patch': 'diff',
    'toml': 'toml', 'ini': 'ini', 'cfg': 'ini', 'txt': 'text', 'log': 'text',
}
_FILENAME_LANGUAGES = {'dockerfile': 'docker', 'nginx.conf': 'nginx', '.bashrc': 'bash',
                       '.zshrc': 'bash', '.profile': 'bash'}

def _supported_languages():
    return {entry['value'] for entry in CFG.get('languages', [])}

def _detect_language(filename):
    """Language for a filename, or None when it isn't recognised."""
    name = (filename or '').lower()
    language = _FILENAME_LANGUAGES.get(name) or _EXTENSION_LANGUAGES.get(name.rsplit('.', 1)[-1] if '.' in name else '')
    return language if language in _supported_languages() else None

def _language_hints():
    """Detection tables for the editor page, restricted to the languages this site offers."""
    ok = _supported_languages()
    return {'extensions': {k: v for k, v in _EXTENSION_LANGUAGES.items() if v in ok},
            'names': {k: v for k, v in _FILENAME_LANGUAGES.items() if v in ok}}

def _human_size(n):
    for unit in ('B', 'KB', 'MB'):
        if n < 1024 or unit == 'MB':
            return f'{n} B' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024

# Expiry choices are configured in config.json as a map from a name to a duration, like pinnwand:
#   "expiries": {"1hour": 3600, "3days": "3d", "forever": null}
# A duration is a number of seconds, a short form ("30m", "36h", "3d", "2w", "1y"), or null for never.
# A choice can also be {"duration": ..., "label": "Shown in the menu"}; labels default to the duration.
_DURATION_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 7 * 86400, 'y': 365 * 86400}
_DURATION_RE    = re.compile(r'^\s*(\d+)\s*([smhdwy])\s*$', re.I)
_OPTION_VALUE_RE = re.compile(r'^[a-z0-9_-]{1,32}$')
_LABEL_UNITS = [('year', 365 * 86400), ('month', 30 * 86400), ('week', 7 * 86400), ('day', 86400),
                ('hour', 3600), ('minute', 60), ('second', 1)]

def _parse_duration(value):
    """A number of seconds, '10m', '36h', '3d', '2w', '1y' -> timedelta; None or 'never' -> None."""
    if value is None or value == 'never':
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return datetime.timedelta(seconds=value)
        match = _DURATION_RE.match(value) if isinstance(value, str) else None
        if match and int(match.group(1)) > 0:
            return datetime.timedelta(seconds=int(match.group(1)) * _DURATION_UNITS[match.group(2).lower()])
    except OverflowError:
        raise ValueError(f'{value!r} is too long: use null for an expiry that never comes') from None
    raise ValueError(f'bad duration {value!r}: use a number of seconds, a short form like "30m", "3d" or '
                     f'"2w" (units s, m, h, d, w, y), or null for no expiry')

def _humanize(delta):
    """'1 hour', '3 days', '90 minutes': the largest unit that divides the duration evenly."""
    if delta is None:
        return 'Never'
    seconds = int(delta.total_seconds())
    for unit, size in _LABEL_UNITS:
        if seconds % size == 0:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f'{seconds} seconds'

def _load_expiry_options(pastes_cfg):
    """The configured expiry choices as [{'value', 'label', 'delta'}], plus the default value.

    Raises RuntimeError for a broken configuration, so a typo fails at startup instead of
    quietly meaning "never expires".
    """
    configured = pastes_cfg.get('expiries')
    if configured is None:
        old = ' (allow_expiry_options and expiry_labels are no longer used)' \
            if any(k in pastes_cfg for k in ('allow_expiry_options', 'expiry_labels')) else ''
        raise RuntimeError(f'pastes.expiries is missing{old}: list the choices as a map, for example '
                           f'{{"1day": 86400, "1week": "7d"}}')
    if not isinstance(configured, dict) or not configured:
        raise RuntimeError('pastes.expiries must be a map with at least one entry, for example {"1day": 86400}')
    options = []
    for value, spec in configured.items():
        if not _OPTION_VALUE_RE.match(value):
            raise RuntimeError(f"pastes.expiries: the name '{value}' must be lowercase letters, digits, '-' or "
                               f"'_' (up to 32 characters)")
        label = None
        if isinstance(spec, dict):
            if 'duration' not in spec:
                raise RuntimeError(f"pastes.expiries '{value}': an object needs a \"duration\" (null for never)")
            label, spec = spec.get('label'), spec['duration']
        try:
            delta = _parse_duration(spec)
        except ValueError as e:
            raise RuntimeError(f"pastes.expiries '{value}': {e}") from None
        options.append({'value': value, 'label': str(label) if label else _humanize(delta), 'delta': delta})
    default = pastes_cfg.get('default_expiry', options[0]['value'])
    if default not in {o['value'] for o in options}:
        raise RuntimeError(f"pastes.default_expiry '{default}' is not one of: {', '.join(o['value'] for o in options)}")
    return options, default

_EXPIRY_OPTIONS, _DEFAULT_EXPIRY = _load_expiry_options(_pastes)

def _expiry_option(choice):
    """The option for a submitted value; empty means the default, anything else unknown is None."""
    wanted = _DEFAULT_EXPIRY if choice in (None, '') else choice
    return next((o for o in _EXPIRY_OPTIONS if o['value'] == wanted), None)

def _expires_at(option):
    """ISO timestamp for an option, or None when it never expires."""
    if option['delta'] is None:
        return None
    try:
        return (datetime.datetime.now(_UTC) + option['delta']).isoformat()
    except OverflowError:                            # so long that it is effectively "never"
        return None

# ── Colour themes ─────────────────────────────────────────────────────────────
# config.json: theme.palettes maps a name to {"label", "logo_filter" (optional), "light": {...}, "dark": {...}}.
# The server turns them into one stylesheet (/theme.css); the page picks a palette through
# <html data-palette="..."> and light or dark through data-theme, so there is no flash of other colours.
_THEME_COLOURS = {     # key in config.json -> CSS variable
    'primary': '--primary', 'primary_hover': '--primary-h', 'on_primary': '--on-primary',
    'background': '--bg', 'surface': '--surface', 'border': '--border',
    'text': '--text', 'text_secondary': '--text-sub', 'text_muted': '--text-muted', 'danger': '--danger',
}
_HEX_COLOUR_RE   = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')
_PALETTE_NAME_RE = re.compile(r'^[a-z0-9-]{1,32}$')
_LOGO_FILTER_RE  = re.compile(r'^[a-z0-9(),. -]{1,80}$')

def _load_palettes(theme_cfg):
    """The configured palettes as [{'value', 'label', 'logo_filter', 'light', 'dark'}], plus the default.

    Raises RuntimeError for a broken definition, so a typo fails at startup.
    """
    configured = theme_cfg.get('palettes')
    if not isinstance(configured, dict) or not configured:
        raise RuntimeError('theme.palettes must be a map with at least one palette; see config.example.json')
    palettes = []
    for name, spec in configured.items():
        if not _PALETTE_NAME_RE.match(name):
            raise RuntimeError(f"theme.palettes: the name '{name}' must be lowercase letters, digits or '-' "
                               f"(up to 32 characters)")
        if not isinstance(spec, dict):
            raise RuntimeError(f"theme.palettes '{name}' must be a map with \"light\" and \"dark\" colours")
        modes = {}
        for mode in ('light', 'dark'):
            colours = spec.get(mode)
            if not isinstance(colours, dict):
                raise RuntimeError(f"theme.palettes '{name}' needs a \"{mode}\" map of colours")
            missing = [key for key in _THEME_COLOURS if key not in colours]
            if missing:
                raise RuntimeError(f"theme.palettes '{name}' {mode}: missing {', '.join(missing)}")
            for key in _THEME_COLOURS:
                if not isinstance(colours[key], str) or not _HEX_COLOUR_RE.match(colours[key]):
                    raise RuntimeError(f"theme.palettes '{name}' {mode}: '{key}' must be a hex colour like "
                                       f"#c2410c, not {colours[key]!r}")
            modes[mode] = {key: colours[key] for key in _THEME_COLOURS}
        logo_filter = spec.get('logo_filter') or 'none'
        if not isinstance(logo_filter, str) or not _LOGO_FILTER_RE.match(logo_filter):
            raise RuntimeError(f"theme.palettes '{name}': logo_filter must be a CSS filter such as "
                               f"\"hue-rotate(120deg)\" or \"grayscale(1)\"")
        palettes.append({'value': name, 'label': str(spec.get('label') or name), 'logo_filter': logo_filter, **modes})
    default = theme_cfg.get('palette', palettes[0]['value'])
    if default not in {p['value'] for p in palettes}:
        raise RuntimeError(f"theme.palette '{default}' is not one of: {', '.join(p['value'] for p in palettes)}")
    return palettes, default

def _build_theme_css(palettes):
    blocks = []
    for pal in palettes:
        for mode in ('light', 'dark'):
            c = pal[mode]
            selector = f'[data-palette="{pal["value"]}"]' + ('[data-theme="dark"]' if mode == 'dark' else '')
            lines = [f'    {var}: {c[key]};' for key, var in _THEME_COLOURS.items()]
            lines += [f"    --code-bg: {c['surface'] if mode == 'light' else c['background']};",
                      f"    --nav-bg: {c['background'] if mode == 'light' else c['surface']};",
                      f"    --nav-border: {c['border']};",
                      f'    color-scheme: {mode};']              # native controls and scrollbars follow
            if mode == 'light':
                lines.append(f"    --logo-filter: {pal['logo_filter']};")
            blocks.append(selector + ' {\n' + '\n'.join(lines) + '\n}')
    return '\n'.join(blocks) + '\n'

_PALETTES, _DEFAULT_PALETTE = _load_palettes(_theme)
_THEME_CSS = _build_theme_css(_PALETTES)
_THEME_CSS_VERSION = hashlib.sha256(_THEME_CSS.encode()).hexdigest()[:10]

# ── Site notice and footer ───────────────────────────────────────────────────
# site.notice (a banner under the top bar) and site.footer (the bar at the bottom of every page) are
# inline Markdown from config.json: links and emphasis work, raw HTML is escaped, like comments.
_MAX_SITE_TEXT = 1000

def _render_site_text(key):
    text = _site.get(key) or ''
    if not isinstance(text, str) or len(text) > _MAX_SITE_TEXT:
        raise RuntimeError(f'site.{key} must be text of at most {_MAX_SITE_TEXT} characters')
    text = text.strip()
    if not text:
        return Markup('')
    html = render_markdown(text)
    single = re.fullmatch(r'<p>(.*)</p>\s*', html, re.S)            # one paragraph: render it inline
    return Markup(single.group(1) if single and '<p>' not in single.group(1) else html)

# The footer always links to the source, whatever site.footer says (or doesn't).
SOURCE_URL = 'https://github.com/TehPeGaSuS/papyrus'

_SITE_NOTICE = _render_site_text('notice')
_SITE_FOOTER = _render_site_text('footer')

# Prism language ids differ from ours for a few languages; False = no highlighting.
_PRISM_LANG_MAP = {'text': False, 'html': 'markup', 'xml': 'markup'}

def _prism_language(language):
    return _PRISM_LANG_MAP.get(language, language)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', language_hints=_language_hints(), max_paste_bytes=MAX_PASTE_BYTES,
                           expiry_options=_EXPIRY_OPTIONS, default_expiry=_DEFAULT_EXPIRY, max_files=MAX_FILES)

@app.route('/theme.css')
def theme_css():
    response = Response(_THEME_CSS, mimetype='text/css')
    response.headers['Cache-Control'] = 'public, max-age=86400'      # pages link it as /theme.css?v=<hash>
    return response

@app.route('/favicon.ico')
def favicon():
    # Browsers and crawlers ask for this path directly, whatever the <link> tags say.
    return app.send_static_file('favicon.ico')

class _PasteError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.message, self.status = message, status

MAX_FILES = max(1, min(int(_pastes.get('max_files', 10)), 50))     # pastes.max_files; more than 50 is silly

def _unique_filename(name, taken):
    """'a.py' -> 'a-2.py', 'a-3.py' ... so every file in a paste has its own raw URL."""
    if name not in taken:
        return name
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        suffix = f'-{n}'
        candidate = stem[:MAX_FILENAME_LENGTH - len(ext) - len(suffix)] + suffix + ext
        if candidate not in taken:
            return candidate
        n += 1

def _clean_files(data):
    """The submitted files as [{'filename', 'language', 'content'}], validated, in order.

    Either {"files": [{content, filename, language}, ...]} or the single-file fields
    content/filename/language at the top level.
    """
    submitted = data.get('files')
    if submitted is None:
        submitted = [{'content': data.get('content'), 'filename': data.get('filename'), 'language': data.get('language')}]
    if not isinstance(submitted, list) or not submitted:
        raise _PasteError('files must be a non-empty list', 400)
    if len(submitted) > MAX_FILES:
        raise _PasteError(f'A paste holds at most {MAX_FILES} files', 413)
    many = len(submitted) > 1
    cleaned, taken, total = [], set(), 0
    for number, item in enumerate(submitted, 1):
        if not isinstance(item, dict):
            raise _PasteError(f'File {number} must be an object', 400)
        given = _clean_filename(item.get('filename'))
        where = f'File {number}{f" ({given})" if given else ""}: ' if many else ''
        content = item.get('content')
        if not content or not isinstance(content, str) or not content.strip():
            raise _PasteError(f'{where}content must be a non-empty string', 400)
        if '\x00' in content:  # images and other binary formats are full of NUL bytes
            raise _PasteError(f'{where}Only text is accepted', 415)
        total += len(content.encode('utf-8'))
        if total > MAX_PASTE_BYTES:
            raise _PasteError('Paste too large', 413)
        name = _unique_filename(given or (f'file-{number}' if many else ''), taken)
        taken.add(name)
        requested = item.get('language')
        if isinstance(requested, str) and requested not in ('', 'auto'):
            language = _clean_language(requested)         # an explicit choice always wins
        else:
            language = _detect_language(name if given else '') or _pastes.get('default_language', 'text')
        cleaned.append({'filename': name, 'language': language, 'content': content, 'named': bool(given)})
    return cleaned

def _store_paste(data):
    """Validate a new paste (one file or several) and store it: (paste_id, token, expires_at, files)."""
    files = _clean_files(data)
    raw_title = data.get('title')
    title = _clean_title(raw_title)
    if not (isinstance(raw_title, str) and raw_title.strip()):
        first = files[0]['filename'] if files[0]['named'] else ''
        title = first or 'Untitled'                   # no description given: show the first filename
    discussions = bool(data.get('discussions', False))
    expiry = _expiry_option(data.get('expires_in'))
    if expiry is None:
        raise _PasteError(f"Unknown expiry {str(data.get('expires_in'))[:40]!r}. Use one of: "
                          f"{', '.join(o['value'] for o in _EXPIRY_OPTIONS)}", 400)
    expires_at = _expires_at(expiry)

    token = _new_removal_token()
    with _db() as conn:
        for _ in range(5):
            paste_id = generate_paste_id()
            try:
                conn.execute(
                    'INSERT INTO pastes (id, title, expires_at, removal_hash, discussions_enabled)'
                    ' VALUES (?, ?, ?, ?, ?)',
                    (paste_id, title, expires_at, _hash_token(token), int(discussions)))
            except sqlite3.IntegrityError:
                continue
            conn.executemany(
                'INSERT INTO files (paste_id, position, filename, language, content) VALUES (?, ?, ?, ?, ?)',
                [(paste_id, i, f['filename'], f['language'], f['content']) for i, f in enumerate(files)])
            conn.commit()
            return paste_id, token, expires_at, files
    raise _PasteError('Service temporarily unavailable', 503)

def _file_raw_path(paste_id, index, filename):
    """The first file is /raw/<id>; every file is also reachable by name at /raw/<id>/<filename>."""
    return f'/raw/{paste_id}' if index == 0 else f'/raw/{paste_id}/{quote(filename, safe="")}'

def _text_response(body, status=200):
    return Response(body, status=status, mimetype='text/plain')

def _creation_summary(paste_id, token, files):
    """What `curl` prints: the paste, the raw text of each file, and the link that removes it."""
    base = request.host_url.rstrip('/')
    lines = [f'Paste URL:   {base}/{paste_id}']
    if len(files) == 1:
        lines.append(f'Raw URL:     {base}{_file_raw_path(paste_id, 0, files[0]["filename"])}')
    else:
        lines += [f'Raw URL ({f["filename"]}): {base}{_file_raw_path(paste_id, i, f["filename"])}'
                  for i, f in enumerate(files)]
    lines.append(f'Removal URL: {base}/remove/{token}')
    return '\n'.join(lines) + '\n'

def _set_removal_cookie(response, paste_id, token, expires_at):
    """Remember in this browser that it made the paste, so the page keeps offering "Remove now"
    (across refreshes, closed tabs and restarts) until the paste expires."""
    if expires_at:
        seconds = max(60, int((_parse_expiry(expires_at) - datetime.datetime.now(_UTC)).total_seconds()))
    else:
        seconds = 365 * 86400
    response.set_cookie('removal', token, max_age=seconds, path=f'/{paste_id}', httponly=True,
                        samesite='Lax', secure=bool(_server.get('use_https', False)))

def _flag(value):
    return value in (True, 1, '1', 'true', 'yes', 'on')

@app.route('/create', methods=['POST'])
def create_paste():
    """The web UI posts JSON; `curl --data-binary @file 'https://host/create?filename=x.py'` posts text."""
    plain_text = request.mimetype == 'text/plain'
    def fail(message, status):
        return _text_response(message + '\n', status) if plain_text else (jsonify({'error': message}), status)

    # 10 pastes per 10 minutes per IP address (SQLite backed for worker safety)
    if not _check_rate_limit(_client_ip(), key_prefix='create', limit=10):
        return fail('Rate limit exceeded. Please wait a few minutes.', 429)
    if plain_text:
        try:
            body = request.get_data().decode('utf-8')   # strict: binary files are not text
        except UnicodeDecodeError:
            return fail('Only UTF-8 text is accepted', 415)
        data = {'content': body, **{k: request.args.get(k) for k in ('title', 'filename', 'language', 'expires_in')},
                'discussions': _flag(request.args.get('discussions'))}
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return fail('JSON body required', 400)
    try:
        paste_id, token, expires_at, files = _store_paste(data)
    except _PasteError as e:
        return fail(e.message, e.status)
    if plain_text:
        return _text_response(_creation_summary(paste_id, token, files))
    response = jsonify({'paste_id': paste_id, 'url': f'/{paste_id}',
                        'removal_token': token, 'removal_url': f'/remove/{token}'})
    _set_removal_cookie(response, paste_id, token, expires_at)
    return response

@app.route('/api')
def api_docs():
    """How to post pastes from scripts: curl, plain text, JSON. The examples use this site's own address."""
    return render_template('api.html', base=url_for('index', _external=True).rstrip('/'), max_files=MAX_FILES,
                           max_bytes=MAX_PASTE_BYTES, max_size=_human_size(MAX_PASTE_BYTES),
                           expiries=_EXPIRY_OPTIONS, default_expiry=_DEFAULT_EXPIRY,
                           example_expiry=next((o['value'] for o in _EXPIRY_OPTIONS if o['value'] != _DEFAULT_EXPIRY), _DEFAULT_EXPIRY))

@app.route('/curl', methods=['POST'])
def create_paste_curl():
    """`echo hi | curl -X POST https://host/curl -F 'raw=<-'`, plus optional filename, title, language,
    expires_in and discussions fields. Prints the paste, raw and removal URLs."""
    if not _check_rate_limit(_client_ip(), key_prefix='create', limit=10):
        return _text_response('Rate limit exceeded. Please wait a few minutes.\n', 429)
    form = request.form
    raws, names, languages = form.getlist('raw'), form.getlist('filename'), form.getlist('language')
    # Repeat the fields for more files: -F 'raw=<a.py' -F filename=a.py -F 'raw=<b.sh' -F filename=b.sh
    files = [{'content': raw, 'filename': names[i] if i < len(names) else '',
              'language': languages[i] if i < len(languages) else None} for i, raw in enumerate(raws)]
    data = {'files': files or None, 'content': None, 'title': form.get('title'),
            'expires_in': form.get('expires_in'), 'discussions': _flag(form.get('discussions'))}
    try:
        paste_id, token, _, stored = _store_paste(data)
    except _PasteError as e:
        return _text_response(e.message + '\n', e.status)
    return _text_response(_creation_summary(paste_id, token, stored))

# ── Removing a paste ──────────────────────────────────────────────────────────

def _paste_for_removal(conn, token):
    if not token or len(token) > 200:
        return None
    return conn.execute("SELECT * FROM pastes WHERE removal_hash = ? AND removal_hash != ''",
                        (_hash_token(token),)).fetchone()

@app.route('/remove/<string:token>', methods=['GET', 'POST'])
def remove_paste(token):
    """The removal link: opening it removes the paste at once, as in PrivateBin and pinnwand (POST works too,
    for scripts). The token is the only credential. HEAD only checks: probing bots must not delete."""
    if _is_rate_limited(_client_ip(), 'remove', 600, 30):      # only misses count: tokens can't be guessed
        abort(429, 'Too many attempts. Please wait a few minutes.')
    with _db() as conn:
        paste = _paste_for_removal(conn, token)
        if paste is not None and _is_expired(paste['expires_at']):
            _delete_paste(conn, paste['id'])
            conn.commit()
            paste = None
        if paste is None:
            _record_hit(_client_ip(), 'remove')
            abort(404)
        if request.method == 'HEAD':
            return '', 200
        _delete_paste(conn, paste['id'])
        conn.commit()
        return render_template('removed.html', title=paste['title'])

@app.route('/<string:paste_id>')
def view_paste(paste_id):
    paste = _get_paste_or_abort(paste_id)
    rows = []
    with _db() as conn:
        if not is_admin():
            conn.execute('UPDATE pastes SET views = views + 1 WHERE id = ?', (paste_id,))
        if paste['discussions_enabled']:
            rows = conn.execute(
                'SELECT id, parent_id, nick, content, created_at FROM comments WHERE paste_id = ?'
                ' ORDER BY created_at ASC, rowid ASC', (paste_id,)).fetchall()
        conn.commit()
    files = []
    for f in _paste_files(paste_id):
        content = f['content']
        previewable = f['language'] == 'markdown' and len(content.encode('utf-8')) <= MAX_PREVIEW_BYTES
        files.append({
            'index': f['position'], 'filename': f['filename'], 'language': f['language'], 'content': content,
            'prism_lang': _prism_language(f['language']),
            'lines': content.count('\n') + (0 if content.endswith('\n') else 1),
            'size': _human_size(len(content.encode('utf-8'))),
            'preview_html': Markup(render_markdown(content)) if previewable else None,
            'raw_path': _file_raw_path(paste_id, f['position'], f['filename']),
        })
    highlight = request.args.get('f', '')
    cookie = request.cookies.get('removal', '')
    owner = bool(cookie) and len(cookie) < 200 and bool(paste['removal_hash']) \
        and hmac.compare_digest(_hash_token(cookie), paste['removal_hash'])
    return render_template('view.html', paste=paste, comments=_comment_tree(rows),
                           removal_token=cookie if owner else None,
                           comment_count=len(rows), files=files,
                           show_preview=request.args.get('view') == 'preview',
                           highlight_file=int(highlight) if highlight.isdigit() and 0 < int(highlight) <= len(files) else 1)

@app.route('/raw/<string:paste_id>')
def view_paste_raw(paste_id):
    _get_paste_or_abort(paste_id)
    return Response(_paste_files(paste_id)[0]['content'], mimetype='text/plain; charset=utf-8')

@app.route('/raw/<string:paste_id>/<path:filename>')
def view_paste_raw_file(paste_id, filename):
    _get_paste_or_abort(paste_id)
    for f in _paste_files(paste_id):
        if f['filename'] == filename:
            return Response(f['content'], mimetype='text/plain; charset=utf-8')
    abort(404)

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
        },
        'theme': {
            'default':          _theme.get('default', 'auto'),
            'allow_user_toggle': _theme.get('allow_user_toggle', True),
            'palette':          _DEFAULT_PALETTE,
            'palettes':         [{'value': p['value'], 'label': p['label']} for p in _PALETTES],
        },
        'features': {
            'show_recent':      _feat.get('show_recent', False),
            'show_view_count':  _feat.get('show_view_count', True),
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
            'default_expiry':       _DEFAULT_EXPIRY,
            'expiry_options':       [{'value': o['value'], 'label': o['label']} for o in _EXPIRY_OPTIONS],
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
            SELECT p.id, p.title, p.created_at, p.expires_at, p.views,
                   (SELECT language FROM files f WHERE f.paste_id = p.id ORDER BY position LIMIT 1) AS language,
                   (SELECT COUNT(*) FROM files f WHERE f.paste_id = p.id) AS file_count
            FROM pastes p
            WHERE p.expires_at IS NULL OR p.expires_at > ?
            ORDER BY p.created_at DESC LIMIT ?
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
    if not _check_rate_limit(_client_ip(), key_prefix='comment', window=600, limit=20):
        abort(429, 'Rate limit exceeded. Please wait a few minutes.')

    content = (request.form.get('content') or '').strip()
    if not content:
        abort(400, 'A comment cannot be empty.')
    if len(content.encode('utf-8')) > _MAX_COMMENT_BYTES:
        abort(413, 'Comment too large.')
    if '\x00' in content:
        abort(415, 'Only text is accepted.')
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
    if not _check_rate_limit(_client_ip(), key_prefix='preview', window=600, limit=120):
        return jsonify({'error': 'Rate limit exceeded.'}), 429
    data = request.get_json(silent=True) or {}
    text = data.get('text')
    if not isinstance(text, str) or len(text.encode('utf-8')) > _MAX_COMMENT_BYTES:
        return jsonify({'error': 'text must be a string under 10 KB'}), 400
    return jsonify({'html': render_markdown(text)})

# ── Reports ───────────────────────────────────────────────────────────────────

_REPORT_REASONS = {
    'spam':    'Spam or advertising',
    'abuse':   'Harassment or abuse',
    'illegal': 'Illegal content',
    'malware': 'Malware or phishing',
    'other':   'Something else',
}
_MAX_REPORT_NOTE   = 500
_REPORT_RATE_LIMIT = 10   # reports per 10 minutes per visitor

def _reporter_hash(ip):
    """Identify a reporter for de-duplication without storing their address."""
    return hmac.new(app.config['SECRET_KEY'].encode(), ip.encode(), 'sha256').hexdigest()[:32]

def _snippet(text, limit=140):
    flat = ' '.join((text or '').split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + '\u2026'

@app.route('/<string:paste_id>/report', methods=['GET', 'POST'])
def report(paste_id):
    if not _reports_enabled():
        abort(404)
    paste = _get_paste_or_abort(paste_id)
    comment_id = (request.values.get('comment') or '').strip()
    comment = None
    if comment_id:
        with _db() as conn:
            comment = conn.execute('SELECT id, nick, content FROM comments WHERE id = ? AND paste_id = ?',
                                   (comment_id, paste_id)).fetchone()
        if comment is None:
            abort(404)
    first = _paste_files(paste_id)[0]['content']
    if request.method == 'GET':
        return render_template('report.html', paste=paste, comment=comment, reasons=_REPORT_REASONS,
                               snippet=_snippet(comment['content'] if comment else first, 300))

    if not _is_same_origin():
        abort(403, 'Cross-origin request blocked.')
    if not _check_rate_limit(_client_ip(), key_prefix='report', window=600, limit=_REPORT_RATE_LIMIT):
        abort(429, 'Too many reports. Please wait a few minutes.')
    reason = request.form.get('reason')
    if reason not in _REPORT_REASONS:
        abort(400, 'Choose a reason for the report.')
    note = _CONTROL_CHARS_RE.sub('', request.form.get('note') or '').strip()[:_MAX_REPORT_NOTE]
    with _db() as conn:
        # One report per visitor and target; repeating it is silently accepted.
        conn.execute('INSERT OR IGNORE INTO reports (id, paste_id, comment_id, reason, note, reporter)'
                     ' VALUES (?, ?, ?, ?, ?, ?)',
                     (secrets.token_hex(8), paste_id, comment_id, reason, note, _reporter_hash(_client_ip())))
        conn.commit()
    return redirect(url_for('view_paste', paste_id=paste_id, reported=1), code=303)

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
        if _is_rate_limited(_client_ip(), 'admin_login', _ADMIN_LOGIN_WINDOW, _ADMIN_LOGIN_LIMIT):
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
        _record_hit(_client_ip(), 'admin_login')
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
            'SELECT p.id, p.title, p.created_at, p.expires_at, p.views,'
            ' (SELECT language FROM files f WHERE f.paste_id = p.id ORDER BY position LIMIT 1) AS language,'
            ' (SELECT COUNT(*) FROM files f WHERE f.paste_id = p.id) AS file_count,'
            ' (SELECT COUNT(*) FROM comments c WHERE c.paste_id = p.id) AS comments,'
            ' (SELECT COUNT(*) FROM reports r WHERE r.paste_id = p.id) AS reports'
            ' FROM pastes p ORDER BY p.created_at DESC'
        ).fetchall()
        reports = _report_queue(conn)
    return render_template('admin_dashboard.html', pastes=pastes, reports=reports,
                           reasons=_REPORT_REASONS)

def _report_queue(conn, limit=200):
    """Reported pastes/comments, most-reported first, with what an admin needs to judge them."""
    queue = []
    groups = conn.execute(
        'SELECT paste_id, comment_id, COUNT(*) AS n, MAX(created_at) AS last FROM reports'
        ' GROUP BY paste_id, comment_id ORDER BY n DESC, last DESC LIMIT ?', (limit,)).fetchall()
    for g_ in groups:
        paste = conn.execute('SELECT id, title FROM pastes WHERE id = ?', (g_['paste_id'],)).fetchone()
        if paste is None:
            continue
        comment = None
        if g_['comment_id']:
            comment = conn.execute('SELECT id, nick, content FROM comments WHERE id = ?',
                                   (g_['comment_id'],)).fetchone()
            if comment is None:
                continue
        where = (g_['paste_id'], g_['comment_id'])
        queue.append({
            'paste_id': paste['id'], 'comment_id': g_['comment_id'], 'count': g_['n'], 'last': g_['last'],
            'title': paste['title'], 'nick': comment['nick'] if comment else '',
            'snippet': _snippet(comment['content'] if comment else _first_content(conn, paste['id'])),
            'reasons': conn.execute('SELECT reason, COUNT(*) AS n FROM reports WHERE paste_id = ? AND comment_id = ?'
                                    ' GROUP BY reason ORDER BY n DESC', where).fetchall(),
            'notes': [r['note'] for r in conn.execute(
                "SELECT note FROM reports WHERE paste_id = ? AND comment_id = ? AND note != ''"
                ' ORDER BY created_at DESC LIMIT 3', where)],
        })
    return queue

@app.route('/admin/reports/dismiss', methods=['POST'])
def admin_dismiss_reports():
    if not is_admin():
        abort(403)
    if not _is_same_origin():
        abort(403)
    with _db() as conn:
        conn.execute('DELETE FROM reports WHERE paste_id = ? AND comment_id = ?',
                     (request.form.get('paste_id', ''), request.form.get('comment_id', '')))
        conn.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/comments/<string:comment_id>/delete', methods=['POST'])
def admin_delete_comment(comment_id):
    if not is_admin():
        abort(403)
    if not _is_same_origin():
        abort(403)
    with _db() as conn:
        row = conn.execute('SELECT paste_id FROM comments WHERE id = ?', (comment_id,)).fetchone()
        if row:
            _delete_comment(conn, comment_id)
            conn.commit()
    if row and request.form.get('return') == 'paste':
        return redirect(url_for('view_paste', paste_id=row['paste_id']) + '#discussionsPanel')
    return redirect(url_for('admin_dashboard'))

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


def _access_logger_class():
    """Gunicorn's access log shows the socket peer, which behind a proxy is always the proxy (127.0.0.1).
    Log the visitor's address instead, resolved by the same rule as the rate limits: X-Forwarded-For, but
    only from a trusted proxy, so a forged header from anyone else is never logged as the address."""
    from gunicorn.glogging import Logger

    class ProxyAwareLogger(Logger):
        def atoms(self, resp, req, environ, request_time):
            atoms = super().atoms(resp, req, environ, request_time)
            atoms['h'] = _resolve_client_ip(environ.get('REMOTE_ADDR', ''), environ.get('HTTP_X_FORWARDED_FOR', '')) or '-'
            return atoms

    return ProxyAwareLogger


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
        'graceful_timeout':   8,      # on stop, finish running requests, but don't wait for idle connections
        'keepalive':          5,
        'accesslog':          '-',
        'logger_class':       _access_logger_class(),
        'errorlog':           '-',
        'loglevel':           'info',
        'post_fork':          _post_fork,
        # Newer Gunicorn opens a control socket at one path shared by every instance of
        # the same user; give each Papyrus its own (0600, per port) so they can't collide.
        'control_socket':     _control_socket_path(port),
        # X-Forwarded-Proto (set by the proxy) decides whether a request counts as HTTPS,
        # but only from the same trusted proxies as X-Forwarded-For.
        'forwarded_allow_ips': ','.join(str(network) for network in _TRUSTED_PEERS),
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


_STOP_GRACE_SECONDS = 12   # a little longer than Gunicorn's graceful_timeout

def cmd_stop():
    pid = _read_pid()
    if not pid or not _alive(pid):
        print('Not running')
        if pid:
            os.remove(_PID_FILE)
        return
    def wait_for_exit(seconds):
        for _ in range(int(seconds * 10)):
            time.sleep(0.1)
            if not _alive(pid):
                return True
        return False

    os.kill(pid, signal.SIGTERM)  # Gunicorn: graceful shutdown (running requests finish, within graceful_timeout)
    if not wait_for_exit(_STOP_GRACE_SECONDS):
        print('Still draining connections, forcing the shutdown ...', file=sys.stderr)
        os.kill(pid, signal.SIGQUIT)  # Gunicorn: quick shutdown
        if not wait_for_exit(5):
            sys.exit(f'PID {pid} did not exit; stop it manually (kill -9 {pid}).')
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
    parser.add_argument('command', nargs='?', choices=['start', 'stop', 'restart', 'status', 'admin'],
                        help='start (the default), stop, restart, status, or admin (set the admin password)')
    parser.add_argument('--start', action='store_true',
                        help='start the server (same as the start command); the very first run, which only sets '
                             'things up, starts it too')
    parser.add_argument('--host', help='address to bind (default: server.host in config.json)')
    parser.add_argument('--port', type=int, help='port to bind (default: server.port)')
    parser.add_argument('--workers', type=int, help='worker processes (default: server.workers or CPUs+1)')
    parser.add_argument('-d', '--daemon', action='store_true', help='start in the background')
    parser.add_argument('--dev', action='store_true', help='use the Flask development server')
    parser.add_argument('--user', default='admin', help='admin username (with the admin command)')
    parser.add_argument('--version', action='version', version=f'Papyrus {__version__}')
    parser.add_argument('--pid', help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _first_run_notice():
    return ('Papyrus is set up: .venv, its dependencies and config.json (with a random secret_key).\n'
            'The server is not running yet. Next:\n'
            '  1. Look through config.json (site name, expiry times, port, footer, ...)\n'
            '  2. python3 app.py admin     optional: set the admin password and enable /admin\n'
            '  3. python3 app.py --start   start it (add -d to run in the background)')

def main(argv=None):
    args = _parse_args(argv)
    explicit = args.start or args.command == 'start'
    args.command = args.command or 'start'
    if args.command == 'start' and _FIRST_RUN and not explicit and not os.environ.get('SECRET_KEY'):
        # The very first run only prepares things, so you can adjust the config before anything listens.
        # (`start` or `--start` says "go ahead"; so does a SECRET_KEY in the environment, e.g. in a container.)
        print(_first_run_notice())
        return
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
