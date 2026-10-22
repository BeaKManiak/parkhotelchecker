"""SQLite access and schema migration.

Design notes:

- WAL, so a dashboard read never blocks a poll write.
- Money is stored as **integer cents**; floats never touch the database.
- Timestamps are ISO-8601 strings in UTC. SQLite has no timezone type, and
  "what timezone was this written in" is not a question worth having.
- ``PRAGMA user_version`` carries the schema version. One migration list, no
  framework. If this ever needs more than a list, that is a signal the project
  outgrew SQLite, not a signal to add a migration library.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_V1: tuple[str, ...] = (
    """
    CREATE TABLE party (
      id INTEGER PRIMARY KEY,
      party_key TEXT NOT NULL UNIQUE,
      adults INTEGER NOT NULL,
      child_birthdates TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE series (
      id INTEGER PRIMARY KEY,
      hotel_code TEXT NOT NULL,
      market TEXT NOT NULL,
      currency TEXT NOT NULL,
      arrival_date TEXT NOT NULL,
      los TEXT NOT NULL,
      party_id INTEGER NOT NULL REFERENCES party(id),
      UNIQUE (hotel_code, market, arrival_date, los, party_id)
    )
    """,
    """
    CREATE TABLE track (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('active','stopped')) DEFAULT 'active',
      party_id INTEGER NOT NULL REFERENCES party(id),
      los TEXT NOT NULL,
      arrival_start TEXT NOT NULL,
      arrival_end TEXT NOT NULL,
      created_at TEXT NOT NULL,
      stopped_at TEXT,
      min_delta_cents INTEGER NOT NULL DEFAULT 2000
    )
    """,
    """
    CREATE TABLE track_hotel (
      track_id INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
      hotel_code TEXT NOT NULL,
      shortlisted INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (track_id, hotel_code)
    )
    """,
    """
    CREATE TABLE track_market (
      track_id INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
      market TEXT NOT NULL,
      cadence_hours INTEGER NOT NULL,
      PRIMARY KEY (track_id, market)
    )
    """,
    """
    CREATE TABLE track_series (
      track_id INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
      series_id INTEGER NOT NULL REFERENCES series(id),
      watched INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY (track_id, series_id)
    )
    """,
    """
    CREATE TABLE observation (
      id INTEGER PRIMARY KEY,
      series_id INTEGER NOT NULL REFERENCES series(id),
      price_cents INTEGER,
      available INTEGER NOT NULL,
      observed_at TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('change','heartbeat','first'))
    )
    """,
    "CREATE INDEX ix_obs_series_time ON observation(series_id, observed_at)",
    """
    CREATE TABLE ticket_price (
      id INTEGER PRIMARY KEY,
      market TEXT NOT NULL,
      currency TEXT NOT NULL,
      visit_start TEXT NOT NULL,
      duration_days INTEGER NOT NULL,
      parks INTEGER NOT NULL,
      product_type TEXT NOT NULL,
      party_id INTEGER NOT NULL REFERENCES party(id),
      adult_cents INTEGER,
      child_cents INTEGER,
      total_cents INTEGER,
      available INTEGER NOT NULL,
      observed_at TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('change','heartbeat','first'))
    )
    """,
    "CREATE INDEX ix_ticket_lookup ON ticket_price(market, visit_start, duration_days, parks, party_id)",
    """
    CREATE TABLE plu_product (
      market TEXT NOT NULL,
      duration_days INTEGER NOT NULL,
      parks INTEGER NOT NULL,
      product_type TEXT NOT NULL,
      adult_code TEXT NOT NULL,
      child_code TEXT NOT NULL,
      travel_from TEXT,
      travel_to TEXT,
      discovered_at TEXT NOT NULL,
      PRIMARY KEY (market, duration_days, parks)
    )
    """,
    """
    CREATE TABLE fetch_log (
      id INTEGER PRIMARY KEY,
      source TEXT NOT NULL,
      key TEXT NOT NULL,
      requested_at TEXT NOT NULL,
      ok INTEGER NOT NULL,
      status INTEGER,
      error TEXT,
      bytes INTEGER
    )
    """,
    """
    CREATE TABLE health (
      source TEXT PRIMARY KEY,
      last_success_at TEXT,
      last_error_at TEXT,
      last_error TEXT,
      consecutive_failures INTEGER NOT NULL DEFAULT 0
    )
    """,
)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and migrate) a database. `:memory:` is allowed for tests."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    if version < 1:
        for statement in SCHEMA_V1:
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def backup(conn: sqlite3.Connection, destination: str | Path) -> None:
    """Consistent snapshot of a live WAL database."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    conn.execute("VACUUM INTO ?", (str(destination),))


def table_names(conn: sqlite3.Connection) -> Iterable[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return (row["name"] for row in rows)
