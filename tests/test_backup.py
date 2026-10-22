"""Nightly snapshots and their retention."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from app import db
from app.tracking import backup


def test_a_snapshot_is_a_readable_copy(tmp_path) -> None:
    conn = db.connect(tmp_path / "live.db")
    conn.execute(
        "INSERT INTO health (source, last_success_at, consecutive_failures) VALUES ('packages', '2026-01-01T00:00:00+00:00', 0)"
    )
    conn.commit()

    path = backup.maybe_backup(conn, tmp_path / "backups", today=date(2026, 1, 1))

    assert path is not None and path.exists()
    copy = sqlite3.connect(path)
    try:
        assert copy.execute("SELECT COUNT(*) FROM health").fetchone()[0] == 1
    finally:
        copy.close()
        conn.close()


def test_only_one_snapshot_per_day(tmp_path) -> None:
    conn = db.connect(tmp_path / "live.db")

    first = backup.maybe_backup(conn, tmp_path / "backups", today=date(2026, 1, 1))
    second = backup.maybe_backup(conn, tmp_path / "backups", today=date(2026, 1, 1))

    assert first is not None
    assert second is None
    conn.close()


def test_retention_keeps_a_week_daily_plus_four_weeks_of_sundays(tmp_path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    today = date(2026, 1, 31)

    days = [today - timedelta(days=offset) for offset in range(0, 40)]
    for day in days:
        (directory / f"{backup.PREFIX}{day.isoformat()}.db").write_bytes(b"")

    removed = backup.prune(directory, today=today)

    for day in days:
        age = (today - day).days
        keep = age <= backup.KEEP_DAILY_DAYS or (
            day.weekday() == backup.WEEKLY_WEEKDAY and age <= backup.KEEP_WEEKLY_DAYS
        )
        path = directory / f"{backup.PREFIX}{day.isoformat()}.db"
        assert path.exists() is keep, f"{day} age={age} weekday={day.weekday()}"
    assert len(removed) == sum(
        1
        for day in days
        if (today - day).days > backup.KEEP_DAILY_DAYS
        and not (
            day.weekday() == backup.WEEKLY_WEEKDAY
            and (today - day).days <= backup.KEEP_WEEKLY_DAYS
        )
    )


def test_retention_leaves_foreign_files_alone(tmp_path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    stranger = directory / "somebody-elses.db"
    stranger.write_bytes(b"keep me")
    junk = directory / f"{backup.PREFIX}not-a-date.db"
    junk.write_bytes(b"keep me too")

    backup.prune(directory, today=date(2026, 1, 31))

    assert stranger.exists()
    assert junk.exists()


def test_backups_can_be_disabled(tmp_path) -> None:
    conn = db.connect(tmp_path / "live.db")
    assert backup.maybe_backup(conn, None, today=date(2026, 1, 1)) is None
    assert backup.maybe_backup(conn, "", today=date(2026, 1, 1)) is None
    conn.close()