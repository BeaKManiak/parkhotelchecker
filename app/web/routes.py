"""HTTP routes.

Plain server-rendered forms, POST then redirect. No client-side framework, no
JSON API: the dashboard has to work with a stale browser on a phone on the sofa,
and a full page load is the cheapest way to guarantee that.

Every POST carries a CSRF token. HTTP Basic credentials are attached by the
browser to *any* request to this origin, including a cross-site form POST, so
"it is behind auth" is not the same as "it cannot be forged".
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import config
from app.dlp import hotels as hotel_catalog
from app.tracking import tracks
from app.web import queries


def build_router(*, require_user) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_user)])
    public = APIRouter()

    # --- helpers ----------------------------------------------------------

    def render(request: Request, name: str, **context) -> HTMLResponse:
        conn = request.app.state.conn
        context.setdefault("last_success", queries.last_success(conn))
        context.setdefault("msg", request.query_params.get("msg"))
        context.setdefault("err", request.query_params.get("err"))
        context.setdefault("csrf", request.app.state.csrf_token)
        return request.app.state.templates.TemplateResponse(request, name, context)

    def check_csrf(request: Request, token: str) -> None:
        if not secrets.compare_digest(token or "", request.app.state.csrf_token):
            raise HTTPException(status_code=403, detail="bad or missing CSRF token")

    def require_track(request: Request, track_id: int):
        row = queries.track(request.app.state.conn, track_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such track")
        return row

    # --- tracks -----------------------------------------------------------

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        conn = request.app.state.conn
        return render(
            request,
            "tracks.html",
            tracks=queries.track_list(conn),
            archived=queries.track_list(conn, archived=True),
            health_age=queries.age_of_last_success(conn),
        )

    @router.get("/tracks/new", response_class=HTMLResponse)
    async def new_track_form(request: Request) -> HTMLResponse:
        tomorrow = date.today() + timedelta(days=1)
        return render(
            request,
            "track_new.html",
            form={
                "name": "",
                "adults": 2,
                "children_count": 0,
                "los": config.DEFAULT_LOS,
                "arrival_start": tomorrow.isoformat(),
                "arrival_end": (tomorrow + timedelta(days=6)).isoformat(),
                "min_delta": "20",
                "markets": ["es-es"],
                "shortlist": [],
                "children": [""] * (config.MAX_PARTY - 1),
            },
            hotels=hotel_catalog.HOTELS,
            los_choices=config.VALID_LOS,
            horizon=config.horizon_latest(),
            canaries=config.CANARY_MARKETS,
            max_party=config.MAX_PARTY,
        )

    @router.post("/tracks")
    async def create_track(
        request: Request,
        name: str = Form(""),
        adults: int = Form(2),
        los: str = Form(config.DEFAULT_LOS),
        arrival_start: str = Form(""),
        arrival_end: str = Form(""),
        min_delta: str = Form("20"),
        market: list[str] | None = Form(None),
        shortlist: list[str] | None = Form(None),
        child: list[str] | None = Form(None),
        children_count: int = Form(0),
        csrf: str = Form(""),
    ) -> Response:
        check_csrf(request, csrf)
        shortlist = shortlist or []
        children = tuple(sorted(value.strip() for value in (child or []) if value.strip()))

        marked = {code: code in shortlist for code in hotel_catalog.BY_CODE}
        markets: list[tuple[str, int]] = [(config.PRIMARY_MARKET, 6)]
        for canary in config.CANARY_MARKETS:
            if canary in (market or []):
                markets.append((canary, 24))

        try:
            if not 0 <= children_count <= config.MAX_PARTY - 1:
                raise ValueError(
                    f"children must be between 0 and {config.MAX_PARTY - 1}"
                )
            if len(children) != children_count:
                given = len(children)
                noun = "birthdate" if given == 1 else "birthdates"
                raise ValueError(
                    f"you said {children_count} children but gave "
                    f"{given} {noun}"
                )

            spec = tracks.TrackSpec(
                name=name,
                adults=adults,
                arrival_start=date.fromisoformat(arrival_start),
                arrival_end=date.fromisoformat(arrival_end),
                los=los,
                hotels=tuple(marked.items()),
                child_birthdates=children,
                markets=tuple(markets),
                min_delta_cents=_euros_to_cents(min_delta),
            )
            track_id = tracks.create_track(request.app.state.conn, spec)
        except (ValueError, InvalidOperation) as exc:
            return render(
                request,
                "track_new.html",
                err=str(exc),
                form={
                    "name": name,
                    "adults": adults,
                    "los": los,
                    "arrival_start": arrival_start,
                    "arrival_end": arrival_end,
                    "min_delta": min_delta,
                    "markets": market or [],
                    "shortlist": shortlist,
                    "children_count": children_count,
                    "children": list(children)[: config.MAX_PARTY - 1]
                    + [""]
                    * max(0, config.MAX_PARTY - 1 - len(children)),
                },
                hotels=hotel_catalog.HOTELS,
                los_choices=config.VALID_LOS,
                horizon=config.horizon_latest(),
                canaries=config.CANARY_MARKETS,
                max_party=config.MAX_PARTY,
            )

        return RedirectResponse(
            url=f"/tracks/{track_id}?msg=Track+created", status_code=303
        )

    @router.get("/tracks/{track_id}", response_class=HTMLResponse)
    async def track_detail(request: Request, track_id: int) -> HTMLResponse:
        conn = request.app.state.conn
        row = require_track(request, track_id)
        market = queries.primary_market(conn, track_id)
        party = conn.execute(
            "SELECT * FROM party WHERE id = ?", (row["party_id"],)
        ).fetchone()

        return render(
            request,
            "track.html",
            track=row,
            party=party,
            party_children=json.loads(party["child_birthdates"]) if party else [],
            market=market,
            markets=queries.markets(conn, track_id),
            hotels=queries.hotels(conn, track_id, market),
            alerts_24h=queries.track_alerts(
                conn, track_id, datetime.now(timezone.utc) - timedelta(hours=24)
            ),
        )

    @router.post("/tracks/{track_id}/window")
    async def edit_window(
        request: Request,
        track_id: int,
        arrival_start: str = Form(...),
        arrival_end: str = Form(...),
        csrf: str = Form(""),
    ) -> RedirectResponse:
        check_csrf(request, csrf)
        try:
            counts = tracks.set_arrival_window(
                request.app.state.conn,
                track_id,
                date.fromisoformat(arrival_start),
                date.fromisoformat(arrival_end),
            )
        except ValueError as exc:
            return RedirectResponse(
                url=f"/tracks/{track_id}?err={_quote(str(exc))}", status_code=303
            )
        message = f"Window updated: {counts['added']} added, {counts['untrimmed']} restored"
        return RedirectResponse(url=f"/tracks/{track_id}?msg={_quote(message)}", status_code=303)

    @router.post("/tracks/{track_id}/shortlist")
    async def toggle_shortlist(
        request: Request,
        track_id: int,
        hotel_code: str = Form(...),
        shortlisted: str = Form("0"),
        csrf: str = Form(""),
    ) -> RedirectResponse:
        check_csrf(request, csrf)
        row = require_track(request, track_id)
        if row["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="track is archived")
        if hotel_code not in hotel_catalog.BY_CODE:
            raise HTTPException(status_code=400, detail="unknown hotel")
        tracks.set_shortlist(
            request.app.state.conn, track_id, hotel_code, shortlisted == "1"
        )
        state = "alerting" if shortlisted == "1" else "silent"
        return RedirectResponse(
            url=f"/tracks/{track_id}?msg={_quote(hotel_catalog.label(hotel_code) + ' is now ' + state)}",
            status_code=303,
        )

    @router.post("/tracks/{track_id}/stop")
    async def stop(
        request: Request, track_id: int, csrf: str = Form("")
    ) -> RedirectResponse:
        check_csrf(request, csrf)
        row = require_track(request, track_id)
        if row["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="track is archived")
        tracks.stop_track(request.app.state.conn, track_id)
        return RedirectResponse(
            url=f"/tracks/{track_id}?msg=Track+stopped.+History+is+kept.", status_code=303
        )

    @router.post("/tracks/{track_id}/archive")
    async def archive(
        request: Request, track_id: int, csrf: str = Form("")
    ) -> RedirectResponse:
        check_csrf(request, csrf)
        require_track(request, track_id)
        tracks.archive_track(request.app.state.conn, track_id, archived=True)
        return RedirectResponse(url="/?msg=Track+archived.+History+is+kept.", status_code=303)

    @router.post("/tracks/{track_id}/unarchive")
    async def unarchive(
        request: Request, track_id: int, csrf: str = Form("")
    ) -> RedirectResponse:
        check_csrf(request, csrf)
        require_track(request, track_id)
        tracks.archive_track(request.app.state.conn, track_id, archived=False)
        return RedirectResponse(
            url=f"/tracks/{track_id}?msg=Track+restored.", status_code=303
        )

    @router.post("/tracks/{track_id}/delete")
    async def delete(
        request: Request, track_id: int, csrf: str = Form("")
    ) -> RedirectResponse:
        check_csrf(request, csrf)
        require_track(request, track_id)
        tracks.delete_track(request.app.state.conn, track_id)
        return RedirectResponse(
            url="/?msg=Track+deleted.+Price+history+is+kept.", status_code=303
        )

    @router.post("/tracks/{track_id}/check")
    async def check_now(
        request: Request, track_id: int, csrf: str = Form("")
    ) -> RedirectResponse:
        check_csrf(request, csrf)
        row = require_track(request, track_id)
        if row["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="track is archived")
        request.app.state.check_now()
        return RedirectResponse(
            url=f"/tracks/{track_id}?msg=Check+started.+Refresh+in+a+minute.", status_code=303
        )

    # --- hotel detail -----------------------------------------------------

    @router.get("/tracks/{track_id}/hotels/{hotel_code}", response_class=HTMLResponse)
    async def hotel_detail(
        request: Request, track_id: int, hotel_code: str
    ) -> HTMLResponse:
        conn = request.app.state.conn
        row = require_track(request, track_id)
        watched = conn.execute(
            "SELECT 1 FROM track_hotel WHERE track_id = ? AND hotel_code = ?",
            (track_id, hotel_code),
        ).fetchone()
        if not watched:
            raise HTTPException(status_code=404, detail="hotel not tracked")

        market = queries.primary_market(conn, track_id)
        rows = queries.series(conn, track_id, hotel_code, market)
        chart = _chart_payload(rows)
        return render(
            request,
            "hotel.html",
            track=row,
            hotel=hotel_catalog.BY_CODE[hotel_code],
            series=rows,
            chart=chart,
            market=market,
        )

    # --- health -----------------------------------------------------------

    @router.get("/health", response_class=HTMLResponse)
    async def health_page(request: Request) -> HTMLResponse:
        conn = request.app.state.conn
        return render(
            request,
            "health.html",
            health=queries.health(conn),
            failures=queries.recent_failures(conn),
            polls=queries.recent_polls(conn),
            age=queries.age_of_last_success(conn),
            last_success=queries.last_success(conn),
        )

    @public.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        """Unauthenticated liveness probe for Docker. Says nothing useful."""
        try:
            request.app.state.conn.execute("SELECT 1").fetchone()
        except Exception:  # noqa: BLE001 - a probe must answer, not raise
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": True})

    return router, public


def _euros_to_cents(value: str) -> int:
    return int((Decimal(value.strip().replace(",", ".")) * 100).to_integral_value())


def _quote(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


def _chart_payload(rows: list[queries.SeriesRow], limit: int = 12) -> dict:
    """Both views of the same data, plus which one to open by default.

    - ``time``: one stepped line per arrival, x = when the price was recorded.
      Capped at the cheapest arrivals; thirty overlapping lines is a smear.
    - ``snapshot``: every currently priced arrival, x = check-in date. This is
      the only honest view on the first day, when there is no trend yet.

    The time view is the default whenever there is history; the snapshot is
    the fallback for a hotel whose prices have never been recorded.
    """

    def short(day: date) -> str:
        label = f"{day.day} {day.strftime('%b')}"
        return f"{label} {day.year}" if day.year != date.today().year else label

    with_history = [row for row in rows if row.history]
    with_history.sort(key=lambda row: (row.price is None, row.price or Decimal("0")))
    chosen = with_history[:limit]
    checks = sorted({when for row in rows for when, _ in row.history})

    time_series = [
        {
            "label": short(row.arrival),
            "points": [
                [int(when.timestamp() * 1000), float(price)]
                for when, price in row.history
            ],
        }
        for row in chosen
    ]
    snapshot = [
        {
            "date": row.arrival.isoformat(),
            "price": float(row.price),
            "label": short(row.arrival),
        }
        for row in rows
        if row.price is not None and row.available
    ]
    as_of = max((row.observed_at for row in rows if row.observed_at), default=None)

    return {
        "time": time_series,
        "snapshot": snapshot,
        "default": "time" if time_series else "snapshot",
        "checks": len(checks),
        "skipped": len(with_history) - len(chosen),
        "as_of": as_of.isoformat() if as_of else None,
    }
