# parkhotelchecker

A self-hosted price tracker for Disneyland Paris hotel + park-ticket packages.

It watches sixteen hotels across a configurable arrival window, records every
price change, draws the history, and sends one Telegram digest per poll when
something is worth knowing. The server, the database and the dashboard all run
on your own NAS; there is no hosted backend. Price lookups go to Disneyland
Paris, and alerts (if enabled) go to Telegram.

Two people use it. That is the scale it is built for.

---

## What it does

- Polls the Disneyland Paris package price calendar every 6 hours (±15 min
  jitter) for the arrival dates you care about.
- Prices the park tickets separately, so it can show the package price, the
  ticket price, and the implied hotel-only cost for the same date.
- Tracks all 16 hotels on the DLP site (7 on-site, 9 partner). You choose which
  ones may alert you.
- Stores prices in the currency of the market they came from, as integer cents,
  and never converts them after the fact.
- Sends a Telegram digest only when there is something to say: a move at or
  above your threshold, a new all-time low, or a sold-out / back-available flip.
- Says so, loudly, when it cannot see: three consecutive fetch failures, or a
  response whose shape it does not recognise.
- Keeps the whole history forever. There is no "clean up old data" button.

---

## Quick start: Synology NAS (recommended)

### Before you start

- Container Manager (or Docker with Compose v2) and terminal/SSH access.
- Permission to use Docker. On many DSM setups a normal user cannot talk to the
  Docker daemon; if `docker compose build` fails with `permission denied ...
  docker.sock`, prefix the compose commands with `sudo` (see Troubleshooting).
- The NAS's LAN IP, ideally reserved in your router's DHCP table so it does not
  move. The examples use `192.168.1.20`; substitute yours.
- About ten minutes, most of it the first image build.

**Trust boundary, stated plainly:** the dashboard speaks plain HTTP and is
meant for a trusted home LAN. Basic auth is not encryption, and anyone who can
watch LAN traffic can read the credentials. Do not port-forward it, and do not
put it on guest Wi-Fi. Restrict port 8080 in the DSM firewall as well. If you
need encryption in transit, terminate TLS in a reverse proxy or use a VPN and
publish the container on loopback; this repo does not configure that for you.

### Step 1 — get the code

```bash
# on the NAS, via SSH
git clone git@github.com:BeaKManiak/parkhotelchecker.git
cd parkhotelchecker
```

If git is not installed or you prefer not to use it, download the repository
ZIP from GitHub, upload it with File Station (or `scp`), and unpack it.

### Step 2 — create `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set at least:

- `DASHBOARD_USER` — the username you will type in the browser.
- `LAN_BIND_IP` — the NAS's LAN IP, never `0.0.0.0`.
- `DATA_DIR` — where the database and its backups live. The examples use
  `/volume1/docker/parkhotelchecker/data`; use one path consistently.
- `DASHBOARD_PASSWORD_HASH` — generated below (Step 4).
- Telegram values, if you want alerts (optional).

`.env` holds secrets: never commit it, never paste it into a chat, and keep it
mode `600`.

### Step 3 — create the data folder

```bash
sudo mkdir -p /volume1/docker/parkhotelchecker/data
sudo chown -R 1000:1000 /volume1/docker/parkhotelchecker/data
```

Use your own `DATA_DIR` instead of the example path, and do this **before** the
next step: compose bind-mounts `${DATA_DIR}` into `/data`, and on DSM that mount
fails if the folder does not exist yet. The `chown` matters too: the container
runs as uid 1000, and a root-owned folder means SQLite cannot create its WAL
files, so the container restarts in a loop.

### Step 4 — build, then generate the password hash

```bash
docker compose build
docker compose run --rm app python -m app.web.auth hash
```

Type the password when prompted; it is hidden. The command prints a hash and
exits — it does **not** start the server or publish a port.

Put the hash in `.env` **wrapped in single quotes**:

```dotenv
DASHBOARD_PASSWORD_HASH='$2b$12$.....................................................'
```

The quotes matter. Compose treats an unquoted `$2b$12$...` as variable
interpolation and truncates it; the symptom is an app that starts but rejects
the correct password. The app now refuses an incomplete hash at startup and
prints this same explanation, so the failure is not a mystery.

### Step 5 — start

```bash
docker compose up -d
docker compose ps
docker compose logs -f app
```

Healthy looks like: container status `healthy`, and logs showing the dashboard
URL, "nightly backups: ...", and the scheduler starting. Open
`http://192.168.1.20:8080` (your NAS IP) and sign in.

The port is published as `LAN_BIND_IP:8080:8080`, never `0.0.0.0`. If you leave
`LAN_BIND_IP` at its default (`127.0.0.1`) the dashboard is unreachable from
your phone — that is deliberate, so an unfinished config fails closed.

### Step 6 — create your first track

The dashboard is empty until you create a track. Click **New track** and fill
in:

- a name, the number of adults and children, and a birthdate for each child
  (a room holds at most 7 people, adults and children together; children under
  3 are free and are left out of the price request entirely);
- the earliest and latest **check-in** date, within the booking horizon shown on
  the form (the window can cover at most 31 arrival dates; these are not
  check-out dates);
- the length of stay (default 2 nights, 3 park days);
- which hotels are **shortlisted**. Shortlisting controls *who may alert you*;
  all 16 hotels are polled regardless, and none are selected by default;
- optionally, canary markets. They are charted but never alerted;
- the minimum change that deserves an alert (default €20). New all-time lows
  and sold-out / back-available flips alert regardless.

The first poll starts immediately. Give it about a minute, then refresh. If the
track page is still empty, open **Health**: it shows per-source last success,
consecutive failures and the last error, which is always more useful than
guessing.

### Telegram (optional, short version)

1. In Telegram, talk to **@BotFather**, send `/newbot`, and follow the prompts.
   Copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot.
3. Read the chat id from the Bot API:

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```

   The id is `result[0].message.chat.id`. Group ids are negative; add the bot
   to the group first.
4. Put one or more ids, comma-separated, in `TELEGRAM_CHAT_IDS`, then
   `docker compose up -d` to recreate the container with the new environment.

Without a token the app still tracks and charts; each digest is written to
`docker compose logs app` instead of being sent. Keep the token private and
revoke it with BotFather if it leaks.

---

## Troubleshooting the first run

| Symptom | Likely cause | Fix |
|---|---|---|
| `permission denied` on `/var/run/docker.sock` | Your NAS user may not use the Docker daemon | Prefix the compose commands with `sudo`; or, if the socket's group is `docker`, add your user to that group and log in again |
| `Bind mount failed: '<DATA_DIR>' does not exist` | The data folder was created after the compose run | `sudo mkdir -p "$DATA_DIR" && sudo chown -R 1000:1000 "$DATA_DIR"`, then retry |
| Container restarts in a loop | Data folder owned by root | `sudo chown -R 1000:1000 "$DATA_DIR"` |
| Cannot sign in, password is right | Hash not single-quoted, or incomplete | Regenerate, single-quote it, `docker compose up -d` |
| Unreachable from phone | `LAN_BIND_IP` still `127.0.0.1` | Set the NAS LAN IP, `docker compose up -d` |
| HTTP 429 | 5 failed logins from one address in 10 minutes | Wait about ten minutes |
| No prices on the track page | First poll still running, or upstream failing | Wait, refresh, check **Health** |
| No Telegram messages | Token or chat ids unset/wrong | `docker compose logs app` shows the digest and the send error |

Adding a user to the `docker` group is effectively giving them root on the NAS.
That is usually acceptable on a personal machine, but `sudo` per command is the
more conservative option. Never `chmod 666 /var/run/docker.sock`.

---

## Updating and stopping

```bash
git pull                       # or replace the files
docker compose build
docker compose up -d
```

Data in `DATA_DIR` persists across rebuilds and updates. To stop the service:

```bash
docker compose down
```

That stops the container; it does not delete the database or the backups.
Remember to copy a snapshot off the NAS occasionally.

---

## Optional: run it locally first

This is a **live run**: it contacts Disneyland Paris and writes to a separate
local database. It is not needed before deploying to the NAS.

```bash
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'

.venv/bin/python -m app.web.auth hash

export DASHBOARD_USER=admin
export DASHBOARD_PASSWORD_HASH='$2b$12$...'
export DB_PATH=data/parkhotelchecker.db
.venv/bin/python -m app
```

Open <http://127.0.0.1:8080>. It binds to loopback; do not expose it. The
pinned container versions are in `requirements.txt`; local installs use the
ranges in `pyproject.toml`, which is fine for development.

## Offline tests

Separate from the live run, and safe to run anywhere:

```bash
./.venv/bin/pytest -q
```

The suite is offline: recorded fixtures, no network, no sleeps.

---

## Configuration

Everything comes from the environment. The container reads `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_USER` | — | Required. Basic-auth username. |
| `DASHBOARD_PASSWORD_HASH` | — | Required. bcrypt hash (quote it). |
| `TELEGRAM_BOT_TOKEN` | unset | Without it, digests are logged, not sent. |
| `TELEGRAM_CHAT_IDS` | unset | Comma-separated chat ids. |
| `DB_PATH` | `data/parkhotelchecker.db` | SQLite location in the container (`/data/...`). |
| `BACKUP_DIR` | `<db folder>/backups` | Nightly snapshots. Set to `""` to disable. |
| `HOST` | `127.0.0.1` | Bind address. `0.0.0.0` is refused unless `ALLOW_PUBLIC_BIND=1`. |
| `PORT` | `8080` | Dashboard port. |
| `POLL_INTERVAL_SECONDS` | `60` | How often to *look* for due work, not how often to fetch. |
| `TEST_MODE` | `0` | Development only: record every check and let “Check now” ignore cadence. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |
| `LAN_BIND_IP` | `127.0.0.1` | Compose only: the LAN address to publish on. |
| `DATA_DIR` | `./data` | Compose only: host folder mounted at `/data`. |

`POLL_INTERVAL_SECONDS=60` does not mean a request every minute. The poller asks
"what is due?" and fetches only buckets whose cadence has elapsed: 6 hours for
the Spanish market, 24 hours for canaries, 24 hours for tickets.

---

## Hardening

The container is:

- non-root, uid 1000;
- `read_only: true`, with `/tmp` as tmpfs and `/data` as the only writable
  volume;
- `cap_drop: ALL`, `no-new-privileges`;
- healthchecked via `GET /healthz`, which needs no credentials and reveals
  nothing but "the process is up".

The app:

- requires Basic auth on every page except `/healthz`;
- verifies a CSRF token on every POST;
- sends a strict CSP with no inline script or style, and does not use a CDN;
- sets `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` and
  `Cache-Control: no-store`;
- disables the generated API docs;
- parameterises every SQL statement;
- brakes brute force at 5 failed logins per source address per 10 minutes;
- refuses to bind `0.0.0.0` unless `ALLOW_PUBLIC_BIND=1` (set only inside the
  container, where Compose publishes just `LAN_BIND_IP`).

Limits worth knowing: one shared credential with no per-user audit trail; no
TLS; the CSRF token is deterministic for a given password hash (adequate here,
not a session-specific token).

---

## How a price becomes an observation

A **series** is one indivisible question: *this hotel, this market, this arrival
date, this length of stay, this party*. Its identity deliberately excludes the
track, so two tracks watching the same holiday share one history and one upstream
request.

A row is written to the change-log only when:

- the price differs from the last row, or
- availability differs (sold out, or back), or
- 24 hours have passed with no change (a heartbeat, so the chart has a pulse).

With `TEST_MODE=1`, every check writes a row instead; see [Test mode](#test-mode-development-only).

Two absences are *not* the same thing:

- **Sold out** — the date was seen before and is missing from today's response.
  Recorded with a NULL price, and the UI says "sold out".
- **Never seen** — the date has never appeared for this party. Usually the
  hotel cannot sell that occupancy at all. Nothing is recorded, and the UI says
  "not seen".

Conflating them would have produced fake price drops on the first day of every
new track, so they are separate states forever.

Children under 3 are free and absent from the upstream payload, so they are left
out of the party entirely. A child who turns 3 during the arrival window is
refused with a message to split the track: one track prices one party.

Prices are stored in the currency implied by the market, which is *not* in the
payload. `es-es` is EUR; `en-gb` is GBP; `fr-ch` is CHF. A missing market in the
currency map is an error, never a default.

---

## Test mode (development only)

Set `TEST_MODE=1` in `.env` and recreate the container:

```bash
docker compose up -d --force-recreate app
```

While it is on:

- every poll writes an observation, even when the price has not changed, so the
  chart gets a point per check instead of one per change or per day;
- **Check now** fetches every watched bucket immediately, ignoring the 6-hour
  and 24-hour cadence. Scheduled polls still honour it.

It is off by default. Expect roughly four times the normal Spanish-market row
count at 6-hourly polling. The extra rows are ordinary heartbeats: alerts
ignore them, but they stay in the history. The dashboard shows a warning banner
and the startup log says so, so it is hard to leave on by accident. Turn it off
and restart when you are done.

---

## Alerts

One message per poll, and only if there is something to say.

A line appears when, for an **active** track, on the **Spanish** market, for a
**shortlisted** hotel:

- the price moves by at least the track's `min_delta` (default €20) in either
  direction, or
- the price sets a new all-time low for that arrival, even below the threshold,
- or availability flips (sold out ↔ back available).

`min_delta` is per track and editable.

```
🎢 Disneyland Paris — 22 Oct, 18:04  ·  3 changes

February half term · 2 nights · 3 park days · 2a1c
  ▼ 7 Feb   €690 → €640   (−€50, all-time low)
  ▲ 3 Feb   €612 → €648   (+€36)

Best arrival this window
  Cheyenne 7 Feb — package €640 · tickets €549 · implied hotel €91

Last successful check: 12 min ago
```

At most 12 change lines; the rest become `… and 9 more`.

Failure alerts are separate and immediate:

| Trigger | What you get |
|---|---|
| 3 consecutive failures for a source | one message, then silence until recovery |
| recovery | one all-clear |
| unrecognised response shape | immediately, throttled to once per 6 h |
| missing ticket product code | the ticket line is disabled, never the package prices |

Telegram send failures cannot be alerted over Telegram; they are logged and
increment the `telegram` row on the health page.

**Silence is only allowed to mean "nothing moved".** If the tracker cannot see,
it says so, and the dashboard header says "last successful check: N hours ago".

---

## The dashboard

- **Tracks** — one row per track: party, window, nights, cheapest arrival now.
  Archived tracks sit in a collapsed section below.
- **Track** — per-hotel cheapest available arrival, all-time low, last seen, the
  alert toggle, and a strip of facts (markets, threshold, changes in the last
  24 h). Window trim/extend, check-now, stop, archive and delete live here.
- **Hotel** — price history opens **by check-in date**, with the cheapest
  current date in the summary and #1/#2/#3 badges on the three cheapest
  arrivals. **Over time** is the alternative: x is when the price was checked,
  one stepped line per arrival date (the 12 cheapest, with history), with a
  7 day / 30 day / all selector. Below it: package, previous, all-time low,
  tickets, implied hotel cost, and when each arrival was last seen.
- **Health** — per-source last success, consecutive failures, last error, recent
  failures and recent requests.

Trimmed arrivals are hidden from the chart but their observations are untouched,
so extending the window again restores the lines exactly as they were.

### Archiving and deleting tracks

**Archive** hides a track, stops its polling and alerts, and keeps every link
and price. It can be restored; the track's own state (tracking or stopped) is
unchanged. **Stop** is still terminal: a stopped track stays in the list until
you archive or delete it.

**Delete** removes the track row and its hotel, market and date links. It does
not remove prices: a series is keyed by hotel, market, arrival date, stay length
and party, independently of any track, and observations belong to the series.
Ticket prices, parties and fetch history stay too. A future track for the same
holiday reuses those series and picks up their history, including observations
from before it was created.

---

## Backups and restore

Once a day, before polling, the app writes a consistent snapshot with
`VACUUM INTO`:

```
${DATA_DIR}/backups/parkhotelchecker-YYYY-MM-DD.db
```

Retention is seven daily files, plus Sundays for four weeks. Nothing else is
ever deleted.

Restore (stop the service first, so nothing is writing):

```bash
docker compose down
cp /volume1/docker/parkhotelchecker/data/backups/parkhotelchecker-2026-10-22.db \
   /volume1/docker/parkhotelchecker/data/parkhotelchecker.db
rm -f /volume1/docker/parkhotelchecker/data/parkhotelchecker.db-wal \
      /volume1/docker/parkhotelchecker/data/parkhotelchecker.db-shm
docker compose up -d
```

Substitute your `DATA_DIR` if it differs. Copy a snapshot off the NAS
occasionally. A backup on the same disk as the database is a backup against
mistakes, not against the disk.

**Privacy note:** the database stores the party's children's birthdates and the
trip windows in plain text, and the backups do too. Protect the `DATA_DIR`
share and any copies you take; do not expose it to more SMB/NFS/File Station
users than necessary.

---

## Development

```bash
./.venv/bin/pytest -q                 # offline, under a second
./.venv/bin/python scripts/live_probe.py packages --hotel DCHH --month 2027-01
./.venv/bin/python scripts/live_probe.py tickets  --month 2026-11 --child 2020-01-01
```

The live probe is manual and read-only. It exists because the tests cannot tell
you that Disneyland changed a payload; it can. It is never run automatically.

`tests/fixtures/` contains recorded real responses. **Do not edit a fixture to
make a test pass.** A changed fixture means the upstream changed, which is a
finding to act on, not a test to adjust.

Layout:

```
app/
  __main__.py        entrypoint: db, scheduler, notifier, uvicorn
  config.py          endpoints, markets, horizon, money
  db.py              schema, migrations, WAL, snapshot
  dlp/               package + ticket API clients, hotels, shape guards
  tracking/          planner, poller, scheduler, change-log, health, backups
  alerts/            digest selection, failure alerts, Telegram, notifier
  web/               FastAPI app, routes, auth, templates, vendored assets
tests/               offline unit and integration tests
scripts/             manual live probe
requirements.txt     pinned runtime set used by the container
```

Python 3.14 is used everywhere. `uvicorn[standard]` is deliberately avoided:
its uvloop dependency adds a compiled piece we do not need, and the plain wheel
set installs cleanly on a DS218+ with no compiler.

### Updating dependencies

The container installs the exact versions in `requirements.txt`; `pyproject.toml`
keeps ranges for local development. Update deliberately, never by letting a
rebuild float:

```bash
python3.14 -m venv /tmp/update
/tmp/update/bin/pip install -U fastapi uvicorn httpx jinja2 bcrypt python-multipart
/tmp/update/bin/pip install -e '.[dev]'
./.venv/bin/pytest -q
# rewrite requirements.txt with the new runtime closure, then:
docker compose build && docker compose up -d
```

Periodically, in a scratch venv:

```bash
pip install pip-audit && pip-audit -r requirements.txt
```

---

## Known limits

- **Upstream drift.** If Disneyland changes a payload, the shape guard fails
  loudly rather than guessing. Expect to fix `app/dlp/` when that happens.
- **Seasonal product codes.** Ticket codes rotate roughly every April. Discovery
  resolves them automatically; a missing duration disables the ticket reference
  line and alerts, rather than printing a wrong number.
- **Occupancy ambiguity.** A hotel that cannot sell a party's occupancy looks
  identical to a hotel with no availability, until you have history. The
  never-seen rule keeps them distinguishable after the fact.
- **One process.** A second container would double-poll. Deployment is the lock.
- **No TLS, no multi-user accounts, no CSRF-free API.** It is a LAN appliance.
- **Personal use.** Volume is kept low and the user agent honest, in line with
  DLP's personal, non-commercial copying clause. Do not turn this into a scraper
  farm.
