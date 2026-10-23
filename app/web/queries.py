"""Read-only queries for the dashboard.

No ORM, no repository abstraction: the schema is small and the queries are the
documentation. Everything here is a plain ``sqlite3.Row`` or a frozen dataclass,
and nothing writes.

Two rules that keep the numbers honest:

- "current price" always means the **latest observation for that series**, not
  the latest row in the table (series are independent).
- a series with an observation that has no price is **sold out**, and a series
  with no observations at all is **never seen** - they are not the same thing
  and the UI says so.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app import config
from app.dlp import hotels as hotel_catalog
from app.dlp.hotels import HotelKind
from app.tracking.store import from_cents


@dataclass(frozen=True, slots=True)
class HotelRow:
    code: str
    name: str
    kind: HotelKind
    seasonal: bool
    shortlisted: bool
    has_data: bool
    all_sold_out: bool
    cheapest: Decimal | None
    cheapest_arrival: date | None
    all_time_low: Decimal | None
    last_seen: datetime | None
    has_history: bool


@dataclass(frozen=True, slots=True)
class SeriesRow:
    series_id: int
    arrival: date
    currency: str
    price: Decimal | None
    available: bool
    observed_at: datetime | None
    previous_price: Decimal | None
    all_time_low: Decimal | None
    ticket: Decimal | None
    implied: Decimal | None
    history: tuple[tuple[datetime, Decimal], ...]


@dataclass(frozen=True, slots=True)
class TrackRow:
    id: int
    name: str
    status: str
    los: str
    arrival_start: date
    arrival_end: date
    created_at: datetime | None
    stopped_at: datetime | None
    archived_at: datetime | None
    adults: int
    child_birthdates: tuple[str, ...]
    min_delta: Decimal
    series_watched: int
    shortlisted: int
    market: str
    cheapest: Decimal | None
    last_observation: datetime | None


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def track(conn: sqlite3.Connection, track_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM track WHERE id = ?", (track_id,)).fetchone()


def markets(conn: sqlite3.Connection, track_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT market, cadence_hours FROM track_market WHERE track_id = ? ORDER BY market",
        (track_id,),
    ).fetchall()


def primary_market(conn: sqlite3.Connection, track_id: int) -> str:
    """The market whose prices the dashboard displays.

    Canary markets are fetched to answer "is ES being overcharged", not to
    clutter every table with four currencies.
    """
    watched = [row["market"] for row in markets(conn, track_id)]
    if config.PRIMARY_MARKET in watched:
        return config.PRIMARY_MARKET
    return watched[0] if watched else config.PRIMARY_MARKET


def hotels(conn: sqlite3.Connection, track_id: int, market: str) -> list[HotelRow]:
    shortlist = {
        row["hotel_code"]: bool(row["shortlisted"])
        for row in conn.execute(
            "SELECT hotel_code, shortlisted FROM track_hotel WHERE track_id = ?", (track_id,)
        )
    }

    latest = conn.execute(
        """
        SELECT s.hotel_code, s.arrival_date,
               o.price_cents, o.available, o.observed_at,
               low.min_cents,
               (SELECT COUNT(*) FROM observation c WHERE c.series_id = s.id) AS obs_count
        FROM series s
        JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ? AND ts.watched = 1
        LEFT JOIN observation o ON o.id = (
            SELECT id FROM observation x WHERE x.series_id = s.id
            ORDER BY x.observed_at DESC, x.id DESC LIMIT 1
        )
        LEFT JOIN (
            SELECT series_id, MIN(price_cents) AS min_cents
            FROM observation WHERE available = 1 GROUP BY series_id
        ) low ON low.series_id = s.id
        WHERE s.market = ?
        """,
        (track_id, market),
    ).fetchall()

    by_hotel: dict[str, list[sqlite3.Row]] = {}
    for row in latest:
        by_hotel.setdefault(row["hotel_code"], []).append(row)

    rows: list[HotelRow] = []
    for code in shortlist:
        catalog_entry = hotel_catalog.BY_CODE.get(code)
        if catalog_entry is None:
            continue
        observations = by_hotel.get(code, [])
        priced = [r for r in observations if r["price_cents"] is not None and r["available"]]
        seen = [r for r in observations if r["observed_at"]]

        cheapest = min((r["price_cents"] for r in priced), default=None)
        cheapest_arrival = None
        if cheapest is not None:
            cheapest_arrival = min(
                date.fromisoformat(r["arrival_date"])
                for r in priced
                if r["price_cents"] == cheapest
            )
        lows = [r["min_cents"] for r in observations if r["min_cents"] is not None]
        # A single observation is both the cheapest and the all-time low. Saying
        # "lowest yet" on the first poll would be true and useless.
        has_history = any(r["obs_count"] > 1 for r in observations)

        rows.append(
            HotelRow(
                code=code,
                name=catalog_entry.name,
                kind=catalog_entry.kind,
                seasonal=catalog_entry.seasonal,
                shortlisted=shortlist[code],
                has_data=bool(seen),
                all_sold_out=bool(seen) and not priced,
                cheapest=from_cents(cheapest),
                cheapest_arrival=cheapest_arrival,
                all_time_low=from_cents(min(lows)) if lows else None,
                last_seen=max((_dt(r["observed_at"]) for r in seen if r["observed_at"]), default=None),
                has_history=has_history,
            )
        )

    rows.sort(key=lambda r: (r.kind != HotelKind.ONSITE, r.cheapest is None, r.cheapest or 0))
    return rows


def series(conn: sqlite3.Connection, track_id: int, hotel_code: str, market: str) -> list[SeriesRow]:
    """Every watched arrival for one hotel, with its full price history."""
    package = conn.execute(
        """
        SELECT s.id, s.arrival_date, s.currency, s.los, s.party_id,
               o.price_cents, o.available, o.observed_at,
               (SELECT p.price_cents FROM observation p
                 WHERE p.series_id = s.id AND p.id <> o.id
                   AND (p.observed_at < o.observed_at
                        OR (p.observed_at = o.observed_at AND p.id < o.id))
                 ORDER BY p.observed_at DESC, p.id DESC LIMIT 1) AS previous_cents,
               (SELECT MIN(l.price_cents) FROM observation l
                 WHERE l.series_id = s.id AND l.available = 1) AS low_cents
        FROM series s
        JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ? AND ts.watched = 1
        LEFT JOIN observation o ON o.id = (
            SELECT id FROM observation x WHERE x.series_id = s.id
            ORDER BY x.observed_at DESC, x.id DESC LIMIT 1
        )
        WHERE s.hotel_code = ? AND s.market = ?
        ORDER BY s.arrival_date
        """,
        (track_id, hotel_code, market),
    ).fetchall()

    if not package:
        return []

    los = package[0]["los"]
    tickets = _ticket_prices(
        conn,
        market=market,
        duration=config.park_days(los),
        party_id=int(package[0]["party_id"]),
    )

    rows: list[SeriesRow] = []
    for row in package:
        arrival = date.fromisoformat(row["arrival_date"])
        ticket = tickets.get(arrival)
        price = from_cents(row["price_cents"])
        implied = (
            (price - ticket).quantize(Decimal("0.01"))
            if price is not None and ticket is not None
            else None
        )
        rows.append(
            SeriesRow(
                series_id=int(row["id"]),
                arrival=arrival,
                currency=row["currency"],
                price=price,
                available=bool(row["available"]) if row["observed_at"] else False,
                observed_at=_dt(row["observed_at"]),
                previous_price=from_cents(row["previous_cents"]),
                all_time_low=from_cents(row["low_cents"]),
                ticket=ticket,
                implied=implied,
                history=_history(conn, int(row["id"])),
            )
        )
    return rows


def _history(conn: sqlite3.Connection, series_id: int) -> tuple[tuple[datetime, Decimal], ...]:
    rows = conn.execute(
        """
        SELECT observed_at, price_cents FROM observation
        WHERE series_id = ? AND price_cents IS NOT NULL
        ORDER BY observed_at, id
        """,
        (series_id,),
    ).fetchall()
    points = [(_dt(row["observed_at"]), from_cents(row["price_cents"])) for row in rows]
    return tuple((when, price) for when, price in points if when is not None and price is not None)


def _ticket_prices(
    conn: sqlite3.Connection, *, market: str, duration: int, party_id: int
) -> dict[date, Decimal]:
    rows = conn.execute(
        """
        SELECT t.visit_start, t.total_cents
        FROM ticket_price t
        WHERE t.market = ? AND t.duration_days = ? AND t.parks = ? AND t.party_id = ?
          AND t.total_cents IS NOT NULL
          AND t.id = (
            SELECT x.id FROM ticket_price x
            WHERE x.market = t.market AND x.visit_start = t.visit_start
              AND x.duration_days = t.duration_days AND x.parks = t.parks
              AND x.party_id = t.party_id
            ORDER BY x.observed_at DESC, x.id DESC LIMIT 1
          )
        """,
        (market, duration, config.PARKS, party_id),
    ).fetchall()
    return {
        date.fromisoformat(row["visit_start"]): from_cents(row["total_cents"]) for row in rows
    }


def track_list(
    conn: sqlite3.Connection, *, archived: bool = False
) -> list[TrackRow]:
    visibility = "IS NOT NULL" if archived else "IS NULL"
    ordering = "t.archived_at DESC" if archived else "t.status = 'stopped', t.id DESC"
    rows = conn.execute(
        f"""
        SELECT t.*, p.adults, p.child_birthdates,
               (SELECT COUNT(*) FROM track_series ts
                 WHERE ts.track_id = t.id AND ts.watched = 1) AS series_watched,
               (SELECT COUNT(*) FROM track_hotel th
                 WHERE th.track_id = t.id AND th.shortlisted = 1) AS shortlisted,
               (SELECT MAX(o.observed_at) FROM observation o
                 JOIN track_series ts2 ON ts2.series_id = o.series_id
                 WHERE ts2.track_id = t.id AND ts2.watched = 1) AS last_observation
        FROM track t JOIN party p ON p.id = t.party_id
        WHERE t.archived_at {visibility}
        ORDER BY {ordering}
        """
    ).fetchall()

    out: list[TrackRow] = []
    for row in rows:
        market = primary_market(conn, int(row["id"]))
        cheapest = min(
            (h.cheapest for h in hotels(conn, int(row["id"]), market) if h.cheapest is not None),
            default=None,
        )
        out.append(
            TrackRow(
                id=int(row["id"]),
                name=row["name"],
                status=row["status"],
                los=row["los"],
                arrival_start=date.fromisoformat(row["arrival_start"]),
                arrival_end=date.fromisoformat(row["arrival_end"]),
                created_at=_dt(row["created_at"]),
                stopped_at=_dt(row["stopped_at"]),
                archived_at=_dt(row["archived_at"]),
                adults=int(row["adults"]),
                child_birthdates=tuple(json.loads(row["child_birthdates"])),
                min_delta=from_cents(int(row["min_delta_cents"])) or Decimal("0"),
                series_watched=int(row["series_watched"]),
                shortlisted=int(row["shortlisted"]),
                market=market,
                cheapest=cheapest,
                last_observation=_dt(row["last_observation"]),
            )
        )
    return out


def health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM health ORDER BY source").fetchall()


def last_success(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(last_success_at) AS at FROM health WHERE source <> 'telegram'"
    ).fetchone()
    return _dt(row["at"])


def recent_failures(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fetch_log WHERE ok = 0 ORDER BY requested_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def recent_polls(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT source, key, requested_at, ok, status, error FROM fetch_log
        ORDER BY requested_at DESC, id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


def age_of_last_success(conn: sqlite3.Connection) -> timedelta | None:
    last = last_success(conn)
    if last is None:
        return None
    return datetime.now(timezone.utc) - last


def track_alerts(conn: sqlite3.Connection, track_id: int, since: datetime) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM observation o
        JOIN track_series ts ON ts.series_id = o.series_id AND ts.track_id = ?
        WHERE o.kind = 'change' AND o.observed_at >= ?
        """,
        (track_id, since.astimezone(timezone.utc).isoformat(timespec="seconds")),
    ).fetchone()
    return int(row["n"])
