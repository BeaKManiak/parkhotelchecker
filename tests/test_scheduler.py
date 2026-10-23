"""The scheduler's promise: a crash is contained, and it is never silent."""

from __future__ import annotations

import sqlite3

import pytest

from app import db
from app.alerts.notifier import Notifier
from app.alerts.telegram import Delivery
from app.tracking import planner
from app.tracking.poller import PollResult
from app.tracking.scheduler import Scheduler


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def deliver(self, text: str) -> list[Delivery]:
        self.messages.append(text)
        return [Delivery("test", True, 200)]


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


async def test_a_forced_tick_asks_the_planner_to_ignore_cadence(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    seen = {}
    real = planner.build_plan

    def spy(connection, *, now=None, force=False, track_id=None):
        seen["force"] = force
        return real(connection, now=now, track_id=track_id)

    monkeypatch.setattr(planner, "build_plan", spy)
    scheduler = Scheduler(conn, client=None)

    await scheduler.run_once(force=True)

    assert seen["force"] is True


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, bool]] = []

    async def on_poll(
        self, result, now=None, *, track_id=None, announce_test=False
    ) -> None:
        self.calls.append((track_id, announce_test))


async def test_a_manual_check_scope_reaches_planner_and_notifier(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    seen = {}

    class NonEmptyPlan:
        is_empty = False

    def spy(connection, *, now=None, force=False, track_id=None):
        seen.update(force=force, track_id=track_id)
        return NonEmptyPlan()

    async def fake_execute(*args, **kwargs):
        return PollResult()

    monkeypatch.setattr(planner, "build_plan", spy)
    monkeypatch.setattr("app.tracking.scheduler.execute", fake_execute)
    notifier = FakeNotifier()
    scheduler = Scheduler(conn, client=None, notifier=notifier)

    await scheduler.run_once(force=True, track_id=7, announce_test=True)

    assert seen == {"force": True, "track_id": 7}
    assert notifier.calls == [(7, True)]


async def test_test_mode_reaches_the_poller(conn: sqlite3.Connection, monkeypatch) -> None:
    seen = {}

    async def fake_execute(*args, **kwargs):
        seen["record_all"] = kwargs.get("record_all")
        return PollResult()

    class NonEmptyPlan:
        is_empty = False

    monkeypatch.setattr(planner, "build_plan", lambda *a, **k: NonEmptyPlan())
    monkeypatch.setattr("app.tracking.scheduler.execute", fake_execute)
    scheduler = Scheduler(conn, client=None, record_all=True)

    await scheduler.run_once()

    assert seen["record_all"] is True
