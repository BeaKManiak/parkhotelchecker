"""The scheduler's promise: a crash is contained, and it is never silent."""

from __future__ import annotations

import sqlite3

import pytest

from app import db
from app.alerts.notifier import Notifier
from app.tracking import planner
from app.tracking.scheduler import Scheduler


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


async def test_a_crashing_planner_raises_the_alarm(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(planner, "build_plan", boom)
    telegram = FakeTelegram()
    scheduler = Scheduler(conn, client=None, notifier=Notifier(conn, telegram))

    for _ in range(3):
        await scheduler.run_once()

    assert len(telegram.messages) == 1
    assert "poller failing" in telegram.messages[0]
    assert "3 consecutive failures" in telegram.messages[0]


async def test_a_healthy_empty_tick_sends_nothing(conn: sqlite3.Connection) -> None:
    telegram = FakeTelegram()
    scheduler = Scheduler(conn, client=None, notifier=Notifier(conn, telegram))

    result = await scheduler.run_once()

    assert result.requests == 0
    assert telegram.messages == []
