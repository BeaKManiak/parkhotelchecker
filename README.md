# parkhotelchecker

A self-hosted price tracker for Disneyland Paris hotel + park-ticket packages.

It watches the 16 Disneyland Paris hotels across an arrival window you choose,
records every price change, charts the history, and sends a Telegram digest when
something is worth knowing. Everything runs on your own machine (built for a
Synology NAS); the only outside calls are to Disneyland Paris for prices and,
if enabled, to Telegram for alerts. Built for personal use by one or two people.

## Features

- **Package price tracking.** Polls the Disneyland Paris package calendar every
  6 hours for the arrival dates you care about, and optionally other EUR markets
  every 72 hours for comparison.
- **Ticket and hotel-only cost.** Prices park tickets separately, so you see the
  package price, the ticket price and the implied hotel-only cost.
- **All 16 hotels** (7 on-site, 9 partner). You choose which ones may alert you.
- **Full history.** Prices are stored as integer cents in their original
  currency and kept forever.
- **Smart Telegram alerts.** One digest per poll, only for a price move above
  your threshold, a new all-time low, or a sold-out / back-available flip.
- **Honest failures.** Alerts you when it cannot see: repeated fetch failures or
  an unrecognised response format.
- **Dashboard.** Tracks, per-hotel price charts (by check-in date or over time),
  market comparison, and a health page. Basic-auth protected, LAN only.
- **Nightly SQLite backups.**

## Getting started

Requires Docker with Compose v2. Telegram is optional; without it, digests are
logged instead of sent.

```sh
git clone git@github.com:BeaKManiak/parkhotelchecker.git && cd parkhotelchecker
cp .env.example .env && chmod 600 .env
# edit .env: DASHBOARD_USER, LAN_BIND_IP, DATA_DIR
sudo mkdir -p <DATA_DIR> && sudo chown -R 1000:1000 <DATA_DIR>   # the path from .env
docker compose build
docker compose run --rm app python -m app.web.auth hash   # paste into .env as DASHBOARD_PASSWORD_HASH
docker compose up -d
```

Open `http://<LAN_BIND_IP>:8080` and sign in. The full Synology walkthrough,
configuration reference, hardening, backups and troubleshooting are in
[docs/deployment.md](docs/deployment.md).

## Usage

1. **Create a track** on the dashboard: party (adults, children and their
   birthdates), check-in window (up to 31 arrival dates), length of stay, the
   hotels to shortlist, and the minimum price change that should alert you
   (default €20).
2. **Watch it.** The first poll starts immediately. The track page shows each
   hotel's cheapest arrival and all-time low; hotel pages show the full history.
3. **Get alerts.** Telegram digests list price moves, new lows and availability
   changes for shortlisted hotels. Use the **Health** page if something looks
   stale.

See [docs/usage.md](docs/usage.md) for alert rules and a dashboard tour.

## Known limits

- If Disneyland changes its response format, the tracker fails loudly rather
  than guessing, and `app/dlp/` will need a fix.
- Single process only; no TLS or multi-user accounts. It is a LAN appliance.
- For personal, non-commercial use. Keep request volume low.

## More documentation

- [docs/deployment.md](docs/deployment.md): install, configure, update, back up
- [docs/usage.md](docs/usage.md): alerts and dashboard
- [docs/development.md](docs/development.md): local run, tests, internals
