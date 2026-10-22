"""The notifier that the scheduler calls after every poll.

Kept deliberately thin: it composes messages (digest + failures), sends them,
and records whether Telegram itself worked. It never raises into the poller -
losing a message is bad, but a notification bug must not stop the tracker from
recording prices.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from app.alerts import failures
from app.alerts.digest import build_digest
from app.alerts.telegram import Telegram
from app.tracking import health
from app.tracking.poller import PollResult
from app.tracking.store import utcnow

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, conn: sqlite3.Connection, telegram: Telegram | None) -> None:
        self._conn = conn
        self._telegram = telegram

    @property
    def enabled(self) -> bool:
        return self._telegram is not None

    async def on_poll(self, result: PollResult, now: datetime | None = None) -> list[str]:
        """Send at most a couple of messages per poll. Returns what was sent."""
        now = now or utcnow()
        messages: list[str] = []

        try:
            messages.extend(failures.build_failure_alerts(self._conn, result, now=now))
        except Exception:  # noqa: BLE001 - never break the poll over alerting
            log.exception("could not build failure alerts")

        try:
            digest = build_digest(self._conn, result, now=now)
            if digest:
                messages.append(digest)
        except Exception:  # noqa: BLE001
            log.exception("could not build digest")

        sent: list[str] = []
        for message in messages:
            if await self.send(message, now=now):
                sent.append(message)
        return sent

    async def send(self, message: str, *, now: datetime | None = None) -> bool:
        now = now or utcnow()
        if self._telegram is None:
            # Visible in `docker logs`, and useful while setting the thing up.
            log.warning("Telegram is not configured; alert not sent:\n%s", message)
            return False

        try:
            ok = await self._telegram.send(message)
        except Exception:  # noqa: BLE001
            log.exception("telegram send blew up")
            ok = False

        if ok:
            health.record_success(self._conn, "telegram", now=now)
        else:
            health.record_failure(self._conn, "telegram", "send failed", now=now)
            log.error("telegram rejected or dropped an alert:\n%s", message)
        return ok
