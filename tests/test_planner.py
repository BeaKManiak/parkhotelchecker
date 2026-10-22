"""Planner behaviour: what is due, and how much of it coalesces."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db
from app.tracking import planner, tracks


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def start(offset_days: int = 40) -> date:
    return date.today() + timedelta(days=offset_days)


def make_track(
    conn: sqlite3.Connection,
    *,
    name: str = "t",
    start_day: date | None = None,
    days: int = 2,
    hotels: tuple[tuple[str, bool], ...] = (("DCHH", True),),
    markets: tuple[tuple[str, int], ...] = (("es-es", 6),),
    child_birthdates: tuple[str, ...] = (),
) -> int:
    start_day = start_day or start()
    return tracks.create_track(
        conn,
        tracks.TrackSpec(
            name=name,
            adults=2,
            child_birthdates=child_birthdates,
            arrival_start=start_day,
            arrival_end=start_day + timedelta(days=days - 1),
            hotels=hotels,
            markets=markets,
        ),
    )


def test_one_hotel_month_is_one_request(conn: sqlite3.Connection) -> None:
    make_track(conn, days=5)
    plan = planner.build_plan(conn)

    assert len(plan.packages) == 1
    assert plan.packages[0].hotel == "DCHH"
    assert plan.packages[0].month == start().replace(day=1)


def test_overlapping_tracks_share_one_fetch(conn: sqlite3.Connection) -> None:
    make_track(conn, name="hers", days=5)
    make_track(conn, name="his", days=5)

    plan = planner.build_plan(conn)

    assert len(plan.packages) == 1, "two tracks watching the same thing must coalesce"


def test_different_parties_are_different_requests(conn: sqlite3.Connection) -> None:
    """A request prices exactly one party, so this is not coalescable."""
    make_track(conn, name="couple")
    make_track(conn, name="family", child_birthdates=("2020-01-01",))

    plan = planner.build_plan(conn)

    assert len(plan.packages) == 2
    assert len({task.party_id for task in plan.packages}) == 2


def test_different_hotels_are_different_requests(conn: sqlite3.Connection) -> None:
    make_track(conn, hotels=(("DCHH", True), ("DSFH", True)))
    plan = planner.build_plan(conn)
    assert {task.hotel for task in plan.packages} == {"DCHH", "DSFH"}


def test_cadence_is_the_shortest_of_the_watching_tracks(conn: sqlite3.Connection) -> None:
    make_track(conn, name="fast", markets=(("es-es", 6),))
    make_track(conn, name="slow", markets=(("es-es", 24),))

    first = datetime.now(timezone.utc)
    assert len(planner.build_plan(conn, now=first).packages) == 1

    # 7 hours later the 6h cadence (plus at most 15 min jitter) must have fired.
    later = first + timedelta(hours=7)
    assert len(planner.build_plan(conn, now=later).packages) == 1


def test_nothing_is_due_immediately_after_a_fetch(conn: sqlite3.Connection) -> None:
    make_track(conn)
    now = datetime.now(timezone.utc)
    planner.build_plan(conn, now=now)

    # Pretend the fetch happened by writing a fetch_log row.
    conn.execute(
        "INSERT INTO fetch_log (source, key, requested_at, ok) VALUES ('packages', ?, ?, 1)",
        (planner.build_plan(conn, now=now).packages[0].key, now.isoformat()),
    )
    conn.commit()

    assert planner.build_plan(conn, now=now + timedelta(minutes=5)).packages == ()


def test_jitter_is_deterministic_and_bounded() -> None:
    key = "DCHH|es-es|2027-01|2n_3j|1"
    first = planner.jitter_seconds(key)
    second = planner.jitter_seconds(key)

    assert first == second, "jitter must not be re-randomised per tick"
    assert -900 <= first <= 900

    spread = {planner.jitter_seconds(f"hotel-{i}") for i in range(50)}
    assert len(spread) > 10, "jitter should actually spread buckets apart"


def test_stopped_tracks_are_not_planned(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn)
    assert planner.build_plan(conn).packages

    tracks.stop_track(conn, track_id)

    assert planner.build_plan(conn).is_empty


def test_trimmed_arrivals_do_not_keep_a_month_alive(conn: sqlite3.Connection) -> None:
    """A month stops being fetched once nothing in it is watched any more."""
    base = (date.today().replace(day=1) + timedelta(days=62)).replace(day=1)
    next_month = (base + timedelta(days=32)).replace(day=1)
    crossing = base + timedelta(days=25)  # late in the month, stay spills over

    track_id = tracks.create_track(
        conn,
        tracks.TrackSpec(
            name="crosses a month boundary",
            adults=2,
            arrival_start=crossing,
            arrival_end=crossing + timedelta(days=6),
            hotels=(("DCHH", True),),
        ),
    )
    assert {t.month for t in planner.build_plan(conn).packages} == {base, next_month}

    tracks.set_arrival_window(
        conn, track_id, next_month, next_month + timedelta(days=2)
    )
    # Everything in the first month is now trimmed, so that bucket disappears.
    assert {t.month for t in planner.build_plan(conn).packages} == {next_month}


def test_ticket_tasks_follow_the_los(conn: sqlite3.Connection) -> None:
    make_track(conn, days=2, child_birthdates=("2020-01-01",))
    plan = planner.build_plan(conn)

    assert len(plan.tickets) == 1
    task = plan.tickets[0]
    assert task.duration_days == 3  # 2n_3j
    assert task.children == 1
    assert task.market == "es-es"
