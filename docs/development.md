# Development

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
- a manual **Check now** also sends a compact test message, so you can verify
  Telegram without waiting for a price move.

Check now itself always forces that track's fetch, in normal mode too; test mode
only changes what is recorded and whether that extra message is sent. Scheduled
polls keep their cadence.

It is off by default. Expect roughly four times the normal Spanish-market row
count at 6-hourly polling. The extra rows are ordinary heartbeats: alerts
ignore them, but they stay in the history. The dashboard shows a warning banner
and the startup log says so, so it is hard to leave on by accident. Turn it off
and restart when you are done.

---

## Development

```bash
./.venv/bin/pytest -q                 # offline, under a second
./.venv/bin/python scripts/live_probe.py packages --hotel DCHH --month 2027-01
./.venv/bin/python scripts/live_probe.py tickets  --month 2026-11 --child 2020-01-01
./.venv/bin/python scripts/probe_markets.py --hotel DCHH --month 2027-02
```

`probe_markets.py` asks every market for the same hotel-month and prints a
table: expected currency, returned currency, sellable days, cheapest price.
It is the check to run before adding a market to a comparison track. Use
`--markets fr-fr,de-de,en-gb` to narrow it.

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

