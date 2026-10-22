"""Notification bookkeeping.

``alert_state`` is a key/value shelf, not a domain table. It answers "have we
already said this?", so a failing source does not message every minute and a
recovered one gets exactly one all-clear.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.tracking.store import iso_utc, utcnow


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM alert_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set(conn: sqlite3.Connection, key: str, value: str, *, now: datetime | None = None) -> None:
    conn.execute(
        """
        INSERT INTO alert_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, iso_utc(now or utcnow())),
    )
    conn.commit()


def clear(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM alert_state WHERE key = ?", (key,))
    conn.commit()


def updated_at(conn: sqlite3.Connection, key: str) -> datetime | None:
    row = conn.execute("SELECT updated_at FROM alert_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row["updated_at"])
