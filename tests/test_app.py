import datetime
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


def test_is_expired_handles_legacy_space_format():
    future = (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).isoformat(' ')
    past = (datetime.datetime.now(UTC) - datetime.timedelta(hours=1)).isoformat(' ')
    assert not papyrus._is_expired(future)
    assert papyrus._is_expired(past)
    assert not papyrus._is_expired(None)


def test_init_db_migrates_legacy_expiry_format(client):
    conn = db()
    conn.execute("INSERT INTO pastes (id, title, content, expires_at) VALUES (?,?,?,?)",
                 ('legacy000001', 't', 'x', '2099-01-01 10:00:00+00:00'))
    conn.commit()
    conn.close()
    papyrus._db_initialized = False
    papyrus.init_db()
    stored = db().execute("SELECT expires_at FROM pastes WHERE id='legacy000001'").fetchone()[0]
    assert stored == '2099-01-01T10:00:00+00:00'


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

def test_delete_requires_matching_token(client):
    made = create(client)
    pid = made['paste_id']
    bad = client.post(f'/api/delete/{pid}', json={'token': 'nope'}, headers=ORIGIN)
    assert bad.status_code == 403
    nontext = client.post(f'/api/delete/{pid}', json={'token': 123}, headers=ORIGIN)
    assert nontext.status_code == 403
    good = client.post(f'/api/delete/{pid}', json={'token': made['deletion_token']}, headers=ORIGIN)
    assert good.status_code == 200
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
    url = r.get_data(as_text=True).strip()
    pid = url.rsplit('/', 1)[1]
    assert url == f'http://localhost/{pid}'
    assert r.headers['X-Deletion-Token']
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
    pid = made['paste_id']
    comment(client, pid, 'hi')
    assert db().execute('SELECT COUNT(*) FROM comments').fetchone()[0] == 1
    client.post(f'/api/delete/{pid}', json={'token': made['deletion_token']}, headers=ORIGIN)
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

def test_migrates_encrypted_schema(tmp_path, monkeypatch):
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE pastes (id TEXT PRIMARY KEY, encrypted_data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP,
            views INTEGER DEFAULT 0, deletion_token TEXT, discussions_enabled INTEGER DEFAULT 0);
        CREATE TABLE comments (id TEXT PRIMARY KEY, paste_id TEXT NOT NULL, content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    plain = '{"title": "T", "content": "hello", "language": "python", "discussions": true}'
    conn.execute("INSERT INTO pastes (id, encrypted_data, discussions_enabled, views) VALUES ('aaaaaaaaaaaa', ?, 1, 7)", (plain,))
    conn.execute("INSERT INTO pastes (id, encrypted_data) VALUES ('bbbbbbbbbbbb', 'abc_-:def_-')")
    conn.execute("INSERT INTO comments (id, paste_id, content) VALUES ('c1', 'aaaaaaaaaaaa', ?)",
                 ('{"content": "nice", "nick": "bob"}',))
    conn.execute("INSERT INTO comments (id, paste_id, content) VALUES ('c2', 'bbbbbbbbbbbb', 'cipher:text')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(papyrus, 'DATABASE', path)
    monkeypatch.setattr(papyrus, '_db_initialized', False)
    monkeypatch.setattr(papyrus, '_cleanup_running', True)
    papyrus.init_db()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    paste = conn.execute("SELECT * FROM pastes WHERE id='aaaaaaaaaaaa'").fetchone()
    assert (paste['title'], paste['content'], paste['language'], paste['views']) == ('T', 'hello', 'python', 7)
    assert conn.execute("SELECT COUNT(*) FROM pastes WHERE id='bbbbbbbbbbbb'").fetchone()[0] == 0
    c = conn.execute('SELECT nick, content FROM comments').fetchall()
    assert [(r['nick'], r['content']) for r in c] == [('bob', 'nice')]
    # The unreadable encrypted paste is preserved, not dropped.
    assert conn.execute('SELECT COUNT(*) FROM pastes_encrypted_legacy').fetchone()[0] == 2


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


def test_comments_table_gains_parent_id_column(tmp_path, monkeypatch):
    path = str(tmp_path / 'prev.db')
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE pastes (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'Untitled', content TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'text', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT, views INTEGER DEFAULT 0, deletion_token TEXT, discussions_enabled INTEGER DEFAULT 0);
        CREATE TABLE comments (id TEXT PRIMARY KEY, paste_id TEXT NOT NULL, nick TEXT, content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO comments (id, paste_id, content) VALUES ('c1', 'p1', 'old comment');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(papyrus, 'DATABASE', path)
    monkeypatch.setattr(papyrus, '_db_initialized', False)
    monkeypatch.setattr(papyrus, '_cleanup_running', True)
    papyrus.init_db()
    conn = sqlite3.connect(path)
    assert 'parent_id' in {r[1] for r in conn.execute('PRAGMA table_info(comments)')}
    assert conn.execute('SELECT content, parent_id FROM comments').fetchone() == ('old comment', None)


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
    pid = r.get_data(as_text=True).strip().rsplit('/', 1)[1]
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
    client.post(f"/api/delete/{made['paste_id']}", json={'token': made['deletion_token']}, headers=ORIGIN)
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
