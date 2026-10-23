"""Deciding what to fetch, and when.

The planner is pure computation over the database: no network, no writes. That
makes the interesting behaviour - coalescing, cadence, jitter - testable
without mocking HTTP.

A **fetch bucket** is the smallest request that satisfies several series at
once: ``(hotel, market, month, los, party)``. Party and los are part of the key
because the request prices exactly one party for one stay shape; two tracks
watching the same hotel-month with different parties genuinely need two
requests. Everything else coalesces.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app import config

TICKET_CADENCE_HOURS = 24
JITTER_SPREAD_SECONDS = 15 * 60  # +/- 15 minutes


@dataclass(frozen=True, slots=True)
class PackageTask:
    hotel: str
    market: str
    month: date
    los: str
    party_id: int
    adults: int
    child_birthdates: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.hotel}|{self.market}|{self.month:%Y-%m}|{self.los}|{self.party_id}"


@dataclass(frozen=True, slots=True)
class TicketTask:
    market: str
    month: date
    duration_days: int
    party_id: int
    adults: int
    children: int

    @property
    def key(self) -> str:
        return f"{self.market}|{self.month:%Y-%m}|{self.duration_days}|{self.party_id}"


@dataclass(frozen=True, slots=True)
class Plan:
    packages: tuple[PackageTask, ...]
    tickets: tuple[TicketTask, ...]

    @property
    def is_empty(self) -> bool:
        return not self.packages and not self.tickets


def jitter_seconds(key: str) -> int:
    """Deterministic per-bucket jitter in [-900, +900].

    Deterministic rather than random: a bucket must not draw a new offset every
    tick, or a 6-hour cadence would drift and eventually fire in a burst.
    """
    digest = hashlib.sha256(key.encode()).digest()
    offset = int.from_bytes(digest[:4], "big") % (2 * JITTER_SPREAD_SECONDS + 1)
    return offset - JITTER_SPREAD_SECONDS


def _last_attempts(conn: sqlite3.Connection) -> dict[tuple[str, str], datetime]:
    rows = conn.execute(
        "SELECT source, key, MAX(requested_at) AS last FROM fetch_log GROUP BY source, key"
    ).fetchall()
    return {
        (row["source"], row["key"]): datetime.fromisoformat(row["last"]) for row in rows
    }


def _due(last: datetime | None, *, now: datetime, cadence_hours: int, key: str) -> bool:
    if last is None:
        return True
    next_due = last + timedelta(hours=cadence_hours, seconds=jitter_seconds(key))
    return now >= next_due


def build_plan(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    force: bool = False,
    track_id: int | None = None,
) -> Plan:
    """What to fetch now.

    ``force`` ignores cadence; ``track_id`` limits the plan to one track
    (the Check now button). Both are user-initiated; scheduled ticks use
    neither.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    attempts = _last_attempts(conn)

    package_filter = ""
    package_params: tuple[int, ...] = ()
    if track_id is not None:
        package_filter = "AND ts.track_id = ?"
        package_params = (track_id,)

    rows = conn.execute(
        f"""
        SELECT s.hotel_code, s.market, s.arrival_date, s.los, s.party_id,
               p.adults, p.child_birthdates, MIN(tm.cadence_hours) AS cadence_hours
        FROM track_series ts
        JOIN track t        ON t.id = ts.track_id AND t.status = 'active'
                           AND t.archived_at IS NULL
        JOIN series s       ON s.id = ts.series_id
        JOIN party p        ON p.id = s.party_id
        JOIN track_market tm ON tm.track_id = t.id AND tm.market = s.market
        WHERE ts.watched = 1 {package_filter}
        GROUP BY s.hotel_code, s.market, s.arrival_date, s.los, s.party_id,
                 p.adults, p.child_birthdates
        """,
        package_params,
    ).fetchall()

    packages: dict[str, PackageTask] = {}
    cadences: dict[str, int] = {}

    for row in rows:
        arrival = date.fromisoformat(row["arrival_date"])
        month = arrival.replace(day=1)
        if not config.within_horizon(arrival, today):
            continue

        task = PackageTask(
            hotel=row["hotel_code"],
            market=row["market"],
            month=month,
            los=row["los"],
            party_id=int(row["party_id"]),
            adults=int(row["adults"]),
            child_birthdates=tuple(json.loads(row["child_birthdates"])),
        )
        packages.setdefault(task.key, task)
        previous = cadences.get(task.key)
        current = int(row["cadence_hours"])
        cadences[task.key] = current if previous is None else min(previous, current)

    ticket_filter = ""
    ticket_params: tuple[int, ...] = ()
    if track_id is not None:
        ticket_filter = "AND t.id = ?"
        ticket_params = (track_id,)

    tickets: dict[str, TicketTask] = {}
    for track in conn.execute(
        f"""
        SELECT t.id, t.los, t.party_id, t.arrival_start, t.arrival_end, p.adults, p.child_birthdates
        FROM track t JOIN party p ON p.id = t.party_id
        WHERE t.status = 'active' AND t.archived_at IS NULL {ticket_filter}
        """,
        ticket_params,
    ):
        children = len(json.loads(track["child_birthdates"]))
        duration = config.park_days(track["los"])
        start = date.fromisoformat(track["arrival_start"])
        end = date.fromisoformat(track["arrival_end"])
        markets = [
            row["market"]
            for row in conn.execute(
                "SELECT market FROM track_market WHERE track_id = ?", (track["id"],)
            )
        ]
        for month in _months_between(start, end):
            if not config.within_horizon(month, today):
                continue
            for market in markets:
                task = TicketTask(
                    market=market,
                    month=month,
                    duration_days=duration,
                    party_id=int(track["party_id"]),
                    adults=int(track["adults"]),
                    children=children,
                )
                tickets.setdefault(task.key, task)

    due_packages = tuple(
        task
        for task in sorted(packages.values(), key=lambda t: t.key)
        if force
        or _due(
            attempts.get(("packages", task.key)),
            now=now,
            cadence_hours=cadences[task.key],
            key=task.key,
        )
    )
    due_tickets = tuple(
        task
        for task in sorted(tickets.values(), key=lambda t: t.key)
        if force
        or _due(
            attempts.get(("tickets", task.key)),
            now=now,
            cadence_hours=TICKET_CADENCE_HOURS,
            key=task.key,
        )
    )

    return Plan(packages=due_packages, tickets=due_tickets)


def _months_between(start: date, end: date) -> list[date]:
    months: list[date] = []
    cursor = start.replace(day=1)
    last = end.replace(day=1)
    while cursor <= last:
        months.append(cursor)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return months
