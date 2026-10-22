"""Process entrypoint: open the database, start the scheduler, stay alive.

At M3 this is the whole app (no web yet). M4 adds the dashboard alongside the
scheduler in this same process, so there is exactly one thing to run.

Environment:
  DB_PATH                 default data/parkhotelchecker.db
  POLL_INTERVAL_SECONDS   default 60 (how often to *look* for due work)
  LOG_LEVEL               default INFO
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from app import db
from app.dlp.http import create_client
from app.tracking.scheduler import Scheduler


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


async def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("parkhotelchecker")

    db_path = Path(os.environ.get("DB_PATH", "data/parkhotelchecker.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    log.info("database: %s", db_path)

    interval = _env_int("POLL_INTERVAL_SECONDS", _env_int("POLL_INTERVAL", 60))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # not available on all platforms
            pass

    async with create_client() as client:
        scheduler = Scheduler(conn, client, interval_seconds=interval)
        await scheduler.run(stop=stop)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
