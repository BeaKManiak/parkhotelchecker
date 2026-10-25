"""Latest-price comparison across markets."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db
from app.tracking import tracks
from app.web import queries
from app.web.comparison import build as build_comparison


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def seed(conn: sqlite3.Connection, *, markets=("es-es", "fr-fr", "en-gb"), days=2) -> tuple[int, date]:
    start = date.today() + timedelta(days=40)
    track_id = tracks.create_track(
        conn,
        tracks.TrackSpec(
            name="compare",
            adults=2,
            arrival_start=start,
            arrival_end=start + timedelta(days=days - 1),
            hotels=(("DCHH", True),),
            markets=tuple((market, 72 if market != "es-es" else 6) for market in markets),
        ),
    )
    return track_id, start


def observe(conn: sqlite3.Connection, series_id: int, cents: int | None) -> None:
    conn.execute(
        """
        INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
        VALUES (?, ?, ?, ?, 'first')
        """,
        (
            series_id,
            cents,
            0 if cents is None else 1,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def series_for(conn: sqlite3.Connection, track_id: int, market: str, arrival: date) -> int:
    return int(
        conn.execute(
            """
            SELECT s.id FROM series s
            JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ?
            WHERE s.hotel_code = 'DCHH' AND s.market = ? AND s.arrival_date = ?
            """,
            (track_id, market, arrival.isoformat()),
        ).fetchone()["id"]
    )


def test_the_view_collapses_identical_eur_markets(conn: sqlite3.Connection) -> None:
    track_id, start = seed(conn)
    for market, cents in (("es-es", 70000), ("fr-fr", 70000), ("en-gb", 60000)):
        observe(conn, series_for(conn, track_id, market, start), cents)

    rows = queries.comparison(conn, track_id, "DCHH")
    view = build_comparison(
        rows, [{"market": "es-es"}, {"market": "fr-fr"}, {"market": "en-gb"}]
    )

    assert view.reference == "es-es"
    assert view.differing == ()
    assert view.matched == ("fr-fr",)
    assert [column.market for column in view.columns] == ["es-es", "en-gb"]
    assert [column.market for column in view.all_columns] == [
        "es-es",
        "fr-fr",
        "en-gb",
    ]
    assert view.columns[1].status == "other"


def test_a_market_that_differs_gets_a_column(conn: sqlite3.Connection) -> None:
    track_id, start = seed(conn)
    for market, cents in (("es-es", 70000), ("fr-fr", 70000), ("en-gb", 60000)):
        observe(conn, series_for(conn, track_id, market, start), cents)
    # A later observation makes fr-fr €100 more expensive than ES.
    observe(conn, series_for(conn, track_id, "fr-fr", start), 80000)

    rows = queries.comparison(conn, track_id, "DCHH")
    view = build_comparison(
        rows, [{"market": "es-es"}, {"market": "fr-fr"}, {"market": "en-gb"}]
    )

    assert view.differing == ("fr-fr",)
    assert view.matched == ()
    assert [column.market for column in view.columns] == [
        "es-es",
        "fr-fr",
        "en-gb",
    ]
    assert view.columns[1].status == "differs"


def test_comparison_groups_latest_prices_by_arrival(conn: sqlite3.Connection) -> None:
    track_id, start = seed(conn)
    second = start + timedelta(days=1)

    observe(conn, series_for(conn, track_id, "es-es", start), 70000)
    observe(conn, series_for(conn, track_id, "fr-fr", start), 69000)
    observe(conn, series_for(conn, track_id, "en-gb", start), 60000)
    observe(conn, series_for(conn, track_id, "es-es", second), 71000)
    observe(conn, series_for(conn, track_id, "fr-fr", second), None)

    rows = queries.comparison(conn, track_id, "DCHH")

    assert [row.arrival for row in rows] == [start, second]
    first = rows[0].cells
    assert set(first) == {"es-es", "fr-fr", "en-gb"}
    assert first["es-es"].price == queries.from_cents(70000)
    assert first["fr-fr"].price == queries.from_cents(69000)
    assert first["en-gb"].currency == "GBP"
    assert first["en-gb"].observed is True

    second_cells = rows[1].cells
    assert second_cells["es-es"].price == queries.from_cents(71000)
    assert second_cells["fr-fr"].sold_out is True
    assert second_cells["en-gb"].observed is False
    assert second_cells["en-gb"].price is None
