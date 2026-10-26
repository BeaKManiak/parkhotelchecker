"""Small, non-diff messages: "first check done" and a test-mode check.

These are deliberately separate from the digest. The digest answers "what
changed?"; these answer "did the tracker actually run, and what does it see
right now?". A first check has no previous price, so it can never appear in the
change digest, and inventing a fake "change" would be a lie.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from app import config
from app.alerts import state
from app.dlp import hotels as hotel_catalog
from app.tracking.store import iso_utc, utcnow

# Existing tracks are marked as already checked when this feature first runs.
# Otherwise every track that predates the deployment would send a misleading
# "first check" message on the next poll.
BACKFILL_KEY = "first_check_backfill_v1"


def ensure_backfill(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> None:
    """Mark tracks that existed before this feature as already announced.

    Called at application startup, before any new track can be created. Calling
    it lazily from the first poll would also swallow the first check of a track
    created just after boot.
    """
    now = now or utcnow()
    if state.get(conn, BACKFILL_KEY) is not None:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO alert_state (key, value, updated_at)
        SELECT 'first_check:' || id, 'backfill', ? FROM track
        """,
        (iso_utc(now),),
    )
    state.set(conn, BACKFILL_KEY, "1", now=now)


def _current_prices(conn: sqlite3.Connection, track_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.hotel_code, s.arrival_date,
               o.price_cents, o.available, o.observed_at
        FROM series s
        JOIN track_series ts
          ON ts.series_id = s.id AND ts.track_id = ? AND ts.watched = 1
        LEFT JOIN observation o ON o.id = (
          SELECT x.id FROM observation x WHERE x.series_id = s.id
          ORDER BY x.observed_at DESC, x.id DESC LIMIT 1
        )
        WHERE s.market = ?
        """,
        (track_id, config.PRIMARY_MARKET),
    ).fetchall()


def _first_check_body(conn: sqlite3.Connection, track_id: int) -> str:
    """One line per shortlisted hotel, cheapest first.

    The overall cheapest hotel is usually the one the owner already knows;
    the useful baseline is the cheapest date for each hotel they actually
    watch.
    """
    rows = _current_prices(conn, track_id)
    seen = [row for row in rows if row["observed_at"]]
    priced = [row for row in rows if row["price_cents"] is not None and row["available"]]
    sold_out = [row for row in seen if row["price_cents"] is None]

    noun = "arrival" if len(priced) == 1 else "arrivals"
    lines = [f"{len(priced)} {noun} priced · {len(sold_out)} sold out"]

    if not priced:
        lines.extend(["", "No prices yet; the tracker will keep trying."])
        return "\n".join(lines)

    shortlisted = [
        row["hotel_code"]
        for row in conn.execute(
            "SELECT hotel_code FROM track_hotel WHERE track_id = ? AND shortlisted = 1",
            (track_id,),
        )
    ]

    if not shortlisted:
        best = min(
            priced,
            key=lambda row: (int(row["price_cents"]), row["arrival_date"]),
        )
        arrival = date.fromisoformat(best["arrival_date"])
        price = int(best["price_cents"]) / 100
        lines.extend(
            [
                "",
                f"Cheapest: {hotel_catalog.label(best['hotel_code'])} "
                f"{arrival:%d %b} — €{price:,.2f}",
                "",
                "No hotels are shortlisted; showing the overall cheapest.",
            ]
        )
        return "\n".join(lines)

    entries = []
    for code in shortlisted:
        candidates = [row for row in priced if row["hotel_code"] == code]
        best = (
            min(
                candidates,
                key=lambda row: (int(row["price_cents"]), row["arrival_date"]),
            )
            if candidates
            else None
        )
        entries.append((code, best))
    entries.sort(
        key=lambda item: (
            item[1] is None,
            int(item[1]["price_cents"]) if item[1] is not None else 0,
            hotel_catalog.label(item[0]),
        )
    )

    lines.append("")
    for code, best in entries:
        name = hotel_catalog.label(code)
        if best is None:
            lines.append(f"{name} — no current price")
        else:
            arrival = date.fromisoformat(best["arrival_date"])
            price = int(best["price_cents"]) / 100
            lines.append(f"{name} — {arrival:%d %b} — €{price:,.2f}")
    return "\n".join(lines)


def build_first_checks(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> list[str]:
    """One message per active track that has data and has never been announced."""
    now = now or utcnow()
    ensure_backfill(conn, now=now)

    pending = conn.execute(
        """
        SELECT t.id, t.name
        FROM track t
        WHERE t.status = 'active' AND t.archived_at IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM alert_state a WHERE a.key = 'first_check:' || t.id
          )
          AND EXISTS (
            SELECT 1 FROM track_series ts
            JOIN observation o ON o.series_id = ts.series_id
            WHERE ts.track_id = t.id AND ts.watched = 1
          )
        ORDER BY t.id
        """
    ).fetchall()

    messages = []
    for track in pending:
        messages.append(
            f"🎢 {track['name']} — first check done\n"
            f"{_first_check_body(conn, int(track['id']))}"
        )
        # Bookkeeping, not delivery state: a failed send is logged like any
        # other alert and is not retried forever.
        state.set(conn, f"first_check:{int(track['id'])}", "sent", now=now)
    return messages


def build_test_summary(
    conn: sqlite3.Connection,
    track_id: int,
    *,
    requests: int,
    changes: int,
    errors: int,
    now: datetime | None = None,
) -> str | None:
    """A forced-check message for test mode; sent even when nothing moved."""
    row = conn.execute("SELECT name FROM track WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        return None
    lines = [
        f"🧪 Test check — {row['name']}",
        f"{requests} requests · {changes} changes · {errors} errors",
        "",
        _first_check_body(conn, track_id),
    ]
    return "\n".join(lines)
