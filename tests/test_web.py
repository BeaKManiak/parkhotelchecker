"""The dashboard, exercised through the real ASGI app.

No mocking of FastAPI internals: these tests log in over HTTP Basic, submit the
same forms a browser submits, and read the HTML that comes back. The scheduler
is replaced by a fake `check_now`, so nothing here touches the network.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.alerts.telegram import Delivery
from app.dlp import hotels as hotel_catalog
from app.tracking import tracks
from app.web import auth
from app.web.app import create_app

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
AUTH = ("miquel", PASSWORD)


def new_track(conn: sqlite3.Connection, *, name: str = "February half term") -> int:
    start = date.today() + timedelta(days=40)
    return tracks.create_track(
        conn,
        tracks.TrackSpec(
            name=name,
            adults=2,
            child_birthdates=("2019-04-02",),
            arrival_start=start,
            arrival_end=start + timedelta(days=2),
            hotels=(("DCHH", True), ("DSFH", True)),
        ),
    )


def seed_prices(conn: sqlite3.Connection, track_id: int, prices: list[float]) -> None:
    """Write one observation per watched DCHH series at successive timestamps."""
    series = conn.execute(
        """
        SELECT s.id FROM series s
        JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ?
        WHERE s.hotel_code = 'DCHH' ORDER BY s.arrival_date
        """,
        (track_id,),
    ).fetchall()
    base = datetime.now(timezone.utc) - timedelta(hours=6)
    for index, row in enumerate(series):
        for step, price in enumerate(prices):
            conn.execute(
                """
                INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
                VALUES (?, ?, 1, ?, ?)
                """,
                (
                    row["id"],
                    int(price * 100),
                    (base + timedelta(hours=step)).isoformat(timespec="seconds"),
                    "first" if step == 0 else "change",
                ),
            )
    conn.commit()


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF token in the page"
    return match.group(1)


@pytest.fixture
def app_and_conn():
    conn = db.connect(":memory:")
    auth._failures.clear()
    calls = {"check_now": 0, "track_id": None}

    def check_now(track_id: int) -> bool:
        calls["check_now"] += 1
        calls["track_id"] = track_id
        return True

    app = create_app(
        conn=conn, user=AUTH[0], password_hash=PASSWORD_HASH, check_now=check_now
    )
    yield app, conn, calls
    conn.close()


@pytest.fixture
def client(app_and_conn):
    app, conn, calls = app_and_conn
    with TestClient(app) as test_client:
        yield test_client, conn, calls


def test_the_dashboard_requires_a_password(client) -> None:
    http, _, _ = client
    response = http.get("/", follow_redirects=False)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


def test_a_wrong_password_is_rejected(client) -> None:
    http, _, _ = client
    response = http.get("/", auth=(AUTH[0], "wrong"))
    assert response.status_code == 401


def test_the_liveness_probe_needs_no_auth(client) -> None:
    http, _, _ = client
    response = http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_the_track_list_renders(client) -> None:
    http, conn, _ = client
    new_track(conn)

    response = http.get("/", auth=AUTH)

    assert response.status_code == 200
    assert "February half term" in response.text
    assert "Create the first track" not in response.text


def test_security_headers_are_present(client) -> None:
    http, _, _ = client
    response = http.get("/", auth=AUTH)

    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_test_mode_is_visible_in_the_dashboard() -> None:
    conn = db.connect(":memory:")
    app = create_app(
        conn=conn,
        user=AUTH[0],
        password_hash=PASSWORD_HASH,
        check_now=lambda: None,
        test_mode=True,
    )
    with TestClient(app) as http:
        response = http.get("/", auth=AUTH)
    conn.close()

    assert "Test mode" in response.text
    assert "TEST_MODE" in response.text


def test_the_new_track_form_lists_every_hotel(client) -> None:
    http, _, _ = client
    response = http.get("/tracks/new", auth=AUTH)

    assert response.status_code == 200
    assert "Disneyland Hotel" in response.text
    assert "Les Villages Nature Paris" in response.text


def test_the_new_track_form_explains_the_party_and_check_in_window(client) -> None:
    http, _, _ = client
    response = http.get("/tracks/new", auth=AUTH)
    text = response.text

    assert "Earliest check-in" in text
    assert "Latest check-in" in text
    assert "2 nights · 3 park days" in text
    assert "A room holds up to 7 people" in text
    assert "Disney hotels (on-site)" in text
    assert "Partner hotels" in text
    assert 'data-check-all="shortlist"' in text
    assert re.search(r'name="compare_eur"[^>]*checked', text)
    assert "Other EUR markets" in text
    # The shortlist starts empty; all hotels are tracked regardless.
    assert re.search(r'name="shortlist"[^>]*checked', text) is None

    start = date.today() + timedelta(days=1)
    assert re.search(
        r'name="arrival_end"[^>]*min="' + re.escape(start.isoformat()) + '"', text
    )


def test_the_latest_check_in_cannot_be_before_the_earliest(client) -> None:
    http, conn, _ = client
    page = http.get("/tracks/new", auth=AUTH)
    token = csrf_from(page.text)
    start = date.today() + timedelta(days=40)

    response = http.post(
        "/tracks",
        auth=AUTH,
        data={
            "csrf": token,
            "name": "Backwards",
            "adults": "2",
            "children_count": "0",
            "los": "2n_3j",
            "arrival_start": (start + timedelta(days=2)).isoformat(),
            "arrival_end": start.isoformat(),
            "min_delta": "20",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "latest check-in must be on or after the earliest check-in" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM track").fetchone()["n"] == 0


def test_children_count_must_match_the_birthdates(client) -> None:
    http, _, _ = client
    page = http.get("/tracks/new", auth=AUTH)
    token = csrf_from(page.text)
    start = date.today() + timedelta(days=40)

    response = http.post(
        "/tracks",
        auth=AUTH,
        data={
            "csrf": token,
            "name": "Mismatch",
            "adults": "2",
            "children_count": "2",
            "child": ["2018-06-01"],
            "los": "2n_3j",
            "arrival_start": start.isoformat(),
            "arrival_end": (start + timedelta(days=2)).isoformat(),
            "min_delta": "20",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "you said 2 children but gave 1 birthdate" in response.text


def test_a_room_cannot_hold_more_than_seven_people(client) -> None:
    http, conn, _ = client
    page = http.get("/tracks/new", auth=AUTH)
    token = csrf_from(page.text)
    start = date.today() + timedelta(days=40)

    response = http.post(
        "/tracks",
        auth=AUTH,
        data={
            "csrf": token,
            "name": "Too many",
            "adults": "7",
            "children_count": "1",
            "child": ["2018-06-01"],
            "los": "2n_3j",
            "arrival_start": start.isoformat(),
            "arrival_end": (start + timedelta(days=2)).isoformat(),
            "min_delta": "20",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "a room holds at most 7" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM track").fetchone()["n"] == 0


def test_creating_a_track_redirects_to_it(client) -> None:
    http, conn, _ = client
    page = http.get("/tracks/new", auth=AUTH)
    token = csrf_from(page.text)
    start = date.today() + timedelta(days=40)

    response = http.post(
        "/tracks",
        auth=AUTH,
        data={
            "csrf": token,
            "name": "Autumn 2027",
            "adults": "2",
            "los": "3n_4j",
            "arrival_start": start.isoformat(),
            "arrival_end": (start + timedelta(days=3)).isoformat(),
            "min_delta": "25",
            "shortlist": ["DCHH", "STAY"],
            "market": ["en-gb"],
            "children_count": "1",
            "child": ["2018-06-01"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/tracks/")

    track_id = int(response.headers["location"].split("/")[2].split("?")[0])
    row = conn.execute("SELECT * FROM track WHERE id = ?", (track_id,)).fetchone()
    assert row["name"] == "Autumn 2027"
    assert row["los"] == "3n_4j"
    assert row["min_delta_cents"] == 2500

    markets = {
        r["market"]: r["cadence_hours"]
        for r in conn.execute("SELECT market, cadence_hours FROM track_market")
    }
    assert markets == {"es-es": 6, "en-gb": 72}

    tracked = {
        r["hotel_code"] for r in conn.execute("SELECT hotel_code FROM track_hotel")
    }
    assert tracked == {hotel.code for hotel in hotel_catalog.HOTELS}


def test_creating_a_track_can_include_the_eur_comparison_set(client) -> None:
    http, conn, _ = client
    page = http.get("/tracks/new", auth=AUTH)
    token = csrf_from(page.text)
    start = date.today() + timedelta(days=40)

    response = http.post(
        "/tracks",
        auth=AUTH,
        data={
            "csrf": token,
            "name": "With comparison",
            "adults": "2",
            "children_count": "0",
            "compare_eur": "1",
            "los": "2n_3j",
            "arrival_start": start.isoformat(),
            "arrival_end": (start + timedelta(days=2)).isoformat(),
            "min_delta": "20",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    markets = {
        row["market"]: row["cadence_hours"]
        for row in conn.execute("SELECT market, cadence_hours FROM track_market")
    }
    assert set(markets) == {"es-es", *config.COMPARISON_MARKETS}
    assert markets["es-es"] == 6
    assert all(
        markets[market] == config.COMPARISON_CADENCE_HOURS
        for market in config.COMPARISON_MARKETS
    )


def test_adding_comparison_markets_to_an_existing_track(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    page = http.get(f"/tracks/{track_id}", auth=AUTH)
    assert "Add comparison markets (72 h)" in page.text
    token = csrf_from(page.text)

    response = http.post(
        f"/tracks/{track_id}/markets",
        auth=AUTH,
        data={"csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    markets = {
        row["market"]: row["cadence_hours"]
        for row in conn.execute(
            "SELECT market, cadence_hours FROM track_market WHERE track_id = ?",
            (track_id,),
        )
    }
    expected = {
        "es-es",
        *config.COMPARISON_MARKETS,
        *config.CANARY_MARKETS,
    }
    assert set(markets) == expected
    assert markets["es-es"] == 6
    assert all(
        markets[market] == 72
        for market in (*config.COMPARISON_MARKETS, *config.CANARY_MARKETS)
    )

    watched = conn.execute(
        "SELECT COUNT(*) AS n FROM track_series WHERE track_id = ? AND watched = 1",
        (track_id,),
    ).fetchone()["n"]
    assert watched == 2 * 3 * len(expected)  # two hotels x three arrivals x markets

    http.post(
        f"/tracks/{track_id}/markets",
        auth=AUTH,
        data={"csrf": token},
        follow_redirects=False,
    )
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM track_market WHERE track_id = ?", (track_id,)
    ).fetchone()["n"] == len(expected)

    page = http.get(f"/tracks/{track_id}", auth=AUTH)
    assert "market-pill" in page.text
    assert ">Primary<" in page.text
    assert ">Comparison<" in page.text
    assert ">Chart only<" in page.text
    assert "10 EUR markets" in page.text
    assert "Show all" in page.text


def test_a_track_can_start_with_no_alerting_hotels(client) -> None:
    http, conn, _ = client
    page = http.get("/tracks/new", auth=AUTH)
    token = csrf_from(page.text)
    start = date.today() + timedelta(days=40)

    response = http.post(
        "/tracks",
        auth=AUTH,
        data={
            "csrf": token,
            "name": "Silent at first",
            "adults": "2",
            "children_count": "0",
            "los": "2n_3j",
            "arrival_start": start.isoformat(),
            "arrival_end": (start + timedelta(days=2)).isoformat(),
            "min_delta": "20",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    shortlisted = conn.execute(
        "SELECT COUNT(*) AS n FROM track_hotel WHERE shortlisted = 1"
    ).fetchone()["n"]
    assert shortlisted == 0


def test_a_validation_error_re_renders_the_form_with_the_message(client) -> None:
    http, _, _ = client
    page = http.get("/tracks/new", auth=AUTH)
    token = csrf_from(page.text)

    response = http.post(
        "/tracks",
        auth=AUTH,
        data={
            "csrf": token,
            "name": "   ",
            "adults": "2",
            "los": "2n_3j",
            "arrival_start": (date.today() + timedelta(days=10)).isoformat(),
            "arrival_end": (date.today() + timedelta(days=12)).isoformat(),
            "min_delta": "20",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "track needs a name" in response.text


def test_a_post_without_a_csrf_token_is_refused(client) -> None:
    http, conn, _ = client
    before = conn.execute("SELECT COUNT(*) AS n FROM track").fetchone()["n"]

    response = http.post(
        "/tracks",
        auth=AUTH,
        data={"name": "forged", "adults": "2", "arrival_start": "2027-01-01", "arrival_end": "2027-01-02"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert conn.execute("SELECT COUNT(*) AS n FROM track").fetchone()["n"] == before


def test_track_detail_shows_prices_and_the_window(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    seed_prices(conn, track_id, [820.00, 780.00])

    response = http.get(f"/tracks/{track_id}", auth=AUTH)

    assert response.status_code == 200
    assert "Disney Hotel Cheyenne" in response.text
    assert "€780.00" in response.text
    assert "2 nights · 3 park days" in response.text
    assert "Alerting" in response.text
    assert "lowest yet" in response.text


def test_the_first_poll_is_not_called_a_record_low(client) -> None:
    """One observation is trivially the lowest ever; the badge would be noise."""
    http, conn, _ = client
    track_id = new_track(conn)
    seed_prices(conn, track_id, [820.00])

    response = http.get(f"/tracks/{track_id}", auth=AUTH)

    assert "€820.00" in response.text
    assert "lowest yet" not in response.text


def test_flash_messages_are_escaped(client) -> None:
    http, _, _ = client
    response = http.get("/?msg=%3Cscript%3Ealert(1)%3C/script%3E", auth=AUTH)
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_unknown_track_is_a_404(client) -> None:
    http, _, _ = client
    assert http.get("/tracks/999", auth=AUTH).status_code == 404


def test_the_hotel_page_can_switch_market_and_currency(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    tracks.add_markets(conn, track_id, ("en-gb",))
    series_id = conn.execute(
        """
        SELECT s.id FROM series s
        JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ?
        WHERE s.hotel_code = 'DCHH' AND s.market = 'en-gb'
        ORDER BY s.arrival_date LIMIT 1
        """,
        (track_id,),
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
        VALUES (?, 65000, 1, ?, 'first')
        """,
        (series_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()

    response = http.get(
        f"/tracks/{track_id}/hotels/DCHH?market=en-gb", auth=AUTH
    )

    assert response.status_code == 200
    assert "prices in GBP" in response.text
    assert "£650.00" in response.text
    assert "?market=es-es" in response.text
    match = re.search(r"data-chart='([^']+)'", response.text)
    chart = json.loads(match.group(1))
    assert chart["currency"] == "GBP"

    default = http.get(f"/tracks/{track_id}/hotels/DCHH", auth=AUTH)
    assert "prices in EUR" in default.text

    unknown = http.get(
        f"/tracks/{track_id}/hotels/DCHH?market=xx-xx", auth=AUTH
    )
    assert unknown.status_code == 400


def test_hotel_page_carries_the_chart_data(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    seed_prices(conn, track_id, [820.00, 780.00, 799.00])

    response = http.get(f"/tracks/{track_id}/hotels/DCHH", auth=AUTH)

    assert response.status_code == 200
    match = re.search(r"data-chart='([^']+)'", response.text)
    assert match, "chart canvas has no data"
    chart = json.loads(match.group(1))
    assert chart["default"] == "snapshot"
    assert len(chart["time"][0]["points"]) == 3
    assert chart["time"][0]["points"][-1][1] == 799.0
    assert chart["snapshot"], "the check-in view should have prices too"

    # a hotel that is not part of this track must not be reachable
    assert http.get(f"/tracks/{track_id}/hotels/DDLH", auth=AUTH).status_code == 404


def test_the_comparison_view_collapses_matches_and_shows_differences(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    tracks.add_markets(conn, track_id, ("fr-fr", "en-gb"))

    def series_id(market: str) -> int:
        return conn.execute(
            """
            SELECT s.id FROM series s
            JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ?
            WHERE s.hotel_code = 'DCHH' AND s.market = ?
            ORDER BY s.arrival_date LIMIT 1
            """,
            (track_id, market),
        ).fetchone()["id"]

    def observe(market: str, cents: int, *, seconds: int = 0) -> None:
        conn.execute(
            """
            INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
            VALUES (?, ?, 1, ?, 'first')
            """,
            (
                series_id(market),
                cents,
                (
                    datetime.now(timezone.utc) + timedelta(seconds=seconds)
                ).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()

    observe("es-es", 70000)
    observe("fr-fr", 70000)
    observe("en-gb", 60000)

    page = http.get(f"/tracks/{track_id}/hotels/DCHH", auth=AUTH)
    assert "All 1 other EUR market" in page.text
    assert "matches es-es" in page.text
    assert "£600.00" in page.text
    assert "Show all markets (3)" in page.text
    assert page.text.index("<h2>Arrivals</h2>") < page.text.index("<h2>Markets</h2>")
    markets_section = page.text.split("<h2>Markets</h2>")[1]
    assert "#1" in markets_section
    assert "EUR markets get one" in markets_section
    assert "Ranks follow the selected market" in markets_section

    observe("fr-fr", 80000, seconds=1)

    page = http.get(f"/tracks/{track_id}/hotels/DCHH", auth=AUTH)
    assert "Showing 1 EUR market" in page.text
    assert "that differs from es-es" in page.text
    assert "+€100.00" in page.text


def test_the_check_in_view_is_the_default_and_has_a_range_selector(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    seed_prices(conn, track_id, [820.00])

    response = http.get(f"/tracks/{track_id}/hotels/DCHH", auth=AUTH)

    match = re.search(r"data-chart='([^']+)'", response.text)
    chart = json.loads(match.group(1))
    assert chart["checks"] == 1
    assert chart["default"] == "snapshot"
    assert "By check-in date" in response.text
    assert "Over time" in response.text
    assert "Window" in response.text
    assert 'data-chart-range="7"' in response.text
    assert 'data-chart-range="30"' in response.text
    assert 'data-chart-range="all"' in response.text


def test_the_hotel_page_shows_the_cheapest_dates_and_ranks(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    arrivals = [
        row["arrival_date"]
        for row in conn.execute(
            """
            SELECT s.arrival_date FROM series s
            JOIN track_series ts ON ts.series_id = s.id AND ts.track_id = ?
            WHERE s.hotel_code = 'DCHH' ORDER BY s.arrival_date
            """,
            (track_id,),
        ).fetchall()
    ]
    # 799 (third cheapest), 760 (cheapest), 790 (second cheapest)
    for arrival, price in zip(arrivals, [799.00, 760.00, 790.00]):
        conn.execute(
            """
            INSERT INTO observation (series_id, price_cents, available, observed_at, kind)
            SELECT s.id, ?, 1, ?, 'first' FROM series s
            WHERE s.hotel_code = 'DCHH' AND s.arrival_date = ?
            """,
            (
                int(price * 100),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                arrival,
            ),
        )
    conn.commit()

    response = http.get(f"/tracks/{track_id}/hotels/DCHH", auth=AUTH)
    text = response.text

    cheapest_day = date.fromisoformat(arrivals[1]).strftime("%a %d %b %Y")
    second_day = date.fromisoformat(arrivals[2]).strftime("%a %d %b %Y")
    third_day = date.fromisoformat(arrivals[0]).strftime("%a %d %b %Y")

    assert "€760.00" in text
    assert cheapest_day in text
    assert re.search(re.escape(cheapest_day) + r'\s*<span class="badge rank low">#1', text)
    assert re.search(re.escape(second_day) + r'\s*<span class="badge rank">#2', text)
    assert re.search(re.escape(third_day) + r'\s*<span class="badge rank">#3', text)


def test_trimming_the_window_keeps_the_track_and_changes_it(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    page = http.get(f"/tracks/{track_id}", auth=AUTH)
    token = csrf_from(page.text)
    start = date.today() + timedelta(days=41)

    response = http.post(
        f"/tracks/{track_id}/window",
        auth=AUTH,
        data={"csrf": token, "arrival_start": start.isoformat(), "arrival_end": (start + timedelta(days=1)).isoformat()},
        follow_redirects=False,
    )

    assert response.status_code == 303
    row = conn.execute("SELECT * FROM track WHERE id = ?", (track_id,)).fetchone()
    assert row["arrival_start"] == start.isoformat()
    watched = conn.execute(
        "SELECT COUNT(*) AS n FROM track_series WHERE track_id = ? AND watched = 1",
        (track_id,),
    ).fetchone()["n"]
    assert watched == 2 * 2  # two hotels x two arrival dates


def test_toggling_the_shortlist(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    page = http.get(f"/tracks/{track_id}", auth=AUTH)
    token = csrf_from(page.text)

    http.post(
        f"/tracks/{track_id}/shortlist",
        auth=AUTH,
        data={"csrf": token, "hotel_code": "DSFH", "shortlisted": "0"},
        follow_redirects=False,
    )

    row = conn.execute(
        "SELECT shortlisted FROM track_hotel WHERE track_id = ? AND hotel_code = 'DSFH'",
        (track_id,),
    ).fetchone()
    assert row["shortlisted"] == 0


def test_archiving_hides_the_track_and_restoring_brings_it_back(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    page = http.get(f"/tracks/{track_id}", auth=AUTH)
    token = csrf_from(page.text)

    response = http.post(
        f"/tracks/{track_id}/archive",
        auth=AUTH,
        data={"csrf": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert conn.execute(
        "SELECT archived_at FROM track WHERE id = ?", (track_id,)
    ).fetchone()["archived_at"] is not None

    index = http.get("/", auth=AUTH)
    assert "Archived tracks (1)" in index.text
    detail = http.get(f"/tracks/{track_id}", auth=AUTH)
    assert "archived" in detail.text

    http.post(
        f"/tracks/{track_id}/unarchive",
        auth=AUTH,
        data={"csrf": token},
        follow_redirects=False,
    )
    assert conn.execute(
        "SELECT archived_at FROM track WHERE id = ?", (track_id,)
    ).fetchone()["archived_at"] is None


def test_an_archived_track_cannot_be_checked_or_stopped(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    page = http.get(f"/tracks/{track_id}", auth=AUTH)
    token = csrf_from(page.text)
    http.post(
        f"/tracks/{track_id}/archive",
        auth=AUTH,
        data={"csrf": token},
        follow_redirects=False,
    )

    for action in ("check", "stop"):
        response = http.post(
            f"/tracks/{track_id}/{action}",
            auth=AUTH,
            data={"csrf": token},
            follow_redirects=False,
        )
        assert response.status_code == 409


def test_deleting_a_track_keeps_the_prices(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    seed_prices(conn, track_id, [820.00, 780.00])
    observations = conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"]
    page = http.get(f"/tracks/{track_id}", auth=AUTH)
    token = csrf_from(page.text)

    response = http.post(
        f"/tracks/{track_id}/delete",
        auth=AUTH,
        data={"csrf": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert conn.execute("SELECT COUNT(*) AS n FROM track").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM series").fetchone()["n"] > 0
    assert conn.execute("SELECT COUNT(*) AS n FROM observation").fetchone()["n"] == observations


def test_stopping_is_terminal_and_visible(client) -> None:
    http, conn, _ = client
    track_id = new_track(conn)
    page = http.get(f"/tracks/{track_id}", auth=AUTH)
    token = csrf_from(page.text)

    response = http.post(
        f"/tracks/{track_id}/stop", auth=AUTH, data={"csrf": token}, follow_redirects=False
    )

    assert response.status_code == 303
    assert conn.execute("SELECT status FROM track WHERE id = ?", (track_id,)).fetchone()["status"] == "stopped"

    detail = http.get(f"/tracks/{track_id}", auth=AUTH)
    assert "stopped" in detail.text
    assert "Check now" not in detail.text


def test_check_now_calls_the_injected_scheduler(client) -> None:
    http, conn, calls = client
    track_id = new_track(conn)
    page = http.get(f"/tracks/{track_id}", auth=AUTH)
    token = csrf_from(page.text)

    http.post(
        f"/tracks/{track_id}/check", auth=AUTH, data={"csrf": token}, follow_redirects=False
    )

    assert calls["check_now"] == 1
    assert calls["track_id"] == track_id


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.messages: list[str] = []

    async def deliver(self, text: str) -> list[Delivery]:
        self.messages.append(text)
        return [
            Delivery(
                "-100123456789",
                self.ok,
                200 if self.ok else 400,
                None if self.ok else "Bad Request: chat not found",
            )
        ]


def test_the_health_page_can_send_a_telegram_test(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "-100123456789")
    conn = db.connect(":memory:")
    sender = FakeSender()
    app = create_app(
        conn=conn,
        user=AUTH[0],
        password_hash=PASSWORD_HASH,
        check_now=lambda track_id: True,
        telegram=sender,
    )
    with TestClient(app) as http:
        page = http.get("/health", auth=AUTH)
        assert "Send test message" in page.text
        assert "…6789" in page.text
        token = csrf_from(page.text)
        response = http.post(
            "/health/telegram/test",
            auth=AUTH,
            data={"csrf": token},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert sender.messages
    row = conn.execute("SELECT * FROM health WHERE source = 'telegram'").fetchone()
    assert row["consecutive_failures"] == 0
    conn.close()


def test_a_rejected_telegram_test_shows_the_chat_error(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "-100123456789")
    conn = db.connect(":memory:")
    sender = FakeSender(ok=False)
    app = create_app(
        conn=conn,
        user=AUTH[0],
        password_hash=PASSWORD_HASH,
        check_now=lambda track_id: True,
        telegram=sender,
    )
    with TestClient(app) as http:
        page = http.get("/health", auth=AUTH)
        token = csrf_from(page.text)
        response = http.post(
            "/health/telegram/test",
            auth=AUTH,
            data={"csrf": token},
            follow_redirects=False,
        )
    assert response.status_code == 303
    row = conn.execute("SELECT * FROM health WHERE source = 'telegram'").fetchone()
    assert row["consecutive_failures"] == 1
    assert "chat not found" in row["last_error"]
    conn.close()


def test_health_says_when_telegram_is_not_configured(client, monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_IDS", raising=False)
    http, _, _ = client
    response = http.get("/health", auth=AUTH)

    assert "Telegram is not configured" in response.text
    assert "TELEGRAM_BOT_TOKEN" in response.text


def test_health_page_reports_last_success(client) -> None:
    http, conn, _ = client
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO health (source, last_success_at, consecutive_failures) VALUES ('packages', ?, 0)",
        (now,),
    )
    conn.execute(
        """INSERT INTO fetch_log (source, key, requested_at, ok, error)
           VALUES ('packages', 'DCHH|es-es|2026-12|2n_3j|1', ?, 0, 'boom')""",
        (now,),
    )
    conn.commit()

    response = http.get("/health", auth=AUTH)

    assert response.status_code == 200
    assert "packages" in response.text
    assert "boom" in response.text


def test_telegram_success_does_not_look_like_a_price_check(client) -> None:
    http, conn, _ = client
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO health (source, last_success_at, consecutive_failures) VALUES ('packages', ?, 0)",
        ((now - timedelta(hours=6)).isoformat(timespec="seconds"),),
    )
    conn.execute(
        "INSERT INTO health (source, last_success_at, consecutive_failures) VALUES ('telegram', ?, 0)",
        (now.isoformat(timespec="seconds"),),
    )
    conn.commit()

    response = http.get("/", auth=AUTH)

    assert "6 h ago" in response.text


def test_too_many_failed_logins_are_braked(client) -> None:
    http, _, _ = client
    for _ in range(auth.MAX_FAILURES):
        assert http.get("/", auth=(AUTH[0], "nope")).status_code == 401

    assert http.get("/", auth=(AUTH[0], "nope")).status_code == 429
    # and the brake applies even with the right password, from the same client
    assert http.get("/", auth=AUTH).status_code == 429


def test_chart_js_is_vendored_locally(client) -> None:
    http, _, _ = client
    response = http.get("/static/chart.umd.min.js")
    assert response.status_code == 200
    assert len(response.content) > 100_000
