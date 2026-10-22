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

from app.tracking import health, planner
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
    ) -> None:
        self._conn = conn
        self._client = client
        self._interval = interval_seconds
        self._concurrency = concurrency
        self._min_gap = min_gap_seconds

    async def run_once(self, *, now: datetime | None = None) -> PollResult:
        now = now or datetime.now(timezone.utc)
        try:
            plan = planner.build_plan(self._conn, now=now)
        except Exception as exc:  # a planner bug must not kill the loop
            log.exception("planner failed")
            health.record_failure(
                self._conn, "internal", f"planner: {exc}", now=now
            )
            result = PollResult()
            result.errors.append(f"planner: {exc}")
            return result

        if plan.is_empty:
            return PollResult()

        try:
            return await execute(
                self._conn,
                plan,
                client=self._client,
                now=now,
                concurrency=self._concurrency,
                min_gap_seconds=self._min_gap,
            )
        except Exception as exc:  # e.g. an unexpected exception inside a task
            log.error("poll crashed: %s\n%s", exc, traceback.format_exc())
            health.record_failure(self._conn, "internal", f"poll: {exc}", now=now)
            result = PollResult()
            result.errors.append(f"poll: {exc}")
            return result

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
