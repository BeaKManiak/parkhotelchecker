"""Digest line selection and failure alerts.

These tests do not send anything. They prove the message that *would* be sent:
which moves earn a line, which stay silent, and when a failing source crosses
the threshold into an alert (exactly once, plus one recovery).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import db
from app.alerts import failures
from app.alerts.digest import build_digest
from app.tracking import health, tracks
from app.tracking.poller import PollResult
from app.tracking.store import Written, utcnow


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def make_track(
    conn: sqlite3.Connection,
    *,
    name: str = "February",
    hotels: tuple[tuple[str, bool], ...] = (("DCHH", True), ("DSFH", False)),
    markets: tuple[tuple[str, int], ...] = (("es-es", 6),),
    days: int = 2,
    min_delta_cents: int = 2000,
) -> int:
    start = date.today() + timedelta(days=40)
    return tracks.create_track(
        conn,
        tracks.TrackSpec(
            name=name,
            adults=2,
            child_birthdates=("2019-01-01",),
            arrival_start=start,
            arrival_end=start + timedelta(days=days - 1),
            hotels=hotels,
            markets=markets,
            min_delta_cents=min_delta_cents,
        ),
    )


def series_ids(
    conn: sqlite3.Connection, track_id: int, *, hotel: str = "DCHH", market: str = "es-es"
) -> list[int]:
    rows = conn.execute(
        """
        SELECT s.id FROM series s
        JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ?
        WHERE s.hotel_code = ? AND s.market = ?
        ORDER BY s.arrival_date
        """,
        (track_id, hotel, market),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def write_observation(
    conn: sqlite3.Connection,
    series_id: int,
    price: Decimal | None,
    *,
    when: datetime,
    available: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
        VALUES (?, ?, ?, ?, 'change')
        """,
        (
            series_id,
            None if price is None else int(price * 100),
            1 if available else 0,
            when.isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def change(series_id: int) -> PollResult:
    return PollResult(
        requests=1,
        written=[
            Written(
                series_id=series_id,
                kind="change",
                arrival=None,
                price=None,
                available=True,
                previous_price=None,
            )
        ],
        changes=[
            Written(
                series_id=series_id,
                kind="change",
                arrival=None,
                price=None,
                available=True,
                previous_price=None,
            )
        ],
    )


def history(conn, series_id: int, prices: list[Decimal | None]) -> None:
    start = datetime.now(timezone.utc) - timedelta(days=5)
    for index, price in enumerate(prices):
        write_observation(
            conn,
            series_id,
            price,
            when=start + timedelta(hours=index),
            available=price is not None,
        )


# --- digest selection ------------------------------------------------------


def test_no_changes_means_no_message(conn: sqlite3.Connection) -> None:
    make_track(conn)
    assert build_digest(conn, PollResult(), now=utcnow()) is None


def test_a_drop_larger_than_the_threshold_gets_a_line(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn)
    series_id = series_ids(conn, track_id)[0]
    history(conn, series_id, [Decimal("820.00"), Decimal("770.00")])

    message = build_digest(conn, change(series_id), now=utcnow())

    assert message is not None
    assert "February" in message
    assert "2n/3j" in message
    assert "2a1c" in message
    assert "▼" in message
    assert "€820 → €770" in message
    assert "(−€50" in message
    assert "all-time low" in message


def test_a_small_rise_that_is_not_a_low_is_silent(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn)
    series_id = series_ids(conn, track_id)[0]
    # 800 → 810 → 805: the last move is −5, still above the all-time low of 800.
    history(conn, series_id, [Decimal("800.00"), Decimal("810.00"), Decimal("805.00")])

    assert build_digest(conn, change(series_id), now=utcnow()) is None


def test_a_new_low_alerts_even_below_the_threshold(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn)
    series_id = series_ids(conn, track_id)[0]
    history(conn, series_id, [Decimal("800.00"), Decimal("810.00"), Decimal("795.00")])

    message = build_digest(conn, change(series_id), now=utcnow())

    assert message is not None
    assert "€810 → €795" in message
    assert "all-time low" in message


def test_sold_out_and_back_available(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn)
    series_id = series_ids(conn, track_id)[0]
    history(conn, series_id, [Decimal("700.00"), None])

    sold_out = build_digest(conn, change(series_id), now=utcnow())
    assert sold_out is not None
    assert "sold out" in sold_out
    assert "was €700" in sold_out

    write_observation(conn, series_id, Decimal("690.00"), when=datetime.now(timezone.utc))
    back = build_digest(conn, change(series_id), now=utcnow())
    assert back is not None
    assert "back available at €690" in back


def test_canary_market_changes_never_alert(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn, markets=(("es-es", 6), ("en-gb", 24)))
    canary = series_ids(conn, track_id, market="en-gb")[0]
    history(conn, canary, [Decimal("719.84"), Decimal("619.84")])

    assert build_digest(conn, change(canary), now=utcnow()) is None


def test_non_shortlisted_hotels_are_silent(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn, hotels=(("DCHH", False),))
    series_id = series_ids(conn, track_id)[0]
    history(conn, series_id, [Decimal("820.00"), Decimal("700.00")])

    assert build_digest(conn, change(series_id), now=utcnow()) is None


def test_stopped_tracks_are_silent(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn)
    series_id = series_ids(conn, track_id)[0]
    history(conn, series_id, [Decimal("820.00"), Decimal("700.00")])
    tracks.stop_track(conn, track_id)

    assert build_digest(conn, change(series_id), now=utcnow()) is None


def test_the_digest_is_capped_and_says_how_much_it_hid(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn, days=14)
    for series_id in series_ids(conn, track_id):
        history(conn, series_id, [Decimal("800.00"), Decimal("700.00")])

    result = PollResult(changes=[])
    for series_id in series_ids(conn, track_id):
        result.changes.extend(change(series_id).changes)

    message = build_digest(conn, result, now=utcnow())

    assert message is not None
    assert message.count("▼") == 12
    assert "… and 2 more" in message


def test_two_tracks_sharing_a_series_both_hear_about_it(conn: sqlite3.Connection) -> None:
    first = make_track(conn, name="Hers")
    second = make_track(conn, name="His")
    assert first != second

    series_id = series_ids(conn, first)[0]
    assert series_id in series_ids(conn, second)
    history(conn, series_id, [Decimal("820.00"), Decimal("700.00")])

    message = build_digest(conn, change(series_id), now=utcnow())

    assert message is not None
    assert "Hers" in message and "His" in message


def test_best_arrival_shows_ticket_and_implied_cost(conn: sqlite3.Connection) -> None:
    track_id = make_track(conn)
    series_id = series_ids(conn, track_id)[0]
    history(conn, series_id, [Decimal("820.00"), Decimal("700.00")])

    arrival = conn.execute(
        "SELECT arrival_date, party_id FROM series WHERE id = ?", (series_id,)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO ticket_price (market, currency, visit_start, duration_days, parks,
          product_type, party_id, adult_cents, child_cents, total_cents, available,
          observed_at, kind)
        VALUES ('es-es', 'EUR', ?, 3, 2, 'TKITL34NP', ?, 28000, 26500, 54900, 1, ?, 'first')
        """,
        (arrival["arrival_date"], arrival["party_id"], utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()

    message = build_digest(conn, change(series_id), now=utcnow())

    assert message is not None
    assert "Best arrival this window" in message
    assert "package €700" in message
    assert "tickets €549" in message
    assert "implied hotel €151" in message


def test_the_footer_reports_how_long_since_a_successful_check(
    conn: sqlite3.Connection,
) -> None:
    track_id = make_track(conn)
    series_id = series_ids(conn, track_id)[0]
    history(conn, series_id, [Decimal("820.00"), Decimal("700.00")])
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO health (source, last_success_at, consecutive_failures) VALUES ('packages', ?, 0)",
        ((now - timedelta(hours=2)).isoformat(timespec="seconds"),),
    )
    conn.commit()

    # A successful Telegram send is not a successful price check.
    conn.execute(
        "INSERT INTO health (source, last_success_at, consecutive_failures) VALUES ('telegram', ?, 0)",
        (now.isoformat(timespec="seconds"),),
    )
    conn.commit()

    message = build_digest(conn, change(series_id), now=now)

    assert message is not None
    assert "Last successful check: 2 h ago" in message


# --- failure alerts --------------------------------------------------------


def test_three_consecutive_failures_alert_once_then_recover(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    for _ in range(2):
        health.record_failure(conn, "packages", "HTTP 503", now=now)

    assert failures.build_failure_alerts(conn, PollResult(), now=now) == []

    health.record_failure(conn, "packages", "HTTP 503", now=now)
    first = failures.build_failure_alerts(conn, PollResult(), now=now)
    assert len(first) == 1
    assert "package API failing" in first[0]
    assert "3 consecutive failures" in first[0]

    health.record_failure(conn, "packages", "HTTP 503", now=now)
    assert failures.build_failure_alerts(conn, PollResult(), now=now) == []

    health.record_success(conn, "packages", now=now)
    recovered = failures.build_failure_alerts(conn, PollResult(), now=now)
    assert len(recovered) == 1
    assert "recovered" in recovered[0]


def test_a_shape_error_alerts_immediately_and_is_throttled(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    result = PollResult(shape_errors=["packages DCHH|es-es|2027-01|2n_3j|1: no 'dates' key"])

    first = failures.build_failure_alerts(conn, result, now=now)
    assert len(first) == 1
    assert "shape changed" in first[0]
    assert "no 'dates' key" in first[0]

    assert failures.build_failure_alerts(conn, result, now=now + timedelta(hours=1)) == []
    later = failures.build_failure_alerts(conn, result, now=now + timedelta(hours=7))
    assert len(later) == 1


def test_a_plu_shape_error_says_the_ticket_line_is_disabled(
    conn: sqlite3.Connection,
) -> None:
    now = datetime.now(timezone.utc)
    result = PollResult(shape_errors=["plu es-es: no product for 3 days"])

    messages = failures.build_failure_alerts(conn, result, now=now)

    assert len(messages) == 1
    assert "Ticket product codes changed" in messages[0]


def test_a_shape_error_does_not_also_raise_the_threshold_alert(
    conn: sqlite3.Connection,
) -> None:
    now = datetime.now(timezone.utc)
    for _ in range(3):
        health.record_failure(conn, "packages", "bad shape", now=now)
    result = PollResult(shape_errors=["packages DCHH|es-es|2027-01|2n_3j|1: bad shape"])

    messages = failures.build_failure_alerts(conn, result, now=now)

    assert len(messages) == 1
    assert "shape changed" in messages[0]


def test_a_failure_reports_that_sources_own_last_success(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO health (source, last_success_at, last_error_at, last_error,
                               consecutive_failures)
           VALUES ('packages', ?, ?, 'HTTP 503', 3)""",
        (
            (now - timedelta(hours=6)).isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
        ),
    )
    conn.execute(
        "INSERT INTO health (source, last_success_at, consecutive_failures) VALUES ('telegram', ?, 0)",
        (now.isoformat(timespec="seconds"),),
    )
    conn.commit()

    messages = failures.build_failure_alerts(conn, PollResult(), now=now)

    assert len(messages) == 1
    assert "Last success: 6 h ago" in messages[0]
    assert "just now" not in messages[0]


def test_telegram_is_never_alerted_about_itself(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    for _ in range(5):
        health.record_failure(conn, "telegram", "send failed", now=now)

    assert failures.build_failure_alerts(conn, PollResult(), now=now) == []
