"""The example proxy configs in webserver/ must stay consistent with the app's own config."""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def active(text, comment='#'):
    return '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith(comment))


PORT = json.loads(read('config.example.json'))['server']['port']


def test_every_example_proxies_to_the_default_port():
    for name in ('nginx.conf', 'apache.conf', 'Caddyfile'):
        assert f'127.0.0.1:{PORT}' in active(read('webserver', name)), name


def test_nginx_sets_both_headers_with_set_semantics():
    conf = active(read('webserver', 'nginx.conf'))
    assert 'proxy_set_header X-Forwarded-For $remote_addr;' in conf     # not $proxy_add_x_forwarded_for
    assert 'proxy_set_header X-Forwarded-Proto $scheme;' in conf
    assert 'ssl_certificate' not in conf and 'return 301' not in conf    # certbot adds those


def test_apache_reports_the_real_scheme_and_leaves_tls_to_certbot():
    conf = active(read('webserver', 'apache.conf'))
    assert 'RequestHeader set X-Forwarded-Proto "expr=%{REQUEST_SCHEME}"' in conf
    assert 'SSLEngine' not in conf and 'Redirect' not in conf


def test_caddy_cloudflare_block_only_trusts_cloudflare_peers():
    text = read('webserver', 'Caddyfile')
    assert 'handle @cloudflare' in text and 'CF-Connecting-IP' in text
    assert '@cloudflare' not in active(text)    # off by default: the block is commented out


def test_cloudflare_options_are_off_by_default_in_the_examples():
    assert 'cloudflare-realip' not in active(read('webserver', 'nginx.conf'))
    assert 'RemoteIP' not in active(read('webserver', 'apache.conf'))


def test_documented_header_names_match_what_the_app_reads():
    app = read('app.py')
    assert "request.headers.get('X-Forwarded-For'" in app
    assert re.search(r"'forwarded_allow_ips'", app)
    assert 'trusted_proxy_ips' in read('webserver', 'README.md')
