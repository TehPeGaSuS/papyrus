# Papyrus behind Cloudflare

There are two ways to put Cloudflare in front of Papyrus.

## 1. Cloudflare Tunnel (`cloudflared`): no open ports

`cloudflared` runs on the Papyrus machine and connects out to Cloudflare, so the server needs no
inbound ports and no certificate. Point the tunnel straight at Papyrus:

```yaml
# ~/.cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /home/<user>/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: paste.example.com
    service: http://127.0.0.1:5500
  - service: http_status:404
```

Papyrus sees the connection from `cloudflared` on the same machine (a trusted proxy by default) and reads
the `X-Forwarded-For` and `X-Forwarded-Proto` headers Cloudflare adds. Cloudflare appends the visitor's
address to `X-Forwarded-For`, so the last entry is the visitor, which is the one Papyrus uses; it also
overwrites `X-Forwarded-Proto` with `https`. Set `"use_https": true` in `config.json`. Nothing else needs
configuring.

To try it without an account or a domain, a quick tunnel gives a temporary public address (anyone with the
link can reach your Papyrus while it runs, so use test data):

```bash
python3 app.py &
cloudflared tunnel --url http://127.0.0.1:5500    # prints https://<random>.trycloudflare.com
```

The new hostname can take up to a minute to resolve.

## 2. Proxied DNS record (orange cloud) in front of nginx, Apache or Caddy

Cloudflare connects to your web server, so the web server sees a Cloudflare address, not the visitor.
Make it use the visitor's address (`CF-Connecting-IP`) *only for connections that really come from
Cloudflare*, then follow the normal config for your server (`nginx.conf`, `apache.conf`, `Caddyfile`).

**SSL/TLS mode: use "Full (strict)"** with a certificate on the origin (Let's Encrypt, or a free
Cloudflare Origin CA certificate). Avoid "Flexible": Cloudflare would talk to your server over plain HTTP,
so that leg is unencrypted and your proxy's idea of the scheme is wrong. It also loops forever once
certbot has added its HTTP to HTTPS redirect: Cloudflare asks over HTTP, gets redirected to HTTPS, and
asks over HTTP again (the browser reports "too many redirects").

Cloudflare publishes its address ranges at <https://www.cloudflare.com/ips-v4> and
<https://www.cloudflare.com/ips-v6>. They change occasionally, so generate the include file instead of
copying the ranges into your config, and refresh it now and then (a monthly cron job is plenty).

### nginx

```bash
{ curl -fsS https://www.cloudflare.com/ips-v4; echo; curl -fsS https://www.cloudflare.com/ips-v6; } \
  | sed '/^$/d; s/^/set_real_ip_from /; s/$/;/' \
  | sed '1i real_ip_header CF-Connecting-IP;' \
  | sudo tee /etc/nginx/cloudflare-realip.conf >/dev/null
sudo nginx -t && sudo systemctl reload nginx
```

Then uncomment `include /etc/nginx/cloudflare-realip.conf;` in `nginx.conf`. `$remote_addr` is now the
visitor, and `proxy_set_header X-Forwarded-For $remote_addr;` hands it to Papyrus.

### Apache (`mod_remoteip`)

```bash
sudo a2enmod remoteip
{ curl -fsS https://www.cloudflare.com/ips-v4; echo; curl -fsS https://www.cloudflare.com/ips-v6; } \
  | sed '/^$/d; s/^/RemoteIPTrustedProxy /' \
  | sudo tee /etc/apache2/cloudflare-remoteip.conf >/dev/null
sudo apachectl configtest && sudo systemctl reload apache2
```

Then uncomment the two `RemoteIP…`/`Include` lines in `apache.conf`. mod_proxy then adds the visitor
(not Cloudflare) to `X-Forwarded-For`.

### Caddy

```bash
{ curl -fsS https://www.cloudflare.com/ips-v4; echo; curl -fsS https://www.cloudflare.com/ips-v6; } \
  | sed '/^$/d' | paste -sd' ' | sed 's/^/@cloudflare remote_ip /' \
  | sudo tee /etc/caddy/cloudflare-ips.caddy >/dev/null
sudo systemctl reload caddy
```

Then uncomment the `import` line and the `handle @cloudflare { ... }` block in the `Caddyfile`. Connections
from Cloudflare get `X-Forwarded-For` set to `CF-Connecting-IP`; everyone else gets the normal Caddy
behavior, so a forged `CF-Connecting-IP` from anywhere else is ignored. This form works on Caddy 2.6 and
later (newer versions also offer `trusted_proxies` with `client_ip_headers`, which is not needed here).
If a request from a Cloudflare address ever arrives without `CF-Connecting-IP` (Cloudflare always sends
it), Papyrus falls back to Caddy's own address for that request.

## Cloudflare settings that interfere with a pastebin

- **Rocket Loader**, **Email Address Obfuscation** and other HTML-rewriting features: turn them off.
  Pastes are HTML pages, so rewriting corrupts the paste text, and injected scripts clash with
  Papyrus's Content-Security-Policy.
- **WAF managed rules**: code looks like an attack to a WAF, so pastes containing SQL, shell commands or
  HTML can be blocked with a 403 before they reach Papyrus. If that happens, add a WAF skip rule for
  `POST /create` and `POST /*/comment`, or lower the sensitivity.
- **Lock the origin down.** If the server is reachable without Cloudflare, anyone can send a fake
  `CF-Connecting-IP`. Allow inbound 443 only from Cloudflare's ranges (firewall), or use a Tunnel and
  open nothing.
