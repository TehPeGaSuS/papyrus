# Running Papyrus behind a web server

Papyrus listens on `127.0.0.1:5500` (`server.host` / `server.port` in `config.json`) and is meant to sit
behind a reverse proxy that terminates TLS. Pick the file for your setup:

| File | For |
|---|---|
| [`nginx.conf`](nginx.conf) | nginx |
| [`apache.conf`](apache.conf) | Apache httpd 2.4 |
| [`Caddyfile`](Caddyfile) | Caddy 2 |
| [`cloudflare.md`](cloudflare.md) | Cloudflare in front of any of the above, or a Cloudflare Tunnel |

## Certificates

- **nginx / Apache**: start from the plain HTTP example and let certbot do the rest:
  `sudo certbot --nginx -d paste.example.com` or `sudo certbot --apache -d paste.example.com`.
  certbot adds the TLS lines and the HTTP to HTTPS redirect itself, so the examples have neither.
  They work unchanged before and after: `X-Forwarded-Proto` reports the scheme of each request.
- **Caddy**: certificates and the redirect are automatic.
- **Your own certificates**: add `listen 443 ssl;` plus `ssl_certificate` / `ssl_certificate_key` to the
  nginx server block (or `SSLEngine on` and the certificate lines to a second `<VirtualHost *:443>` in
  Apache), and a port-80 block that redirects.

## What the proxy has to do

Papyrus needs two headers, and the proxy must **set** them (not pass along whatever the visitor sent):

- **`X-Forwarded-For`**: the visitor's address. It drives the per-IP rate limits. Without it every
  visitor looks like `127.0.0.1` and shares one limit. Papyrus uses the **last** entry, the one your
  proxy added, so anything a visitor puts in front of it is ignored.
- **`X-Forwarded-Proto`**: `https` when the visitor used TLS. Without it, commenting, deleting and
  the admin login fail the origin check.

Papyrus believes these headers only when the connection comes from `server.trusted_proxy_ips`
(default `["127.0.0.1", "::1"]`, a proxy on the same machine). A visitor who reaches the app directly
cannot forge them. If the proxy runs on another machine, add its address (CIDR ranges work):

```json
"server": { "host": "10.0.0.5", "trusted_proxy_ips": ["10.0.0.2"] }
```

Also set `"use_https": true` in `config.json` when serving over TLS (marks the admin session cookie
`Secure`), and keep Papyrus bound to `127.0.0.1` unless the proxy is on another host.

## Checking it works

Run Papyrus and the proxy, then post a paste while sending a forged header:

```bash
curl -i -H 'X-Forwarded-For: 1.2.3.4' --data-binary 'hello' https://paste.example.com/create
```

The rate limit must apply to *your* address, not `1.2.3.4`. After 10 pastes in 10 minutes you get
`429 Rate limit exceeded` however the header is forged, and a different visitor is unaffected. On the
server, `sqlite3 papyrus.db "select distinct ip_address from rate_limits"` lists the addresses Papyrus saw
(`create:<address>`); behind a correctly configured proxy it is your address and never `127.0.0.1`.

Commenting from a browser over HTTPS is the check for `X-Forwarded-Proto`: if it works, the header arrives.

## Tested with

Each example was run for real, with Papyrus behind it, using forged `X-Forwarded-For` / `X-Forwarded-Proto`
headers: **nginx 1.28**, **Apache 2.4.66** (`expr=` in `RequestHeader` needs 2.4.10 or newer) and
**Caddy 2.6.2**, in both plain-HTTP and TLS layouts, and with the Cloudflare variants in
[`cloudflare.md`](cloudflare.md). Forged headers had no effect, and `X-Forwarded-Proto` matched the
real scheme. The certbot layouts were checked by writing out the result certbot produces, not by
running certbot itself.
