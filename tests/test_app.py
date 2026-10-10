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
    row = stored(pid)
    assert row['title'] == 'Untitled' and row['language'] == papyrus._pastes.get('default_language', 'text')


def test_oversized_paste_rejected(client, monkeypatch):
    monkeypatch.setattr(papyrus, 'MAX_PASTE_BYTES', 100)
    assert client.post('/create', json={'content': 'x' * 101}).status_code == 413


def test_curl_style_create(client):
    r = upload(client, ('', 'print(1)\n'), title='notes', language='python', expires_in='1day')
    assert r.status_code == 200
    urls = curl_urls(r)
    pid = urls['Paste URL'].rsplit('/', 1)[1]
    assert urls['Paste URL'] == f'http://localhost/{pid}' and urls['Raw URL'] == f'http://localhost/raw/{pid}'
    assert 'X-Deletion-Token' not in r.headers          # the removal URL replaced the header
    assert client.get(f'/raw/{pid}').get_data(as_text=True) == 'print(1)\n'
    row = stored(pid)
    assert (row['title'], row['language']) == ('notes', 'python')
    assert db().execute('SELECT expires_at FROM pastes WHERE id=?', (pid,)).fetchone()[0]


# ── Comments / Markdown ───────────────────────────────────────────────────────

def comment(client, pid, content, nick='', origin=ORIGIN, parent=''):
    return client.post(f'/{pid}/comment', headers=origin,
                       data={'content': content, 'nick': nick, 'parent': parent})


def comment_id(response):
    return response.headers['Location'].rsplit('#c-', 1)[1]


def test_comment_roundtrip_renders_markdown(client):
    pid = create(client, comments=True)['paste_id']
    r = comment(client, pid, '**bold** and `code`\n\n```python\nprint(1)\n```', nick='ann')
    assert r.status_code == 303 and '#c-' in r.headers['Location']
    page = client.get(f'/{pid}').get_data(as_text=True)
    assert '<strong>bold</strong>' in page
    assert '<code>code</code>' in page
    assert 'class="language-python"' in page
    assert 'ann' in page


def test_comment_markdown_is_sanitised(client):
    pid = create(client, comments=True)['paste_id']
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


def test_comment_requires_comments_and_origin(client):
    off = create(client)['paste_id']
    assert comment(client, off, 'hi').status_code == 403
    on = create(client, comments=True)['paste_id']
    assert comment(client, on, 'hi', origin={'Origin': 'http://evil.example'}).status_code == 403
    assert comment(client, on, 'hi', origin={}).status_code == 403
    assert comment(client, on, '   ').status_code == 400
    assert comment(client, 'deadbeef0000', 'hi').status_code == 404


def test_comment_limits(client, monkeypatch):
    pid = create(client, comments=True)['paste_id']
    assert comment(client, pid, 'x' * 10241).status_code == 413
    monkeypatch.setattr(papyrus, '_MAX_COMMENTS_PER_PASTE', 2)
    assert comment(client, pid, 'one').status_code == 303
    assert comment(client, pid, 'two').status_code == 303
    assert comment(client, pid, 'three').status_code == 429


def test_comment_nick_is_cleaned(client):
    pid = create(client, comments=True)['paste_id']
    comment(client, pid, 'hi', nick='  a\x00b' + 'z' * 100)
    nick = db().execute('SELECT nick FROM comments').fetchone()[0]
    assert '\x00' not in nick and len(nick) <= 32


def test_comment_posting_is_rate_limited(client):
    pid = create(client, comments=True)['paste_id']
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
    made = create(client, comments=True)
    comment(client, made['paste_id'], 'hi')
    assert db().execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 1
    client.post(f"/remove/{made['removal_token']}")
    assert db().execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 0


def test_purge_expired_removes_comments_too(client):
    pid = create(client, comments=True)['paste_id']
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
    pid = create(client, comments=True)['paste_id']
    a = comment_id(comment(client, pid, 'top level A'))
    b = comment_id(comment(client, pid, 'reply B', parent=a))
    c = comment_id(comment(client, pid, 'reply C to B', parent=b))
    d = comment_id(comment(client, pid, 'top level D'))
    rows = db().execute('SELECT id, parent_id FROM comments').fetchall()
    assert {r['id']: r['parent_id'] for r in rows} == {a: None, b: a, c: b, d: None}

    page = client.get(f'/{pid}').get_data(as_text=True)
    assert 'id="commentsCount">4<' in page  # the count covers replies too
    # B's box sits inside A's replies container, C inside B's, and D after A entirely.
    pos = {k: page.index(f'id="c-{k}"') for k in (a, b, c, d)}
    assert pos[a] < pos[b] < pos[c] < pos[d]
    a_block = page[pos[a]:pos[d]]
    assert f'id="c-{b}"' in a_block and f'id="c-{c}"' in a_block


def test_reply_to_unknown_or_foreign_comment_is_rejected(client):
    p1 = create(client, comments=True)['paste_id']
    p2 = create(client, comments=True)['paste_id']
    other = comment_id(comment(client, p2, 'on the other paste'))
    assert comment(client, p1, 'x', parent='nope').status_code == 400
    assert comment(client, p1, 'x', parent=other).status_code == 400


def test_reply_depth_is_capped(client):
    pid = create(client, comments=True)['paste_id']
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
    pid = create(client, comments=True)['paste_id']
    comment(client, pid, '![tracker](http://tracker.example/p.png) <img src=http://evil.example/x.png>')
    for path in ('/', f'/{pid}'):
        html = client.get(path).get_data(as_text=True)
        imgs = re.findall(r'<img\b[^>]*>', html)
        assert len(imgs) == 1 and 'src="/static/img/logo-64.png"' in imgs[0]   # the navbar mark, nothing else
    page = client.get(f'/{pid}').get_data(as_text=True)
    assert 'href="http://tracker.example/p.png"' in page        # a Markdown image became a link
    assert '<img src=http://evil.example' not in page            # raw HTML stayed escaped text


def test_binary_upload_is_rejected(client):
    assert upload(client, ('x.png', PNG)).status_code == 415
    assert upload(client, ('x.txt', b'valid start \xff\xfe invalid utf-8')).status_code == 415
    r = client.post('/create', data=PNG, content_type='image/png')   # the JSON endpoint takes JSON only
    assert r.status_code == 400
    r = client.post('/create', data='plain text', content_type='text/plain')
    assert r.status_code == 400 and 'JSON body required' in r.get_json()['error']
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 0


def test_nul_bytes_are_rejected_in_json_pastes_and_comments(client):
    assert client.post('/create', json={'content': 'ab\x00cd'}).status_code == 415
    pid = create(client, comments=True)['paste_id']
    assert comment(client, pid, 'a\x00b').status_code == 415


def test_any_utf8_text_is_still_accepted(client):
    text = 'héllo wörld \u2603 \U0001F600\n\tindented\r\n<svg xmlns="http://www.w3.org/2000/svg"/>'
    r = upload(client, ('unicode.txt', text.encode('utf-8')))
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
    pid = create(client, comments=True)['paste_id']
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
    pid = create(client, title='My <b>paste</b>', comments=True, content='secret <i>code</i>')['paste_id']
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
    p1 = create(client, comments=True)['paste_id']
    p2 = create(client, comments=True)['paste_id']
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
    pid = create(client, comments=True)['paste_id']
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
    pid = create(client, title='<b>Spammy</b> paste', content='buy   <i>cheap</i>\n pills', comments=True)['paste_id']
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
    pid = create(client, comments=True)['paste_id']
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
    pid = create(client, comments=True)['paste_id']
    a = comment_id(comment(client, pid, 'top'))
    b = comment_id(comment(client, pid, 'abusive', parent=a))
    c = comment_id(comment(client, pid, 'innocent reply', parent=b))
    report(client, pid, comment=b)
    assert client.post(f'/admin/comments/{b}/delete', headers=ORIGIN).status_code == 403    # not admin
    login(client)
    assert client.post(f'/admin/comments/{b}/delete', headers={'Origin': 'http://evil'}).status_code == 403
    r = client.post(f'/admin/comments/{b}/delete', data={'return': 'paste'}, headers=ORIGIN)
    assert r.status_code == 302 and r.headers['Location'].endswith(f'/{pid}#commentsPanel')
    rows = {x['id']: x['parent_id'] for x in db().execute('SELECT id, parent_id FROM comments')}
    assert rows == {a: None, c: a}                                    # the reply moved up to a
    assert report_rows() == []                                        # its reports went with it
    r = client.post(f'/admin/comments/{c}/delete', headers=ORIGIN)    # default: back to the dashboard
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin')
    assert client.post('/admin/comments/missing/delete', headers=ORIGIN).status_code == 302


def test_admin_delete_buttons_on_comments_are_for_admins_only(client):
    pid = create(client, comments=True)['paste_id']
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
    """title, filename, language and content of the paste's first file."""
    return db().execute('SELECT p.title, f.filename, f.language, f.content FROM pastes p JOIN files f ON f.paste_id = p.id'
                        ' WHERE p.id = ? ORDER BY f.position LIMIT 1', (pid,)).fetchone()


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
    r = upload(client, ('script.rb', 'puts 1\n'))
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
    for fragment in ('id="title"', 'placeholder="Description (optional)"', 'f-name', 'f-lang',
                     'Indent mode', 'value="spaces"', 'value="tabs"', 'Indent size',
                     'Line wrap mode', 'value="soft"', 'value="off"', 'No wrap', 'Soft wrap'):
        assert fragment in html, fragment
    assert [m for m in re.findall(r'<option value="(\d)"', html) if m in '248'][:3] == ['2', '4', '8']   # (the card is also in the <template>)
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
    r = upload(client, ('a.py', 'print(1)\n'), expires_in='1day')
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


def test_the_old_terminal_forms_are_gone(client):
    assert client.post('/curl', data={'raw': 'x'}).status_code in (404, 405)               # no /curl endpoint
    r = client.post('/', data={'raw': 'x', 'filename': 'a.txt'}, content_type='multipart/form-data')
    assert r.status_code == 400 and "file=@-" in r.get_data(as_text=True)                  # raw= is not a file
    r = client.post('/create?filename=a.txt', data='x', content_type='text/plain')
    assert r.status_code == 400                                                              # no plain-text bodies
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 0


def test_curl_endpoint_errors_are_plain_text(client):
    r = client.post('/', data={}, content_type='multipart/form-data')
    assert (r.status_code, r.mimetype) == (400, 'text/plain') and 'Nothing to save' in r.get_data(as_text=True)
    r = upload(client, ('a.txt', b'a\x00b'))
    assert r.status_code == 415 and r.mimetype == 'text/plain'
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 0


def test_curl_endpoint_takes_pastes_up_to_the_size_limit(client):
    big = 'x' * (1024 * 1024)                                       # far beyond Werkzeug's 500 kB form default
    assert upload(client, ('big.txt', big)).status_code == 200
    too_big = 'x' * (papyrus.MAX_PASTE_BYTES + 10)
    r = upload(client, ('huge.txt', too_big))
    assert r.status_code == 413 and r.get_data(as_text=True).strip() == 'Paste too large'


def test_curl_endpoint_shares_the_creation_rate_limit(client):
    for _ in range(10):
        assert upload(client, ('', 'x')).status_code == 200
    r = upload(client, ('', 'x'))
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


def test_opening_the_removal_link_removes_the_paste_at_once(client):
    made = create(client, title='My <b>paste</b>', content='some   text\nhere')
    pid, token = made['paste_id'], made['removal_token']
    page = fresh_client().get(f'/remove/{token}')                  # no cookie, no confirmation step
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and 'Paste removed' in html
    assert 'My &lt;b&gt;paste&lt;/b&gt;' in html and 'some   text' not in html      # the title, escaped; no content shown
    assert 'Remove this paste?' not in html and '<form' not in html.split('</nav>')[1].split('</main>')[0]
    assert client.get(f'/{pid}').status_code == 404
    assert client.get(f'/remove/{token}').status_code == 404       # gone for good


def test_head_requests_never_delete(client):
    made = create(client)
    token = made['removal_token']
    assert client.head(f'/remove/{token}').status_code == 200      # a bot probing the link
    assert client.get(f"/{made['paste_id']}").status_code == 200   # ...removed nothing
    assert client.head('/remove/not-a-token').status_code == 404


def test_the_remove_now_button_removes_without_asking(client):
    made = create(client)
    link = re.search(r'href="(/remove/[^"]+)"[^>]*>Remove now', client.get(f"/{made['paste_id']}").get_data(as_text=True)).group(1)
    assert 'Paste removed' in client.get(link).get_data(as_text=True)
    assert client.get(f"/{made['paste_id']}").status_code == 404


def test_removing_through_the_link_needs_no_cookie_and_cleans_up(client):
    made = create(client, comments=True)
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
    r = upload(client, ('', 'x'), title='t')                                          # curl without expires_in
    assert 86300 < expires_in_seconds(curl_urls(r)['Paste URL'].rsplit('/', 1)[1]) <= 86400


def test_an_unknown_expiry_is_refused_not_silently_defaulted(client, monkeypatch):
    set_expiries(monkeypatch, {'short': '5m', 'long': '1d'})
    r = client.post('/create', json={'content': 'x', 'expires_in': '1decade'})
    assert r.status_code == 400 and 'short, long' in r.get_json()['error'] and '1decade' in r.get_json()['error']
    r = upload(client, ('', 'x'), expires_in='forever')
    assert r.status_code == 400 and r.mimetype == 'text/plain' and 'short, long' in r.get_data(as_text=True)
    r = upload(client, ('', 'x'), expires_in='x' * 500)
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
    assert '1month' not in menu


def test_api_config_exposes_the_expiries(client, monkeypatch):
    set_expiries(monkeypatch, {'a': '5m', 'b': '1d'}, default='b')
    pastes = client.get('/api/config').get_json()['pastes']
    assert pastes['default_expiry'] == 'b'
    assert pastes['expiry_options'] == [{'value': 'a', 'label': '5 minutes'}, {'value': 'b', 'label': '1 day'}]
    assert 'allow_expiry_options' not in pastes and 'expiry_labels' not in pastes


def test_the_shipped_example_config_is_valid_and_readable():
    cfg = json.load(open(os.path.join(os.path.dirname(papyrus.__file__), 'config.example.json')))['pastes']
    options, default = papyrus._load_expiry_options(cfg)
    assert [o['value'] for o in options] == ['1hour', '1day', '1week', '1month']
    assert [o['label'] for o in options] == ['1 hour', '1 day', '1 week', '1 month'] and default == '1week'
    # Papyrus is for temporary pastes: nothing in the shipped config lives longer than a month.
    assert all(o['delta'] is not None and o['delta'] <= datetime.timedelta(days=30) for o in options)
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


def test_long_lines_do_not_wrap_by_default(client):
    editor = client.get('/').get_data(as_text=True)
    assert re.search(r'<option value="off" selected>No wrap</option>\s*<option value="soft">Soft wrap</option>', editor)
    pid = create(client, content='x' * 500)['paste_id']
    page = client.get(f'/{pid}').get_data(as_text=True)
    assert 'class="view-full"' in page and 'wrap-lines' not in page.split('id="viewFull"')[0]
    assert not re.search(r'id="wrapToggle"[^>]*checked', page)


def test_every_paste_control_lives_in_the_file_bar(client):
    html = client.get('/').get_data(as_text=True)
    top_bar, form = html.split('</nav>')
    # Top bar: logo | description | theme toggle. Nothing else.
    assert top_bar.index('class="brand"') < top_bar.index('id="title"') < top_bar.index('id="themeToggle"')
    for control in ('submitBtn', 'clearBtn', 'expires_in', 'allowComments', 'f-name', 'f-lang', 'f-indent-mode'):
        assert control not in top_bar, control
    # Per file: Filename | Language | Indent mode | Indent size | Wrap | Remove file. Per paste, below (script moves
    # them into the first file's bar while there is one file): Add file | Expiry | Allow comments | Clear | Save
    order = [form.index(c) for c in ('f-name', 'f-lang', 'f-indent-mode', 'f-indent-size', 'f-wrap', 'f-remove', 'f-content',
                                     'id="addFileBtn"', 'id="expires_in"', 'id="allowComments"', 'id="clearBtn"', 'id="submitBtn"')]
    assert order == sorted(order)
    assert 'Allow comments' in form and not re.search(r'>\s*Discuss\s*<|> Discuss\b', html)   # the old label is gone
    assert 'Control+Shift+M' in form


def nav_of(html):
    return html.split('</nav>')[0]


def test_filename_and_stats_live_in_the_top_bar(client):
    pid = create(client, filename='deploy.sh', content='echo a\necho b\n')['paste_id']
    html = client.get(f'/{pid}').get_data(as_text=True)
    nav = nav_of(html)
    assert 'class="file-header"' not in html                      # no separate strip under the bar any more
    # Logo | filename (stats) | Show lines | Wrap | Raw | Copy | Download | Report | New | Theme toggle
    order = [nav.index(m) for m in ('class="brand"', 'class="file-name"', 'class="file-meta"', 'id="lineNoToggle"',
                                    'id="wrapToggle"', 'data-role="raw"', 'data-role="copy"', 'data-role="download"',
                                    '>Report<', '>New<', 'id="themeToggle"')]
    assert order == sorted(order)
    assert 'Show lines' in nav and 'bash &middot; 2 lines &middot; 14 B' in nav
    assert 'paste-description' not in nav                         # no description: just the filename


def test_a_description_shows_in_front_of_the_filename_only_when_there_is_one(client):
    nav = nav_of(client.get('/' + create(client, title='Deploy <b>helper</b>', filename='deploy.sh')['paste_id']).get_data(as_text=True))
    assert nav.index('paste-description') < nav.index('class="file-name"')
    assert 'Deploy &lt;b&gt;helper&lt;/b&gt;' in nav and '<b>helper' not in nav
    nav = nav_of(client.get('/' + create(client, title='Just notes')['paste_id']).get_data(as_text=True))
    assert 'Just notes' in nav and 'class="file-name"' not in nav     # a description alone is the name
    nav = nav_of(client.get('/' + create(client)['paste_id']).get_data(as_text=True))
    assert '<span class="file-name">untitled</span>' in nav and 'paste-description' not in nav
    nav = nav_of(client.get('/' + create(client, title='deploy.sh', filename='deploy.sh')['paste_id']).get_data(as_text=True))
    assert 'paste-description' not in nav                         # a description equal to the filename isn't repeated


# ── Colour themes: palettes from the config, a dropdown, sober icons ──────────

def palette(**overrides):
    colours = dict(primary='#c2410c', primary_hover='#9a3412', on_primary='#ffffff', background='#ffffff',
                   surface='#f6f6f7', border='#e4e4e7', text='#18181b', text_secondary='#52525b',
                   text_muted='#a1a1aa', danger='#b91c1c')
    spec = {'label': 'Test', 'light': dict(colours), 'dark': dict(colours, background='#111113')}
    spec.update(overrides)
    return spec


def theme_cfg(**palettes):
    return {'palettes': palettes or {'one': palette()}}


def test_palettes_load_in_order_with_a_default():
    palettes, default = papyrus._load_palettes({'palettes': {'b': palette(label='Bee'), 'a': palette()}, 'palette': 'a'})
    assert [p['value'] for p in palettes] == ['b', 'a'] and default == 'a'
    assert palettes[0]['label'] == 'Bee' and palettes[1]['logo_filter'] == 'none'
    assert papyrus._load_palettes({'palettes': {'b': palette(), 'a': palette()}})[1] == 'b'     # first is the default


def test_the_label_defaults_to_the_name():
    spec = palette()
    del spec['label']
    assert papyrus._load_palettes({'palettes': {'solo': spec}})[0][0]['label'] == 'solo'


def test_short_hex_colours_are_fine():
    spec = palette()
    spec['light']['text'] = '#111'
    assert papyrus._load_palettes({'palettes': {'x': spec}})[0][0]['light']['text'] == '#111'


def broken(mutate):
    spec = palette()
    mutate(spec)
    return {'palettes': {'x': spec}}


@pytest.mark.parametrize('cfg,message', [
    ({}, 'at least one palette'),
    ({'palettes': {}}, 'at least one palette'),
    ({'palettes': []}, 'at least one palette'),
    ({'palettes': {'Bad Name': palette()}}, 'lowercase'),
    ({'palettes': {'x': 'blue'}}, 'must be a map'),
    (broken(lambda s: s.pop('dark')), 'needs a "dark"'),
    (broken(lambda s: s.__setitem__('light', 'red')), 'needs a "light"'),
    (broken(lambda s: s['light'].pop('primary')), 'missing primary'),
    (broken(lambda s: s['dark'].pop('danger')), 'dark: missing danger'),
    (broken(lambda s: s['light'].__setitem__('primary', 'red')), "must be a hex colour"),
    (broken(lambda s: s['light'].__setitem__('primary', '#12345')), "must be a hex colour"),
    (broken(lambda s: s['light'].__setitem__('primary', 'red; } body { display: none')), "must be a hex colour"),
    (broken(lambda s: s['light'].__setitem__('text', 12)), "must be a hex colour"),
    (broken(lambda s: s.__setitem__('logo_filter', 'none; } body { x: y')), 'logo_filter'),
    (broken(lambda s: s.__setitem__('logo_filter', 'url(http://evil/x.svg)')), 'logo_filter'),
    ({'palettes': {'x': palette()}, 'palette': 'nope'}, 'is not one of'),
])
def test_broken_palettes_fail_at_startup(cfg, message):
    with pytest.raises(RuntimeError) as exc:
        papyrus._load_palettes(cfg)
    assert message in str(exc.value)


def test_the_generated_stylesheet_covers_every_palette_and_mode():
    palettes, _ = papyrus._load_palettes({'palettes': {'one': palette(logo_filter='hue-rotate(90deg)'),
                                                      'two': palette(label='Two')}})
    css = papyrus._build_theme_css(palettes)
    for name in ('one', 'two'):
        assert f'[data-palette="{name}"] {{' in css and f'[data-palette="{name}"][data-theme="dark"] {{' in css
    assert css.count('{') == css.count('}') == 4
    assert '--logo-filter: hue-rotate(90deg);' in css and '--logo-filter: none;' in css
    assert 'color-scheme: light;' in css and 'color-scheme: dark;' in css
    light, dark = css.split('[data-theme="dark"]')[0], css.split('[data-theme="dark"]')[1]
    assert '--code-bg: #f6f6f7;' in light and '--nav-bg: #ffffff;' in light     # light: code on the surface colour
    assert '--code-bg: #111113;' in dark and '--nav-bg: #f6f6f7;' in dark       # dark: code on the background


def test_theme_css_is_served_and_cacheable(client):
    r = client.get('/theme.css')
    assert r.status_code == 200 and r.mimetype == 'text/css'
    assert 'max-age' in r.headers['Cache-Control'] and r.get_data(as_text=True) == papyrus._THEME_CSS
    page = client.get('/').get_data(as_text=True)
    assert f'/theme.css?v={papyrus._THEME_CSS_VERSION}' in page
    assert page.index('/theme.css?') < page.index('css/style.css')           # the theme comes first
    other, _ = papyrus._load_palettes({'palettes': {'x': palette(primary='#000000')}})
    assert hashlib_sha(papyrus._build_theme_css(other)) != papyrus._THEME_CSS_VERSION   # a change busts the cache


def hashlib_sha(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:10]


def test_pages_start_in_the_default_palette_and_remember_a_choice_before_painting(client):
    html = client.get('/').get_data(as_text=True)
    assert re.search(r'<html lang="en" data-palette="rust" data-theme-default="(dark|light|auto)">', html)
    head = html.split('</head>')[0]
    assert "localStorage.getItem('palette')" in head and 'setAttribute(\'data-palette\', p)' in head
    names = re.search(r'(\[[^\]]*\])\.indexOf\(p\)', head).group(1)
    assert json.loads(names) == ['rust', 'parchment', 'green', 'violet', 'mono']      # only configured names are accepted


def test_the_dropdown_sits_next_to_the_toggle_on_every_page(client):
    pid = create(client, comments=True)['paste_id']
    tok = create(client)['removal_token']
    for path in ('/', f'/{pid}', f'/{pid}/report', f'/remove/{tok}'):
        html = client.get(path).get_data(as_text=True)
        nav = html.split('</nav>')[0]
        assert nav.index('id="paletteSelect"') < nav.index('id="themeToggle"'), path
        menu = re.search(r'<select id="paletteSelect".*?</select>', nav, re.S).group(0)
        assert re.findall(r'<option value="([^"]+)"', menu) == ['rust', 'parchment', 'green', 'violet', 'mono']
        assert re.findall(r'>([^<]+)</option>', menu) == ['Rust', 'Parchment', 'Green', 'Violet', 'Mono']
        assert re.search(r'<option value="rust" selected>', menu)
    gone = client.post(f'/remove/{tok}')
    assert 'id="paletteSelect"' in gone.get_data(as_text=True)


def test_one_palette_means_no_dropdown(client, monkeypatch):
    only, default = papyrus._load_palettes({'palettes': {'solo': palette()}})
    monkeypatch.setattr(papyrus, '_PALETTES', only)
    monkeypatch.setattr(papyrus, '_DEFAULT_PALETTE', default)
    html = client.get('/').get_data(as_text=True)
    assert 'id="paletteSelect"' not in html and 'id="themeToggle"' in html and 'data-palette="solo"' in html


def test_the_light_dark_toggle_can_be_turned_off_without_losing_the_dropdown(client, monkeypatch):
    monkeypatch.setitem(papyrus._theme, 'allow_user_toggle', False)
    html = client.get('/').get_data(as_text=True)
    assert 'id="themeToggle"' not in html and 'id="paletteSelect"' in html


def test_the_toggle_uses_plain_line_icons_not_emoji(client):
    html = client.get('/').get_data(as_text=True)
    button = re.search(r'<button id="themeToggle".*?</button>', html, re.S).group(0)
    assert 'class="icon-moon"' in button and 'class="icon-sun"' in button and button.count('<svg') == 2
    root = os.path.dirname(papyrus.__file__)
    for folder, ext in (('templates', '.html'), ('static/js', '.js'), ('static/css', '.css')):
        for name in os.listdir(os.path.join(root, folder)):
            if name.endswith(ext):
                text = open(os.path.join(root, folder, name), encoding='utf-8').read()
                assert '\U0001F319' not in text and '\u2600' not in text, name         # no moon or sun emoji


def test_colours_are_defined_in_one_place():
    root = os.path.dirname(papyrus.__file__)
    css = open(os.path.join(root, 'static', 'css', 'style.css'), encoding='utf-8').read()
    js = open(os.path.join(root, 'static', 'js', 'app.js'), encoding='utf-8').read()
    assert not re.search(r'--(primary|bg|surface|border|text|danger)\s*:', css)     # only /theme.css defines them
    assert 'setProperty' not in js.replace('--radius', '') or "'--radius'" in js      # no colours pushed in from JS
    assert '_CSS_VAR_MAP' not in js and 'applyConfigCssVars' not in js and 'initPalette' in js


def test_api_config_lists_the_palettes_not_their_colours(client):
    theme = client.get('/api/config').get_json()['theme']
    assert theme['palette'] == 'rust' and [p['value'] for p in theme['palettes']] == ['rust', 'parchment', 'green', 'violet', 'mono']
    assert 'light' not in theme and 'dark' not in theme and '#' not in json.dumps(theme)


def test_the_shipped_palettes_are_complete_and_readable():
    cfg = json.load(open(os.path.join(os.path.dirname(papyrus.__file__), 'config.example.json')))['theme']
    palettes, default = papyrus._load_palettes(cfg)
    assert default == 'rust' and palettes[0]['value'] == 'rust' and palettes[0]['logo_filter'] == 'none'
    def luminance(hex_colour):
        h = hex_colour.lstrip('#')
        h = ''.join(c * 2 for c in h) if len(h) == 3 else h
        channel = lambda v: (v / 255 / 12.92) if v / 255 <= 0.03928 else (((v / 255) + 0.055) / 1.055) ** 2.4
        r, g, b = (channel(int(h[i:i + 2], 16)) for i in (0, 2, 4))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    def contrast(a, b):
        la, lb = sorted((luminance(a), luminance(b)), reverse=True)
        return (la + 0.05) / (lb + 0.05)
    for pal in palettes:
        for mode in ('light', 'dark'):
            c = pal[mode]
            assert contrast(c['text'], c['background']) >= 7, (pal['value'], mode, 'body text')
            assert contrast(c['text_secondary'], c['background']) >= 4.5, (pal['value'], mode, 'secondary text')
            assert contrast(c['primary'], c['background']) >= 3, (pal['value'], mode, 'accent on background')
            assert contrast(c['on_primary'], c['primary']) >= 4.5, (pal['value'], mode, 'text on the accent')


def test_every_function_app_js_calls_is_defined():
    """A cleanup once deleted a still-used helper; unit tests can't run the script, so check the names."""
    js = open(os.path.join(os.path.dirname(papyrus.__file__), 'static', 'js', 'app.js'), encoding='utf-8').read()
    for name in ('initialiseTheme', 'applyUiVars', 'applyTheme', 'swapPrismTheme', 'updateThemeToggle',
                 'initPalette', 'toggleTheme', 'copyToClipboard'):
        if re.search(rf'\b{name}\(', js):
            assert re.search(rf'function\s+{name}\b|{name}\s*=\s*(async\s*)?(function|\()', js), f'{name} is called but not defined'


# ── Site notice and footer ────────────────────────────────────────────────────

def site_text(monkeypatch, key, value):
    monkeypatch.setitem(papyrus._site, key, value)
    return papyrus._render_site_text(key)


def test_site_text_is_inline_markdown(monkeypatch):
    html = site_text(monkeypatch, 'footer', '**Hello** and [terms](https://example.org/terms) &mdash; bare https://example.org/x')
    assert html == ('<strong>Hello</strong> and <a href="https://example.org/terms" rel="noopener noreferrer nofollow">terms</a> '
                    '&amp;mdash; bare <a href="https://example.org/x" rel="noopener noreferrer nofollow">https://example.org/x</a>')
    assert not str(html).startswith('<p>')                      # inline, no paragraph wrapper


def test_site_text_cannot_inject_html_or_scripts(monkeypatch):
    html = str(site_text(monkeypatch, 'notice', '<script>alert(1)</script>\n\nsee <b onclick="x()">this</b> '
                                                 '[x](javascript:alert(3)) ![pic](http://tracker.example/p.png)'))
    assert '<script' not in html and '<img' not in html and '<b ' not in html and '<b>' not in html
    assert '&lt;script&gt;' in html                              # raw HTML survives only as inert text
    assert 'href="javascript' not in html.lower()
    assert 'href="http://tracker.example/p.png"' in html          # an image becomes a plain link, as in comments


def test_longer_site_text_keeps_its_paragraphs(monkeypatch):
    html = str(site_text(monkeypatch, 'footer', 'First line.\n\nSecond paragraph.'))
    assert html.count('<p>') == 2


@pytest.mark.parametrize('value', ['', '   \n ', None])
def test_empty_site_text_renders_nothing(monkeypatch, value):
    assert str(site_text(monkeypatch, 'footer', value)) == ''


@pytest.mark.parametrize('value', [42, ['a'], {'a': 1}, 'x' * 1001])
def test_bad_site_text_fails_at_startup(monkeypatch, value):
    monkeypatch.setitem(papyrus._site, 'notice', value)
    with pytest.raises(RuntimeError) as exc:
        papyrus._render_site_text('notice')
    assert 'site.notice' in str(exc.value)


def test_the_shipped_footer_is_the_disclaimer_and_there_is_no_notice():
    site = json.load(open(os.path.join(os.path.dirname(papyrus.__file__), 'config.example.json')))['site']
    assert site['footer'] == ('This website does not claim ownership of, copyright on, and assumes no '
                              'liability for provided content.')
    assert site['notice'] == '' and 'footer_text' not in site


def test_the_footer_is_on_every_page_and_no_notice_by_default(client):
    pid = create(client, comments=True)['paste_id']
    tok = create(client)['removal_token']
    paths = ['/', f'/{pid}', f'/{pid}/report', f'/remove/{tok}', '/admin/login', '/nothing-here-0000']
    for path in paths:
        html = client.get(path).get_data(as_text=True)
        assert '<footer class="site-footer">' in html and 'assumes no liability for provided content.' in html, path
        assert html.index('</main>') < html.index('<footer class="site-footer">') < html.index('<script', html.index('</main>')), path
        assert 'site-notice' not in html.replace('.site-notice', ''), path
    assert 'class="site-footer"' in client.post(f'/remove/{tok}').get_data(as_text=True)


def test_a_notice_shows_as_a_banner_under_the_top_bar(client, monkeypatch):
    monkeypatch.setattr(papyrus, '_SITE_NOTICE', site_text(monkeypatch, 'notice', 'Pastes are **temporary**. See the [rules](/rules).'))
    for path in ('/', '/admin/login'):
        html = client.get(path).get_data(as_text=True)
        assert html.index('</nav>') < html.index('<div class="site-notice"') < html.index('<main'), path
        assert '<strong>temporary</strong>' in html and 'href="/rules"' in html


def test_footer_text_and_notice_can_be_switched_off_but_the_source_link_stays(client, monkeypatch):
    monkeypatch.setattr(papyrus, '_SITE_FOOTER', site_text(monkeypatch, 'footer', ''))
    html = client.get('/').get_data(as_text=True)
    footer = re.search(r'<footer class="site-footer">.*?</footer>', html, re.S).group(0)
    assert 'class="site-notice"' not in html
    assert footer == ('<footer class="site-footer">'
                      f'<span class="footer-version">Powered by Papyrus {papyrus.__version__}</span> '
                      '<span class="footer-sep" aria-hidden="true">&middot;</span> '
                      '<a href="https://github.com/TehPeGaSuS/papyrus" rel="noopener">Source</a></footer>')


def test_the_source_link_follows_the_configured_footer_text(client):
    footer = re.search(r'<footer class="site-footer">.*?</footer>', client.get('/').get_data(as_text=True), re.S).group(0)
    assert footer.index('assumes no liability') < footer.index('&middot;') < footer.index('Powered by Papyrus') < footer.index('>Source</a>')
    assert 'href="https://github.com/TehPeGaSuS/papyrus"' in footer


def test_the_source_link_is_on_every_page(client):
    pid = create(client)['paste_id']
    for path in ('/', f'/{pid}', '/admin/login', '/nothing-here-0000'):
        assert '<a href="https://github.com/TehPeGaSuS/papyrus" rel="noopener">Source</a>' in client.get(path).get_data(as_text=True), path


def test_api_config_no_longer_has_the_unused_footer_text(client):
    assert 'footer_text' not in client.get('/api/config').get_json()['site']


# ── Markdown pastes: Source | Preview ─────────────────────────────────────────

MD = '# Title\n\nSome **bold** text, a [link](https://example.org) and `code`.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nprint(1)\n```\n'


def md_paste(client, content=MD, **extra):
    return create(client, filename='README.md', content=content, **extra)['paste_id']


def test_markdown_pastes_get_a_source_preview_switch(client):
    html = client.get('/' + md_paste(client)).get_data(as_text=True)
    assert 'data-role="source"' in html and 'data-role="preview"' in html and 'class="view-switch"' in html
    nav = html.split('</nav>')[0]
    # After the checkboxes, which Preview hides: the right-aligned group re-packs when they vanish, and a
    # switch in front of them would jump away from the cursor. Behind them it stays where it is.
    assert nav.index('id="wrapToggle"') < nav.index('data-role="source"') < nav.index('data-role="preview"') < nav.index('data-role="raw"')
    article = re.search(r'<article class="paste-preview[^>]*>.*?</article>', html, re.S).group(0)
    assert '<h1>Title</h1>' in article and '<strong>bold</strong>' in article and '<table>' in article
    assert 'href="https://example.org"' in article and 'class="language-python"' in article
    assert 'print(1)' in html and '# Title' in html                  # the source is still there


def test_source_is_the_default_and_a_link_opens_the_preview(client):
    pid = md_paste(client)
    plain = client.get(f'/{pid}').get_data(as_text=True)
    assert '<body class="">' in plain
    assert 'class="switch-btn active" data-role="source" data-file="0" href="/%s"' % pid in plain
    shared = client.get(f'/{pid}?view=preview').get_data(as_text=True)
    assert '<body class="previewing">' in shared                      # rendered on the server: no flash, no JavaScript
    assert 'class="switch-btn active" data-role="preview" data-file="0" href="/%s?view=preview"' % pid in shared
    assert '<body class="">' in client.get(f'/{pid}?view=bogus').get_data(as_text=True)


def test_other_pastes_have_no_preview(client):
    for content, extra in (('# not markdown', {'filename': 'notes.txt'}), ('<h1>hi</h1>', {'filename': 'page.html'}),
                           ('x', {})):
        pid = create(client, content=content, **extra)['paste_id']
        html = client.get(f'/{pid}?view=preview').get_data(as_text=True)
        assert 'data-role="preview"' not in html and 'class="paste-preview' not in html, extra
        assert '<body class="">' in html                              # ?view=preview is ignored


def test_the_language_menu_choice_counts_too(client):
    pid = create(client, content='# hi', language='markdown')['paste_id']      # no filename at all
    assert 'class="paste-preview' in client.get(f'/{pid}').get_data(as_text=True)


def test_hostile_markdown_is_sanitised_in_the_preview(client):
    evil = ('<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n\n[click](javascript:alert(3))\n\n'
            '![tracker](http://tracker.example/p.png)\n\n<a href="http://evil" onclick="y()">raw</a>\n\n'
            '<iframe src="http://evil"></iframe>\n\n<style>body{display:none}</style>\n')
    article = re.search(r'<article class="paste-preview.*?</article>',
                        client.get('/' + md_paste(client, evil)).get_data(as_text=True), re.S).group(0)
    # Whatever the Markdown said, only the sanitiser's tags and attributes can be in the page.
    allowed_tags = papyrus._MD_TAGS | {'article'}
    for tag, attrs in re.findall(r'<([a-z0-9]+)([^<>]*)>', article):
        assert tag in allowed_tags, tag
        names = set(re.findall(r'\s([a-z-]+)=', attrs))
        assert names <= {'a': {'href', 'rel', 'title'}, 'code': {'class'},
                         'article': {'class', 'aria-label'}}.get(tag, set()), (tag, names)
    assert not [a for a in re.findall(r'<a ([^>]*)>', article) if 'evil' in a]        # no real link to it
    assert 'href="javascript' not in article.lower()
    assert '&lt;script&gt;' in article                              # raw HTML is shown as inert text
    assert 'href="http://tracker.example/p.png"' in article and 'rel="noopener noreferrer nofollow"' in article


def test_big_markdown_is_not_previewable(client):
    big = 'word ' * (papyrus.MAX_PREVIEW_BYTES // 5 + 100)              # just over the limit
    assert len(big.encode()) > papyrus.MAX_PREVIEW_BYTES
    html = client.get('/' + md_paste(client, big)).get_data(as_text=True)
    assert 'data-role="preview"' not in html and 'class="paste-preview' not in html
    ok = 'word ' * (papyrus.MAX_PREVIEW_BYTES // 5 - 100)
    assert 'class="paste-preview' in client.get('/' + md_paste(client, ok)).get_data(as_text=True)


def test_raw_and_downloads_stay_the_source(client):
    pid = md_paste(client)
    assert client.get(f'/raw/{pid}').get_data(as_text=True) == MD
    assert client.get(f'/raw/{pid}?view=preview').get_data(as_text=True) == MD


def test_the_markdown_cache_only_keeps_short_texts():
    papyrus._render_markdown_cached.cache_clear()
    papyrus.render_markdown('short **text**')
    papyrus.render_markdown('x ' * papyrus._MARKDOWN_CACHE_LIMIT)       # a long document
    info = papyrus._render_markdown_cached.cache_info()
    assert info.currsize == 1 and info.maxsize == 1024                   # only the short one is kept
    assert papyrus.render_markdown('short **text**') == '<p>short <strong>text</strong></p>\n'
    assert papyrus._render_markdown_cached.cache_info().hits == 1


def test_preview_markup_is_in_the_script_and_stylesheet():
    root = os.path.dirname(papyrus.__file__)
    js = open(os.path.join(root, 'static', 'js', 'paste_view.js'), encoding='utf-8').read()
    css = open(os.path.join(root, 'static', 'css', 'style.css'), encoding='utf-8').read()
    assert 'initMarkdownPreview' in js and 'initMarkdownPreview();' in js
    assert '.file-card.previewing .view-full' in css and 'body.previewing .nav-label' in css


# ── Loading a text file into the editor ───────────────────────────────────────

def test_editor_page_offers_loading_a_file(client):
    html = client.get('/').get_data(as_text=True)
    assert f'data-max-bytes="{papyrus.MAX_PASTE_BYTES}"' in html           # the browser checks the same limit
    assert 'drop a text file' in re.search(r'placeholder="(Paste[^"]*)"', html).group(0)
    assert re.search(r'<input type="file" id="openFile" multiple hidden[^>]*>', html)
    assert '<button type="button" id="openFileBtn" class="link-btn">open files</button>' in html
    assert '<span id="fileNote" class="file-note" role="status" aria-live="polite">' in html
    assert 'Drop text files on the page' in html


def test_file_loading_is_client_side_and_text_only():
    root = os.path.dirname(papyrus.__file__)
    js = open(os.path.join(root, 'static', 'js', 'paste_create.js'), encoding='utf-8').read()
    assert "new TextDecoder('utf-8', { fatal: true })" in js            # invalid UTF-8 is refused, not repaired
    assert "indexOf('\\u0000')" in js and 'file.size > maxBytes' in js and "execCommand('insertText'" in js
    assert 'FormData' not in js and "'/upload" not in js               # the file itself is never sent anywhere
    css = open(os.path.join(root, 'static', 'css', 'style.css'), encoding='utf-8').read()
    assert '.file-editors.drop-target .full-editor' in css and '.file-note.error' in css


# ── Version ───────────────────────────────────────────────────────────────────

def test_the_version_is_semver():
    assert re.fullmatch(r'\d+\.\d+\.\d+', papyrus.__version__)


def test_the_version_is_in_the_footer_of_every_page(client):
    pid = create(client)['paste_id']
    for path in ('/', f'/{pid}', '/admin/login', '/nothing-here-0000'):
        footer = re.search(r'<footer class="site-footer">.*?</footer>', client.get(path).get_data(as_text=True), re.S).group(0)
        assert footer.index(f'class="footer-version">Powered by Papyrus {papyrus.__version__}</span>') < footer.index('>Source</a>'), path


def test_the_version_stays_when_the_footer_text_is_switched_off(client, monkeypatch):
    monkeypatch.setattr(papyrus, '_SITE_FOOTER', site_text(monkeypatch, 'footer', ''))
    assert f'Powered by Papyrus {papyrus.__version__}' in client.get('/').get_data(as_text=True)


def test_there_is_one_version_and_packaging_reads_it_from_app_py():
    import ast
    import tomllib
    root = os.path.dirname(papyrus.__file__)
    meta = tomllib.load(open(os.path.join(root, 'pyproject.toml'), 'rb'))
    assert 'version' not in meta['project'] and meta['project']['dynamic'] == ['version']
    assert meta['tool']['setuptools']['dynamic']['version'] == {'attr': 'app.__version__'}
    # setuptools reads a plain literal from the source without importing it (Flask need not be installed)
    tree = ast.parse(open(os.path.join(root, 'app.py'), encoding='utf-8').read())
    literal = [n.value for n in tree.body if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Name) and n.targets[0].id == '__version__']
    assert len(literal) == 1 and isinstance(literal[0], ast.Constant) and literal[0].value == papyrus.__version__


def test_version_flag_prints_it_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        papyrus._parse_args(['--version'])
    assert exc.value.code == 0 and capsys.readouterr().out.strip() == f'Papyrus {papyrus.__version__}'


def test_running_the_script_with_version_does_not_start_a_server():
    import subprocess
    root = os.path.dirname(papyrus.__file__)
    run = subprocess.run([sys.executable, os.path.join(root, 'app.py'), '--version'], cwd=root, capture_output=True,
                         text=True, timeout=60, env={**os.environ, 'SECRET_KEY': 'test-secret-key-not-for-production'})
    assert run.returncode == 0 and run.stdout.strip() == f'Papyrus {papyrus.__version__}', (run.stdout, run.stderr)


def test_one_time_notice_parameters_are_removed_from_the_address_bar():
    js = open(os.path.join(os.path.dirname(papyrus.__file__), 'static', 'js', 'paste_view.js'), encoding='utf-8').read()
    assert "['created', 'reported']" in js and 'history.replaceState' in js
    assert js.index('tidyAddressBar();') < js.index('initMarkdownPreview();')      # before anything reads the URL


# ── Several files in one paste ────────────────────────────────────────────────

def multi(client, files, **extra):
    r = client.post('/create', json={'files': files, **extra})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['paste_id']


def test_a_paste_can_hold_several_files(client):
    pid = multi(client, [{'filename': 'a.py', 'content': 'print(1)\n'}, {'filename': 'b.sh', 'content': 'echo hi\n'},
                         {'content': 'plain'}], title='Three')
    rows = db().execute('SELECT position, filename, language FROM files WHERE paste_id=? ORDER BY position', (pid,)).fetchall()
    assert [tuple(r) for r in rows] == [(0, 'a.py', 'python'), (1, 'b.sh', 'bash'), (2, 'file-3', papyrus._pastes.get('default_language', 'text'))]
    assert client.get(f'/raw/{pid}').get_data(as_text=True) == 'print(1)\n'                 # the first file
    assert client.get(f'/raw/{pid}/b.sh').get_data(as_text=True) == 'echo hi\n'
    assert client.get(f'/raw/{pid}/nope.txt').status_code == 404
    html = client.get(f'/{pid}').get_data(as_text=True)
    assert html.count('class="file-card') == 3 and '3 files' in html.split('</nav>')[0]
    assert 'print(1)' in html and 'echo hi' in html and 'class="file-head"' in html


def test_duplicate_names_are_numbered_and_the_title_falls_back_to_the_first_name(client):
    pid = multi(client, [{'filename': 'a.py', 'content': 'x'}, {'filename': 'a.py', 'content': 'y'}])
    names = [r[0] for r in db().execute('SELECT filename FROM files WHERE paste_id=? ORDER BY position', (pid,))]
    assert names == ['a.py', 'a-2.py'] and stored(pid)['title'] == 'a.py'
    assert client.get(f'/raw/{pid}/a-2.py').get_data(as_text=True) == 'y'


def test_file_count_limits_and_errors_name_the_file(client, monkeypatch):
    monkeypatch.setattr(papyrus, 'MAX_FILES', 2)
    assert client.post('/create', json={'files': [{'content': 'a'}] * 3}).status_code == 413
    assert client.post('/create', json={'files': []}).status_code == 400
    r = client.post('/create', json={'files': [{'filename': 'ok.txt', 'content': 'a'}, {'filename': 'e.txt', 'content': '  '}]})
    assert r.status_code == 400 and 'File 2 (e.txt)' in r.get_json()['error']
    monkeypatch.setattr(papyrus, 'MAX_PASTE_BYTES', 10)
    assert client.post('/create', json={'files': [{'content': 'x' * 6}, {'content': 'y' * 6}]}).status_code == 413   # the total counts


def test_a_one_file_paste_looks_as_before(client):
    html = client.get('/' + multi(client, [{'filename': 'a.py', 'content': 'x'}])).get_data(as_text=True)
    assert 'class="file-head"' not in html and 'python &middot; 1 line' in html.split('</nav>')[0]


def test_removing_a_multi_file_paste_removes_every_file(client):
    made = client.post('/create', json={'files': [{'content': 'a'}, {'content': 'b'}]}).get_json()
    client.get(f"/remove/{made['removal_token']}")
    assert db().execute('SELECT COUNT(*) FROM files').fetchone()[0] == 0


def test_the_markdown_preview_works_per_file(client):
    pid = multi(client, [{'filename': 'a.txt', 'content': 'x'}, {'filename': 'R.md', 'content': '# Hi'}])
    html = client.get(f'/{pid}?view=preview').get_data(as_text=True)
    assert html.count('data-role="preview"') == 1 and '<h1>Hi</h1>' in html
    assert html.count('class="file-card previewing"') == 1


# ── The API page ──────────────────────────────────────────────────────────────

def test_the_api_page_explains_the_ways_to_post(client):
    r = client.get('/api')
    html = __import__('html').unescape(r.get_data(as_text=True))
    assert r.status_code == 200
    for text in ("curl -F 'file=@-' http://localhost/", "curl -F 'file=@deploy.sh' http://localhost/", ";filename=notes.md", 'application/json', '"files"',
                 '/raw/<id>/<filename>', 'Removal URL', 'expires_in', '415', '429',
                 f'at most {papyrus._human_size(papyrus.MAX_PASTE_BYTES)}'):
        assert text in html, text
    for option in papyrus._EXPIRY_OPTIONS:
        assert f"<code>{option['value']}</code>" in html
    assert '/curl' not in html and 'data-binary' not in html and "raw=" not in html       # one terminal form


def test_the_api_examples_really_work(client):
    """The commands the page shows are the ones the app accepts."""
    r = upload(client, ('deploy.sh', 'x'), title='Deploy helper', comments='1', expires_in=papyrus._DEFAULT_EXPIRY)
    assert r.status_code == 200
    r = upload(client, ('main.py', 'print(1)\n'), ('README.md', '# hi'))
    assert r.status_code == 200 and 'Raw URL (README.md)' in r.get_data(as_text=True)
    r = client.post('/create', json={'title': 'P', 'files': [{'filename': 'main.py', 'content': 'print(1)\n'}, {'filename': 'README.md', 'content': '# Hi'}]})
    assert r.status_code == 200 and set(r.get_json()) == {'paste_id', 'url', 'removal_token', 'removal_url'}


def test_the_editor_has_an_api_button_before_the_theme_controls(client):
    top = client.get('/').get_data(as_text=True).split('</nav>')[0]
    assert top.index('id="title"') < top.index('href="/api"') < top.index('id="themeToggle"')
    assert '>API<' in top


# ── Blocking addresses from the admin panel ───────────────────────────────────

def forwarded(ip):
    """Requests as the proxy on this machine would send them (it vouches for X-Forwarded-For)."""
    return {'environ_overrides': {'REMOTE_ADDR': '127.0.0.1'}, 'headers': {'X-Forwarded-For': ip, **ORIGIN}}


def admin_client():
    c = papyrus.app.test_client()
    assert login(c).status_code in (200, 302, 303)
    return c


def block(admin, **fields):
    return admin.post('/admin/blocks', data=fields, headers=ORIGIN, follow_redirects=True).get_data(as_text=True)


def post_as(ip, client, path='/create', **body):
    return client.post(path, json={'content': 'x', 'expires_in': '1hour', **body}, **forwarded(ip))


def test_a_blocked_address_cannot_create_pastes_or_comment_but_can_read(client):
    pid = create(client, comments=True)['paste_id']
    admin = admin_client()
    assert 'Blocked 203.0.113.9/32' in block(admin, address='203.0.113.9', note='spam', duration='1day')
    r = post_as('203.0.113.9', client)
    assert r.status_code == 403 and 'blocked' in r.get_json()['error']
    assert post_as('203.0.113.10', client).status_code == 200                         # a neighbour is fine
    comment = client.post(f'/{pid}/comment', data={'content': 'hi'}, **forwarded('203.0.113.9'))
    assert comment.status_code == 403
    assert client.get(f'/{pid}', **forwarded('203.0.113.9')).status_code == 200      # reading stays open


def test_ranges_ipv6_and_mapped_addresses_are_matched(client):
    admin = admin_client()
    block(admin, address='198.51.100.0/24', duration='never')
    block(admin, address='2001:db8:1:2::/64', duration='never')
    assert post_as('198.51.100.77', client).status_code == 403
    assert post_as('::ffff:198.51.100.77', client).status_code == 403
    assert post_as('2001:db8:1:2:aaaa:bbbb:cccc:dddd', client).status_code == 403
    assert post_as('2001:db8:1:3::1', client).status_code == 200
    assert post_as('198.51.101.1', client).status_code == 200


def test_a_forged_header_cannot_get_someone_blocked_or_unblocked(client):
    admin = admin_client()
    block(admin, address='203.0.113.9', duration='never')
    # Not from a trusted proxy: the header is ignored and the real peer is used.
    r = client.post('/create', json={'content': 'x'}, environ_overrides={'REMOTE_ADDR': '192.0.2.50'},
                    headers={'X-Forwarded-For': '203.0.113.9', **ORIGIN})
    assert r.status_code == 200


def test_expired_blocks_stop_applying_and_are_purged(client):
    admin = admin_client()
    block(admin, address='203.0.113.9', duration='1day')
    with papyrus._db() as conn:
        conn.execute("UPDATE blocks SET expires_at = '2000-01-01T00:00:00+00:00'")
        conn.commit()
    assert post_as('203.0.113.9', client).status_code == 200
    papyrus._purge_expired()
    assert db().execute('SELECT COUNT(*) FROM blocks').fetchone()[0] == 0


def test_unblocking_lets_the_address_post_again(client):
    admin = admin_client()
    block(admin, address='203.0.113.9', duration='never')
    block_id = db().execute('SELECT id FROM blocks').fetchone()[0]
    assert admin.post(f'/admin/blocks/{block_id}/delete', headers=ORIGIN).status_code == 302
    assert post_as('203.0.113.9', client).status_code == 200


def test_the_admin_cannot_block_themselves_or_the_whole_internet(client):
    admin = admin_client()
    mine = block(admin, address='127.0.0.1')                                  # the test client's own address
    assert 'would block your own address' in mine
    assert 'too large' in block(admin, address='0.0.0.0/0')
    assert 'too large' in block(admin, address='::/0')
    assert 'not an IP address' in block(admin, address='banana')
    assert db().execute('SELECT COUNT(*) FROM blocks').fetchone()[0] == 0


def test_only_the_admin_can_change_the_blocklist(client):
    anon = papyrus.app.test_client()
    assert anon.post('/admin/blocks', data={'address': '203.0.113.9'}, headers=ORIGIN).status_code == 403
    admin = admin_client()
    assert admin.post('/admin/blocks', data={'address': '203.0.113.9'}).status_code == 403          # no Origin: CSRF
    assert db().execute('SELECT COUNT(*) FROM blocks').fetchone()[0] == 0


def test_addresses_are_not_stored_unless_asked_for(client):
    pid = post_as('198.51.100.88', client).get_json()['paste_id']
    assert db().execute('SELECT creator_ip FROM pastes WHERE id=?', (pid,)).fetchone()[0] == ''
    html = admin_client().get('/admin').get_data(as_text=True)
    assert '198.51.100.88' not in html and 'pastes.store_ips' in html


def test_with_store_ips_the_admin_can_block_a_paste_or_comment_author(client, monkeypatch):
    monkeypatch.setattr(papyrus, '_STORE_IPS', True)
    pid = post_as('203.0.113.9', client, comments=True).get_json()['paste_id']
    cid_resp = client.post(f'/{pid}/comment', data={'content': 'rude'}, **forwarded('2001:db8:5:6:7:8:9:a'))
    assert cid_resp.status_code in (302, 303)
    admin = admin_client()
    html = admin.get('/admin').get_data(as_text=True)
    assert '<code>203.0.113.9</code>' in html and 'Block</button>' in html
    assert 'Blocked 203.0.113.9/32' in block(admin, paste_id=pid)
    cid = db().execute('SELECT id FROM comments').fetchone()[0]
    assert 'Blocked 2001:db8:5:6::/64' in block(admin, comment_id=cid)           # an IPv6 visitor owns a /64
    assert post_as('203.0.113.9', client).status_code == 403
    assert post_as('2001:db8:5:6::99', client).status_code == 403


def test_a_stored_address_goes_when_the_paste_does(client, monkeypatch):
    monkeypatch.setattr(papyrus, '_STORE_IPS', True)
    made = post_as('203.0.113.9', client, comments=True).get_json()
    client.get(f"/remove/{made['removal_token']}")
    assert db().execute("SELECT COUNT(*) FROM pastes WHERE creator_ip != ''").fetchone()[0] == 0


def test_the_report_queue_offers_to_block_the_author(client, monkeypatch):
    monkeypatch.setattr(papyrus, '_STORE_IPS', True)
    pid = post_as('203.0.113.9', client).get_json()['paste_id']
    client.post(f'/{pid}/report', data={'reason': 'spam'}, **forwarded('198.51.100.4'))
    html = admin_client().get('/admin').get_data(as_text=True)
    assert 'Block author' in html and 'from <code>203.0.113.9</code>' in html


def test_a_released_version_2_database_is_upgraded_in_place(tmp_path, monkeypatch):
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE pastes (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'Untitled',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TEXT, views INTEGER DEFAULT 0,
            removal_hash TEXT NOT NULL DEFAULT '', discussions_enabled INTEGER DEFAULT 0);
        CREATE TABLE files (paste_id TEXT NOT NULL, position INTEGER NOT NULL, filename TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'text', content TEXT NOT NULL, PRIMARY KEY (paste_id, position));
        CREATE TABLE comments (id TEXT PRIMARY KEY, paste_id TEXT NOT NULL, parent_id TEXT, nick TEXT,
            content TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO pastes (id, title) VALUES ('abc123abc123', 'kept');
        INSERT INTO files VALUES ('abc123abc123', 0, 'a.txt', 'text', 'still here');
        INSERT INTO comments (id, paste_id, content) VALUES ('c1', 'abc123abc123', 'a comment');
        PRAGMA user_version = 2;
    ''')
    conn.commit(); conn.close()
    monkeypatch.setattr(papyrus, 'DATABASE', path)
    monkeypatch.setattr(papyrus, '_db_initialized', False)
    monkeypatch.setattr(papyrus, '_cleanup_running', True)
    papyrus.init_db()
    c = papyrus.app.test_client()
    assert c.get('/raw/abc123abc123').get_data(as_text=True) == 'still here'
    check = sqlite3.connect(path)
    assert check.execute('PRAGMA user_version').fetchone()[0] == papyrus.SCHEMA_VERSION == 4
    columns = [r[1] for r in check.execute('PRAGMA table_info(pastes)')]
    assert 'comments_enabled' in columns and 'discussions_enabled' not in columns and 'creator_ip' in columns
    assert check.execute("SELECT creator_ip FROM pastes").fetchone()[0] == ''
    assert check.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0
    papyrus._db_initialized = False
    papyrus.init_db()                                   # running it again changes nothing
    check.close()


def test_a_database_from_before_version_2_is_still_refused(tmp_path, monkeypatch):
    path = str(tmp_path / 'ancient.db')
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE pastes (id TEXT PRIMARY KEY, content TEXT); PRAGMA user_version = 1;")
    conn.commit(); conn.close()
    monkeypatch.setattr(papyrus, 'DATABASE', path)
    monkeypatch.setattr(papyrus, '_db_initialized', False)
    with pytest.raises(RuntimeError, match='delete or move'):
        papyrus.init_db()


# ── The admin panel's View window ─────────────────────────────────────────────

def test_the_admin_view_data_has_files_comments_and_changes_nothing(client, monkeypatch):
    monkeypatch.setattr(papyrus, '_STORE_IPS', True)
    pid = post_as('203.0.113.9', client, title='Look <b>here</b>', comments=True,
                  files=[{'filename': 'a.txt', 'content': '<script>alert(1)</script>'}, {'filename': 'b.py', 'content': 'x=1'}]).get_json()['paste_id']
    client.post(f'/{pid}/comment', data={'content': '**rude**'}, **forwarded('198.51.100.4'))
    admin = admin_client()
    r = admin.get(f'/admin/paste/{pid}/data')
    data = r.get_json()
    assert r.status_code == 200 and r.headers['Cache-Control'] == 'no-store'
    assert data['title'] == 'Look <b>here</b>' and data['address'] == '203.0.113.9' and data['url'] == f'/{pid}'
    assert [f['filename'] for f in data['files']] == ['a.txt', 'b.py'] and data['files'][0]['content'] == '<script>alert(1)</script>'
    assert data['comments'][0]['content'] == '**rude**' and data['comments'][0]['creator_ip'] == '198.51.100.4'
    assert db().execute('SELECT views FROM pastes WHERE id=?', (pid,)).fetchone()[0] == 0      # looking is not a view


def test_only_the_admin_can_fetch_view_data_and_gone_pastes_are_404(client):
    pid = create(client)['paste_id']
    assert papyrus.app.test_client().get(f'/admin/paste/{pid}/data').status_code == 403
    admin = admin_client()
    assert admin.get('/admin/paste/000000000000/data').status_code == 404
    assert admin.get('/admin/paste/not-an-id/data').status_code == 404
    with papyrus._db() as conn:
        conn.execute("UPDATE pastes SET expires_at = '2000-01-01T00:00:00+00:00'"); conn.commit()
    assert admin.get(f'/admin/paste/{pid}/data').status_code == 404
    assert db().execute('SELECT COUNT(*) FROM pastes').fetchone()[0] == 1            # looking did not delete it


def test_the_dashboard_has_the_view_window_and_its_script_shows_only_text(client):
    pid = create(client)['paste_id']
    html = admin_client().get('/admin').get_data(as_text=True)
    assert 'class="btn-small view-paste" data-paste="%s"' % pid in html and '<dialog id="viewDialog"' in html
    assert '/static/js/admin.js' in html
    js = open(os.path.join(os.path.dirname(papyrus.__file__), 'static', 'js', 'admin.js'), encoding='utf-8').read()
    assert 'innerHTML' not in js and 'insertAdjacentHTML' not in js and 'textContent' in js     # never parsed as HTML


def test_one_click_blocks_ask_how_long_first(client, monkeypatch):
    monkeypatch.setattr(papyrus, '_STORE_IPS', True)
    pid = post_as('203.0.113.9', client).get_json()['paste_id']
    admin = admin_client()
    html = admin.get('/admin').get_data(as_text=True)
    assert 'data-block-form data-address="203.0.113.9"' in html and 'id="blockDialog"' in html
    dialog = html.split('id="blockDialog"')[1].split('</dialog>')[0]
    assert all(f'value="{key}"' in dialog for key in ('1day', '1week', '1month', 'never')) and 'name="note"' in dialog
    assert 'Blocked 203.0.113.9/32' in block(admin, paste_id=pid, duration='1day', note='spam run')   # what the dialog posts
    row = db().execute('SELECT note, expires_at FROM blocks').fetchone()
    until = datetime.datetime.fromisoformat(row['expires_at'])
    assert row['note'] == 'spam run' and datetime.timedelta(hours=23) < until - datetime.datetime.now(datetime.timezone.utc) < datetime.timedelta(hours=25)


def test_code_is_never_shown_with_font_ligatures():
    css = open(os.path.join(os.path.dirname(papyrus.__file__), 'static', 'css', 'style.css'), encoding='utf-8').read()
    rule = re.search(r'pre, code, kbd, samp, textarea, \.removal-url \{([^}]*)\}', css).group(1)
    assert 'font-variant-ligatures: none' in rule and '"calt" 0' in rule        # <- must not turn into an arrow


# ── Uploads with curl -F file=@... straight to the root URL ───────────────────

def upload(client, *files, path='/', **fields):
    """What `curl -F 'file=@name'` sends: multipart with a file part per upload."""
    import io
    data = {k: v for k, v in fields.items()}
    parts = [(io.BytesIO(content if isinstance(content, bytes) else content.encode()), name) for name, content in files]
    data['file'] = parts
    return client.post(path, data=data, content_type='multipart/form-data')


def test_a_file_upload_to_the_root_url_makes_a_paste_with_its_name(client):
    r = upload(client, ('deploy.sh', '#!/bin/sh\necho hi\n'))
    urls = curl_urls(r)
    assert r.status_code == 200 and urls['Paste URL'] == f"http://localhost/{urls['Paste URL'].rsplit('/', 1)[1]}"
    pid = urls['Paste URL'].rsplit('/', 1)[1]
    row = stored(pid)
    assert (row['filename'], row['language'], row['title'], row['content']) == ('deploy.sh', 'bash', 'deploy.sh', '#!/bin/sh\necho hi\n')
    assert client.get(f'/raw/{pid}').get_data(as_text=True) == '#!/bin/sh\necho hi\n'


def test_piping_into_file_at_dash_works_and_the_dash_is_not_a_filename(client):
    r = upload(client, ('-', 'piped text\n'))                  # curl -F 'file=@-' names the part "-" (or nothing)
    pid = curl_urls(r)['Paste URL'].rsplit('/', 1)[1]
    assert stored(pid)['filename'] == '' and stored(pid)['content'] == 'piped text\n' and stored(pid)['title'] == 'Untitled'
    r = upload(client, ('', 'no name at all\n'))
    assert r.status_code == 200
    r = upload(client, ('notes.md', '# hi\n'))                  # file=@-;filename=notes.md
    assert stored(curl_urls(r)['Paste URL'].rsplit('/', 1)[1])['language'] == 'markdown'


def test_several_uploads_make_a_multi_file_paste(client):
    r = upload(client, ('a.py', 'print(1)\n'), ('b.sh', 'echo b\n'), title='Two')
    text = r.get_data(as_text=True)
    assert 'Raw URL (a.py)' in text and 'Raw URL (b.sh)' in text
    pid = re.search(r'Paste URL:\s+\S+/([0-9a-f]+)', text).group(1)
    assert client.get(f'/raw/{pid}/b.sh').get_data(as_text=True) == 'echo b\n' and stored(pid)['title'] == 'Two'


def test_uploads_and_text_fields_in_one_post_are_not_a_thing(client):
    """Only file fields make files; a stray raw= field is ignored, not turned into a second file."""
    r = upload(client, ('up.txt', 'from a file'), raw='from a field')
    assert r.status_code == 200 and 'Raw URL (' not in r.get_data(as_text=True)      # one file, not two


def test_uploads_must_be_small_utf8_text(client, monkeypatch):
    assert upload(client, ('blob.bin', b'\xff\xfe\x00\x01')).status_code == 415
    assert upload(client, ('nul.txt', b'text\x00more')).status_code == 415
    monkeypatch.setattr(papyrus, 'MAX_PASTE_BYTES', 50)
    assert upload(client, ('big.txt', 'x' * 51)).status_code == 413
    assert upload(client, ('ok.txt', 'x' * 50)).status_code == 200


def test_posting_nothing_to_the_root_says_what_to_send(client):
    r = client.post('/', data={'title': 'nothing'}, content_type='multipart/form-data')
    assert r.status_code == 400 and "-F 'file=@name'" in r.get_data(as_text=True) and "file=@-" in r.get_data(as_text=True)
    assert client.get('/').status_code == 200                    # the create page is still the root's GET


def test_blocked_addresses_cannot_upload_to_the_root_either(client):
    block(admin_client(), address='203.0.113.9', duration='never')
    import io
    r = client.post('/', data={'file': (io.BytesIO(b'x'), 'a.txt')}, content_type='multipart/form-data',
                    environ_overrides={'REMOTE_ADDR': '127.0.0.1'}, headers={'X-Forwarded-For': '203.0.113.9'})
    assert r.status_code == 403 and 'blocked' in r.get_data(as_text=True)


def test_a_version_3_database_gets_the_column_renamed_and_keeps_its_comments_setting(tmp_path, monkeypatch):
    path = str(tmp_path / 'v3.db')
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE pastes (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'Untitled',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TEXT, views INTEGER DEFAULT 0,
            removal_hash TEXT NOT NULL DEFAULT '', discussions_enabled INTEGER DEFAULT 0, creator_ip TEXT NOT NULL DEFAULT '');
        CREATE TABLE files (paste_id TEXT NOT NULL, position INTEGER NOT NULL, filename TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'text', content TEXT NOT NULL, PRIMARY KEY (paste_id, position));
        CREATE TABLE comments (id TEXT PRIMARY KEY, paste_id TEXT NOT NULL, parent_id TEXT, nick TEXT,
            content TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, creator_ip TEXT NOT NULL DEFAULT '');
        CREATE TABLE blocks (id TEXT PRIMARY KEY, network TEXT NOT NULL UNIQUE, note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TEXT);
        INSERT INTO pastes (id, title, discussions_enabled) VALUES ('abc123abc123', 'with comments', 1);
        INSERT INTO files VALUES ('abc123abc123', 0, 'a.txt', 'text', 'body');
        INSERT INTO comments (id, paste_id, content) VALUES ('c1', 'abc123abc123', 'kept comment');
        PRAGMA user_version = 3;
    ''')
    conn.commit(); conn.close()
    monkeypatch.setattr(papyrus, 'DATABASE', path)
    monkeypatch.setattr(papyrus, '_db_initialized', False)
    monkeypatch.setattr(papyrus, '_cleanup_running', True)
    papyrus.init_db()
    html = papyrus.app.test_client().get('/abc123abc123').get_data(as_text=True)
    assert 'kept comment' in html and 'id="commentsPanel"' in html          # the paste still takes comments
    papyrus._db_initialized = False
    papyrus.init_db()                                                         # and running it again is harmless


# ── Comments: the site-wide switch and the default ────────────────────────────

def test_comments_are_off_by_default_and_follow_pastes_comments_default(client, monkeypatch):
    plain = client.post('/create', json={'content': 'x', 'expires_in': '1hour'}).get_json()['paste_id']
    assert db().execute('SELECT comments_enabled FROM pastes WHERE id=?', (plain,)).fetchone()[0] == 0
    monkeypatch.setattr(papyrus, '_COMMENTS_DEFAULT', True)
    for_json = client.post('/create', json={'content': 'x', 'expires_in': '1hour'}).get_json()['paste_id']
    via_curl = curl_urls(upload(client, ('', 'x')))['Paste URL'].rsplit('/', 1)[1]
    off = client.post('/create', json={'content': 'x', 'expires_in': '1hour', 'comments': False}).get_json()['paste_id']
    off_curl = curl_urls(upload(client, ('', 'x'), comments='0'))['Paste URL'].rsplit('/', 1)[1]
    flags = {pid: db().execute('SELECT comments_enabled FROM pastes WHERE id=?', (pid,)).fetchone()[0]
             for pid in (for_json, via_curl, off, off_curl)}
    assert flags == {for_json: 1, via_curl: 1, off: 0, off_curl: 0}          # said nothing: the default; said no: no


def test_the_create_page_checkbox_starts_as_the_site_default(client, monkeypatch):
    html = client.get('/').get_data(as_text=True)
    assert 'id="allowComments" data-default="0"' in html
    monkeypatch.setattr(papyrus, '_COMMENTS_DEFAULT', True)
    assert 'id="allowComments" data-default="1"' in client.get('/').get_data(as_text=True)


def test_comments_can_be_switched_off_for_the_whole_site(client, monkeypatch):
    pid = create(client, comments=True)['paste_id']
    assert comment(client, pid, 'before').status_code in (302, 303)
    monkeypatch.setattr(papyrus, '_COMMENTS_ENABLED', False)
    page = client.get(f'/{pid}').get_data(as_text=True)
    assert 'id="commentsPanel"' not in page and 'before' not in page                  # hidden, not deleted
    assert 'allowComments' not in client.get('/').get_data(as_text=True)
    assert comment(client, pid, 'after').status_code == 403
    newest = create(client, comments=True)['paste_id']                                 # asking for comments is ignored
    assert db().execute('SELECT comments_enabled FROM pastes WHERE id=?', (newest,)).fetchone()[0] == 0
    assert db().execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 1           # the old comment is still stored


# ── Light/dark default ────────────────────────────────────────────────────────

@pytest.mark.parametrize('value, expected', [(None, 'dark'), ('dark', 'dark'), ('light', 'light'), ('auto', 'auto')])
def test_the_theme_default_is_validated(value, expected):
    assert papyrus._load_theme_default(value) == expected


def test_a_bad_theme_default_stops_startup_with_a_clear_message():
    with pytest.raises(RuntimeError, match='theme.default must be'):
        papyrus._load_theme_default('purple')


@pytest.mark.parametrize('mode', ['dark', 'light', 'auto'])
def test_the_theme_default_is_in_the_page_before_it_paints(client, monkeypatch, mode):
    monkeypatch.setattr(papyrus, '_THEME_DEFAULT', mode)
    html = client.get('/').get_data(as_text=True)
    assert f'data-theme-default="{mode}"' in html
    head = html.split('</head>')[0]
    assert "getAttribute('data-theme-default')" in head and "d === 'dark'" in head      # decided in the head script: no flash


def test_the_shipped_example_config_defaults_to_dark_and_comments_are_off():
    example = json.load(open(os.path.join(os.path.dirname(papyrus.__file__), 'config.example.json'), encoding='utf-8'))
    assert example['theme']['default'] == 'dark'
    assert example['features']['comments'] is True and example['pastes']['comments_default'] is False


def test_the_os_theme_listener_only_applies_to_auto():
    js = open(os.path.join(os.path.dirname(papyrus.__file__), 'static', 'js', 'app.js'), encoding='utf-8').read()
    assert "configDefault === 'auto' && !localStorage.getItem('theme')" in js
