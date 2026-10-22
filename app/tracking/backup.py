"""Nightly SQLite snapshots without cron, systemd or a sidecar.

The app is already awake every minute; asking it to write one file per day is
less machinery than adding a scheduler to the NAS. ``VACUUM INTO`` gives a
consistent copy of a live WAL database, so the file is safe to copy off the NAS
while the tracker keeps polling.

Retention: seven daily files, plus Sundays for four weeks. Deleting a backup is
the only destructive thing this project does, and it never touches the database
itself or the change-log.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path

from app import db
from app.tracking.store import utcnow

log = logging.getLogger(__name__)

PREFIX = "parkhotelchecker-"
KEEP_DAILY_DAYS = 7
KEEP_WEEKLY_DAYS = 28
WEEKLY_WEEKDAY = 6  # Sunday


def maybe_backup(
    conn: sqlite3.Connection, directory: str | Path | None, *, today: date | None = None
) -> Path | None:
    """Take today's snapshot if it does not exist yet. Safe to call every tick."""
    if not directory:
        return None
    today = today or utcnow().date()
    directory = Path(directory)
    target = directory / f"{PREFIX}{today.isoformat()}.db"
    if target.exists():
        return None

    directory.mkdir(parents=True, exist_ok=True)
    db.backup(conn, target)
    log.info("backup written: %s", target)
    prune(directory, today=today)
    return target


def prune(directory: str | Path, *, today: date | None = None) -> list[Path]:
    today = today or utcnow().date()
    removed: list[Path] = []

    for path in sorted(Path(directory).glob(f"{PREFIX}*.db")):
        stamp = path.stem[len(PREFIX):]
        try:
            day = date.fromisoformat(stamp)
        except ValueError:
            continue  # not ours; leave it alone
        age = (today - day).days
        keep = age <= KEEP_DAILY_DAYS or (
            day.weekday() == WEEKLY_WEEKDAY and age <= KEEP_WEEKLY_DAYS
        )
        if not keep:
            path.unlink(missing_ok=True)
            removed.append(path)

    return removed
