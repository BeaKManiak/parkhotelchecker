"""Turning a poll into one readable Telegram message.

The digest is deliberately narrow. With sixteen hotels and weeks of arrivals,
"tell me about every change" produces a wall of numbers that gets muted within
a week. The rules, from the agreed plan:

- only active tracks, only the primary market, only shortlisted hotels;
- a line when the price moves by at least the track's ``min_delta``, or when the
  price sets a new all-time low for that arrival, or when availability flips;
- one message per poll, and only when there is something to say.

Everything excluded here is still written to the change-log and still drawn on
the chart. Silence means "nothing worth waking you for", not "we stopped looking"
- the footer and the health page carry that distinction.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app import config
from app.dlp import hotels as hotel_catalog
from app.tracking.poller import PollResult
from app.tracking.store import from_cents, utcnow

MAX_LINES = 12

DOWN = "▼"
UP = "▲"
SOLD_OUT = "●"
BACK = "○"


@dataclass(frozen=True, slots=True)
class Line:
    hotel_code: str
    hotel_name: str
    arrival: date
    old_price: Decimal | None
    new_price: Decimal | None
    old_available: bool
    new_available: bool
    all_time_low: bool

    @property
    def is_sold_out(self) -> bool:
        return self.old_available and not self.new_available

    @property
    def is_back(self) -> bool:
        return not self.old_available and self.new_available

    @property
    def delta(self) -> Decimal | None:
        if self.old_price is None or self.new_price is None:
            return None
        return self.new_price - self.old_price


@dataclass
class Section:
    track_id: int
    track_name: str
    los: str
    adults: int
    children: int
    lines: list[Line] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Best:
    hotel_code: str
    hotel_name: str
    arrival: date
    package: Decimal
    tickets: Decimal | None
    implied: Decimal | None


def build_digest(
    conn: sqlite3.Connection, result: PollResult, *, now: datetime | None = None
) -> str | None:
    now = now or utcnow()
    sections = _sections(conn, result)

    total = sum(len(section.lines) for section in sections)
    if total == 0:
        return None

    shown: list[Section] = []
    remaining = MAX_LINES
    for section in sections:
        if remaining <= 0:
            break
        kept = section.lines[:remaining]
        remaining -= len(kept)
        shown.append(
            Section(
                track_id=section.track_id,
                track_name=section.track_name,
                los=section.los,
                adults=section.adults,
                children=section.children,
                lines=kept,
            )
        )

    noun = "change" if total == 1 else "changes"
    parts = [f"🎢 Disneyland Paris — {now:%d %b, %H:%M}  ·  {total} {noun}", ""]

    for section in shown:
        parts.append(_section_header(section))
        for line in section.lines:
            parts.append(_line_text(line))
        parts.append("")

    if total > MAX_LINES:
        parts.append(f"… and {total - MAX_LINES} more")
        parts.append("")

    bests = [(section, _best(conn, section)) for section in shown]
    bests = [(section, best) for section, best in bests if best is not None]
    if bests:
        heading = (
            "Best arrival this window" if len(bests) == 1 else "Best arrival per track"
        )
        parts.append(heading)
        for section, best in bests:
            prefix = "" if len(bests) == 1 else f"{section.track_name}: "
            parts.append(f"  {prefix}{_best_text(best)}")
        parts.append("")

    parts.append(f"Last successful check: {_last_success(conn, now)}")
    return "\n".join(parts)


# --- selection -------------------------------------------------------------


def _sections(conn: sqlite3.Connection, result: PollResult) -> list[Section]:
    sections: dict[int, Section] = {}

    for written in result.changes:
        if written.series_id == 0:
            continue  # a ticket reference move is not a hotel alert

        series = conn.execute(
            "SELECT * FROM series WHERE id = ?", (written.series_id,)
        ).fetchone()
        if series is None or series["market"] != config.PRIMARY_MARKET:
            # Canaries are stored and charted; the agreed plan keeps them silent
            # in v1 because their currencies are market multipliers, not FX.
            continue

        latest, previous = _last_two(conn, written.series_id)
        if latest is None:
            continue

        new_price = from_cents(latest["price_cents"])
        new_available = bool(latest["available"])
        old_price = from_cents(previous["price_cents"]) if previous else None
        old_available = bool(previous["available"]) if previous else False
        all_time_low = _is_new_low(conn, written.series_id, latest)

        for track in _watching_tracks(conn, written.series_id, series["hotel_code"]):
            if not _worth_a_line(
                track,
                old_price=old_price,
                new_price=new_price,
                old_available=old_available,
                new_available=new_available,
                all_time_low=all_time_low,
            ):
                continue

            section = sections.get(int(track["id"]))
            if section is None:
                section = Section(
                    track_id=int(track["id"]),
                    track_name=track["name"],
                    los=series["los"],
                    adults=int(track["adults"]),
                    children=len(json.loads(track["child_birthdates"])),
                )
                sections[int(track["id"])] = section

            section.lines.append(
                Line(
                    hotel_code=series["hotel_code"],
                    hotel_name=hotel_catalog.label(series["hotel_code"]),
                    arrival=date.fromisoformat(series["arrival_date"]),
                    old_price=old_price,
                    new_price=new_price,
                    old_available=old_available,
                    new_available=new_available,
                    all_time_low=all_time_low,
                )
            )

    ordered: list[Section] = []
    for section in sections.values():
        section.lines.sort(key=lambda line: (line.hotel_name, line.arrival))
        ordered.append(section)
    ordered.sort(key=lambda section: section.track_id)
    return ordered


def _worth_a_line(
    track: sqlite3.Row,
    *,
    old_price: Decimal | None,
    new_price: Decimal | None,
    old_available: bool,
    new_available: bool,
    all_time_low: bool,
) -> bool:
    if old_available != new_available:
        return True  # sold out or back available: always worth knowing
    if new_price is None or old_price is None or new_price == old_price:
        return False
    delta_cents = abs(int(new_price * 100) - int(old_price * 100))
    return delta_cents >= int(track["min_delta_cents"]) or all_time_low


def _last_two(conn: sqlite3.Connection, series_id: int) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    rows = conn.execute(
        """
        SELECT id, price_cents, available, observed_at FROM observation
        WHERE series_id = ? ORDER BY observed_at DESC, id DESC LIMIT 2
        """,
        (series_id,),
    ).fetchall()
    latest = rows[0] if rows else None
    previous = rows[1] if len(rows) > 1 else None
    return latest, previous


def _is_new_low(conn: sqlite3.Connection, series_id: int, latest: sqlite3.Row) -> bool:
    if not latest["available"] or latest["price_cents"] is None:
        return False
    # "Low before this row" depends on the row's identity, not just its
    # timestamp: a heartbeat and a change can share a second.
    row = conn.execute(
        """
        SELECT MIN(price_cents) AS low FROM observation
        WHERE series_id = ? AND available = 1
          AND (observed_at < ? OR (observed_at = ? AND id < ?))
        """,
        (series_id, latest["observed_at"], latest["observed_at"], latest["id"]),
    ).fetchone()
    low = row["low"] if row else None
    return low is None or latest["price_cents"] < low


def _watching_tracks(
    conn: sqlite3.Connection, series_id: int, hotel_code: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT t.id, t.name, t.min_delta_cents, p.adults, p.child_birthdates
        FROM track t
        JOIN party p ON p.id = t.party_id
        JOIN track_series ts ON ts.track_id = t.id AND ts.series_id = ? AND ts.watched = 1
        JOIN track_hotel th
          ON th.track_id = t.id AND th.hotel_code = ? AND th.shortlisted = 1
        WHERE t.status = 'active' AND t.archived_at IS NULL
        ORDER BY t.id
        """,
        (series_id, hotel_code),
    ).fetchall()


def _best(conn: sqlite3.Connection, section: Section) -> Best | None:
    row = conn.execute(
        """
        SELECT s.hotel_code, s.arrival_date, s.party_id, o.price_cents, t.los
        FROM series s
        JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ? AND ts.watched = 1
        JOIN track t ON t.id = ts.track_id
        JOIN track_hotel th
          ON th.track_id = t.id AND th.hotel_code = s.hotel_code AND th.shortlisted = 1
        JOIN observation o ON o.id = (
            SELECT x.id FROM observation x WHERE x.series_id = s.id
            ORDER BY x.observed_at DESC, x.id DESC LIMIT 1
        )
        WHERE s.market = ? AND o.available = 1 AND o.price_cents IS NOT NULL
        ORDER BY o.price_cents ASC, s.arrival_date ASC
        LIMIT 1
        """,
        (section.track_id, config.PRIMARY_MARKET),
    ).fetchone()
    if row is None:
        return None

    arrival = date.fromisoformat(row["arrival_date"])
    tickets = _ticket_total(
        conn,
        market=config.PRIMARY_MARKET,
        arrival=arrival,
        duration=config.park_days(row["los"]),
        party_id=int(row["party_id"]),
    )
    package = from_cents(row["price_cents"])
    if package is None:
        return None
    implied = (
        (package - tickets).quantize(Decimal("0.01")) if tickets is not None else None
    )
    return Best(
        hotel_code=row["hotel_code"],
        hotel_name=hotel_catalog.label(row["hotel_code"]),
        arrival=arrival,
        package=package,
        tickets=tickets,
        implied=implied,
    )


def _ticket_total(
    conn: sqlite3.Connection, *, market: str, arrival: date, duration: int, party_id: int
) -> Decimal | None:
    row = conn.execute(
        """
        SELECT total_cents FROM ticket_price
        WHERE market = ? AND visit_start = ? AND duration_days = ? AND parks = ? AND party_id = ?
          AND total_cents IS NOT NULL
        ORDER BY observed_at DESC, id DESC LIMIT 1
        """,
        (market, arrival.isoformat(), duration, config.PARKS, party_id),
    ).fetchone()
    return from_cents(row["total_cents"]) if row else None


# --- rendering -------------------------------------------------------------


def _section_header(section: Section) -> str:
    return (
        f"{section.track_name} · {config.los_label(section.los)}"
        f" · {section.adults}a{section.children}c"
    )


def _line_text(line: Line) -> str:
    day = f"{line.arrival.day} {line.arrival:%b}"

    if line.is_sold_out:
        was = f" (was {_money(line.old_price)})" if line.old_price is not None else ""
        return f"  {SOLD_OUT} {day}   sold out{was}"

    if line.is_back:
        low = ", all-time low" if line.all_time_low else ""
        return f"  {BACK} {day}   back available at {_money(line.new_price)}{low}"

    delta = line.delta or Decimal("0")
    arrow = DOWN if delta < 0 else UP
    sign = "−" if delta < 0 else "+"
    note = ", all-time low" if line.all_time_low else ""
    return (
        f"  {arrow} {day}   {_money(line.old_price)} → {_money(line.new_price)}"
        f"   ({sign}{_money(abs(delta))}{note})"
    )


def _best_text(best: Best) -> str:
    text = (
        f"{_hotel_short(best.hotel_name)} {best.arrival.day} {best.arrival:%b}"
        f" — package {_money(best.package)}"
    )
    if best.tickets is not None:
        text += f" · tickets {_money(best.tickets)}"
    if best.implied is not None:
        text += f" · implied hotel {_money(best.implied)}"
    return text


def _hotel_short(name: str) -> str:
    for prefix in ("Disney Hotel ", "Disney "):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    if value == value.to_integral_value():
        return f"€{value:,.0f}"
    return f"€{value:,.2f}"


def last_success_text(conn: sqlite3.Connection, now: datetime) -> str:
    """Shared with failure alerts so both say the same thing about silence."""
    return _last_success(conn, now)


def _last_success(conn: sqlite3.Connection, now: datetime) -> str:
    # Telegram is a delivery channel, not a check of the world. A sent message
    # must never make the tracker look healthy.
    row = conn.execute(
        "SELECT MAX(last_success_at) AS at FROM health WHERE source <> 'telegram'"
    ).fetchone()
    if not row or not row["at"]:
        return "never"
    last = datetime.fromisoformat(row["at"])
    seconds = int((now - last).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"
