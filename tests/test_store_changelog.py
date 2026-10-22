"""The change-log contract.

These tests are the reason M2 exists. If any of them fails, everything built on
top is drawing a graph of something we do not understand:

- replaying the same response must write nothing
- a cent of movement must write exactly one row
- a day of silence must write a heartbeat, once
- a disappearing date is sold out, but only if it was ever there
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import config, db
from app.dlp.packages import PackageCalendar, PackageDay
from app.dlp.tickets import TicketCalendar, TicketDay
from app.tracking import store, tracks


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def month_start(offset_days: int = 62) -> date:
    """A month comfortably inside the horizon, so the tests never rot."""
    return (date.today().replace(day=1) + timedelta(days=offset_days)).replace(day=1)


def package_calendar(
    prices: dict[date, Decimal],
    *,
    hotel: str = "DCHH",
    market: str = "es-es",
    los: str = "2n_3j",
    month: date | None = None,
) -> PackageCalendar:
    days = {
        arrival: PackageDay(
            arrival=arrival,
            price=price,
            room_code="HD",
            rate_category="2",
            is_best_price=True,
        )
        for arrival, price in prices.items()
    }
    return PackageCalendar(
        hotel=hotel,
        market=market,
        los=los,
        currency=config.MARKET_CURRENCY[market],
        month=month or month_start(),
        days=days,
        rooms=(),
        errors=(),
    )


def make_track(conn: sqlite3.Connection, *, arrivals: list[date], hotel: str = "DCHH") -> int:
    return tracks.create_track(
        conn,
        tracks.TrackSpec(
            name="feb test",
            adults=2,
            arrival_start=arrivals[0],
            arrival_end=arrivals[-1],
            hotels=((hotel, True),),
        ),
    )


def record(conn, cal, *, now=None):
    """Record a package calendar for the party in the database."""
    party_id = conn.execute("SELECT id FROM party").fetchone()["id"]
    return store.record_package_calendar(conn, cal, party_id=party_id, now=now)


def observation_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"])


def test_first_poll_writes_one_row_per_arrival(conn: sqlite3.Connection) -> None:
    base = month_start()
    arrivals = [base, base + timedelta(days=1), base + timedelta(days=2)]
    make_track(conn, arrivals=arrivals)

    cal = package_calendar({a: Decimal("600.00") for a in arrivals})
    written = record(conn, cal)

    assert len(written) == 3
    assert {w.kind for w in written} == {"first"}
    assert observation_count(conn) == 3


def test_replaying_the_same_response_writes_nothing(conn: sqlite3.Connection) -> None:
    base = month_start()
    arrivals = [base, base + timedelta(days=1)]
    make_track(conn, arrivals=arrivals)
    cal = package_calendar({a: Decimal("600.00") for a in arrivals})

    now = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
    record(conn, cal, now=now)

    for hours in (1, 2, 3):
        written = record(
            conn, cal, now=now + timedelta(hours=hours)
        )
        assert written == []

    assert observation_count(conn) == 2


def test_one_cent_of_movement_writes_exactly_one_row(conn: sqlite3.Connection) -> None:
    base = month_start()
    arrivals = [base, base + timedelta(days=1)]
    make_track(conn, arrivals=arrivals)

    now = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
    record(
        conn, package_calendar({a: Decimal("600.00") for a in arrivals}), now=now
    )

    moved = {arrivals[0]: Decimal("600.01"), arrivals[1]: Decimal("600.00")}
    written = record(
        conn, package_calendar(moved), now=now + timedelta(hours=1)
    )

    assert len(written) == 1
    assert written[0].kind == "change"
    assert written[0].arrival == arrivals[0]
    assert written[0].price == Decimal("600.01")
    assert written[0].previous_price == Decimal("600.00")
    assert observation_count(conn) == 3


def test_a_day_of_silence_writes_one_heartbeat(conn: sqlite3.Connection) -> None:
    base = month_start()
    arrivals = [base, base + timedelta(days=1)]
    make_track(conn, arrivals=arrivals)
    cal = package_calendar({a: Decimal("600.00") for a in arrivals})

    now = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
    record(conn, cal, now=now)

    written = record(conn, cal, now=now + timedelta(hours=25))

    assert len(written) == 2
    assert {w.kind for w in written} == {"heartbeat"}

    # ...and only once: a second poll inside the next 24h writes nothing.
    assert record(conn, cal, now=now + timedelta(hours=26)) == []


def test_a_disappearing_date_becomes_sold_out(conn: sqlite3.Connection) -> None:
    base = month_start()
    arrivals = [base, base + timedelta(days=1)]
    make_track(conn, arrivals=arrivals)

    now = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
    record(
        conn, package_calendar({a: Decimal("600.00") for a in arrivals}), now=now
    )

    # The upstream simply omits one date.
    written = record(
        conn, package_calendar({arrivals[1]: Decimal("600.00")}), now=now + timedelta(hours=6)
    )

    assert len(written) == 1
    assert written[0].arrival == arrivals[0]
    assert written[0].available is False
    assert written[0].price is None


def test_never_seen_date_is_not_recorded_as_sold_out(conn: sqlite3.Connection) -> None:
    """Occupancy mismatches must stay distinguishable from sold-out dates."""
    base = month_start()
    arrivals = [base, base + timedelta(days=1)]
    make_track(conn, arrivals=arrivals)

    # Upstream returns nothing at all - e.g. the party is too big for the hotel.
    written = record(conn, package_calendar({}))
    assert written == []
    assert observation_count(conn) == 0


def test_sold_out_then_back_available(conn: sqlite3.Connection) -> None:
    base = month_start()
    arrivals = [base]
    make_track(conn, arrivals=arrivals)

    now = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
    record(conn, package_calendar({base: Decimal("600.00")}), now=now)
    record(conn, package_calendar({}), now=now + timedelta(hours=1))
    written = record(
        conn, package_calendar({base: Decimal("650.00")}), now=now + timedelta(hours=2)
    )

    assert len(written) == 1
    assert written[0].available is True
    assert written[0].price == Decimal("650.00")
    assert written[0].previous_price is None


def test_a_response_for_one_party_never_touches_another(conn: sqlite3.Connection) -> None:
    """A 2a1c quote must not be written onto a 2a0c series."""
    base = month_start()
    arrivals = [base]
    couple = make_track(conn, arrivals=arrivals)
    family = tracks.create_track(
        conn,
        tracks.TrackSpec(
            name="family",
            adults=2,
            child_birthdates=("2020-01-01",),
            arrival_start=base,
            arrival_end=base,
            hotels=(("DCHH", True),),
        ),
    )

    couple_party = conn.execute(
        "SELECT party_id FROM track WHERE id = ?", (couple,)
    ).fetchone()["party_id"]
    family_party = conn.execute(
        "SELECT party_id FROM track WHERE id = ?", (family,)
    ).fetchone()["party_id"]
    assert couple_party != family_party

    written = store.record_package_calendar(
        conn, package_calendar({base: Decimal("812.62")}), party_id=couple_party
    )

    assert len(written) == 1
    family_series = conn.execute(
        "SELECT id FROM series WHERE party_id = ?", (family_party,)
    ).fetchone()["id"]
    assert store.latest_observation(conn, family_series) is None


def test_two_tracks_share_one_series_and_one_history(conn: sqlite3.Connection) -> None:
    base = month_start()
    arrivals = [base]
    first = make_track(conn, arrivals=arrivals)
    second = make_track(conn, arrivals=arrivals)

    assert first != second
    assert conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"] == 1

    record(conn, package_calendar({base: Decimal("600.00")}))

    # Both tracks now see the same observation, because they point at one series.
    assert observation_count(conn) == 1


# --- ticket prices ---------------------------------------------------------


def ticket_calendar(prices: dict[date, tuple[Decimal, Decimal]]) -> TicketCalendar:
    days = {
        visit: TicketDay(visit_start=visit, adult=adult, child=child, available=True)
        for visit, (adult, child) in prices.items()
    }
    return TicketCalendar(
        market="es-es",
        currency="EUR",
        product_type="TKITL34NP",
        duration_days=3,
        parks=2,
        days=days,
    )


def test_ticket_party_total_is_stored(conn: sqlite3.Connection) -> None:
    party_id = store.get_or_create_party(conn, adults=2, child_birthdates=("2020-01-01",))
    visit = month_start()

    store.record_ticket_calendar(
        conn,
        ticket_calendar({visit: (Decimal("280.00"), Decimal("265.00"))}),
        adults=2,
        children=1,
        party_id=party_id,
    )

    row = conn.execute("SELECT * FROM ticket_price").fetchone()
    assert row["total_cents"] == 82500  # 280 + 280 + 265
    assert row["adult_cents"] == 28000
    assert row["child_cents"] == 26500
    assert row["duration_days"] == 3
    assert row["kind"] == "first"


def test_ticket_replay_writes_nothing(conn: sqlite3.Connection) -> None:
    party_id = store.get_or_create_party(conn, adults=2, child_birthdates=())
    visit = month_start()
    cal = ticket_calendar({visit: (Decimal("280.00"), Decimal("265.00"))})
    now = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)

    store.record_ticket_calendar(conn, cal, adults=2, children=0, party_id=party_id, now=now)
    written = store.record_ticket_calendar(
        conn, cal, adults=2, children=0, party_id=party_id, now=now + timedelta(hours=2)
    )

    assert written == []
    assert conn.execute("SELECT COUNT(*) AS n FROM ticket_price").fetchone()["n"] == 1
