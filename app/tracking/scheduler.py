"""The tick loop.

One process, one replica, one asyncio loop. No Celery, no cron, no Redis: the
whole job is "every minute, work out what is due, fetch it, write it down". A
second replica would double-poll and is prevented by deployment (single
container), not by distributed locking.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import traceback
from datetime import datetime, timezone

import httpx

from app.tracking import backup, health, planner
from app.tracking.poller import PollResult, execute

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: httpx.AsyncClient,
        *,
        interval_seconds: float = 60.0,
        concurrency: int = 4,
        min_gap_seconds: float = 0.25,
        notifier=None,
        backup_dir: str | None = None,
        record_all: bool = False,
    ) -> None:
        self._conn = conn
        self._client = client
        self._interval = interval_seconds
        self._concurrency = concurrency
        self._min_gap = min_gap_seconds
        self._notifier = notifier
        self._backup_dir = backup_dir
        self._record_all = record_all

    async def run_once(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        track_id: int | None = None,
        announce_test: bool = False,
    ) -> PollResult:
        now = now or datetime.now(timezone.utc)

        # Once a day, before any fetching: cheap existence check on every tick.
        if self._backup_dir:
            try:
                backup.maybe_backup(self._conn, self._backup_dir, today=now.date())
            except Exception:  # noqa: BLE001 - a backup must not stop polling
                log.exception("backup failed")

        try:
            plan = planner.build_plan(
                self._conn, now=now, force=force, track_id=track_id
            )
        except Exception as exc:  # a planner bug must not kill the loop
            log.exception("planner failed")
            health.record_failure(
                self._conn, "internal", f"planner: {exc}", now=now
            )
            result = PollResult()
            result.errors.append(f"planner: {exc}")
            # A planner that crashes on every tick would otherwise be a tracker
            # that is blind and silent. This is exactly the failure the alerting
            # layer exists for.
            await self._notify(result, now)
            return result

        if plan.is_empty:
            return PollResult()

        try:
            result = await execute(
                self._conn,
                plan,
                client=self._client,
                now=now,
                concurrency=self._concurrency,
                min_gap_seconds=self._min_gap,
                record_all=self._record_all,
            )
        except Exception as exc:  # e.g. an unexpected exception inside a task
            log.error("poll crashed: %s\n%s", exc, traceback.format_exc())
            health.record_failure(self._conn, "internal", f"poll: {exc}", now=now)
            result = PollResult()
            result.errors.append(f"poll: {exc}")
            await self._notify(result, now)
            return result

        await self._notify(
            result, now, track_id=track_id, announce_test=announce_test
        )
        return result

    async def _notify(
        self,
        result: PollResult,
        now: datetime,
        *,
        track_id: int | None = None,
        announce_test: bool = False,
    ) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.on_poll(
                result,
                now=now,
                track_id=track_id,
                announce_test=announce_test,
            )
        except Exception:  # noqa: BLE001 - alerting must not stop the loop
            log.exception("notifier failed")

    async def run(self, *, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        log.info("scheduler started (interval %.0fs)", self._interval)
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                # run_once already contains its own failures; this is last resort.
                log.exception("unhandled error in tick")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue
        log.info("scheduler stopped")
