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
from app.alerts.summary import build_first_checks, build_test_summary
from app.alerts.telegram import Telegram, mask_chat
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

    async def on_poll(
        self,
        result: PollResult,
        now: datetime | None = None,
        *,
        track_id: int | None = None,
        announce_test: bool = False,
    ) -> list[str]:
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

        try:
            messages.extend(build_first_checks(self._conn, now=now))
        except Exception:  # noqa: BLE001
            log.exception("could not build first-check messages")

        if announce_test and track_id is not None:
            try:
                message = build_test_summary(
                    self._conn,
                    track_id,
                    requests=result.requests,
                    changes=len(result.changes),
                    errors=len(result.errors),
                    now=now,
                )
                if message:
                    messages.append(message)
            except Exception:  # noqa: BLE001
                log.exception("could not build test-check message")

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
            deliveries = await self._telegram.deliver(message)
        except Exception:  # noqa: BLE001
            log.exception("telegram send blew up")
            deliveries = []

        ok = bool(deliveries) and all(delivery.ok for delivery in deliveries)
        if ok:
            health.record_success(self._conn, "telegram", now=now)
        else:
            failures = [delivery for delivery in deliveries if not delivery.ok]
            detail = "; ".join(
                f"{mask_chat(delivery.chat_id)}: "
                f"{delivery.detail or ('HTTP ' + str(delivery.status) if delivery.status else 'send failed')}"
                for delivery in failures
            ) or "send failed"
            health.record_failure(self._conn, "telegram", detail, now=now)
            log.error("telegram rejected or dropped an alert:\n%s", message)
        return ok
