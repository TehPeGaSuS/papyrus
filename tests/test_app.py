import datetime
import json
import os
import re
import sqlite3
import sys

import pytest
from werkzeug.security import generate_password_hash

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as papyrus  # noqa: E402

UTC = datetime.timezone.utc
ORIGIN = {'Origin': 'http://localhost'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(papyrus, 'DATABASE', str(tmp_path / 'test.db'))
    monkeypatch.setattr(papyrus, '_db_initialized', False)
    monkeypatch.setattr(papyrus, '_cleanup_running', True)  # no background thread
    monkeypatch.setitem(papyrus.CFG, 'admin', {
        'enabled': True, 'user': 'admin', 'pass': generate_password_hash('hunter2'),
    })
    papyrus.init_db()
    papyrus.app.config['TESTING'] = True
    return papyrus.app.test_client()


def create(client, **extra):
    body = {'content': 'hello', 'expires_in': '1hour', **extra}
    r = client.post('/create', json=body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def set_expiries(monkeypatch, expiries, default=None):
    """Use these expiry choices (same format as pastes.expiries in config.json) for one test."""
    cfg = {'expiries': expiries}
    if default:
        cfg['default_expiry'] = default
    options, chosen = papyrus._load_expiry_options(cfg)
    monkeypatch.setattr(papyrus, '_EXPIRY_OPTIONS', options)
    monkeypatch.setattr(papyrus, '_DEFAULT_EXPIRY', chosen)


def curl_urls(response):
    """Parse the three lines `curl` prints: {'Paste URL': ..., 'Raw URL': ..., 'Removal URL': ...}."""
    lines = response.get_data(as_text=True).strip().splitlines()
    assert [l.split(':', 1)[0] for l in lines] == ['Paste URL', 'Raw URL', 'Removal URL'], lines
    return {l.split(':', 1)[0]: l.split(':', 1)[1].strip() for l in lines}


def db():
    conn = sqlite3.connect(papyrus.DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# ── IDs ───────────────────────────────────────────────────────────────────────

def test_paste_ids_are_long_hex():
    for _ in range(50):
        pid = papyrus.generate_paste_id()
        assert len(pid) >= papyrus.MIN_ID_LENGTH
        assert papyrus._PASTE_ID_RE.match(pid)


def test_configured_short_id_length_is_raised(monkeypatch):
    monkeypatch.setitem(papyrus._pastes, 'id_length', 8)
    assert len(papyrus.generate_paste_id()) == papyrus.MIN_ID_LENGTH


# ── Expiry ────────────────────────────────────────────────────────────────────

def test_expiry_is_stored_in_iso_t_format(client):
    pid = create(client)['paste_id']
    stored = db().execute('SELECT expires_at FROM pastes WHERE id=?', (pid,)).fetchone()[0]
    assert 'T' in stored and ' ' not in stored


def test_cleanup_query_keeps_unexpired_paste(client):
    pid = create(client)['paste_id']
    now = datetime.datetime.now(UTC).isoformat()
    conn = db()
    conn.execute('DELETE FROM pastes WHERE expires_at < ?', (now,))
    assert conn.execute('SELECT COUNT(*) FROM pastes WHERE id=?', (pid,)).fetchone()[0] == 1


def test_expired_paste_is_gone(client):
    pid = create(client)['paste_id']
    past = (datetime.datetime.now(UTC) - datetime.timedelta(minutes=1)).isoformat()
    conn = db()
    conn.execute('UPDATE pastes SET expires_at=? WHERE id=?', (past, pid))
    conn.commit()
    assert client.get(f'/{pid}').status_code == 410


def test_is_expired_reads_both_timestamp_spellings():
    future = (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).isoformat(' ')
    past = (datetime.datetime.now(UTC) - datetime.timedelta(hours=1)).isoformat(' ')
    assert not papyrus._is_expired(future)
    assert papyrus._is_expired(past)
    assert not papyrus._is_expired(None)


# ── Same-origin check ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('headers,ok', [
    ({'Origin': 'http://localhost'}, True),
    ({'Referer': 'http://localhost/some/page'}, True),
    ({'Origin': 'http://localhost.evil.com'}, False),
    ({'Referer': 'http://localhost.evil.com/x'}, False),
    ({'Origin': 'https://localhost'}, False),
    ({'Origin': 'null'}, False),
    ({}, False),
])
def test_same_origin(client, headers, ok):
    with papyrus.app.test_request_context('/', headers=headers):
        assert papyrus._is_same_origin() is ok


# ── Deletion token ────────────────────────────────────────────────────────────

def test_removal_link_needs_the_right_token(client):
    made = create(client)
    pid, token = made['paste_id'], made['removal_token']
    assert client.post('/remove/not-the-token').status_code == 404
    assert client.get(f'/{pid}').status_code == 200
    assert client.post(f'/remove/{token}').status_code == 200
    assert client.get(f'/{pid}').status_code == 404


# ── Admin ─────────────────────────────────────────────────────────────────────

def login(client, password='hunter2', user='admin', **kw):
    return client.post('/admin/login', data={'username': user, 'password': password},
                       headers=ORIGIN, **kw)


def test_admin_login_success_and_dashboard(client):
    assert client.get('/admin').status_code == 302
    assert login(client).status_code == 302
    assert client.get('/admin').status_code == 200


def test_admin_login_wrong_password(client):
    r = login(client, password='wrong')
    assert r.status_code == 200 and b'Invalid credentials' in r.data
    assert client.get('/admin').status_code == 302


def test_admin_login_missing_fields_is_not_500(client):
    r = client.post('/admin/login', data={}, headers=ORIGIN)
    assert r.status_code == 200


def test_admin_login_requires_same_origin(client):
    r = client.post('/admin/login', data={'username': 'admin', 'password': 'hunter2'},
                    headers={'Origin': 'http://evil.example'})
    assert r.status_code == 403


def test_admin_login_locks_out_after_failures_only(client):
    # Successful logins must not eat into the budget.
    for _ in range(papyrus._ADMIN_LOGIN_LIMIT + 2):
        assert login(client).status_code == 302
    for _ in range(papyrus._ADMIN_LOGIN_LIMIT):
        assert login(client, password='bad').status_code == 200
    assert login(client, password='bad').status_code == 429
    # Locked out even with the right password until the window passes.
    assert login(client).status_code == 429


def test_admin_disabled_returns_404(client, monkeypatch):
    monkeypatch.setitem(papyrus.CFG, 'admin', {'enabled': False, 'user': 'a', 'pass': 'x'})
    assert client.get('/admin/login').status_code == 404
    assert client.get('/admin').status_code == 302 or client.get('/admin').status_code == 404


def test_admin_logout_is_post_only(client):
    login(client)
    assert client.get('/admin/logout').status_code == 405
    assert client.post('/admin/logout', headers=ORIGIN).status_code == 302
    assert client.get('/admin').status_code == 302


def test_admin_delete(client):
    pid = create(client)['paste_id']
    assert client.post(f'/admin/delete/{pid}', headers=ORIGIN).status_code == 403
    login(client)
    assert client.post(f'/admin/delete/{pid}', headers={'Origin': 'http://evil'}).status_code == 403
    assert client.post(f'/admin/delete/{pid}', headers=ORIGIN).status_code == 302
    assert client.get(f'/{pid}').status_code == 404


# ── Rate limiting / proxy ─────────────────────────────────────────────────────

def test_create_is_rate_limited(client):
    for _ in range(10):
        assert client.post('/create', json={'content': 'x'}).status_code == 200
    assert client.post('/create', json={'content': 'x'}).status_code == 429


def test_rate_limit_is_per_ip(client):
    for _ in range(10):
        client.post('/create', json={'content': 'x'}, environ_overrides={'REMOTE_ADDR': '10.0.0.1'})
    other = client.post('/create', json={'content': 'x'}, environ_overrides={'REMOTE_ADDR': '10.0.0.2'})
    assert other.status_code == 200


# ── Plaintext pastes, raw URLs ────────────────────────────────────────────────

def test_view_and_raw(client):
    pid = create(client, title='Hi <b>there</b>', content='a < b && "c"', language='python')['paste_id']
    page = client.get(f'/{pid}')
    assert page.status_code == 200
    assert b'a &lt; b &amp;&amp; &#34;c&#34;' in page.data      # escaped, server-rendered
    assert b'Hi &lt;b&gt;there&lt;/b&gt;' in page.data
    assert b'language-python' in page.data
    raw = client.get(f'/raw/{pid}')
    assert raw.status_code == 200
    assert raw.mimetype == 'text/plain'
    assert raw.get_data(as_text=True) == 'a < b && "c"'


def test_old_raw_url_shape_is_gone(client):
    pid = create(client)['paste_id']
    assert client.get(f'/{pid}/raw').status_code == 404


def test_raw_of_missing_or_malformed_id(client):
    assert client.get('/raw/deadbeef0000').status_code == 404
    assert client.get('/raw/NOT-HEX').status_code == 404


def test_expired_paste_raw_is_410(client):
    pid = create(client)['paste_id']
    past = (datetime.datetime.now(UTC) - datetime.timedelta(minutes=1)).isoformat()
    conn = db()
    conn.execute('UPDATE pastes SET expires_at=? WHERE id=?', (past, pid))
    conn.commit()
    assert client.get(f'/raw/{pid}').status_code == 410


def test_create_validation(client):
    assert client.post('/create', json={}).status_code == 400
    assert client.post('/create', json={'content': '   '}).status_code == 400
    assert client.post('/create', json=['x']).status_code == 400
    ok = client.post('/create', json={'content': 'x', 'title': 5, 'language': '../../x'})
    pid = ok.get_json()['paste_id']
    row = db().execute('SELECT title, language FROM pastes WHERE id=?', (pid,)).fetchone()
    assert row['title'] == 'Untitled' and row['language'] == papyrus._pastes.get('default_language', 'text')


def test_oversized_paste_rejected(client, monkeypatch):
    monkeypatch.setattr(papyrus, 'MAX_PASTE_BYTES', 100)
    assert client.post('/create', json={'content': 'x' * 101}).status_code == 413


def test_curl_style_create(client):
    r = client.post('/create?title=notes&language=python&expires_in=1day',
                    data='print(1)\n', content_type='text/plain')
    assert r.status_code == 200
    urls = curl_urls(r)
    pid = urls['Paste URL'].rsplit('/', 1)[1]
    assert urls['Paste URL'] == f'http://localhost/{pid}' and urls['Raw URL'] == f'http://localhost/raw/{pid}'
    assert 'X-Deletion-Token' not in r.headers          # the removal URL replaced the header
    assert client.get(f'/raw/{pid}').get_data(as_text=True) == 'print(1)\n'
    row = db().execute('SELECT title, language, expires_at FROM pastes WHERE id=?', (pid,)).fetchone()
    assert (row['title'], row['language']) == ('notes', 'python') and row['expires_at']


# ── Comments / Markdown ───────────────────────────────────────────────────────

def comment(client, pid, content, nick='', origin=ORIGIN, parent=''):
    return client.post(f'/{pid}/comment', headers=origin,
                       data={'content': content, 'nick': nick, 'parent': parent})


def comment_id(response):
    return response.headers['Location'].rsplit('#c-', 1)[1]


def test_comment_roundtrip_renders_markdown(client):
    pid = create(client, discussions=True)['paste_id']
    r = comment(client, pid, '**bold** and `code`\n\n```python\nprint(1)\n```', nick='ann')
    assert r.status_code == 303 and '#c-' in r.headers['Location']
    page = client.get(f'/{pid}').get_data(as_text=True)
    assert '<strong>bold</strong>' in page
    assert '<code>code</code>' in page
    assert 'class="language-python"' in page
    assert 'ann' in page


def test_comment_markdown_is_sanitised(client):
    pid = create(client, discussions=True)['paste_id']
    comment(client, pid, '<script>alert(1)</script>\n\n'
                         'inline <img src=x onerror=alert(2)> and <b onclick="y()">raw</b>\n\n'
                         '[x](javascript:alert(3)) ![pic](http://tracker.example/p.png)')
    page = client.get(f'/{pid}').get_data(as_text=True)
    body = page[page.index('id="commentsList"'):page.index('id="commentForm"')]
    assert '<script' not in body and '<img' not in body and '<b ' not in body
    assert 'href="javascript' not in body.lower()
    # Raw HTML survives only as escaped, inert text.
    assert '&lt;script&gt;' in body
    # The image degrades to a plain link, with a safe rel.
    assert 'href="http://tracker.example/p.png"' in body
    assert 'rel="noopener noreferrer nofollow"' in body


def test_render_markdown_code_class_is_restricted():
    html = papyrus.render_markdown('```"><b onmouseover=x>\ncode\n```')
    assert 'onmouseover' not in html.replace('&lt;b onmouseover=x&gt;', '')
    assert '<b ' not in html


def test_comment_requires_discussions_and_origin(client):
    off = create(client)['paste_id']
    assert comment(client, off, 'hi').status_code == 403
    on = create(client, discussions=True)['paste_id']
    assert comment(client, on, 'hi', origin={'Origin': 'http://evil.example'}).status_code == 403
    assert comment(client, on, 'hi', origin={}).status_code == 403
    assert comment(client, on, '   ').status_code == 400
    assert comment(client, 'deadbeef0000', 'hi').status_code == 404


def test_comment_limits(client, monkeypatch):
    pid = create(client, discussions=True)['paste_id']
    assert comment(client, pid, 'x' * 10241).status_code == 413
    monkeypatch.setattr(papyrus, '_MAX_COMMENTS_PER_PASTE', 2)
    assert comment(client, pid, 'one').status_code == 303
    assert comment(client, pid, 'two').status_code == 303
    assert comment(client, pid, 'three').status_code == 429


def test_comment_nick_is_cleaned(client):
    pid = create(client, discussions=True)['paste_id']
    comment(client, pid, 'hi', nick='  a\x00b' + 'z' * 100)
    nick = db().execute('SELECT nick FROM comments').fetchone()[0]
    assert '\x00' not in nick and len(nick) <= 32


def test_comment_posting_is_rate_limited(client):
    pid = create(client, discussions=True)['paste_id']
    for _ in range(20):
        assert comment(client, pid, 'hi').status_code == 303
    assert comment(client, pid, 'hi').status_code == 429


def test_preview_endpoint(client):
    ok = client.post('/api/preview', json={'text': '*hi* <script>x</script>'}, headers=ORIGIN)
    assert ok.status_code == 200
    html = ok.get_json()['html']
    assert '<em>hi</em>' in html and '<script>' not in html
    assert client.post('/api/preview', json={'text': 'x'}).status_code == 403
    assert client.post('/api/preview', json={'text': 5}, headers=ORIGIN).status_code == 400
    assert client.post('/api/preview', json={'text': 'x' * 10241}, headers=ORIGIN).status_code == 400


def test_deleting_a_paste_deletes_its_comments(client):
    made = create(client, discussions=True)
    comment(client, made['paste_id'], 'hi')
    assert db().execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 1
    client.post(f"/remove/{made['removal_token']}")
    assert db().execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 0


def test_purge_expired_removes_comments_too(client):
    pid = create(client, discussions=True)['paste_id']
    comment(client, pid, 'hi')
    keep = create(client)['paste_id']
    past = (datetime.datetime.now(UTC) - datetime.timedelta(minutes=1)).isoformat()
    conn = db()
    conn.execute('UPDATE pastes SET expires_at=? WHERE id=?', (past, pid))
    conn.commit()
    papyrus._purge_expired()
    assert client.get(f'/{keep}').status_code == 200
    assert db().execute('SELECT COUNT(*) FROM pastes WHERE id=?', (pid,)).fetchone()[0] == 0
    assert db().execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 0


# ── Migration from the old end-to-end-encrypted schema ────────────────────────

# ── Threaded replies ──────────────────────────────────────────────────────────

def test_replies_are_nested(client):
    pid = create(client, discussions=True)['paste_id']
    a = comment_id(comment(client, pid, 'top level A'))
    b = comment_id(comment(client, pid, 'reply B', parent=a))
    c = comment_id(comment(client, pid, 'reply C to B', parent=b))
    d = comment_id(comment(client, pid, 'top level D'))
    rows = db().execute('SELECT id, parent_id FROM comments').fetchall()
    assert {r['id']: r['parent_id'] for r in rows} == {a: None, b: a, c: b, d: None}

    page = client.get(f'/{pid}').get_data(as_text=True)
    assert 'id="discussionsCount">4<' in page  # the count covers replies too
    # B's box sits inside A's replies container, C inside B's, and D after A entirely.
    pos = {k: page.index(f'id="c-{k}"') for k in (a, b, c, d)}
    assert pos[a] < pos[b] < pos[c] < pos[d]
    a_block = page[pos[a]:pos[d]]
    assert f'id="c-{b}"' in a_block and f'id="c-{c}"' in a_block


def test_reply_to_unknown_or_foreign_comment_is_rejected(client):
    p1 = create(client, discussions=True)['paste_id']
    p2 = create(client, discussions=True)['paste_id']
    other = comment_id(comment(client, p2, 'on the other paste'))
    assert comment(client, p1, 'x', parent='nope').status_code == 400
    assert comment(client, p1, 'x', parent=other).status_code == 400


def test_reply_depth_is_capped(client):
    pid = create(client, discussions=True)['paste_id']
    parent = ''
    for i in range(papyrus._MAX_REPLY_DEPTH + 3):
        parent = comment_id(comment(client, pid, f'level {i}', parent=parent))
    conn = db()
    conn.row_factory = sqlite3.Row
    depths = [papyrus._comment_depth(conn, r['id']) for r in conn.execute('SELECT id FROM comments')]
    assert max(depths) == papyrus._MAX_REPLY_DEPTH
    assert len(depths) == papyrus._MAX_REPLY_DEPTH + 3  # nothing is lost, only flattened


def test_comment_tree_helper_handles_orphans():
    rows = [{'id': 'a', 'parent_id': None}, {'id': 'b', 'parent_id': 'a'}, {'id': 'z', 'parent_id': 'gone'}]
    tree = papyrus._comment_tree(rows)
    assert [n['id'] for n in tree] == ['a', 'z']
    assert [n['id'] for n in tree[0]['children']] == ['b']


# ── Text only ─────────────────────────────────────────────────────────────────

PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00'


def test_csp_only_allows_images_from_our_own_origin(client):
    csp = client.get('/').headers['Content-Security-Policy']
    directives = {d.split()[0]: d.split()[1:] for d in (x.strip() for x in csp.split(';')) if d}
    assert directives['img-src'] == ["'self'"]      # no data:, no https:, no wildcard


def test_pages_show_only_our_own_logo(client):
    pid = create(client, discussions=True)['paste_id']
    comment(client, pid, '![tracker](http://tracker.example/p.png) <img src=http://evil.example/x.png>')
    for path in ('/', f'/{pid}'):
        html = client.get(path).get_data(as_text=True)
        imgs = re.findall(r'<img\b[^>]*>', html)
        assert len(imgs) == 1 and 'src="/static/img/logo-64.png"' in imgs[0]   # the navbar mark, nothing else
    page = client.get(f'/{pid}').get_data(as_text=True)
    assert 'href="http://tracker.example/p.png"' in page        # a Markdown image became a link
    assert '<img src=http://evil.example' not in page            # raw HTML stayed escaped text


def test_binary_upload_is_rejected(client):
    r = client.post('/create', data=PNG, content_type='text/plain')
    assert r.status_code == 415
    r = client.post('/create', data=b'valid start \xff\xfe invalid utf-8', content_type='text/plain')
    assert r.status_code == 415
    r = client.post('/create', data=PNG, content_type='image/png')   # not JSON either
    assert r.status_code == 400
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 0


def test_nul_bytes_are_rejected_in_json_pastes_and_comments(client):
    assert client.post('/create', json={'content': 'ab\x00cd'}).status_code == 415
    pid = create(client, discussions=True)['paste_id']
    assert comment(client, pid, 'a\x00b').status_code == 415


def test_any_utf8_text_is_still_accepted(client):
    text = 'héllo wörld \u2603 \U0001F600\n\tindented\r\n<svg xmlns="http://www.w3.org/2000/svg"/>'
    r = client.post('/create', data=text.encode('utf-8'), content_type='text/plain; charset=utf-8')
    assert r.status_code == 200
    pid = curl_urls(r)['Paste URL'].rsplit('/', 1)[1]
    assert client.get(f'/raw/{pid}').get_data(as_text=True) == text


def test_raw_responses_cannot_be_sniffed_as_images(client):
    pid = create(client, content='GIF89a not really an image')['paste_id']
    r = client.get(f'/raw/{pid}')
    assert r.mimetype == 'text/plain' and r.headers['X-Content-Type-Options'] == 'nosniff'


def test_logo_assets_are_served(client):
    for path, kind in [('/favicon.ico', 'image/'), ('/static/favicon.ico', 'image/'),
                       ('/static/img/logo-64.png', 'image/png'),
                       ('/static/img/favicon-32.png', 'image/png'),
                       ('/static/img/apple-touch-icon.png', 'image/png')]:
        r = client.get(path)
        assert r.status_code == 200 and r.mimetype.startswith(kind) and len(r.data) > 100, path


def test_head_links_point_at_the_icons(client):
    html = client.get('/').get_data(as_text=True)
    for fragment in ('rel="icon" href="/static/favicon.ico"', 'img/favicon-32.png', 'img/apple-touch-icon.png'):
        assert fragment in html


# ── Client address behind a reverse proxy ─────────────────────────────────────

def client_ip(remote, forwarded=None):
    headers = {'X-Forwarded-For': forwarded} if forwarded is not None else {}
    with papyrus.app.test_request_context('/', headers=headers, environ_overrides={'REMOTE_ADDR': remote}):
        return papyrus._client_ip()


def test_proxy_on_this_machine_is_believed():
    assert client_ip('127.0.0.1', '203.0.113.9') == '203.0.113.9'
    assert client_ip('::1', '203.0.113.9') == '203.0.113.9'
    assert client_ip('::ffff:127.0.0.1', '203.0.113.9') == '203.0.113.9'
    assert client_ip('127.0.0.1', '2001:db8::7') == '2001:db8::7'


def test_last_entry_wins_so_forged_prefixes_are_ignored():
    # nginx/Apache append the address they saw; whatever the visitor sent stays to the left.
    assert client_ip('127.0.0.1', '1.2.3.4, 203.0.113.9') == '203.0.113.9'
    assert client_ip('127.0.0.1', '1.2.3.4,203.0.113.9 ') == '203.0.113.9'


def test_untrusted_peer_cannot_spoof_its_address():
    # The app reached directly (not via the proxy): the header is just a visitor's claim.
    assert client_ip('198.51.100.20', '1.2.3.4') == '198.51.100.20'
    assert client_ip('198.51.100.20', '1.2.3.4, 5.6.7.8') == '198.51.100.20'


def test_missing_or_garbage_header_falls_back_to_the_peer():
    assert client_ip('127.0.0.1') == '127.0.0.1'
    assert client_ip('127.0.0.1', '') == '127.0.0.1'
    assert client_ip('127.0.0.1', 'unknown') == '127.0.0.1'
    assert client_ip('127.0.0.1', '203.0.113.9, not-an-ip') == '127.0.0.1'
    assert client_ip('127.0.0.1', '999.1.1.1') == '127.0.0.1'


def test_trusted_proxy_addresses_are_configurable(monkeypatch):
    import ipaddress
    monkeypatch.setattr(papyrus, '_TRUSTED_PEERS', [ipaddress.ip_network('10.0.0.0/8')])
    assert client_ip('10.1.2.3', '203.0.113.9') == '203.0.113.9'
    assert client_ip('127.0.0.1', '203.0.113.9') == '127.0.0.1'   # loopback no longer trusted


def test_rate_limits_follow_the_forwarded_address(client):
    def post(xff):
        return client.post('/create', json={'content': 'x'}, headers={'X-Forwarded-For': xff})
    for _ in range(10):
        assert post('203.0.113.9').status_code == 200
    assert post('203.0.113.9').status_code == 429     # that visitor is used up
    assert post('203.0.113.10').status_code == 200    # another visitor behind the same proxy is not


def test_forged_forwarded_for_cannot_reset_a_direct_visitors_limit(client):
    def post(xff):
        return client.post('/create', json={'content': 'x'}, headers={'X-Forwarded-For': xff},
                           environ_overrides={'REMOTE_ADDR': '198.51.100.20'})
    for i in range(10):
        assert post(f'1.2.3.{i}').status_code == 200
    assert post('9.9.9.9').status_code == 429


def test_gunicorn_trusts_forwarded_proto_from_the_same_proxies():
    opts = papyrus._server_options('127.0.0.1', 1027, 2)
    assert opts['forwarded_allow_ips'] == '127.0.0.1/32,::1/128'


# ── Reports ───────────────────────────────────────────────────────────────────

def report(client, pid, reason='spam', note='', comment='', origin=ORIGIN, headers=None):
    data = {'reason': reason, 'note': note}
    if comment:
        data['comment'] = comment
    return client.post(f'/{pid}/report', data=data, headers={**origin, **(headers or {})})


def report_rows():
    return db().execute('SELECT * FROM reports ORDER BY rowid').fetchall()


def test_reports_exist_only_while_the_admin_panel_is_enabled(client, monkeypatch):
    pid = create(client, discussions=True)['paste_id']
    comment(client, pid, 'hello')
    page = client.get(f'/{pid}').get_data(as_text=True)
    assert f'href="/{pid}/report"' in page and '/report?comment=' in page      # buttons shown
    monkeypatch.setitem(bastebin_cfg(), 'admin', {'enabled': False, 'user': 'a', 'pass': 'x'})
    page = client.get(f'/{pid}').get_data(as_text=True)
    assert '/report' not in page                                                 # no buttons without an admin
    assert client.get(f'/{pid}/report').status_code == 404
    assert report(client, pid).status_code == 404


def bastebin_cfg():
    return papyrus.CFG


def test_reports_can_be_switched_off(client, monkeypatch):
    pid = create(client)['paste_id']
    monkeypatch.setitem(papyrus._feat, 'reports', False)
    assert client.get(f'/{pid}/report').status_code == 404


def test_report_form_for_a_paste_and_a_comment(client):
    pid = create(client, title='My <b>paste</b>', discussions=True, content='secret <i>code</i>')['paste_id']
    cid = comment_id(comment(client, pid, 'a <script>x</script> comment', nick='ann'))
    page = client.get(f'/{pid}/report')
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and 'Report this paste' in html
    assert 'My &lt;b&gt;paste&lt;/b&gt;' in html and 'secret &lt;i&gt;code&lt;/i&gt;' in html
    assert 'name="reason" value="spam"' in html and 'name="comment"' not in html
    html = client.get(f'/{pid}/report?comment={cid}').get_data(as_text=True)
    assert 'Report this comment' in html and f'name="comment" value="{cid}"' in html
    assert '&lt;script&gt;' in html and '<script>x' not in html


def test_report_form_rejects_unknown_targets(client):
    p1 = create(client, discussions=True)['paste_id']
    p2 = create(client, discussions=True)['paste_id']
    other = comment_id(comment(client, p2, 'elsewhere'))
    assert client.get(f'/{p1}/report?comment=nope').status_code == 404
    assert client.get(f'/{p1}/report?comment={other}').status_code == 404      # a comment of another paste
    assert client.get('/deadbeef0000/report').status_code == 404


def test_report_is_stored_and_the_reporter_is_not_identifiable(client):
    pid = create(client)['paste_id']
    r = report(client, pid, 'abuse', 'it is\x00 bad ' + 'x' * 600)
    assert r.status_code == 303 and r.headers['Location'].endswith(f'/{pid}?reported=1')
    [row] = report_rows()
    assert (row['paste_id'], row['comment_id'], row['reason']) == (pid, '', 'abuse')
    assert '\x00' not in row['note'] and len(row['note']) == 500
    assert '127.0.0.1' not in ' '.join(str(v) for v in tuple(row))             # no address stored
    assert len(row['reporter']) == 32
    assert 'Thanks, your report was sent' in client.get(f'/{pid}?reported=1').get_data(as_text=True)
    assert 'Thanks, your report' not in client.get(f'/{pid}').get_data(as_text=True)


def test_comment_reports_are_separate_from_paste_reports(client):
    pid = create(client, discussions=True)['paste_id']
    cid = comment_id(comment(client, pid, 'rude'))
    report(client, pid)
    report(client, pid, comment=cid, reason='abuse')
    assert [(r['comment_id'], r['reason']) for r in report_rows()] == [('', 'spam'), (cid, 'abuse')]


def test_the_same_visitor_cannot_report_a_target_twice(client):
    pid = create(client)['paste_id']
    assert report(client, pid).status_code == 303
    assert report(client, pid, 'abuse').status_code == 303          # accepted, but not counted again
    assert len(report_rows()) == 1
    # a different visitor behind the same proxy counts separately
    assert report(client, pid, headers={'X-Forwarded-For': '203.0.113.9'}).status_code == 303
    assert len(report_rows()) == 2


def test_report_validation_and_origin_check(client):
    pid = create(client)['paste_id']
    assert report(client, pid, reason='nonsense').status_code == 400
    assert client.post(f'/{pid}/report', data={}, headers=ORIGIN).status_code == 400
    assert report(client, pid, origin={'Origin': 'http://evil.example'}).status_code == 403
    assert report(client, pid, origin={}).status_code == 403
    assert report_rows() == []


def test_reports_are_rate_limited_per_visitor(client):
    pid = create(client)['paste_id']
    for _ in range(papyrus._REPORT_RATE_LIMIT):
        assert report(client, pid).status_code == 303
    assert report(client, pid).status_code == 429
    # another visitor behind the same proxy is not affected
    assert report(client, pid, headers={'X-Forwarded-For': '203.0.113.77'}).status_code == 303


def test_reporting_a_gone_paste(client):
    pid = create(client)['paste_id']
    past = (datetime.datetime.now(UTC) - datetime.timedelta(minutes=1)).isoformat()
    conn = db()
    conn.execute('UPDATE pastes SET expires_at=? WHERE id=?', (past, pid))
    conn.commit()
    assert report(client, pid).status_code == 410          # the first request finds it expired and removes it
    assert client.get(f'/{pid}/report').status_code == 404  # after that it is simply gone


def test_deleting_or_expiring_a_paste_removes_its_reports(client):
    made = create(client)
    keep = create(client)['paste_id']
    report(client, made['paste_id'])
    report(client, keep)
    client.post(f"/remove/{made['removal_token']}")
    assert [r['paste_id'] for r in report_rows()] == [keep]
    past = (datetime.datetime.now(UTC) - datetime.timedelta(minutes=1)).isoformat()
    conn = db()
    conn.execute('UPDATE pastes SET expires_at=? WHERE id=?', (past, keep))
    conn.commit()
    papyrus._purge_expired()
    assert report_rows() == []


def test_admin_sees_the_report_queue(client):
    pid = create(client, title='<b>Spammy</b> paste', content='buy   <i>cheap</i>\n pills', discussions=True)['paste_id']
    cid = comment_id(comment(client, pid, 'rude <script>alert(1)</script> words', nick='troll'))
    report(client, pid, 'spam', 'obvious ad')
    report(client, pid, 'malware', headers={'X-Forwarded-For': '203.0.113.1'})
    report(client, pid, 'spam', headers={'X-Forwarded-For': '203.0.113.2'})
    report(client, pid, comment=cid, reason='abuse', note='<img src=x onerror=alert(1)>')
    assert client.get('/admin').status_code == 302                    # not logged in
    login(client)
    html = client.get('/admin').get_data(as_text=True)
    assert 'Reports (2)' in html
    assert html.index('Spammy') < html.index('words')                 # 3 reports outrank 1
    assert 'Spam or advertising &times;2, Malware or phishing' in html
    assert 'buy &lt;i&gt;cheap&lt;/i&gt; pills' in html                # whitespace collapsed, HTML escaped
    assert '&ldquo;obvious ad&rdquo;' in html and 'by troll' in html
    assert '<script>alert' not in html and '<img src=x' not in html
    assert f'#c-{cid}' in html and 'Delete comment' in html and 'Dismiss' in html
    assert 'onsubmit' not in html and 'data-confirm="Delete this paste' in html     # CSP-safe confirmations


def test_admin_dashboard_without_reports(client):
    create(client)
    login(client)
    html = client.get('/admin').get_data(as_text=True)
    assert 'Nothing has been reported.' in html and 'Global Paste Management' in html


def test_admin_can_dismiss_reports_for_one_target_only(client):
    pid = create(client, discussions=True)['paste_id']
    cid = comment_id(comment(client, pid, 'x'))
    report(client, pid)
    report(client, pid, comment=cid)
    form = {'paste_id': pid, 'comment_id': ''}
    assert client.post('/admin/reports/dismiss', data=form, headers=ORIGIN).status_code == 403   # not admin
    login(client)
    assert client.post('/admin/reports/dismiss', data=form, headers={'Origin': 'http://evil'}).status_code == 403
    assert client.post('/admin/reports/dismiss', data=form, headers=ORIGIN).status_code == 302
    assert [r['comment_id'] for r in report_rows()] == [cid]          # the comment report is untouched
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 1   # nothing deleted


def test_admin_deletes_a_comment_but_keeps_its_replies(client):
    pid = create(client, discussions=True)['paste_id']
    a = comment_id(comment(client, pid, 'top'))
    b = comment_id(comment(client, pid, 'abusive', parent=a))
    c = comment_id(comment(client, pid, 'innocent reply', parent=b))
    report(client, pid, comment=b)
    assert client.post(f'/admin/comments/{b}/delete', headers=ORIGIN).status_code == 403    # not admin
    login(client)
    assert client.post(f'/admin/comments/{b}/delete', headers={'Origin': 'http://evil'}).status_code == 403
    r = client.post(f'/admin/comments/{b}/delete', data={'return': 'paste'}, headers=ORIGIN)
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/{pid}#discussionsPanel')
    rows = {x['id']: x['parent_id'] for x in db().execute('SELECT id, parent_id FROM comments')}
    assert rows == {a: None, c: a}                                    # the reply moved up to a
    assert report_rows() == []                                        # its reports went with it
    r = client.post(f'/admin/comments/{c}/delete', headers=ORIGIN)    # default: back to the dashboard
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin')
    assert client.post('/admin/comments/missing/delete', headers=ORIGIN).status_code == 302


def test_admin_delete_buttons_on_comments_are_for_admins_only(client):
    pid = create(client, discussions=True)['paste_id']
    comment(client, pid, 'hello')
    assert 'comment-delete' not in client.get(f'/{pid}').get_data(as_text=True)
    login(client)
    html = client.get(f'/{pid}').get_data(as_text=True)
    assert 'comment-delete' in html and 'data-confirm="Delete this comment?' in html


def test_admin_visits_do_not_inflate_the_view_counter(client):
    pid = create(client)['paste_id']
    client.get(f'/{pid}')
    login(client)
    client.get(f'/{pid}')
    client.get(f'/{pid}')
    assert db().execute('SELECT views FROM pastes WHERE id=?', (pid,)).fetchone()[0] == 1


def test_confirmation_handler_is_loaded_on_every_page():
    js = open(os.path.join(os.path.dirname(papyrus.__file__), 'static', 'js', 'app.js')).read()
    assert "dataset.confirm" in js and 'preventDefault' in js


# ── Filename, language auto-detection, file header ────────────────────────────

@pytest.mark.parametrize('raw,expected', [
    ('deploy.sh', 'deploy.sh'),
    ('../../etc/passwd', 'passwd'),
    ('C:\\Users\\me\\notes.py', 'notes.py'),
    ('  spaced name.txt  ', 'spaced name.txt'),
    ('bad\x00na\x07me.c', 'badname.c'),
    ('a/b/', ''),
    ('', ''), (None, ''), (42, ''), (['x'], ''),
])
def test_clean_filename(raw, expected):
    assert papyrus._clean_filename(raw) == expected


def test_clean_filename_is_length_limited():
    assert len(papyrus._clean_filename('x' * 500 + '.py')) == papyrus.MAX_FILENAME_LENGTH


@pytest.mark.parametrize('name,language', [
    ('deploy.sh', 'bash'), ('Main.PY', 'python'), ('app.js', 'javascript'), ('lib.rs', 'rust'),
    ('Dockerfile', 'docker'), ('nginx.conf', 'nginx'), ('.bashrc', 'bash'), ('notes.txt', 'text'),
    ('index.HTML', 'html'), ('data.yml', 'yaml'), ('fix.patch', 'diff'),
    ('archive.tar.gz', None), ('README', None), ('', None), (None, None), ('trailingdot.', None),
])
def test_detect_language(name, language):
    assert papyrus._detect_language(name) == language


def test_detection_only_returns_languages_the_site_offers(monkeypatch):
    monkeypatch.setitem(papyrus.CFG, 'languages', [{'value': 'text', 'name': 'Text'}, {'value': 'python', 'name': 'Python'}])
    assert papyrus._detect_language('x.py') == 'python'
    assert papyrus._detect_language('x.sh') is None
    hints = papyrus._language_hints()
    assert set(hints['extensions'].values()) <= {'text', 'python'} and 'sh' not in hints['extensions']


def stored(pid):
    return db().execute('SELECT title, filename, language FROM pastes WHERE id=?', (pid,)).fetchone()


def test_filename_picks_the_language_and_the_title(client):
    row = stored(create(client, filename='deploy.sh')['paste_id'])
    assert (row['filename'], row['language'], row['title']) == ('deploy.sh', 'bash', 'deploy.sh')


def test_auto_language_follows_the_filename(client):
    assert stored(create(client, filename='x.py', language='auto')['paste_id'])['language'] == 'python'
    assert stored(create(client, filename='x.py', language='')['paste_id'])['language'] == 'python'


def test_an_explicit_language_beats_the_filename(client):
    assert stored(create(client, filename='x.py', language='ruby')['paste_id'])['language'] == 'ruby'


def test_unknown_extension_and_no_filename_use_the_default_language(client):
    default = papyrus._pastes.get('default_language', 'text')
    assert stored(create(client, filename='data.xyz', language='auto')['paste_id'])['language'] == default
    row = stored(create(client)['paste_id'])
    assert (row['filename'], row['title'], row['language']) == ('', 'Untitled', default)


def test_description_and_filename_are_kept_separately(client):
    row = stored(create(client, title='Deploy helper', filename='deploy.sh')['paste_id'])
    assert (row['title'], row['filename']) == ('Deploy helper', 'deploy.sh')


def test_hostile_filename_is_reduced_to_a_name(client):
    row = stored(create(client, filename='../../etc/<script>x.py')['paste_id'])
    assert row['filename'] == '<script>x.py'          # stored as text; escaped wherever it is shown


def test_curl_upload_accepts_a_filename(client):
    r = client.post('/create?filename=script.rb', data=b'puts 1\n', content_type='text/plain')
    pid = curl_urls(r)['Paste URL'].rsplit('/', 1)[1]
    assert (stored(pid)['filename'], stored(pid)['language']) == ('script.rb', 'ruby')


def test_file_header_on_the_view_page(client):
    pid = create(client, filename='deploy.sh', content='echo a\necho b\n')['paste_id']
    html = client.get(f'/{pid}').get_data(as_text=True)
    assert '<span class="file-name">deploy.sh</span>' in html
    assert 'bash &middot; 2 lines &middot; 14 B' in html
    assert 'data-filename="deploy.sh"' in html


def test_file_header_without_a_filename_and_with_html_in_it(client):
    html = client.get('/' + create(client, content='one line')['paste_id']).get_data(as_text=True)
    assert '<span class="file-name">untitled</span>' in html and '1 line &middot;' in html
    pid = create(client, filename='<img src=x>.py')['paste_id']
    html = client.get(f'/{pid}').get_data(as_text=True)
    assert '<span class="file-name">&lt;img src=x&gt;.py</span>' in html and '<img src=x>' not in html


@pytest.mark.parametrize('content,lines', [('a', 1), ('a\n', 1), ('a\nb', 2), ('a\nb\n', 2), ('a\n\n', 2)])
def test_line_count(client, content, lines):
    pid = create(client, content=content)['paste_id']
    shown = re.search(r'(\d+) lines?', client.get(f'/{pid}').get_data(as_text=True).split('file-meta')[1])
    assert int(shown.group(1)) == lines


@pytest.mark.parametrize('n,text', [(0, '0 B'), (1023, '1023 B'), (1024, '1.0 KB'), (1536, '1.5 KB'),
                                    (3 * 1024 * 1024, '3.0 MB')])
def test_human_size(n, text):
    assert papyrus._human_size(n) == text


def test_editor_page_has_the_gist_style_controls(client):
    html = client.get('/').get_data(as_text=True)
    for fragment in ('id="title"', 'placeholder="Description (optional)"', 'id="filename"', 'id="language"',
                     'Indent mode', 'value="spaces"', 'value="tabs"', 'Indent size',
                     'Line wrap mode', 'value="soft"', 'value="off"', 'No wrap', 'Soft wrap'):
        assert fragment in html, fragment
    assert [m for m in re.findall(r'<option value="(\d)"', html) if m in '248'] == ['2', '4', '8']
    hints = json.loads(re.search(r'id="languageHints">(.*?)</script>', html, re.S).group(1))
    assert hints['extensions']['sh'] == 'bash' and hints['names']['dockerfile'] == 'docker'


def test_nothing_is_saved_in_the_browser_while_typing(client):
    js = ''.join(open(os.path.join(os.path.dirname(papyrus.__file__), 'static', 'js', f), encoding='utf-8').read()
                 for f in ('app.js', 'paste_create.js', 'paste_view.js'))
    assert "setItem('paste_draft'" not in js and 'saveDraft' not in js and 'loadDraft' not in js
    features = client.get('/api/config').get_json()['features']
    assert 'auto_save_draft' not in features and 'draft_max_age_days' not in features


# ── Removal links, the owner cookie and the curl endpoint (pinnwand-style) ─────

def fresh_client():
    return papyrus.app.test_client()


def test_curl_endpoint_prints_paste_raw_and_removal_urls(client):
    r = client.post('/curl', data={'raw': 'print(1)\n', 'filename': 'a.py', 'expires_in': '1day'},
                    content_type='multipart/form-data')       # what `curl -F 'raw=<-'` sends
    assert r.status_code == 200 and r.mimetype == 'text/plain'
    urls = curl_urls(r)
    pid = urls['Paste URL'].rsplit('/', 1)[1]
    assert urls['Raw URL'] == f'http://localhost/raw/{pid}'
    assert urls['Removal URL'].startswith('http://localhost/remove/') and pid not in urls['Removal URL']
    assert client.get(f'/raw/{pid}').get_data(as_text=True) == 'print(1)\n'
    assert (stored(pid)['filename'], stored(pid)['language']) == ('a.py', 'python')
    assert 'Set-Cookie' not in r.headers                          # no browser, no cookie
    assert client.post(urls['Removal URL'].replace('http://localhost', '')).status_code == 200
    assert client.get(f'/{pid}').status_code == 404


def test_curl_endpoint_also_takes_a_urlencoded_form(client):
    r = client.post('/curl', data={'raw': 'x', 'title': 'T', 'expires_in': '1week'})
    assert r.status_code == 200 and curl_urls(r)
    row = db().execute('SELECT title, expires_at FROM pastes').fetchone()
    assert row['title'] == 'T' and row['expires_at']


def test_curl_endpoint_errors_are_plain_text(client):
    r = client.post('/curl', data={})
    assert (r.status_code, r.mimetype) == (400, 'text/plain') and 'content' in r.get_data(as_text=True)
    r = client.post('/curl', data={'raw': 'a\x00b'})
    assert r.status_code == 415 and r.mimetype == 'text/plain'
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 0


def test_curl_endpoint_takes_pastes_up_to_the_size_limit(client):
    big = 'x' * (1024 * 1024)                                       # far beyond Werkzeug's 500 kB form default
    assert client.post('/curl', data={'raw': big}, content_type='multipart/form-data').status_code == 200
    too_big = 'x' * (papyrus.MAX_PASTE_BYTES + 10)
    r = client.post('/curl', data={'raw': too_big}, content_type='multipart/form-data')
    assert r.status_code == 413 and r.get_data(as_text=True).strip() == 'Paste too large'


def test_curl_endpoint_shares_the_creation_rate_limit(client):
    for _ in range(10):
        assert client.post('/curl', data={'raw': 'x'}).status_code == 200
    r = client.post('/curl', data={'raw': 'x'})
    assert r.status_code == 429 and r.mimetype == 'text/plain'
    assert client.post('/create', json={'content': 'x'}).status_code == 429


def test_json_create_returns_the_removal_link_and_sets_the_owner_cookie(client, monkeypatch):
    r = client.post('/create', json={'content': 'x', 'expires_in': '1hour'})
    made = r.get_json()
    assert made['removal_url'] == f"/remove/{made['removal_token']}" and 'deletion_token' not in made
    cookie = r.headers['Set-Cookie']
    assert f"removal={made['removal_token']}" in cookie
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and f"Path=/{made['paste_id']}" in cookie
    max_age = int(re.search(r'Max-Age=(\d+)', cookie).group(1))
    assert 3500 < max_age <= 3600                                  # persistent, until the paste expires
    set_expiries(monkeypatch, {'1hour': 3600, 'never': None})
    never = client.post('/create', json={'content': 'x', 'expires_in': 'never'}).headers['Set-Cookie']
    assert int(re.search(r'Max-Age=(\d+)', never).group(1)) == 365 * 86400


def test_owner_keeps_the_remove_now_button_across_visits(client):
    made = create(client)
    pid, token = made['paste_id'], made['removal_token']
    for _ in range(3):                                             # refresh, reopen: the cookie stays
        html = client.get(f'/{pid}').get_data(as_text=True)
        assert 'Remove now' in html and f'href="/remove/{token}"' in html


def test_other_visitors_never_see_the_button_or_the_token(client):
    made = create(client)
    pid, token = made['paste_id'], made['removal_token']
    other = fresh_client()
    html = other.get(f'/{pid}').get_data(as_text=True)
    assert 'Remove now' not in html and token not in html
    assert token not in other.get(f'/{pid}?created=1').get_data(as_text=True)


def test_a_wrong_or_borrowed_cookie_shows_nothing(client):
    a, b = create(client), create(client)
    thief = fresh_client()
    thief.set_cookie('removal', a['removal_token'], path=f"/{b['paste_id']}")   # a's token on b's paste
    assert 'Remove now' not in thief.get(f"/{b['paste_id']}").get_data(as_text=True)
    thief.set_cookie('removal', 'x' * 500, path=f"/{a['paste_id']}")
    assert 'Remove now' not in thief.get(f"/{a['paste_id']}").get_data(as_text=True)


def test_created_notice_with_the_removal_link_is_for_the_owner_only(client):
    made = create(client)
    pid, token = made['paste_id'], made['removal_token']
    html = client.get(f'/{pid}?created=1').get_data(as_text=True)
    assert 'Paste created.' in html and f'value="http://localhost/remove/{token}"' in html
    assert 'id="copyRemoval"' in html
    assert 'Paste created.' not in client.get(f'/{pid}').get_data(as_text=True)


def test_opening_the_removal_link_only_asks(client):
    made = create(client, title='My <b>paste</b>', content='some   text\nhere')
    token = made['removal_token']
    page = fresh_client().get(f'/remove/{token}')                  # a link previewer, or a curious click
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and 'Remove this paste?' in html
    assert 'My &lt;b&gt;paste&lt;/b&gt;' in html and 'some text here' in html
    assert '<form method="post"' in html and 'Remove now' in html
    assert client.get(f"/{made['paste_id']}").status_code == 200   # nothing was deleted
    assert client.head(f'/remove/{token}').status_code == 200
    assert client.get(f"/{made['paste_id']}").status_code == 200


def test_removing_through_the_link_needs_no_cookie_and_cleans_up(client):
    made = create(client, discussions=True)
    pid, token = made['paste_id'], made['removal_token']
    comment(client, pid, 'hi')
    report(client, pid)
    stranger = fresh_client()                                     # other device, no cookie, no Origin header
    r = stranger.post(f'/remove/{token}')
    assert r.status_code == 200 and 'Paste removed' in r.get_data(as_text=True)
    assert client.get(f'/{pid}').status_code == 404
    assert db().execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 0 and report_rows() == []
    assert stranger.post(f'/remove/{token}').status_code == 404    # already gone
    assert stranger.get(f'/remove/{token}').status_code == 404


def test_bad_removal_tokens_get_a_404(client):
    create(client)
    for token in ('nope', 'x' * 201, '%20', '.', 'a' * 10):
        assert client.get(f'/remove/{token}').status_code == 404, token
        assert client.post(f'/remove/{token}').status_code == 404, token
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 1


def test_removal_of_an_expired_paste_is_a_404(client):
    made = create(client)
    past = (datetime.datetime.now(UTC) - datetime.timedelta(minutes=1)).isoformat()
    conn = db()
    conn.execute('UPDATE pastes SET expires_at=?', (past,))
    conn.commit()
    assert client.get(f"/remove/{made['removal_token']}").status_code == 404
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 0


def test_guessing_removal_tokens_is_rate_limited(client):
    made = create(client)
    codes = [client.get(f'/remove/guess{i}').status_code for i in range(31)]
    assert codes[:30] == [404] * 30 and codes[30] == 429
    assert client.get(f"/remove/{made['removal_token']}").status_code == 429     # this visitor is locked out for now
    other = {'X-Forwarded-For': '203.0.113.5'}
    assert client.get(f"/remove/{made['removal_token']}", headers=other).status_code == 200


def test_removal_tokens_are_stored_only_as_hashes(client):
    made = create(client)
    row = db().execute('SELECT * FROM pastes').fetchone()
    assert re.fullmatch(r'[0-9a-f]{64}', row['removal_hash'])
    assert made['removal_token'] not in ' '.join(str(v) for v in tuple(row))
    assert row['removal_hash'] == papyrus._hash_token(made['removal_token'])


def test_the_old_json_delete_endpoint_is_gone(client):
    made = create(client)
    r = client.post(f"/api/delete/{made['paste_id']}", json={'token': made['removal_token']}, headers=ORIGIN)
    assert r.status_code == 404
    assert client.get(f"/{made['paste_id']}").status_code == 200


def test_no_script_ever_sees_a_removal_token(client):
    js = ''.join(open(os.path.join(os.path.dirname(papyrus.__file__), 'static', 'js', f), encoding='utf-8').read()
                 for f in ('app.js', 'paste_create.js', 'paste_view.js'))
    assert 'localStorage.setItem(\'del_' not in js and 'removal_token' not in js and 'deletion_token' not in js


# ── Configurable expiry times ─────────────────────────────────────────────────

@pytest.mark.parametrize('value,seconds', [
    (3600, 3600), (90, 90), (1.5, 1.5), ('10m', 600), ('36h', 129600), ('3d', 259200), ('2w', 1209600),
    ('1y', 31536000), (' 5 M ', 300), ('45s', 45),
])
def test_parse_duration(value, seconds):
    assert papyrus._parse_duration(value).total_seconds() == seconds


@pytest.mark.parametrize('value', [None, 'never'])
def test_parse_duration_never(value):
    assert papyrus._parse_duration(value) is None


@pytest.mark.parametrize('value', [0, -5, True, False, '', '0m', '5x', 'abc', '1.5h', '10', 'm5', ['1d'], {}])
def test_parse_duration_rejects_nonsense(value):
    with pytest.raises(ValueError):
        papyrus._parse_duration(value)


@pytest.mark.parametrize('seconds,label', [
    (60, '1 minute'), (90 * 60, '90 minutes'), (3600, '1 hour'), (36 * 3600, '36 hours'), (86400, '1 day'),
    (3 * 86400, '3 days'), (604800, '1 week'), (14 * 86400, '2 weeks'), (30 * 86400, '1 month'),
    (60 * 86400, '2 months'), (365 * 86400, '1 year'), (45, '45 seconds'),
])
def test_humanize(seconds, label):
    assert papyrus._humanize(datetime.timedelta(seconds=seconds)) == label
    assert papyrus._humanize(None) == 'Never'


def test_expiries_map_in_every_form():
    options, default = papyrus._load_expiry_options({
        'expiries': {'10min': '10m', '1day': 86400, 'later': {'duration': '2w', 'label': 'Two weeks, tops'},
                     'forever': None, 'also-never': {'duration': None}},
        'default_expiry': '1day'})
    assert [o['value'] for o in options] == ['10min', '1day', 'later', 'forever', 'also-never']   # order kept
    assert [o['label'] for o in options] == ['10 minutes', '1 day', 'Two weeks, tops', 'Never', 'Never']
    assert [o['delta'] and o['delta'].total_seconds() for o in options] == [600, 86400, 1209600, None, None]
    assert default == '1day'


def test_the_default_defaults_to_the_first_expiry():
    assert papyrus._load_expiry_options({'expiries': {'a': 60, 'b': 120}})[1] == 'a'


@pytest.mark.parametrize('cfg,message', [
    ({'expiries': {}}, 'at least one'),
    ({'expiries': []}, 'at least one'),
    ({'expiries': {'Bad Name': 60}}, 'lowercase'),
    ({'expiries': {'x' * 33: 60}}, 'lowercase'),
    ({'expiries': {'ok': 0}}, "'ok'"),
    ({'expiries': {'ok': '5x'}}, 'bad duration'),
    ({'expiries': {'ok': True}}, 'bad duration'),
    ({'expiries': {'ok': {'label': 'no duration'}}}, 'needs a "duration"'),
    ({'expiries': {'a': 60}, 'default_expiry': 'zzz'}, 'default_expiry'),
    ({'allow_expiry_options': ['1day'], 'expiry_labels': {}}, 'no longer used'),
    ({}, 'pastes.expiries is missing'),
    ({'expiries': {'x': 10 ** 18}}, 'too long'),
])
def test_broken_expiry_config_fails_loudly(cfg, message):
    with pytest.raises(RuntimeError) as exc:
        papyrus._load_expiry_options(cfg)
    assert message in str(exc.value)


def expires_in_seconds(pid):
    value = db().execute('SELECT expires_at FROM pastes WHERE id=?', (pid,)).fetchone()[0]
    return None if value is None else (papyrus._parse_expiry(value) - datetime.datetime.now(UTC)).total_seconds()


def test_custom_expiries_decide_when_a_paste_expires(client, monkeypatch):
    set_expiries(monkeypatch, {'10min': '10m', 'twohours': 7200, 'forever': None}, default='twohours')
    assert 590 < expires_in_seconds(create(client, expires_in='10min')['paste_id']) <= 600
    assert 7190 < expires_in_seconds(create(client, expires_in='twohours')['paste_id']) <= 7200
    assert expires_in_seconds(create(client, expires_in='forever')['paste_id']) is None


def test_no_expiry_given_means_the_default(client, monkeypatch):
    set_expiries(monkeypatch, {'short': '5m', 'long': '1d'}, default='long')
    for body in ({'content': 'x'}, {'content': 'x', 'expires_in': ''}, {'content': 'x', 'expires_in': None}):
        pid = client.post('/create', json=body).get_json()['paste_id']
        assert 86300 < expires_in_seconds(pid) <= 86400
    r = client.post('/create?title=t', data='x', content_type='text/plain')        # curl without expires_in
    assert 86300 < expires_in_seconds(curl_urls(r)['Paste URL'].rsplit('/', 1)[1]) <= 86400


def test_an_unknown_expiry_is_refused_not_silently_defaulted(client, monkeypatch):
    set_expiries(monkeypatch, {'short': '5m', 'long': '1d'})
    r = client.post('/create', json={'content': 'x', 'expires_in': '1year'})
    assert r.status_code == 400 and 'short, long' in r.get_json()['error'] and '1year' in r.get_json()['error']
    r = client.post('/create?expires_in=forever', data='x', content_type='text/plain')
    assert r.status_code == 400 and r.mimetype == 'text/plain' and 'short, long' in r.get_data(as_text=True)
    r = client.post('/curl', data={'raw': 'x', 'expires_in': 'x' * 500})
    assert r.status_code == 400 and len(r.get_data(as_text=True)) < 200
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 0


def test_a_far_future_expiry_counts_as_never(client, monkeypatch):
    set_expiries(monkeypatch, {'ages': 10 ** 12})          # ~31,700 years: past the year 9999 the calendar ends
    assert expires_in_seconds(create(client, expires_in='ages')['paste_id']) is None


def test_the_owner_cookie_follows_the_chosen_expiry(client, monkeypatch):
    set_expiries(monkeypatch, {'10min': '10m', 'forever': None})
    cookie = client.post('/create', json={'content': 'x', 'expires_in': '10min'}).headers['Set-Cookie']
    assert 500 < int(re.search(r'Max-Age=(\d+)', cookie).group(1)) <= 600


def test_editor_menu_lists_the_configured_expiries(client, monkeypatch):
    set_expiries(monkeypatch, {'10min': '10m', 'custom': {'duration': '3d', 'label': 'Three days <b>'}, 'never': None},
                 default='custom')
    html = client.get('/').get_data(as_text=True)
    menu = re.search(r'<select id="expires_in".*?</select>', html, re.S).group(0)
    assert re.findall(r'value="([^"]+)"', menu) == ['10min', 'custom', 'never']
    assert re.findall(r'>([^<]+)</option>', menu) == ['10 minutes', 'Three days &lt;b&gt;', 'Never']   # escaped
    assert re.search(r'<option value="custom"\s+selected', menu)
    assert '1year' not in menu


def test_api_config_exposes_the_expiries(client, monkeypatch):
    set_expiries(monkeypatch, {'a': '5m', 'b': '1d'}, default='b')
    pastes = client.get('/api/config').get_json()['pastes']
    assert pastes['default_expiry'] == 'b'
    assert pastes['expiry_options'] == [{'value': 'a', 'label': '5 minutes'}, {'value': 'b', 'label': '1 day'}]
    assert 'allow_expiry_options' not in pastes and 'expiry_labels' not in pastes


def test_the_shipped_example_config_is_valid_and_readable():
    cfg = json.load(open(os.path.join(os.path.dirname(papyrus.__file__), 'config.example.json')))['pastes']
    options, default = papyrus._load_expiry_options(cfg)
    assert [o['value'] for o in options] == ['1hour', '1day', '1week', '1month', '1year']
    assert [o['label'] for o in options] == ['1 hour', '1 day', '1 week', '1 month', '1 year'] and default == '1year'
    assert 'allow_expiry_options' not in cfg and 'expiry_labels' not in cfg


# ── Database layout ───────────────────────────────────────────────────────────

def test_a_fresh_database_gets_the_current_layout(client):
    conn = db()
    assert conn.execute('PRAGMA user_version').fetchone()[0] == papyrus.SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'pastes', 'comments', 'reports', 'rate_limits'} <= tables
    assert not [t for t in tables if 'legacy' in t]


def test_starting_twice_keeps_the_data(client):
    pid = create(client)['paste_id']
    papyrus._db_initialized = False
    papyrus.init_db()
    assert client.get(f'/raw/{pid}').status_code == 200


def test_a_database_from_an_earlier_development_version_is_refused_with_a_clear_message(tmp_path, monkeypatch):
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE pastes (id TEXT PRIMARY KEY, encrypted_data TEXT)')   # any layout without our version
    conn.commit()
    conn.close()
    monkeypatch.setattr(papyrus, 'DATABASE', path)
    monkeypatch.setattr(papyrus, '_db_initialized', False)
    with pytest.raises(RuntimeError) as exc:
        papyrus.init_db()
    assert 'delete or move' in str(exc.value) and path in str(exc.value)
    assert sqlite3.connect(path).execute('PRAGMA user_version').fetchone()[0] == 0     # left untouched
    monkeypatch.setattr(papyrus, '_db_initialized', True)       # (so the client fixture isn't confused afterwards)
