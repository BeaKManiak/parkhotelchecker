"""Process entrypoint: web dashboard and poller in one process.

The scheduler runs in FastAPI's lifespan, so there is exactly one thing to run
and one thing to stop. A crash in a poll is contained inside the scheduler; a
crash in the web layer takes the process down, which is the correct order of
priorities for something that must not silently stop watching prices.

Environment:
  DB_PATH                 default data/parkhotelchecker.db
  HOST                    default 127.0.0.1; 0.0.0.0 is refused (see below)
  PORT                    default 8080
  DASHBOARD_USER          required
  DASHBOARD_PASSWORD_HASH required (python -m app.web.auth hash)
  TELEGRAM_BOT_TOKEN      optional; without it alerts are logged, not sent
  TELEGRAM_CHAT_IDS       optional; comma-separated chat ids
  BACKUP_DIR              default <db folder>/backups; empty string disables
  POLL_INTERVAL_SECONDS   default 60 (how often to *look* for due work)
  ALLOW_PUBLIC_BIND       only for containers; see _dashboard_host
  LOG_LEVEL               default INFO
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn

from app import db
from app.alerts.notifier import Notifier
from app.alerts.telegram import from_env as telegram_from_env
from app.dlp.http import create_client
from app.tracking.scheduler import Scheduler
from app.web.app import create_app


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


def _dashboard_host() -> str:
    host = os.environ.get("HOST", "127.0.0.1").strip()
    if host in {"0.0.0.0", "::", "[::]"} and os.environ.get("ALLOW_PUBLIC_BIND") != "1":
        raise SystemExit(
            "refusing to bind HOST=0.0.0.0: this dashboard is LAN-only.\n"
            "Set HOST to the NAS's LAN address (for example 192.168.1.20).\n"
            "Containers that publish only the LAN IP may set ALLOW_PUBLIC_BIND=1."
        )
    return host


async def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("parkhotelchecker")
    # One line per upstream request is noise; failures are logged where they happen.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db_path = Path(os.environ.get("DB_PATH", "data/parkhotelchecker.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    log.info("database: %s", db_path)

    interval = _env_int("POLL_INTERVAL_SECONDS", _env_int("POLL_INTERVAL", 60))
    host = _dashboard_host()
    port = _env_int("PORT", 8080)

    backup_dir = os.environ.get("BACKUP_DIR", str(db_path.parent / "backups"))
    backup_dir = "" if db_path == Path(":memory:") else backup_dir

    async with create_client() as client:
        telegram = telegram_from_env(client)
        notifier = Notifier(conn, telegram)
        if telegram is None:
            log.warning(
                "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_IDS not set: alerts will be logged, not sent"
            )
        scheduler = Scheduler(
            conn,
            client,
            interval_seconds=interval,
            notifier=notifier,
            backup_dir=backup_dir,
        )
        app = create_app(conn=conn, scheduler=scheduler)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level=os.environ.get("LOG_LEVEL", "info").lower(),
                access_log=False,
                server_header=False,
            )
        )
        log.info("dashboard on http://%s:%d (basic auth required)", host, port)
        log.info("nightly backups: %s", backup_dir or "disabled")
        await server.serve()

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
