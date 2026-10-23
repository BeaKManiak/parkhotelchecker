"""The change-log: the only place observations are written.

The rule, in one sentence: **write a row when the price or availability
differs from the last row, or when 24 hours have passed with no difference.**

Three consequences that are easy to get wrong:

1. A date that is *absent* from the upstream response and was seen before is
   **sold out**, recorded with a NULL price.
2. A date that is absent and has **never** been seen is *not* sold out - it is
   probably not sellable for this party (occupancy). Nothing is written, so the
   two cases stay distinguishable forever.
3. A heartbeat is only written when the previous row is stale. Replaying the
   same response must not grow the table.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from app.dlp.packages import PackageCalendar
from app.dlp.tickets import TicketCalendar

Kind = Literal["first", "change", "heartbeat"]

HEARTBEAT_AFTER = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Written:
    """An observation that was actually inserted."""

    series_id: int
    kind: Kind
    arrival: date | None
    price: Decimal | None
    available: bool
    previous_price: Decimal | None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_cents(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int((value * 100).to_integral_value())


def from_cents(value: int | None) -> Decimal | None:
    if value is None:
        return None
    return (Decimal(value) / 100).quantize(Decimal("0.01"))


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- parties & series ------------------------------------------------------


def get_or_create_party(
    conn: sqlite3.Connection, *, adults: int, child_birthdates: tuple[str, ...]
) -> int:
    import hashlib
    import json

    canonical = sorted(child_birthdates)
    key = hashlib.sha256(
        f"{adults}|{','.join(canonical)}".encode()
    ).hexdigest()
    row = conn.execute("SELECT id FROM party WHERE party_key = ?", (key,)).fetchone()
    if row:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO party (party_key, adults, child_birthdates) VALUES (?, ?, ?)",
        (key, adults, json.dumps(canonical)),
    )
    return int(cursor.lastrowid)


def get_or_create_series(
    conn: sqlite3.Connection,
    *,
    hotel_code: str,
    market: str,
    currency: str,
    arrival_date: date,
    los: str,
    party_id: int,
) -> int:
    row = conn.execute(
        """
        SELECT id FROM series
        WHERE hotel_code = ? AND market = ? AND arrival_date = ? AND los = ? AND party_id = ?
        """,
        (hotel_code, market, arrival_date.isoformat(), los, party_id),
    ).fetchone()
    if row:
        return int(row["id"])
    cursor = conn.execute(
        """
        INSERT INTO series (hotel_code, market, currency, arrival_date, los, party_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (hotel_code, market, currency, arrival_date.isoformat(), los, party_id),
    )
    return int(cursor.lastrowid)


# --- observation writing ---------------------------------------------------


def latest_observation(conn: sqlite3.Connection, series_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, price_cents, available, observed_at, kind
        FROM observation WHERE series_id = ?
        ORDER BY observed_at DESC, id DESC LIMIT 1
        """,
        (series_id,),
    ).fetchone()


def _record(
    conn: sqlite3.Connection,
    *,
    series_id: int,
    price: Decimal | None,
    available: bool,
    now: datetime,
    arrival: date | None,
    record_all: bool = False,
) -> Written | None:
    previous = latest_observation(conn, series_id)
    previous_price = from_cents(previous["price_cents"]) if previous else None

    if previous is None:
        # Never seen and currently unpriced: do not fabricate a sold-out row.
        if price is None:
            return None
        kind: Kind = "first"
    elif previous_price != price or bool(previous["available"]) != available:
        kind = "change"
    elif record_all or now - _parse_iso(previous["observed_at"]) >= HEARTBEAT_AFTER:
        kind = "heartbeat"
    else:
        return None

    conn.execute(
        """
        INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
        VALUES (?, ?, ?, ?, ?)
        """,
        (series_id, to_cents(price), 1 if available else 0, iso_utc(now), kind),
    )
    return Written(
        series_id=series_id,
        kind=kind,
        arrival=arrival,
        price=price,
        available=available,
        previous_price=previous_price,
    )


def record_package_calendar(
    conn: sqlite3.Connection,
    calendar: PackageCalendar,
    *,
    party_id: int,
    now: datetime | None = None,
    record_all: bool = False,
) -> list[Written]:
    """Apply one month's package response to every series it covers.

    ``party_id`` is part of the filter, not decoration: one hotel-month can be
    watched by two tracks with different parties, and a 2a1c price must never
    be written onto a 2a0c series.
    """
    now = now or utcnow()
    month_start = calendar.month.replace(day=1)
    month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    rows = conn.execute(
        """
        SELECT id, arrival_date FROM series
        WHERE hotel_code = ? AND market = ? AND los = ? AND party_id = ?
          AND arrival_date BETWEEN ? AND ?
        ORDER BY arrival_date
        """,
        (
            calendar.hotel,
            calendar.market,
            calendar.los,
            party_id,
            month_start.isoformat(),
            month_end.isoformat(),
        ),
    ).fetchall()

    written: list[Written] = []
    for row in rows:
        arrival = date.fromisoformat(row["arrival_date"])
        day = calendar.days.get(arrival)
        price = day.price if day else None
        entry = _record(
            conn,
            series_id=int(row["id"]),
            price=price,
            available=day is not None,
            now=now,
            arrival=arrival,
            record_all=record_all,
        )
        if entry:
            written.append(entry)
    conn.commit()
    return written


def record_ticket_calendar(
    conn: sqlite3.Connection,
    calendar: TicketCalendar,
    *,
    adults: int,
    children: int,
    party_id: int,
    now: datetime | None = None,
    record_all: bool = False,
) -> list[Written]:
    """Ticket prices share one history across tracks, keyed by party + date."""
    now = now or utcnow()
    written: list[Written] = []

    for visit_start, day in sorted(calendar.days.items()):
        previous = conn.execute(
            """
            SELECT id, total_cents, available, observed_at FROM ticket_price
            WHERE market = ? AND visit_start = ? AND duration_days = ? AND parks = ?
              AND party_id = ?
            ORDER BY observed_at DESC, id DESC LIMIT 1
            """,
            (
                calendar.market,
                visit_start.isoformat(),
                calendar.duration_days,
                calendar.parks,
                party_id,
            ),
        ).fetchone()

        total = day.total(adults, children)
        previous_total = (
            (Decimal(previous["total_cents"]) / 100).quantize(Decimal("0.01"))
            if previous and previous["total_cents"] is not None
            else None
        )

        if previous is None:
            kind: Kind = "first"
        elif previous_total != total or bool(previous["available"]) != day.available:
            kind = "change"
        elif record_all or now - _parse_iso(previous["observed_at"]) >= HEARTBEAT_AFTER:
            kind = "heartbeat"
        else:
            continue

        conn.execute(
            """
            INSERT INTO ticket_price (
              market, currency, visit_start, duration_days, parks, product_type, party_id,
              adult_cents, child_cents, total_cents, available, observed_at, kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                calendar.market,
                calendar.currency,
                visit_start.isoformat(),
                calendar.duration_days,
                calendar.parks,
                calendar.product_type,
                party_id,
                to_cents(day.adult),
                to_cents(day.child),
                to_cents(total),
                1 if day.available else 0,
                iso_utc(now),
                kind,
            ),
        )
        written.append(
            Written(
                series_id=0,
                kind=kind,
                arrival=visit_start,
                price=total,
                available=day.available,
                previous_price=previous_total,
            )
        )

    conn.commit()
    return written
