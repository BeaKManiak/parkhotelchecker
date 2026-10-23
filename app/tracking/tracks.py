"""Tracks: creation, series materialisation, window edits, stop.

A **series** is the unit of history: ``(hotel, market, arrival_date, los,
party)``. Its identity deliberately excludes the track, so two tracks watching
the same thing share one set of observations and one upstream request.

A **track** is a configuration that points at a set of series. That split is
what makes window edits safe: trimming stops watching a series, it never
reinterprets one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app import config
from app.dlp import hotels
from app.tracking import store


@dataclass(frozen=True, slots=True)
class TrackSpec:
    name: str
    adults: int
    arrival_start: date
    arrival_end: date
    hotels: tuple[tuple[str, bool], ...]  # (hotel_code, shortlisted)
    child_birthdates: tuple[str, ...] = ()
    los: str = config.DEFAULT_LOS
    markets: tuple[tuple[str, int], ...] = ((config.PRIMARY_MARKET, 6),)  # (market, cadence_hours)
    min_delta_cents: int = 2000


def arrival_dates(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("latest check-in must be on or after the earliest check-in")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# Disneyland Paris charges for children from their third birthday; under-3s
# are free and have no field in the price payload at all. Age is taken at the
# arrival date, which is what the upstream does.
FREE_UNDER = 3


def age_on(birthdate: date, on: date) -> int:
    return on.year - birthdate.year - ((on.month, on.day) < (birthdate.month, birthdate.day))


def chargeable_children(birthdates: tuple[str, ...], arrivals: list[date]) -> tuple[str, ...]:
    """Drop under-3s, and refuse a window where a child's price status flips.

    A child who turns 3 mid-window is chargeable on some arrival dates and free
    on others. One track prices one party, so that has to be two tracks; guessing
    would silently price the wrong holiday.
    """
    chargeable: list[str] = []
    for raw in birthdates:
        birthdate = date.fromisoformat(raw)
        statuses = {age_on(birthdate, arrival) >= FREE_UNDER for arrival in arrivals}
        if len(statuses) > 1:
            raise ValueError(
                f"a child born {raw} turns {FREE_UNDER} during the window; "
                "create one track per side of that birthday"
            )
        if statuses.pop():
            chargeable.append(raw)
    return tuple(chargeable)


def validate_spec(spec: TrackSpec, *, today: date | None = None) -> list[date]:
    today = today or date.today()
    arrivals = arrival_dates(spec.arrival_start, spec.arrival_end)

    if not spec.name.strip():
        raise ValueError("track needs a name")
    if spec.adults < 1:
        raise ValueError("at least one adult is required")
    # Count every child here, including under-3s: they are free but still sleep
    # in the room. chargeable_children() drops them later, so this check must
    # come first or a party of eight would slip through.
    party_size = spec.adults + len(spec.child_birthdates)
    if party_size > config.MAX_PARTY:
        raise ValueError(
            f"{spec.adults} adults and {len(spec.child_birthdates)} children "
            f"is {party_size} people; a room holds at most {config.MAX_PARTY}"
        )
    if len(arrivals) > config.MAX_ARRIVALS:
        raise ValueError(
            f"window covers {len(arrivals)} arrival dates; the cap is {config.MAX_ARRIVALS}"
        )
    if arrivals[0] < today:
        raise ValueError("arrival window starts in the past")
    if arrivals[-1] > config.horizon_latest(today):
        raise ValueError(
            f"arrival window extends past the booking horizon ({config.horizon_latest(today)})"
        )
    if spec.los not in config.VALID_LOS:
        raise ValueError(f"unsupported los {spec.los!r}")
    if not spec.hotels:
        raise ValueError("track needs at least one hotel")
    if not spec.markets:
        raise ValueError("track needs at least one market")
    if spec.min_delta_cents < 0:
        raise ValueError("min_delta_cents must not be negative")

    for code, _ in spec.hotels:
        if code not in hotels.BY_CODE:
            raise ValueError(f"unknown hotel code {code!r}")
    for market, cadence in spec.markets:
        if market not in config.MARKET_CURRENCY:
            raise ValueError(f"unknown market {market!r}")
        if cadence < 1:
            raise ValueError("cadence_hours must be >= 1")
    for birthdate in spec.child_birthdates:
        _ = date.fromisoformat(birthdate)  # raises on malformed input
    chargeable_children(spec.child_birthdates, arrivals)

    return arrivals


def create_track(
    conn: sqlite3.Connection, spec: TrackSpec, *, now: datetime | None = None
) -> int:
    now = now or store.utcnow()
    arrivals = validate_spec(spec)
    # Under-3s are free and absent from the payload; the party that prices is
    # the chargeable one, and it is shared by every track with that party.
    chargeable = chargeable_children(spec.child_birthdates, arrivals)
    party_id = store.get_or_create_party(
        conn, adults=spec.adults, child_birthdates=chargeable
    )

    cursor = conn.execute(
        """
        INSERT INTO track (
          name, status, party_id, los, arrival_start, arrival_end, created_at, min_delta_cents
        ) VALUES (?, 'active', ?, ?, ?, ?, ?, ?)
        """,
        (
            spec.name.strip(),
            party_id,
            spec.los,
            spec.arrival_start.isoformat(),
            spec.arrival_end.isoformat(),
            store.iso_utc(now),
            spec.min_delta_cents,
        ),
    )
    track_id = int(cursor.lastrowid)

    for code, shortlisted in spec.hotels:
        conn.execute(
            "INSERT INTO track_hotel (track_id, hotel_code, shortlisted) VALUES (?, ?, ?)",
            (track_id, code, 1 if shortlisted else 0),
        )
    for market, cadence in spec.markets:
        conn.execute(
            "INSERT INTO track_market (track_id, market, cadence_hours) VALUES (?, ?, ?)",
            (track_id, market, cadence),
        )

    _materialise(conn, track_id, party_id, spec.los, spec.hotels, spec.markets, arrivals)
    conn.commit()
    return track_id


def _materialise(
    conn: sqlite3.Connection,
    track_id: int,
    party_id: int,
    los: str,
    hotel_specs: tuple[tuple[str, bool], ...],
    market_specs: tuple[tuple[str, int], ...],
    arrivals: list[date],
) -> None:
    for code, _ in hotel_specs:
        for market, _ in market_specs:
            currency = config.MARKET_CURRENCY[market]
            for arrival in arrivals:
                series_id = store.get_or_create_series(
                    conn,
                    hotel_code=code,
                    market=market,
                    currency=currency,
                    arrival_date=arrival,
                    los=los,
                    party_id=party_id,
                )
                conn.execute(
                    """
                    INSERT INTO track_series (track_id, series_id, watched)
                    VALUES (?, ?, 1)
                    ON CONFLICT (track_id, series_id) DO UPDATE SET watched = 1
                    """,
                    (track_id, series_id),
                )


def set_arrival_window(
    conn: sqlite3.Connection,
    track_id: int,
    start: date,
    end: date,
    *,
    now: datetime | None = None,
    today: date | None = None,
) -> dict[str, int]:
    """Extend or trim the watched arrivals. Trimmed series keep their history."""
    now = now or store.utcnow()
    track = conn.execute("SELECT * FROM track WHERE id = ?", (track_id,)).fetchone()
    if track is None:
        raise ValueError(f"unknown track {track_id}")
    if track["archived_at"] is not None:
        raise ValueError("cannot edit an archived track")
    if track["status"] != "active":
        raise ValueError("cannot edit a stopped track")

    arrivals = arrival_dates(start, end)
    if len(arrivals) > config.MAX_ARRIVALS:
        raise ValueError(
            f"window covers {len(arrivals)} arrival dates; the cap is {config.MAX_ARRIVALS}"
        )
    if arrivals[-1] > config.horizon_latest(today):
        raise ValueError("arrival window extends past the booking horizon")
    if arrivals[0] < date.today():
        raise ValueError("arrival window starts in the past")

    hotel_specs = tuple(
        (row["hotel_code"], bool(row["shortlisted"]))
        for row in conn.execute(
            "SELECT hotel_code, shortlisted FROM track_hotel WHERE track_id = ?", (track_id,)
        )
    )
    market_specs = tuple(
        (row["market"], int(row["cadence_hours"]))
        for row in conn.execute(
            "SELECT market, cadence_hours FROM track_market WHERE track_id = ?", (track_id,)
        )
    )

    wanted = set(arrivals)
    existing = conn.execute(
        """
        SELECT ts.series_id, s.arrival_date
        FROM track_series ts JOIN series s ON s.id = ts.series_id
        WHERE ts.track_id = ?
        """,
        (track_id,),
    ).fetchall()

    untrimmed = 0
    for row in existing:
        arrival = date.fromisoformat(row["arrival_date"])
        if arrival not in wanted:
            conn.execute(
                "UPDATE track_series SET watched = 0 WHERE track_id = ? AND series_id = ?",
                (track_id, row["series_id"]),
            )
        elif not conn.execute(
            "SELECT watched FROM track_series WHERE track_id = ? AND series_id = ?",
            (track_id, row["series_id"]),
        ).fetchone()["watched"]:
            conn.execute(
                "UPDATE track_series SET watched = 1 WHERE track_id = ? AND series_id = ?",
                (track_id, row["series_id"]),
            )
            untrimmed += 1

    first_materialised = len(existing)
    _materialise(
        conn, track_id, int(track["party_id"]), track["los"], hotel_specs, market_specs, arrivals
    )
    added = _count_series(conn, track_id) - first_materialised

    conn.execute(
        "UPDATE track SET arrival_start = ?, arrival_end = ? WHERE id = ?",
        (start.isoformat(), end.isoformat(), track_id),
    )
    conn.commit()
    return {"added": max(added, 0), "untrimmed": untrimmed}


def stop_track(
    conn: sqlite3.Connection, track_id: int, *, now: datetime | None = None
) -> None:
    """Terminal. History stays; polling and alerts stop."""
    now = now or store.utcnow()
    row = conn.execute(
        "SELECT status, archived_at FROM track WHERE id = ?", (track_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown track {track_id}")
    if row["archived_at"] is not None:
        raise ValueError("cannot stop an archived track")
    if row["status"] == "stopped":
        return
    conn.execute(
        "UPDATE track SET status = 'stopped', stopped_at = ? WHERE id = ?",
        (store.iso_utc(now), track_id),
    )
    conn.commit()


def archive_track(
    conn: sqlite3.Connection,
    track_id: int,
    *,
    archived: bool = True,
    now: datetime | None = None,
) -> None:
    """Hide a track and pause polling, without touching its status or history."""
    row = conn.execute(
        "SELECT archived_at FROM track WHERE id = ?", (track_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown track {track_id}")
    if archived == (row["archived_at"] is not None):
        return
    value = store.iso_utc(now or store.utcnow()) if archived else None
    conn.execute("UPDATE track SET archived_at = ? WHERE id = ?", (value, track_id))
    conn.commit()


def delete_track(conn: sqlite3.Connection, track_id: int) -> None:
    """Remove the track and its links. Prices and their series stay."""
    row = conn.execute("SELECT id FROM track WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown track {track_id}")
    conn.execute("DELETE FROM track_series WHERE track_id = ?", (track_id,))
    conn.execute("DELETE FROM track_hotel WHERE track_id = ?", (track_id,))
    conn.execute("DELETE FROM track_market WHERE track_id = ?", (track_id,))
    conn.execute(
        "DELETE FROM alert_state WHERE key = ?", (f"first_check:{track_id}",)
    )
    conn.execute("DELETE FROM track WHERE id = ?", (track_id,))
    conn.commit()


def set_shortlist(conn: sqlite3.Connection, track_id: int, hotel_code: str, shortlisted: bool) -> None:
    conn.execute(
        "UPDATE track_hotel SET shortlisted = ? WHERE track_id = ? AND hotel_code = ?",
        (1 if shortlisted else 0, track_id, hotel_code),
    )
    conn.commit()


def _count_series(conn: sqlite3.Connection, track_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM track_series WHERE track_id = ?", (track_id,)
    ).fetchone()
    return int(row["n"])
