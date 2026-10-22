"""Track creation, validation, window edits and stop."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from app import db
from app.tracking import tracks


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def start_date(offset_days: int = 40) -> date:
    return date.today() + timedelta(days=offset_days)


def spec(**overrides) -> tracks.TrackSpec:
    start = overrides.pop("start", start_date())
    end = overrides.pop("end", start + timedelta(days=6))
    values = {
        "name": "Feb half term",
        "adults": 2,
        "arrival_start": start,
        "arrival_end": end,
        "hotels": (("DCHH", True), ("DSFH", False)),
        "child_birthdates": ("2020-05-01",),
    }
    values.update(overrides)
    return tracks.TrackSpec(**values)


def series_count(conn: sqlite3.Connection, track_id: int) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM track_series WHERE track_id = ?", (track_id,)
        ).fetchone()["n"]
    )


def test_create_materialises_every_hotel_market_and_arrival(conn: sqlite3.Connection) -> None:
    track_id = tracks.create_track(conn, spec())

    # 7 arrivals x 2 hotels x 1 market
    assert series_count(conn, track_id) == 14
    row = conn.execute("SELECT * FROM track WHERE id = ?", (track_id,)).fetchone()
    assert row["status"] == "active"
    assert row["min_delta_cents"] == 2000


def test_creating_the_same_party_twice_reuses_the_party_row(conn: sqlite3.Connection) -> None:
    tracks.create_track(conn, spec(name="one"))
    tracks.create_track(conn, spec(name="two"))

    assert conn.execute("SELECT COUNT(*) AS n FROM party").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"] == 14


def test_child_birthdates_are_ordered_canonically(conn: sqlite3.Connection) -> None:
    tracks.create_track(conn, spec(child_birthdates=("2020-05-01", "2018-01-01")))
    tracks.create_track(conn, spec(name="other", child_birthdates=("2018-01-01", "2020-05-01")))

    assert conn.execute("SELECT COUNT(*) AS n FROM party").fetchone()["n"] == 1


def test_window_wider_than_the_cap_is_rejected(conn: sqlite3.Connection) -> None:
    start = start_date()
    with pytest.raises(ValueError, match="cap is 31"):
        tracks.create_track(conn, spec(start=start, end=start + timedelta(days=40)))


def test_window_in_the_past_is_rejected(conn: sqlite3.Connection) -> None:
    past = date.today() - timedelta(days=30)
    with pytest.raises(ValueError, match="past"):
        tracks.create_track(conn, spec(start=past, end=past + timedelta(days=3)))


def test_window_past_the_horizon_is_rejected(conn: sqlite3.Connection) -> None:
    far = date.today() + timedelta(days=700)
    with pytest.raises(ValueError, match="horizon"):
        tracks.create_track(conn, spec(start=far, end=far + timedelta(days=3)))


def test_unknown_hotel_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown hotel"):
        tracks.create_track(conn, spec(hotels=(("XXXX", True),)))


def test_unknown_market_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown market"):
        tracks.create_track(conn, spec(markets=(("xx-xx", 6),)))


def test_window_ends_before_it_starts_is_rejected(conn: sqlite3.Connection) -> None:
    start = start_date()
    with pytest.raises(ValueError, match="before it starts"):
        tracks.create_track(conn, spec(start=start, end=start - timedelta(days=1)))


def test_trimming_stops_watching_without_deleting_history(conn: sqlite3.Connection) -> None:
    track_id = tracks.create_track(conn, spec())
    assert series_count(conn, track_id) == 14

    start = start_date()
    result = tracks.set_arrival_window(
        conn, track_id, start, start + timedelta(days=2)
    )
    assert result["untrimmed"] == 0

    watched = conn.execute(
        "SELECT COUNT(*) AS n FROM track_series WHERE track_id = ? AND watched = 1",
        (track_id,),
    ).fetchone()["n"]
    assert watched == 6  # 3 arrivals x 2 hotels
    # Nothing was deleted: series history is still addressable.
    assert series_count(conn, track_id) == 14


def test_untrimming_restores_the_same_series(conn: sqlite3.Connection) -> None:
    track_id = tracks.create_track(conn, spec())
    start = start_date()

    tracks.set_arrival_window(conn, track_id, start, start + timedelta(days=2))
    result = tracks.set_arrival_window(conn, track_id, start, start + timedelta(days=6))

    watched = conn.execute(
        "SELECT COUNT(*) AS n FROM track_series WHERE track_id = ? AND watched = 1",
        (track_id,),
    ).fetchone()["n"]
    assert watched == 14
    assert result["added"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"] == 14


def test_extending_adds_only_new_series(conn: sqlite3.Connection) -> None:
    start = start_date()
    track_id = tracks.create_track(conn, spec(start=start, end=start + timedelta(days=2)))

    tracks.set_arrival_window(conn, track_id, start, start + timedelta(days=6))

    assert conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"] == 14


def test_stop_is_terminal_and_history_survives(conn: sqlite3.Connection) -> None:
    track_id = tracks.create_track(conn, spec())
    tracks.stop_track(conn, track_id)

    row = conn.execute("SELECT * FROM track WHERE id = ?", (track_id,)).fetchone()
    assert row["status"] == "stopped"
    assert row["stopped_at"] is not None
    assert series_count(conn, track_id) == 14

    with pytest.raises(ValueError, match="stopped"):
        tracks.set_arrival_window(conn, track_id, start_date(), start_date() + timedelta(days=2))


def test_stopping_twice_is_idempotent(conn: sqlite3.Connection) -> None:
    track_id = tracks.create_track(conn, spec())
    tracks.stop_track(conn, track_id)
    first = conn.execute("SELECT stopped_at FROM track WHERE id = ?", (track_id,)).fetchone()[0]

    tracks.stop_track(conn, track_id)
    second = conn.execute("SELECT stopped_at FROM track WHERE id = ?", (track_id,)).fetchone()[0]

    assert first == second


def test_shortlist_can_be_toggled(conn: sqlite3.Connection) -> None:
    track_id = tracks.create_track(conn, spec())
    tracks.set_shortlist(conn, track_id, "DSFH", True)

    rows = {
        row["hotel_code"]: row["shortlisted"]
        for row in conn.execute(
            "SELECT hotel_code, shortlisted FROM track_hotel WHERE track_id = ?", (track_id,)
        )
    }
    assert rows == {"DCHH": 1, "DSFH": 1}


def test_children_under_three_are_left_out_of_the_price_request(
    conn: sqlite3.Connection,
) -> None:
    """Under-3s are free and have no field upstream, so they are not in the party."""
    start = start_date()
    toddler = (start - timedelta(days=800)).isoformat()

    track_id = tracks.create_track(conn, spec(start=start, child_birthdates=(toddler,)))

    party = conn.execute(
        "SELECT p.* FROM party p JOIN track t ON t.party_id = p.id WHERE t.id = ?",
        (track_id,),
    ).fetchone()
    assert party["adults"] == 2
    assert party["child_birthdates"] == "[]"


def test_a_child_who_turns_three_during_the_window_is_refused(
    conn: sqlite3.Connection,
) -> None:
    start = start_date()
    turns_three = start - timedelta(days=3 * 365) + timedelta(days=3)

    with pytest.raises(ValueError, match="turns 3 during the window"):
        tracks.create_track(
            conn, spec(start=start, end=start + timedelta(days=6), child_birthdates=(turns_three.isoformat(),))
        )
