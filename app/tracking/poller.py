"""Executing a plan: the only place network I/O meets the change-log.

Every request is logged to ``fetch_log`` (so the planner knows when a bucket
was last attempted, success or failure), and every outcome moves ``health``
(so the dashboard can say "last successful check: N hours ago").
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

from app import config
from app.dlp import packages, tickets
from app.dlp.http import ShapeError, UpstreamError, create_client
from app.tracking import health, store
from app.tracking.planner import PackageTask, Plan, TicketTask
from app.tracking.store import Written, iso_utc

log = logging.getLogger(__name__)

PackageFetcher = Callable[[httpx.AsyncClient, PackageTask], Awaitable[packages.PackageCalendar]]
TicketFetcher = Callable[
    [httpx.AsyncClient, TicketTask, tickets.ProductCode], Awaitable[tickets.TicketCalendar]
]


@dataclass
class PollResult:
    requests: int = 0
    packages_ok: int = 0
    packages_failed: int = 0
    tickets_ok: int = 0
    tickets_failed: int = 0
    written: list[Written] = field(default_factory=list)
    changes: list[Written] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # A shape error means the upstream answered something we do not understand.
    # That is a different failure from a timeout and is alerted on immediately,
    # not after three consecutive polls.
    shape_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


async def fetch_package(client: httpx.AsyncClient, task: PackageTask) -> packages.PackageCalendar:
    return await packages.fetch(
        client,
        hotel=task.hotel,
        market=task.market,
        los=task.los,
        adults=task.adults,
        child_birthdates=task.child_birthdates,
        month=task.month,
    )


async def fetch_ticket(
    client: httpx.AsyncClient, task: TicketTask, product: tickets.ProductCode
) -> tickets.TicketCalendar:
    return await tickets.fetch_calendar(
        client,
        market=task.market,
        start=task.month,
        end=_month_end(task.month),
        product=product,
        adults=task.adults,
        children=task.children,
    )


def _month_end(month: date) -> date:
    return (month.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


class _Gate:
    """Serialises request starts so we never burst, even with concurrency > 1."""

    def __init__(self, min_gap_seconds: float) -> None:
        self._gap = min_gap_seconds
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._gap - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


async def execute(
    conn: sqlite3.Connection,
    plan: Plan,
    *,
    client: httpx.AsyncClient,
    now: datetime | None = None,
    concurrency: int = 4,
    min_gap_seconds: float = 0.25,
    package_fetcher: PackageFetcher = fetch_package,
    ticket_fetcher: TicketFetcher = fetch_ticket,
) -> PollResult:
    now = now or datetime.now(timezone.utc)
    result = PollResult()
    if plan.is_empty:
        return result

    semaphore = asyncio.Semaphore(concurrency)
    gate = _Gate(min_gap_seconds)

    products = await _ensure_products(conn, client, plan, now=now, result=result)
    if not products:
        # Discovery failed; package prices are unaffected, so carry on.
        plan = Plan(packages=plan.packages, tickets=())

    async def run_package(task: PackageTask) -> None:
        async with semaphore:
            await gate.wait()
            result.requests += 1
            try:
                calendar = await package_fetcher(client, task)
            except (UpstreamError, ShapeError) as exc:
                _record_failure(conn, "packages", task.key, exc, now)
                result.packages_failed += 1
                result.errors.append(f"{task.key}: {exc}")
                if isinstance(exc, ShapeError):
                    result.shape_errors.append(f"packages {task.key}: {exc}")
                return

            written = store.record_package_calendar(
                conn, calendar, party_id=task.party_id, now=now
            )
            _record_success(conn, "packages", task.key, now)
            result.packages_ok += 1
            result.written.extend(written)
            # Only genuine movements alert. "first" rows are the baseline: a new
            # track would otherwise fire one message per series on its first poll.
            result.changes.extend(w for w in written if w.kind == "change")

    async def run_ticket(task: TicketTask) -> None:
        product = products.get(task.market)
        if product is None:
            return
        async with semaphore:
            await gate.wait()
            result.requests += 1
            try:
                calendar = await ticket_fetcher(client, task, product)
            except (UpstreamError, ShapeError) as exc:
                _record_failure(conn, "tickets", task.key, exc, now)
                result.tickets_failed += 1
                result.errors.append(f"{task.key}: {exc}")
                if isinstance(exc, ShapeError):
                    result.shape_errors.append(f"tickets {task.key}: {exc}")
                return

            written = store.record_ticket_calendar(
                conn,
                calendar,
                adults=task.adults,
                children=task.children,
                party_id=task.party_id,
                now=now,
            )
            _record_success(conn, "tickets", task.key, now)
            result.tickets_ok += 1
            result.written.extend(written)
            # Only genuine movements alert. "first" rows are the baseline: a new
            # track would otherwise fire one message per series on its first poll.
            result.changes.extend(w for w in written if w.kind == "change")

    await asyncio.gather(
        *(run_package(task) for task in plan.packages),
        *(run_ticket(task) for task in plan.tickets),
    )

    log.info(
        "poll done: %s requests, %s package ok / %s failed, %s ticket ok / %s failed, %s changes",
        result.requests,
        result.packages_ok,
        result.packages_failed,
        result.tickets_ok,
        result.tickets_failed,
        len(result.changes),
    )
    return result


async def _ensure_products(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    plan: Plan,
    *,
    now: datetime,
    result: PollResult,
) -> dict[str, tickets.ProductCode]:
    """Resolve the seasonal product code per market, discovering if needed."""
    if not plan.tickets:
        return {}

    needed = {task.market: task.duration_days for task in plan.tickets}
    products: dict[str, tickets.ProductCode] = {}

    for market, duration in needed.items():
        row = conn.execute(
            "SELECT * FROM plu_product WHERE market = ? AND duration_days = ? AND parks = ?",
            (market, duration, config.PARKS),
        ).fetchone()
        if row:
            products[market] = tickets.ProductCode(
                product_type=row["product_type"],
                adult_code=row["adult_code"],
                child_code=row["child_code"],
                duration_days=int(row["duration_days"]),
                parks=int(row["parks"]),
                travel_from=date.fromisoformat(row["travel_from"]) if row["travel_from"] else None,
                travel_to=date.fromisoformat(row["travel_to"]) if row["travel_to"] else None,
            )
            continue

        try:
            # The category endpoint only answers a browser-like UA, and can take
            # close to a minute. A short-lived slow client keeps that timeout
            # from leaking into every price request.
            async with create_client(slow=True) as discovery_client:
                discovered = await tickets.discover_product_codes(
                    discovery_client, market=market, unique_id=str(uuid.uuid4())
                )
            chosen = tickets.select_product(discovered, duration_days=duration)
        except (UpstreamError, ShapeError) as exc:
            _record_failure(conn, "plu", market, exc, now)
            result.errors.append(f"plu {market}: {exc}")
            if isinstance(exc, ShapeError):
                result.shape_errors.append(f"plu {market}: {exc}")
            continue

        _store_products(conn, market, discovered, now)
        _record_success(conn, "plu", market, now)
        products[market] = chosen

    return products


def _store_products(
    conn: sqlite3.Connection,
    market: str,
    discovered: tuple[tickets.ProductCode, ...],
    now: datetime,
) -> None:
    for product in discovered:
        conn.execute(
            """
            INSERT INTO plu_product (
              market, duration_days, parks, product_type, adult_code, child_code,
              travel_from, travel_to, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (market, duration_days, parks) DO UPDATE SET
              product_type = excluded.product_type,
              adult_code = excluded.adult_code,
              child_code = excluded.child_code,
              travel_from = excluded.travel_from,
              travel_to = excluded.travel_to,
              discovered_at = excluded.discovered_at
            """,
            (
                market,
                product.duration_days,
                product.parks,
                product.product_type,
                product.adult_code,
                product.child_code,
                product.travel_from.isoformat() if product.travel_from else None,
                product.travel_to.isoformat() if product.travel_to else None,
                iso_utc(now),
            ),
        )
    conn.commit()


def _record_success(conn: sqlite3.Connection, source: str, key: str, now: datetime) -> None:
    conn.execute(
        """
        INSERT INTO fetch_log (source, key, requested_at, ok, status, error, bytes)
        VALUES (?, ?, ?, 1, NULL, NULL, NULL)
        """,
        (source, key, iso_utc(now)),
    )
    conn.commit()
    health.record_success(conn, source, now=now)


def _record_failure(
    conn: sqlite3.Connection, source: str, key: str, exc: Exception, now: datetime
) -> None:
    conn.execute(
        """
        INSERT INTO fetch_log (source, key, requested_at, ok, status, error, bytes)
        VALUES (?, ?, ?, 0, ?, ?, NULL)
        """,
        (source, key, iso_utc(now), getattr(exc, "status", None), str(exc)[:500]),
    )
    conn.commit()
    health.record_failure(conn, source, str(exc), now=now)
    log.warning("%s failed for %s: %s", source, key, exc)


def default_client() -> httpx.AsyncClient:
    return create_client()
