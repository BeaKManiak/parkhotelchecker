"""Schema creation and the v1 → v2 migration."""

from __future__ import annotations

import sqlite3

from app import db


def test_a_fresh_database_is_at_the_current_version() -> None:
    conn = db.connect(":memory:")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION == 3
        assert "alert_state" in set(db.table_names(conn))
    finally:
        conn.close()


def test_a_v1_database_gains_the_alert_state_table(tmp_path) -> None:
    path = tmp_path / "old.db"

    # Build a database exactly as v1 shipped, without using db.connect (which
    # would migrate it immediately).
    raw = sqlite3.connect(path)
    for statement in db.SCHEMA_V1:
        raw.execute(statement)
    raw.execute("PRAGMA user_version=1")
    raw.execute(
        "INSERT INTO party (party_key, adults, child_birthdates) VALUES ('k', 2, '[]')"
    )
    raw.commit()
    raw.close()

    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "alert_state" in set(db.table_names(conn))
        # the v1 data is still there
        assert conn.execute("SELECT COUNT(*) FROM party").fetchone()[0] == 1
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path) -> None:
    path = tmp_path / "db.sqlite"
    for _ in range(3):
        conn = db.connect(path)
        conn.close()
    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        conn.close()

def test_the_fetch_log_has_an_index_for_the_planner_scan() -> None:
    conn = db.connect(":memory:")
    try:
        names = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(fetch_log)").fetchall()
        }
        assert "ix_fetch_log_source_key" in names
    finally:
        conn.close()


def test_a_v2_database_gains_the_fetch_log_index(tmp_path) -> None:
    path = tmp_path / "v2.db"
    conn = db.connect(path)
    conn.execute("DROP INDEX ix_fetch_log_source_key")
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    try:
        names = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(fetch_log)").fetchall()
        }
        assert "ix_fetch_log_source_key" in names
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        conn.close()
