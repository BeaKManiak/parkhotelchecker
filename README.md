# parkhotelchecker

A self-hosted price tracker for Disneyland Paris hotel + park-ticket packages.

It watches sixteen hotels across a configurable arrival window, records every
price change, draws the history, and sends one Telegram digest per poll when
something is worth knowing. It runs in Docker on a Synology DS218+, on your LAN,
with no cloud service in the loop.

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

## What it deliberately does not do

- Rank hotels against each other. Each hotel has its own cheapest arrival.
- Alert on non-euro markets. Canary markets are fetched daily and stored, but
  their currencies are market multipliers, not FX, and comparing them without a
  conversion is a lie. They are visible on the charts, silent in Telegram.
- Convert past prices retroactively.
- Pause a track. Stopping is terminal; to resume, duplicate the configuration
  into a new track. History is never deleted.
- Require a JavaScript framework, a build step, a CDN, or an external database.

---

## Quick start (local)

Python 3.14 is required (the container uses `python:3.14-slim`).

```bash
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# a bcrypt hash for the dashboard password
.venv/bin/python -m app.web.auth hash

export DASHBOARD_USER=miquel
export DASHBOARD_PASSWORD_HASH='$2b$12$...'
export DB_PATH=data/parkhotelchecker.db
.venv/bin/python -m app
```

Open <http://127.0.0.1:8080>. The first poll happens immediately; prices appear
on the track page within a minute.

Run the tests with `./.venv/bin/pytest -q`. They are offline: recorded fixtures,
no network, no sleeps.

---

## Configuration

Everything comes from the environment. The container reads `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_USER` | — | Required. Basic-auth username. |
| `DASHBOARD_PASSWORD_HASH` | — | Required. bcrypt hash, not the password. |
| `TELEGRAM_BOT_TOKEN` | unset | Without it, digests are logged, not sent. |
| `TELEGRAM_CHAT_IDS` | unset | Comma-separated chat ids. |
| `DB_PATH` | `data/parkhotelchecker.db` | SQLite location. |
| `BACKUP_DIR` | `<db folder>/backups` | Nightly snapshots. Set to `""` to disable. |
| `HOST` | `127.0.0.1` | Bind address. `0.0.0.0` is refused unless `ALLOW_PUBLIC_BIND=1`. |
| `PORT` | `8080` | Dashboard port. |
| `POLL_INTERVAL_SECONDS` | `60` | How often to *look* for due work, not how often to fetch. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |
| `LAN_BIND_IP` | `127.0.0.1` | Compose only: the LAN address to publish on. |
| `DATA_DIR` | `./data` | Compose only: host folder mounted at `/data`. |

`POLL_INTERVAL_SECONDS=60` does not mean a request every minute. The poller asks
"what is due?" and fetches only buckets whose cadence has elapsed: 6 hours for
the Spanish market, 24 hours for canaries, 24 hours for tickets.

---

## Running on the NAS

```bash
# on the NAS, in the repo folder
cp .env.example .env
chmod 600 .env
# edit .env: password hash, Telegram, LAN_BIND_IP, DATA_DIR

sudo mkdir -p /volume1/docker/parkhotelchecker/data
sudo chown -R 1000:1000 /volume1/docker/parkhotelchecker/data
docker compose build
docker compose up -d
docker compose logs -f
```

The `chown` matters: the container runs as uid 1000, and a Docker-created
folder is owned by root. Without it, SQLite cannot create its WAL files and the
container restarts in a loop.

The port mapping is `LAN_BIND_IP:8080:8080`, never `0.0.0.0`. If `LAN_BIND_IP`
is unset it defaults to loopback, which is safe and unreachable from your phone —
that is deliberate, so an unconfigured deployment fails closed.

The container is:

- `read_only: true`, with `/tmp` as tmpfs and `/data` as the only writable volume;
- `cap_drop: ALL`, `no-new-privileges`;
- non-root, uid 1000;
- healthchecked via `GET /healthz` (which needs no credentials and reveals
  nothing but "the process is up").

As defence in depth, restrict port 8080 to your LAN in the DSM firewall as well.

The SQLite file lives at `/volume1/docker/parkhotelchecker/data/parkhotelchecker.db`
(your `DATA_DIR`), so File Station can see and copy it.

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

February half term · 2n/3j · 2a1c
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
- **Track** — per-hotel cheapest available arrival, all-time low, last seen, the
  alert toggle, and a strip of facts (markets, threshold, changes in the last
  24 h). Window trim/extend, check-now and stop live here.
- **Hotel** — step chart of price over time, one line per arrival date (the 12
  cheapest, with history), plus a table with package, previous, all-time low,
  tickets, implied hotel cost, and when it was last seen.
- **Health** — per-source last success, consecutive failures, last error, recent
  failures and recent requests.

Trimmed arrivals are hidden from the chart but their observations are untouched,
so extending the window again restores the lines exactly as they were.

Authentication is HTTP Basic. **There is no TLS**: credentials and pages travel
in clear text inside your LAN. That is the accepted trade for a two-person tool
on a home network. Do not port-forward it.

---

## Backups

Once a day, before polling, the app writes a consistent snapshot with
`VACUUM INTO`:

```
/data/backups/parkhotelchecker-YYYY-MM-DD.db
```

Retention is seven daily files, plus Sundays for four weeks. Nothing else is
ever deleted.

Restore:

```bash
docker compose down
cp /volume1/docker/parkhotelchecker/data/backups/parkhotelchecker-2026-10-22.db \
   /volume1/docker/parkhotelchecker/data/parkhotelchecker.db
rm -f /volume1/docker/parkhotelchecker/data/parkhotelchecker.db-wal \
      /volume1/docker/parkhotelchecker/data/parkhotelchecker.db-shm
docker compose up -d
```

Copy a snapshot off the NAS occasionally. A backup on the same disk as the
database is a backup against mistakes, not against the disk.

---

## Development

```bash
./.venv/bin/pytest -q                 # 123 tests, offline, under a second
./.venv/bin/python scripts/live_probe.py packages --hotel DCHH --month 2027-01
./.venv/bin/python scripts/live_probe.py tickets  --month 2026-11 --child 2020-01-01
```

The live probe is manual and read-only. It exists because the tests cannot tell
you that Disneyland changed a payload; it can.

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
```

Python 3.14 is used everywhere. `uvicorn[standard]` is deliberately avoided:
its uvloop dependency adds a compiled piece we do not need, and the plain wheel
set installs cleanly on a DS218+ with no compiler.

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
