# Deployment

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
- optionally, the other EUR markets (checked every 72 hours) and/or the `en-gb`
  and `fr-ch` canaries. Canaries are charted but never alerted;
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
5. Open **Health → Telegram** and press **Send test message**. It reports the
   result per chat, or the exact API error (`chat not found`, wrong token, bot
   blocked).

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
| No Telegram messages | Token or chat ids unset/wrong, or no shortlisted hotel has moved | Open **Health → Telegram** and press **Send test message**; it reports each chat's result and the API's own error |

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
| `TEST_MODE` | `0` | Development only: record every check and send a summary after each manual Check now. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |
| `LAN_BIND_IP` | `127.0.0.1` | Compose only: the LAN address to publish on. |
| `DATA_DIR` | `./data` | Compose only: host folder mounted at `/data`. |

`POLL_INTERVAL_SECONDS=60` does not mean a request every minute. The poller asks
"what is due?" and fetches only buckets whose cadence has elapsed: 6 hours for
the Spanish market, 72 hours for comparison markets, and at least 24 hours for
tickets (a slow market's tickets follow its market, so they do not keep polling
daily).

New tracks can include the other EUR markets from the start. Existing tracks
get an **Add comparison markets** panel on the track page; it adds only the
missing markets, leaves existing cadences alone, and the next check fetches them.
The comparison screen itself is the next slice.

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

