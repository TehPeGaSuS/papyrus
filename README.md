<p align="center"><img src="docs/logo.png" alt="Papyrus" width="160"></p>

# Papyrus

A self-hosted pastebin with threaded Markdown discussions and an admin panel. One Python file,
SQLite for storage, no build step.

## Features

- **Text only**: no file uploads and no user images. Binary data is rejected, and Markdown images
  (`![alt](url)`) are shown as plain links, never embedded. The only images the site serves are its
  own logo and icons
- **Paste and share**: short links (`/<id>`), raw text at `/raw/<id>`, `curl` uploads, download as a file
- **Syntax highlighting** with Prism.js, line numbers, line-range links (`?L=5-12`), word wrap toggle
- **Expiry**: 1 hour, 1 day, 1 week, 1 month, 1 year or never (configurable)
- **Threaded discussions**: optional per-paste comments and nested replies, each in its own box,
  written in Markdown (code fences, tables, links) with a Write/Preview tab. Rendered and sanitised
  on the server
- **Admin panel** at `/admin` to list and delete pastes
- Dark/light theme, auto-saved drafts, keyboard shortcuts, optional recent-pastes page

## Requirements

- Python 3.9 or newer, with `venv` support. Debian and Ubuntu ship it separately:
  `sudo apt install python3-venv` (most other distributions include it)
- Internet access on the first run, so pip can download the dependencies
- `git`, to clone it. Nothing else needs installing system-wide; everything else goes into `.venv`
- `nh3` (the Markdown sanitiser) ships prebuilt wheels for common platforms; on an unusual
  architecture pip builds it from source and needs a Rust toolchain

## Quick start

```bash
git clone <repository-url> papyrus && cd papyrus
python3 app.py
```

That's it. The first run creates `.venv`, installs Papyrus into it (`pip install -e .`, which also
gives you a `.venv/bin/papyrus` command), writes `config.json` with a fresh random `secret_key`, and
starts the server on <http://127.0.0.1:5500> using Gunicorn. Python 3.9+ is required.

| Command | What it does |
|---|---|
| `python3 app.py` | Run in the foreground |
| `python3 app.py -d` | Run in the background (`papyrus.pid`, `papyrus.log`) |
| `python3 app.py status` / `stop` / `restart` | Manage the background process |
| `python3 app.py admin` | Set the admin password and enable `/admin` |
| `python3 app.py --dev` | Flask development server instead of Gunicorn |
| `python3 app.py --host 0.0.0.0 --port 8080 --workers 4` | Override `config.json` for one run |

## Configuration

Settings live in `config.json`. It is **not** tracked in git (it holds the secret key and the admin
password hash); `config.example.json` is the tracked template, and it is what the app uses if there is
no `config.json`. Restart after changing it.

- `server.host` / `server.port`: where to listen (default `127.0.0.1:5500`)
- `server.workers`: Gunicorn workers (default: CPUs + 1, between 2 and 8)
- `server.trusted_proxy_ips`: addresses of the reverse proxy whose `X-Forwarded-*` headers are believed
  (default `["127.0.0.1", "::1"]`, i.e. a proxy on the same machine; CIDR ranges work too)
- `server.use_https`: mark the session cookie `Secure`
- `server.secret_key`: signs the admin session. The `SECRET_KEY` environment variable overrides it
- `pastes.*`: size limit, default language, expiry options, `recent_limit`
- `features.show_recent`: enable the `/recent` page
- `admin.*`: managed by `python3 app.py admin`
- `database.path`: SQLite file, relative to the project directory (default `papyrus.db`)

## Deployment

Run Papyrus behind a reverse proxy that terminates TLS. Ready-to-use configs for **nginx**, **Apache**
and **Caddy**, with Let's Encrypt (certbot) and **Cloudflare** notes, are in [`webserver/`](webserver/).

The one thing to know: the proxy has to **set** two headers, `X-Forwarded-For` (the visitor's address,
for the per-IP rate limits) and `X-Forwarded-Proto` (`https` over TLS, for the origin check). Papyrus
believes them only from a proxy on the same machine (`server.trusted_proxy_ips`), so a visitor who
reaches the app directly can't forge them. Keep it bound to `127.0.0.1` and set `"use_https": true`
when serving over TLS. [`webserver/README.md`](webserver/README.md) has the details and a quick check.

systemd (`/etc/systemd/system/papyrus.service`), after running `python3 app.py` once as the service
user to set everything up:

```ini
[Unit]
Description=Papyrus pastebin
After=network.target

[Service]
User=papyrus
WorkingDirectory=/opt/papyrus
ExecStart=/opt/papyrus/.venv/bin/papyrus
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Usage

`curl` works without a browser:

```bash
curl --data-binary @file.py 'https://host/create?title=notes&language=python&expires_in=1day'
# prints the paste URL; the deletion token is in the X-Deletion-Token response header
curl https://host/raw/<id>
```

## API

- `GET /`: create-paste page
- `POST /create`: create a paste. JSON (`content`, optional `title`, `language`, `expires_in`,
  `discussions`) or a plain-text body as above
- `GET /<id>`: view a paste; `GET /raw/<id>`: its content as `text/plain`
- `POST /<id>/comment`: add a comment (form fields `content`, optional `nick`, optional `parent`, the
  id of the comment being answered; threads deeper than 6 levels are flattened)
- `POST /api/preview`: render Markdown for the preview tab
- `POST /api/delete/<id>`: delete a paste with its deletion token
- `GET /recent` (when enabled), `GET /api/languages`
- Admin: `GET|POST /admin/login`, `POST /admin/logout`, `GET /admin`, `POST /admin/delete/<id>`

## Database

SQLite, created automatically, stored as plain text (there is no end-to-end encryption).

```sql
CREATE TABLE pastes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Untitled',
    content TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'text',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,               -- ISO-8601 UTC, NULL = never
    views INTEGER DEFAULT 0,
    deletion_token TEXT,
    discussions_enabled INTEGER DEFAULT 0
);
CREATE TABLE comments (
    id TEXT PRIMARY KEY, paste_id TEXT NOT NULL, parent_id TEXT, nick TEXT,
    content TEXT NOT NULL,         -- Markdown source
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

A database from an older end-to-end-encrypted build is migrated on startup: unencrypted pastes are
kept, encrypted ones stay in `pastes_encrypted_legacy`.

## Security notes

- Pastes are readable by whoever runs the server; tell your users not to paste secrets.
- Paste views are server-rendered with Jinja autoescaping. Comments are Markdown with raw HTML
  escaped and the result sanitised (`nh3`); images are rendered as links, never embedded, and the
  Content-Security-Policy is `img-src 'self'`, so a browser loads images only from Papyrus itself
  (its logo and icons), never from a paste or comment.
- State-changing requests are checked against the request's `Origin`/`Referer`.
- Per-IP, SQLite-backed rate limits apply to paste creation, comments, previews and failed admin logins.
  `X-Forwarded-For` is believed only from `server.trusted_proxy_ips`, so it can't be forged by a visitor.
  See [`webserver/`](webserver/) for proxy configs.
- Paste IDs are random and at least 12 hex characters long.
- Size limits: `pastes.max_size_bytes` (2 MB default) for pastes, 10 KB for comments.
- Gunicorn's control socket is named after the project and port (`$XDG_RUNTIME_DIR/papyrus-<port>.ctl`,
  mode 0600) so several instances don't collide.

## Development

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

## Troubleshooting

- **Port already in use**: `python3 app.py status`, or pick another with `--port`.
- **Reset everything**: stop the server and delete `papyrus.db`.
- **Permission errors**: the app needs write access to its directory (database, `config.json`, log, pid file).
- **"Python cannot create virtual environments"** (Debian/Ubuntu): `sudo apt install python3-venv`, then run `python3 app.py` again.

## License

MIT, see [LICENSE](LICENSE). Syntax highlighting by [Prism](https://prismjs.com/); built with Flask,
Gunicorn, mistune and nh3.

<sub>Originally derived from Bastebin.</sub>
