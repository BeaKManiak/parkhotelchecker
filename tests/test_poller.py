"""Executing a plan without touching the network.

The fetchers are injected, so these tests exercise the parts that actually
matter: change-log integration, fetch_log, health, and the promise that one
failing hotel does not stop the others.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import db
from app.dlp.http import ShapeError, UpstreamError
from app.dlp.packages import PackageCalendar, PackageDay
from app.dlp.tickets import TicketCalendar, TicketDay
from app.tracking import planner, poller, store, tracks
from app.tracking.planner import PackageTask, Plan, TicketTask


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def month_start(offset_days: int = 40) -> date:
    return (date.today() + timedelta(days=offset_days)).replace(day=1)


def make_track(conn: sqlite3.Connection, *, hotels=(("DCHH", True),), start_day=None, days=2) -> int:
    start_day = start_day or (date.today() + timedelta(days=40))
    return tracks.create_track(
        conn,
        tracks.TrackSpec(
            name="t",
            adults=2,
            arrival_start=start_day,
            arrival_end=start_day + timedelta(days=days - 1),
            hotels=hotels,
        ),
    )


def package_plan(conn):
    """Packages only: ticket discovery needs a real client, tested separately."""
    plan = planner.build_plan(conn)
    return Plan(packages=plan.packages, tickets=())


def calendar_for(task: PackageTask, price: Decimal = Decimal("600.00")) -> PackageCalendar:
    day = task.month
    return PackageCalendar(
        hotel=task.hotel,
        market=task.market,
        los=task.los,
        currency="EUR",
        month=task.month,
        days={
            day: PackageDay(day, price, "HD", "2", True),
            day + timedelta(days=1): PackageDay(day + timedelta(days=1), price, "HD", "2", True),
        },
        rooms=(),
        errors=(),
    )


async def ok_fetcher(client, task: PackageTask) -> PackageCalendar:
    return calendar_for(task)


async def failing_fetcher(client, task: PackageTask) -> PackageCalendar:
    raise ShapeError(f"unexpected payload for {task.hotel}")


async def test_a_successful_poll_writes_observations_and_health(conn: sqlite3.Connection) -> None:
    make_track(conn)
    plan = package_plan(conn)
    now = datetime.now(timezone.utc)

    result = await poller.execute(
        conn, plan, client=None, now=now, min_gap_seconds=0, package_fetcher=ok_fetcher
    )

    assert result.requests == len(plan.packages)
    assert result.packages_ok == len(plan.packages)
    assert result.errors == []
    assert result.changes == [], "the first poll is a baseline, not 30 alerts"
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] > 0
    assert conn.execute("SELECT COUNT(*) AS n FROM fetch_log WHERE ok = 1").fetchone()["n"] == len(
        plan.packages
    )
    health_row = conn.execute("SELECT * FROM health WHERE source = 'packages'").fetchone()
    assert health_row["consecutive_failures"] == 0
    assert health_row["last_success_at"] is not None


async def test_a_shape_error_marks_health_and_writes_nothing(conn: sqlite3.Connection) -> None:
    make_track(conn)
    plan = package_plan(conn)
    now = datetime.now(timezone.utc)

    result = await poller.execute(
        conn, plan, client=None, now=now, min_gap_seconds=0, package_fetcher=failing_fetcher
    )

    assert result.packages_failed == len(plan.packages)
    assert result.errors
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == 0
    failed = conn.execute("SELECT * FROM fetch_log WHERE ok = 0").fetchall()
    assert len(failed) == len(plan.packages)
    health_row = conn.execute("SELECT * FROM health WHERE source = 'packages'").fetchone()
    assert health_row["consecutive_failures"] == len(plan.packages)
    assert "unexpected payload" in health_row["last_error"]


async def test_one_failing_hotel_does_not_stop_the_others(conn: sqlite3.Connection) -> None:
    make_track(conn, hotels=(("DCHH", True), ("DSFH", True)))
    plan = package_plan(conn)
    now = datetime.now(timezone.utc)

    async def picky(client, task: PackageTask) -> PackageCalendar:
        if task.hotel == "DCHH":
            raise ShapeError("boom")
        return calendar_for(task)

    result = await poller.execute(
        conn, plan, client=None, now=now, min_gap_seconds=0, package_fetcher=picky
    )

    assert result.packages_failed == 1
    assert result.packages_ok == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] > 0


async def test_an_upstream_error_is_recorded_with_its_status(conn: sqlite3.Connection) -> None:
    make_track(conn)
    plan = package_plan(conn)
    now = datetime.now(timezone.utc)

    async def unavailable(client, task: PackageTask) -> PackageCalendar:
        raise UpstreamError("HTTP 503", status=503, url="https://example.invalid")

    await poller.execute(
        conn, plan, client=None, now=now, min_gap_seconds=0, package_fetcher=unavailable
    )

    row = conn.execute("SELECT * FROM fetch_log WHERE ok = 0").fetchone()
    assert row["status"] == 503


async def test_a_second_poll_reports_movements_but_not_the_baseline(
    conn: sqlite3.Connection,
) -> None:
    make_track(conn)
    plan = package_plan(conn)
    now = datetime.now(timezone.utc)

    await poller.execute(
        conn, plan, client=None, now=now, min_gap_seconds=0, package_fetcher=ok_fetcher
    )

    async def cheaper(client, task: PackageTask) -> PackageCalendar:
        return calendar_for(task, Decimal("550.00"))

    later = now + timedelta(hours=7)  # past the 6h cadence
    moved = await poller.execute(
        conn, plan, client=None, now=later, min_gap_seconds=0, package_fetcher=cheaper
    )

    assert moved.changes, "a real price move must be reported"
    assert {c.kind for c in moved.changes} == {"change"}
    assert all(c.price == Decimal("550.00") for c in moved.changes)


async def test_an_empty_plan_makes_no_requests(conn: sqlite3.Connection) -> None:
    result = await poller.execute(conn, Plan((), ()), client=None, min_gap_seconds=0)
    assert result.requests == 0
    assert result.ok


async def test_ticket_prices_are_recorded_using_stored_product_codes(
    conn: sqlite3.Connection,
) -> None:
    make_track(conn)
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO plu_product (market, duration_days, parks, product_type, adult_code,
                                 child_code, discovered_at)
        VALUES ('es-es', 3, 2, 'TKITL34NP', 'TKITHS003A', 'TKITHS003C', ?)
        """,
        (store.iso_utc(now),),
    )
    conn.commit()

    task = TicketTask(
        market="es-es", month=month_start(), duration_days=3, party_id=1, adults=2, children=0
    )

    async def ticket_fetcher(client, task, product):
        assert product.product_type == "TKITL34NP"
        day = task.month
        return TicketCalendar(
            market="es-es",
            currency="EUR",
            product_type=product.product_type,
            duration_days=3,
            parks=2,
            days={day: TicketDay(day, Decimal("280.00"), Decimal("265.00"), True)},
        )

    result = await poller.execute(
        conn,
        Plan((), (task,)),
        client=None,
        now=now,
        min_gap_seconds=0,
        ticket_fetcher=ticket_fetcher,
    )

    assert result.tickets_ok == 1
    row = conn.execute("SELECT * FROM ticket_price").fetchone()
    assert row["total_cents"] == 56000  # 2 adults, no children


async def test_missing_product_codes_block_tickets_not_packages(
    conn: sqlite3.Connection,
) -> None:
    make_track(conn)
    plan = planner.build_plan(conn)
    now = datetime.now(timezone.utc)

    async def discovery_fails(client, *, market, unique_id):
        raise ShapeError("no products")

    original = poller.tickets.discover_product_codes
    poller.tickets.discover_product_codes = discovery_fails
    try:
        result = await poller.execute(
            conn,
            plan,
            client=None,
            now=now,
            min_gap_seconds=0,
            package_fetcher=ok_fetcher,
        )
    finally:
        poller.tickets.discover_product_codes = original

    assert result.packages_ok == len(plan.packages)
    assert any("plu" in error for error in result.errors)
    health_row = conn.execute("SELECT * FROM health WHERE source = 'plu'").fetchone()
    assert health_row["consecutive_failures"] >= 1