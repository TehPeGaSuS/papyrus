<p align="center"><img src="docs/logo.png" alt="Papyrus" width="160"></p>

# Papyrus

A self-hosted pastebin with threaded Markdown discussions and an admin panel. One Python file,
SQLite for storage, no build step.

## Features

- **Text only**: no file uploads and no user images. Binary data is rejected, and Markdown images
  (`![alt](url)`) are shown as plain links, never embedded. The only images the site serves are its
  own logo and icons
- **Paste and share**: short links (`/<id>`), raw text at `/raw/<id>`, `curl` uploads, download as a file
- **Several files per paste**: "Add file" gives each file its own card (filename, language, indent options), like
  GitHub Gists; up to 10 by default (`pastes.max_files`). Each file has its own raw link and Markdown preview
- **Load text files**: drop them on the editor (or use "open files") and the contents load straight in, with the
  filenames filled in (one file goes in the current card, several get a card each). It stays in your browser until
  you save; only UTF-8 text up to the size limit is accepted
- **Gist-style editor**: description, filename (the extension picks the syntax highlighting), indent mode
  (spaces or tabs), indent size (2, 4, 8), no wrap (the default, like GitHub) or soft wrap, Tab/Shift+Tab indenting and auto-indent
- **Markdown preview**: Markdown pastes (up to 256 KB) get a Source | Preview switch; `?view=preview` links straight to
  the rendered version. It is sanitised like comments, so raw HTML is shown as text, never parsed, and HTML pastes are
  never rendered
- **Syntax highlighting** with Prism.js, line numbers, line-range links (`?L=5-12`), word wrap toggle
- **Expiry**: pastes are temporary: 1 hour, 1 day, 1 week or 1 month, one week by default (configurable)
- **Threaded discussions**: optional per-paste comments and nested replies, each in its own box,
  written in Markdown (code fences, tables, links) with a Write/Preview tab. Rendered and sanitised
  on the server
- **Admin panel** at `/admin` to list and delete pastes and comments, and to work through reports. **View** opens
  a paste (every file, its comments, and where it came from) in a window, as plain text, without leaving the panel
- **Blocking addresses**, from the panel (no firewall needed): a blocked address or range can still read pastes but
  can't create them or comment. Block one by typing an address or a range (IPv4 or IPv6, for a day, a week, a month or
  until removed), or with **Block** next to a paste, a comment or a report (it asks how long, and for an optional note). The panel won't let you block
  your own address or an absurdly large range. An IPv6 visitor is blocked as the /64 they control
- **Reports**: a Report button on every paste and comment, a no-JavaScript report form (reason and an
  optional note), and a queue in `/admin` with the most-reported items first. Reports exist while the admin
  panel is enabled (`python3 app.py admin`); `features.reports` switches them off
- Dark/light theme, keyboard shortcuts, optional recent-pastes page

## Requirements

- Python 3.9 or newer, with `venv` support. Debian and Ubuntu ship it separately:
  `sudo apt install python3-venv` (most other distributions include it)
- Internet access on the first run, so pip can download the dependencies
- `git`, to clone it. Nothing else needs installing system-wide; everything else goes into `.venv`
- `nh3` (the Markdown sanitiser) ships prebuilt wheels for common platforms; on an unusual
  architecture pip builds it from source and needs a Rust toolchain

## Quick start

```bash
git clone https://github.com/TehPeGaSuS/papyrus.git && cd papyrus
python3 app.py
```

The first run only sets things up: it creates `.venv`, installs Papyrus into it (`pip install -e .`, which also
gives you a `.venv/bin/papyrus` command) and writes `config.json` with a fresh random `secret_key`. Nothing is
started, so you can look through `config.json` (and run `python3 app.py admin`) first. **Then start the server with
`python3 app.py --start`**, which listens on <http://127.0.0.1:5500> using Gunicorn. (`--start` also works on the very
first run, if you'd rather not stop in between.) Python 3.9+ is required.

| Command | What it does |
|---|---|
| `python3 app.py --start` | Run in the foreground (plain `python3 app.py` does the same, except on the very first run, which only sets things up) |
| `python3 app.py --start -d` | Run in the background (`papyrus.pid`, `papyrus.log`) |
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
- `pastes.expiries`: the expiry choices, see below. `pastes.default_expiry` names the one selected by default
- `pastes.*`: also the size limit (for the whole paste), `max_files`, the default language and `recent_limit`
- `pastes.store_ips`: keep the author's address with each paste and comment (default `false`). Needed for the
  one-click **Block** buttons and to see addresses in the panel; the address is deleted together with the paste or
  comment. Leave it off and nothing but a keyed hash (for reports) ever records who posted; you can still block by
  typing in an address, for example one copied from the access log
- `site.footer`: a line of text at the bottom of every page (the shipped one says the site claims no ownership of,
  and has no liability for, what people paste). `site.notice`: an optional banner under the top bar, off by default.
  Both are inline Markdown, so `[links](https://example.org)` and `**emphasis**` work and raw HTML is escaped; leave
  either empty to hide it. The footer always ends with `Powered by Papyrus <version> · Source` (the link goes to the project's source)
  For example, a notice with a link (write it as one line of JSON in `config.json`):

  ```json
  "notice": "Pastes are removed after **one month**. Please don't paste secrets. See the [rules](https://example.org/rules)."
  ```
- `theme.palettes`, `theme.palette`: the colour themes visitors can pick from the dropdown next to the light/dark
  toggle, and the default one (rust). See below
- `features.show_recent`: enable the `/recent` page
- `features.reports`: allow reporting (default `true`; also needs the admin panel, since someone has to read them)
- `admin.*`: managed by `python3 app.py admin`
- `database.path`: SQLite file, relative to the project directory (default `papyrus.db`)

### Colour themes

`theme.palettes` holds the themes offered in the dropdown (Rust, Parchment, Green, Violet and Mono are included);
`theme.palette` names the default. The server turns them into one stylesheet, so there is no flash of other
colours, and each visitor's choice is remembered in their browser. Every palette has a `light` and a `dark`
variant with the same ten colours; add your own by copying one:

```json
"palettes": {
  "sea": {
    "label": "Sea",
    "logo_filter": "hue-rotate(160deg)",
    "light": { "primary": "#0e7490", "primary_hover": "#155e75", "on_primary": "#ffffff", "background": "#ffffff",
               "surface": "#f6f6f7", "border": "#e4e4e7", "text": "#18181b", "text_secondary": "#52525b",
               "text_muted": "#a1a1aa", "danger": "#b91c1c" },
    "dark":  { "...": "same ten keys" }
  }
}
```

`logo_filter` (optional) is a CSS filter that recolours the logo, which ships in the default palette's colour.
Colours are hex values; a mistake stops Papyrus at startup with a message saying which one. With a single
palette the dropdown is hidden, and `theme.allow_user_toggle` switches the light/dark toggle off.

### Expiry times

`pastes.expiries` maps a name to a duration, like pinnwand's `expiries`. The names are what the menu, the
API (`expires_in=1day`) and `curl` use; the durations are yours to choose:

```json
"expiries": {
  "10min":   "10m",
  "1hour":   3600,
  "3days":   "3d",
  "forever": null
},
"default_expiry": "1hour"
```

Papyrus is for temporary pastes, so the shipped choices stop at one month and the default is one week.

A duration is a number of seconds, a short form (`30s`, `10m`, `36h`, `3d`, `2w`, `1y`), or `null` for a paste
that never expires. The menu label is made from the duration ("10 minutes", "3 days", "Never"); to word it
yourself write `"3days": {"duration": "3d", "label": "Three days"}`. An unknown `expires_in` is refused with a list
of the valid names. A mistake in this section stops Papyrus at startup with a message saying what is wrong.

## Deployment

Run Papyrus behind a reverse proxy that terminates TLS. Ready-to-use configs for **nginx**, **Apache**
and **Caddy**, with Let's Encrypt (certbot) and **Cloudflare** notes, are in [`webserver/`](webserver/).

The one thing to know: the proxy has to **set** two headers, `X-Forwarded-For` (the visitor's address,
for the per-IP rate limits) and `X-Forwarded-Proto` (`https` over TLS, for the origin check). Papyrus
believes them only from a proxy on the same machine (`server.trusted_proxy_ips`), so a visitor who
reaches the app directly can't forge them. Keep it bound to `127.0.0.1` and set `"use_https": true`
when serving over TLS. [`webserver/README.md`](webserver/README.md) has the details and a quick check.
The access log (`papyrus.log`, or the journal under systemd) shows the visitor's address by the same rule, not the
proxy's 127.0.0.1. If it still does, the proxy is not sending `X-Forwarded-For`.

A systemd system service (`/etc/systemd/system/papyrus.service`), for a checkout in a user's home directory. Run `python3 app.py`
once as that user first to set everything up (that run only bootstraps; the service starts Papyrus with `--start`).
Replace `youruser` with the account that owns the checkout:

```ini
[Unit]
Description=Papyrus pastebin
After=network.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/papyrus
ExecStart=/home/youruser/papyrus/.venv/bin/papyrus --start
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Or as a systemd **user** unit, which needs no root. Save it as `~/.config/systemd/user/papyrus.service` (for a
checkout in `~/papyrus`; `%h` is your home directory):

```ini
[Unit]
Description=Papyrus pastebin

[Service]
WorkingDirectory=%h/papyrus
ExecStart=%h/papyrus/.venv/bin/papyrus --start
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now papyrus
loginctl enable-linger "$USER"        # keep it running after you log out, and start it at boot
systemctl --user status papyrus       # logs: journalctl --user -u papyrus
```

Lingering is what lets a user service run while you are logged out. On Ubuntu and Debian you can switch it on for
your own account without root (on some distributions it needs `sudo loginctl enable-linger "$USER"`). Check it with
`loginctl show-user "$USER" -p Linger`, which prints `Linger=yes` when it is on.

## Usage

`curl` works without a browser:

```bash
$ echo "your text" | curl -X POST https://host/curl -F 'raw=<-'
Paste URL:   https://host/3f9c1a7e2b4d
Raw URL:     https://host/raw/3f9c1a7e2b4d
Removal URL: https://host/remove/DQMUUD4K5ZIRTT62HLC26SPQD4

$ curl -X POST https://host/curl -F 'raw=<file.py' -F filename=file.py -F expires_in=1day
$ curl --data-binary @file.py 'https://host/create?filename=file.py&title=notes'   # same output
```

Optional form fields for `/curl`: `filename` (picks the syntax highlighting), `title` (the description),
`language`, `expires_in` and `discussions`. Repeat `-F raw=… -F filename=…` for a paste with several files (the
reply then has one `Raw URL (<filename>)` line per file).

**Removing a paste.** The Removal URL is the only thing that removes it, so keep it. Opening it removes
the paste at once (as in PrivateBin and pinnwand), so don't post it where a link-previewing bot would read it; from a
script, `curl -X POST <Removal URL>` does the same. In a browser you don't need the link: the browser that
created the paste keeps a cookie, so the page shows **Remove now** to that browser whenever you come back
(after a refresh, a closed tab or a restart), until the paste expires. Nobody else sees the button. The
link is also shown once after saving, for removing the paste from another device.

## API

- `GET /`: create-paste page
- `GET /api`: a page on the site that explains the ways to post (curl, plain text, JSON), with this site's own address in the examples
- `POST /curl`: create a paste from form fields (`raw`, optional `filename`, `title`, `language`, `expires_in`,
  `discussions`); prints the paste, raw and removal URLs as plain text
- `POST /create`: the same from JSON (`content`, or `files`: a list of `{content, filename, language}` for several
  files; optional `title` (the description), `filename`, `language`
  (`auto` or omitted: detected from the filename), `expires_in`, `discussions`; answers with `paste_id`, `url`,
  `removal_token`, `removal_url` and sets the owner cookie) or from a plain-text body (prints like `/curl`)
- `GET /<id>`: view a paste (`?view=preview` for a rendered Markdown paste); `GET /raw/<id>`: its (first) file as `text/plain`; `GET /raw/<id>/<filename>`: any file of it.
  `?f=2&L=5-12` highlights lines of the second file
- `POST /<id>/comment`: add a comment (form fields `content`, optional `nick`, optional `parent`, the
  id of the comment being answered; threads deeper than 6 levels are flattened)
- `POST /api/preview`: render Markdown for the preview tab
- `GET|POST /remove/<token>`: the removal link. GET asks for confirmation, POST removes the paste (and its
  comments and reports). The token is the only credential
- `GET /recent` (when enabled), `GET /api/languages`
- `GET|POST /<id>/report` (optional `?comment=<comment id>`): report a paste or a comment
- Admin: `GET|POST /admin/login`, `POST /admin/logout`, `GET /admin`, `POST /admin/delete/<id>`,
  `POST /admin/comments/<comment id>/delete` (replies are kept and move up a level),
  `POST /admin/reports/dismiss`, `GET /admin/paste/<id>/data` (the View window), `POST /admin/blocks`,
  `POST /admin/blocks/<id>/delete`

## Database

SQLite, created automatically, stored as plain text (there is no end-to-end encryption).

```sql
CREATE TABLE pastes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Untitled',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,               -- ISO-8601 UTC, NULL = never
    views INTEGER DEFAULT 0,
    removal_hash TEXT NOT NULL DEFAULT '',   -- sha256 of the removal token, never the token itself
    discussions_enabled INTEGER DEFAULT 0
);
CREATE TABLE files (
    paste_id TEXT NOT NULL, position INTEGER NOT NULL, filename TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'text', content TEXT NOT NULL,
    PRIMARY KEY (paste_id, position)
);
CREATE TABLE comments (
    id TEXT PRIMARY KEY, paste_id TEXT NOT NULL, parent_id TEXT, nick TEXT,
    content TEXT NOT NULL,         -- Markdown source
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    creator_ip TEXT NOT NULL DEFAULT ''    -- only with pastes.store_ips
);
CREATE TABLE blocks (
    id TEXT PRIMARY KEY, network TEXT NOT NULL UNIQUE,   -- an address or a CIDR range
    note TEXT NOT NULL DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT                                      -- NULL = until removed
);
CREATE TABLE reports (
    id TEXT PRIMARY KEY, paste_id TEXT NOT NULL,
    comment_id TEXT NOT NULL DEFAULT '',   -- '' = the paste itself
    reason TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    reporter TEXT NOT NULL,                -- keyed hash of the address, never the address
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (paste_id, comment_id, reporter)
);
```

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
- Reports store only a keyed hash of the reporter's address (to allow one report per visitor and target),
  never the address; they are rate-limited, and deleting a paste or comment removes its reports.
- Size limits: `pastes.max_size_bytes` (2 MB default) for pastes, 10 KB for comments.
- Gunicorn's control socket is named after the project and port (`$XDG_RUNTIME_DIR/papyrus-<port>.ctl`,
  mode 0600) so several instances don't collide.

## Development

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

## Versioning

Papyrus uses [semantic versioning](https://semver.org/). The version, shown in every page's footer and printed by
`python3 app.py --version`, lives in one place: `__version__` in `app.py` (the package metadata reads it from there).
To release: change it, commit, and tag the commit `vX.Y.Z`.

## Troubleshooting

- **Port already in use**: `python3 app.py status`, or pick another with `--port`.
- **Reset everything**: stop the server and delete `papyrus.db`.
- **Permission errors**: the app needs write access to its directory (database, `config.json`, log, pid file).
- **"Python cannot create virtual environments"** (Debian/Ubuntu): `sudo apt install python3-venv`, then run `python3 app.py` again.

## License

MIT, see [LICENSE](LICENSE). Syntax highlighting by [Prism](https://prismjs.com/); built with Flask,
Gunicorn, mistune and nh3.

<sub>Originally derived from Bastebin.</sub>
