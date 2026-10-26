"""First-check and test-mode messages."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db
from app.alerts.summary import (
    build_first_checks,
    build_test_summary,
    ensure_backfill,
)
from app.tracking import tracks


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def make_track(
    conn: sqlite3.Connection,
    *,
    name: str = "February",
    hotel: str = "DCHH",
    start: date | None = None,
    markets: tuple[tuple[str, int], ...] = (("es-es", 6),),
    hotels: tuple[tuple[str, bool], ...] | None = None,
) -> int:
    start = start or date.today() + timedelta(days=40)
    return tracks.create_track(
        conn,
        tracks.TrackSpec(
            name=name,
            adults=2,
            arrival_start=start,
            arrival_end=start,
            hotels=hotels or ((hotel, True),),
            markets=markets,
        ),
    )


def seed_price(
    conn: sqlite3.Connection,
    track_id: int,
    cents: int = 64000,
    *,
    hotel: str = "DCHH",
) -> None:
    series_id = conn.execute(
        """
        SELECT s.id FROM series s
        JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ?
        WHERE s.market = 'es-es' AND s.hotel_code = ?
        ORDER BY s.arrival_date LIMIT 1
        """,
        (track_id, hotel),
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
        VALUES (?, ?, 1, ?, 'first')
        """,
        (series_id, cents, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


def test_the_first_check_is_announced_once_after_data(conn: sqlite3.Connection) -> None:
    ensure_backfill(conn)  # no tracks yet
    track_id = make_track(conn)

    assert build_first_checks(conn) == [], "wait for the first observation"
    seed_price(conn, track_id)

    messages = build_first_checks(conn)
    assert len(messages) == 1
    assert "first check done" in messages[0]
    assert "1 arrival priced" in messages[0]
    assert "Disney Hotel Cheyenne" in messages[0]
    assert "€640.00" in messages[0]

    assert build_first_checks(conn) == [], "only once per track"


def test_the_first_check_lists_every_shortlisted_hotel(
    conn: sqlite3.Connection,
) -> None:
    ensure_backfill(conn)
    track_id = make_track(
        conn, hotels=(("DCHH", True), ("DSFH", True))
    )
    seed_price(conn, track_id, 70000, hotel="DCHH")
    seed_price(conn, track_id, 65000, hotel="DSFH")

    message = build_first_checks(conn)[0]

    assert "2 arrivals priced" in message
    assert "Disney Hotel Santa Fe" in message
    assert "Disney Hotel Cheyenne" in message
    assert message.index("Disney Hotel Santa Fe") < message.index("Disney Hotel Cheyenne")


def test_the_first_check_falls_back_to_the_overall_cheapest(
    conn: sqlite3.Connection,
) -> None:
    ensure_backfill(conn)
    track_id = make_track(conn, hotels=(("DCHH", False),))
    seed_price(conn, track_id, 70000)

    message = build_first_checks(conn)[0]

    assert "No hotels are shortlisted" in message
    assert "Cheapest: Disney Hotel Cheyenne" in message


def test_tracks_that_predate_the_feature_are_not_announced(
    conn: sqlite3.Connection,
) -> None:
    old = make_track(conn, name="Old")
    seed_price(conn, old)
    ensure_backfill(conn)

    assert build_first_checks(conn) == []

    new = make_track(conn, name="New", hotel="DSFH")
    seed_price(conn, new, hotel="DSFH")
    messages = build_first_checks(conn)
    assert len(messages) == 1
    assert "New" in messages[0]


def test_the_summary_only_compares_the_primary_market(
    conn: sqlite3.Connection,
) -> None:
    ensure_backfill(conn)
    track_id = make_track(
        conn, markets=(("es-es", 6), ("en-gb", 24))
    )
    for row in conn.execute(
        """
        SELECT s.id, s.market FROM series s
        JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ?
        """,
        (track_id,),
    ).fetchall():
        # GBP looks cheaper by raw cents; it must not win.
        cents = 50000 if row["market"] == "en-gb" else 70000
        conn.execute(
            """
            INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
            VALUES (?, ?, 1, ?, 'first')
            """,
            (row["id"], cents, datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()

    messages = build_first_checks(conn)

    assert len(messages) == 1
    assert "€700.00" in messages[0]
    assert "€500.00" not in messages[0]


def test_the_test_summary_uses_the_first_check_layout(
    conn: sqlite3.Connection,
) -> None:
    track_id = make_track(conn, name="Test trip")
    seed_price(conn, track_id, 61500)

    message = build_test_summary(
        conn, track_id, requests=24, changes=3, errors=0
    )

    assert message is not None
    assert "Test check" in message
    assert "24 requests · 3 changes · 0 errors" in message
    assert "1 arrival priced" in message
    assert "Disney Hotel Cheyenne" in message
    assert "€615.00" in message
