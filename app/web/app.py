"""The FastAPI application.

One process, one database connection, one scheduler. The dashboard is a *view*
of the same connection the poller writes through, which is why it can show a
price the moment it lands without any cache to invalidate.

Security posture lives here, in one place:

- HTTP Basic on everything except ``/healthz`` (a liveness probe for Docker,
  which reveals nothing beyond "the process is up").
- a strict CSP with no inline script or style, because the only JavaScript is a
  vendored Chart.js file served from this same origin. No CDN, no third party,
  nothing to leak a request to.
- CSRF tokens on every form, since Basic credentials ride along on cross-site
  POSTs too.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import config
from app.web import auth
from app.web.routes import build_router

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATE_DIR = WEB_DIR / "templates"

DISPLAY_TZ = ZoneInfo("Europe/Madrid")

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _as_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _humanise(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _money(value, currency: str = "EUR") -> str:
    if value is None:
        return "—"
    symbol = {"EUR": "€", "GBP": "£", "USD": "$", "CHF": "CHF "}.get(currency, f"{currency} ")
    return f"{symbol}{value:,.2f}"


def _local(value, fmt: str = "%a %d %b, %H:%M") -> str:
    moment = _as_datetime(value)
    if moment is None:
        return "never"
    return moment.astimezone(DISPLAY_TZ).strftime(fmt)


def _day(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%a %d %b %Y")


def _ago(value) -> str:
    """Accept a datetime, an ISO string, or a timedelta (an age)."""
    if isinstance(value, timedelta):
        return _humanise(value)
    moment = _as_datetime(value)
    if moment is None:
        return "never"
    return _humanise(datetime.now(timezone.utc) - moment)


def _delta(value, previous) -> str:
    if value is None or previous is None:
        return "—"
    change = value - previous
    if change == 0:
        return "no change"
    sign = "+" if change > 0 else "−"
    return f"{sign}€{abs(change):,.2f}"


def _party(adults: int, children: int) -> str:
    if children == 0:
        return f"{adults} adults"
    child_word = "child" if children == 1 else "children"
    return f"{adults} adults, {children} {child_word}"


def create_app(
    *,
    conn,
    scheduler=None,
    check_now=None,
    user: str | None = None,
    password_hash: str | None = None,
    test_mode: bool = False,
) -> FastAPI:
    if user is None or password_hash is None:
        env_user, env_hash = auth.settings_from_env()
        user = user or env_user
        password_hash = password_hash or env_hash

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.globals["test_mode"] = test_mode
    templates.env.filters.update(
        money=_money,
        local=_local,
        day=_day,
        ago=_ago,
        delta=_delta,
        party=_party,
        los=config.los_label,
    )

    csrf_token = hmac.new(
        password_hash.encode(), b"parkhotelchecker-csrf", hashlib.sha256
    ).hexdigest()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        app.state.stop_event = stop
        task = None
        if scheduler is not None:
            task = asyncio.create_task(scheduler.run(stop=stop), name="scheduler")
            log.info("scheduler started with the web app")
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=10)
                except (TimeoutError, asyncio.CancelledError):
                    task.cancel()
                log.info("scheduler stopped")

    app = FastAPI(
        title="parkhotelchecker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.conn = conn
    app.state.templates = templates
    app.state.csrf_token = csrf_token
    app.state.tasks = set()

    if check_now is None:

        def check_now() -> None:  # noqa: F811 - the injected one wins
            if scheduler is None:
                log.info("check-now ignored: no scheduler attached")
                return
            if any(not task.done() for task in app.state.tasks):
                log.info("check-now ignored: a manual check is already running")
                return

            async def runner() -> None:
                try:
                    await scheduler.run_once(force=test_mode)
                except Exception:  # noqa: BLE001 - a manual check must not 500
                    log.exception("manual check failed")

            task = asyncio.create_task(runner(), name="manual-check")
            app.state.tasks.add(task)
            task.add_done_callback(app.state.tasks.discard)

    app.state.check_now = check_now

    require_user = auth.make_dependency(user, password_hash)
    router, public = build_router(require_user=require_user)
    app.include_router(router)
    app.include_router(public)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=3600"
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
