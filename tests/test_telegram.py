"""Telegram transport and the notifier shell around it."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app import db
from app.alerts import telegram as telegram_module
from app.alerts.notifier import Notifier
from app.alerts.telegram import (
    Delivery,
    Telegram,
    from_env,
    mask_chat,
    settings_from_env,
)
from app.tracking import health, tracks
from app.tracking.poller import PollResult
from app.tracking.store import Written, utcnow


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


async def test_every_chat_gets_the_message() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = Telegram("token", ["111", "222"], client)
        assert await sender.send("hello") is True

    assert [payload["chat_id"] for payload in seen] == ["111", "222"]
    assert all(payload["text"] == "hello" for payload in seen)


async def test_a_server_error_is_retried(monkeypatch) -> None:
    monkeypatch.setattr(telegram_module, "BACKOFF_SECONDS", (0.0, 0.0))
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, text="nope")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await Telegram("token", ["1"], client).send("x") is True

    assert attempts["n"] == 3


async def test_a_rejected_message_is_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(telegram_module, "BACKOFF_SECONDS", (0.0, 0.0))
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"ok": False, "description": "bad token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await Telegram("token", ["1"], client).send("x") is False

    assert attempts["n"] == 1, "a 4xx will not improve by repeating it"


async def test_delivery_reports_telegrams_own_description(monkeypatch) -> None:
    monkeypatch.setattr(telegram_module, "BACKOFF_SECONDS", (0.0, 0.0))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"ok": False, "description": "Bad Request: chat not found"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await Telegram("token", ["1234"], client).deliver("x")

    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].status == 400
    assert results[0].detail == "Bad Request: chat not found"


def test_chat_ids_are_masked() -> None:
    assert mask_chat("-100123456789") == "…6789"
    assert mask_chat("123") == "…"


def test_settings_report_what_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", " 1, 2 ")
    settings = settings_from_env()

    assert settings.configured is False
    assert settings.chats == ("1", "2")
    assert settings.missing == ("TELEGRAM_BOT_TOKEN",)


async def test_missing_configuration_disables_alerts(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_IDS", raising=False)
    async with httpx.AsyncClient() as client:
        assert from_env(client) is None


async def test_configuration_is_parsed_from_a_comma_list(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", " 1, 2 ,3 ")
    async with httpx.AsyncClient() as client:
        sender = from_env(client)
    assert sender is not None
    assert sender._chats == ["1", "2", "3"]


class FakeTelegram:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.messages: list[str] = []

    async def deliver(self, text: str) -> list[Delivery]:
        self.messages.append(text)
        return [
            Delivery("test", self.ok, 200 if self.ok else 400,
                     None if self.ok else "bad chat")
        ]


def seed_change(conn: sqlite3.Connection) -> PollResult:
    start = date.today() + timedelta(days=40)
    tracks.create_track(
        conn,
        tracks.TrackSpec(
            name="February",
            adults=2,
            arrival_start=start,
            arrival_end=start,
            hotels=(("DCHH", True),),
        ),
    )
    row = conn.execute("SELECT id FROM series LIMIT 1").fetchone()
    series_id = int(row["id"])
    now = datetime.now(timezone.utc)
    for index, price in enumerate((82000, 70000)):
        conn.execute(
            """
            INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
            VALUES (?, ?, 1, ?, ?)
            """,
            (series_id, price, (now - timedelta(hours=2 - index)).isoformat(), "first" if index == 0 else "change"),
        )
    conn.commit()
    written = Written(
        series_id=series_id,
        kind="change",
        arrival=None,
        price=None,
        available=True,
        previous_price=None,
    )
    return PollResult(requests=1, written=[written], changes=[written])


async def test_the_notifier_sends_a_digest_and_records_success(conn: sqlite3.Connection) -> None:
    telegram = FakeTelegram()
    result = seed_change(conn)

    sent = await Notifier(conn, telegram).on_poll(result, now=utcnow())

    assert len(sent) == 1
    assert "February" in sent[0]
    assert telegram.messages == sent
    row = conn.execute("SELECT * FROM health WHERE source = 'telegram'").fetchone()
    assert row["consecutive_failures"] == 0


async def test_a_failed_send_is_recorded_not_raised(conn: sqlite3.Connection) -> None:
    result = seed_change(conn)

    sent = await Notifier(conn, FakeTelegram(ok=False)).on_poll(result, now=utcnow())

    assert sent == []
    row = conn.execute("SELECT * FROM health WHERE source = 'telegram'").fetchone()
    assert row["consecutive_failures"] == 1
    assert "bad chat" in row["last_error"]


async def test_a_poll_with_nothing_to_say_sends_nothing(conn: sqlite3.Connection) -> None:
    telegram = FakeTelegram()
    sent = await Notifier(conn, telegram).on_poll(PollResult(), now=utcnow())
    assert sent == []
    assert telegram.messages == []


def test_a_success_clears_the_previous_telegram_error(
    conn: sqlite3.Connection,
) -> None:
    health.record_failure(conn, "telegram", "chat not found")
    health.record_success(conn, "telegram")

    row = conn.execute("SELECT * FROM health WHERE source = 'telegram'").fetchone()
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is None


async def test_without_telegram_the_notifier_still_works(conn: sqlite3.Connection) -> None:
    result = seed_change(conn)
    notifier = Notifier(conn, None)

    assert notifier.enabled is False
    assert await notifier.on_poll(result, now=utcnow()) == []