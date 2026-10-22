"""Health bookkeeping: 'last successful check' and consecutive failures.

Deliberately dumb here. The threshold logic that decides when a failure is
worth a Telegram message lives in the alerting layer (M5); this module only
records what happened.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.tracking.store import iso_utc, utcnow


def record_success(
    conn: sqlite3.Connection, source: str, *, now: datetime | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO health (source, last_success_at, last_error_at, last_error, consecutive_failures)
        VALUES (?, ?, NULL, NULL, 0)
        ON CONFLICT (source) DO UPDATE SET
          last_success_at = excluded.last_success_at,
          consecutive_failures = 0
        """,
        (source, iso_utc(now or utcnow())),
    )
    conn.commit()


def record_failure(
    conn: sqlite3.Connection,
    source: str,
    error: str,
    *,
    now: datetime | None = None,
) -> int:
    """Record a failure and return the new consecutive-failure count."""
    moment = now or utcnow()
    conn.execute(
        """
        INSERT INTO health (source, last_success_at, last_error_at, last_error, consecutive_failures)
        VALUES (?, NULL, ?, ?, 1)
        ON CONFLICT (source) DO UPDATE SET
          last_error_at = excluded.last_error_at,
          last_error = excluded.last_error,
          consecutive_failures = health.consecutive_failures + 1
        """,
        (source, iso_utc(moment), error[:500]),
    )
    conn.commit()
    row = conn.execute(
        "SELECT consecutive_failures FROM health WHERE source = ?", (source,)
    ).fetchone()
    return int(row["consecutive_failures"])


def snapshot(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM health ORDER BY source"
    ).fetchall()
