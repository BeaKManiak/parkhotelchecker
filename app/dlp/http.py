"""Shared HTTP client for the DLP APIs.

Two error classes, deliberately distinct:

- ``UpstreamError``  - we could not get an answer (network, 5xx after retries).
- ``ShapeError``     - we got an answer we do not understand.

The second is the dangerous one. It must never be swallowed into "no change",
because a tracker whose silence is ambiguous is worse than no tracker.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import BROWSER_USER_AGENT, USER_AGENT

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 3.0)
# The ticket category endpoint returns ~100 KB and is slow; give it more room.
SLOW_READ_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0)


class UpstreamError(RuntimeError):
    """The upstream did not give us a usable answer."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class ShapeError(UpstreamError):
    """The upstream answered, but not in the shape we expect.

    Carries whatever we could see, so an alert can say something useful.
    """

    def __init__(self, message: str, *, url: str | None = None, saw: Any = None):
        super().__init__(message, url=url)
        self.saw = saw


def create_client(*, slow: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=SLOW_READ_TIMEOUT if slow else TIMEOUT,
        headers={
            "User-Agent": BROWSER_USER_AGENT if slow else USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        follow_redirects=True,
    )


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST JSON and return a decoded object, retrying transient failures only.

    4xx is *not* retried: a bad request will be just as bad the second time.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:  # timeouts, connection resets, DNS
            last_error = exc
            log.warning("request failed (%s/%s) %s: %s", attempt, MAX_ATTEMPTS, url, exc)
        else:
            if response.status_code >= 500:
                last_error = UpstreamError(
                    f"HTTP {response.status_code} from {url}",
                    status=response.status_code,
                    url=url,
                )
                log.warning(
                    "server error (%s/%s) %s: %s",
                    attempt,
                    MAX_ATTEMPTS,
                    url,
                    response.status_code,
                )
            elif response.status_code >= 400:
                raise UpstreamError(
                    f"HTTP {response.status_code} from {url}: {response.text[:200]}",
                    status=response.status_code,
                    url=url,
                )
            else:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise ShapeError(f"response was not JSON: {exc}", url=url) from exc
                if not isinstance(body, dict):
                    raise ShapeError(
                        f"expected a JSON object, got {type(body).__name__}", url=url
                    )
                return body

        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(BACKOFF_SECONDS[attempt - 1])

    raise UpstreamError(
        f"giving up after {MAX_ATTEMPTS} attempts on {url}: {last_error}", url=url
    )


def require(payload: dict[str, Any], key: str, *, url: str | None = None) -> Any:
    """Fetch a required key or raise ShapeError.

    Used instead of ``payload.get(key)`` everywhere a missing key would
    otherwise turn into a silently empty result.
    """
    if key not in payload:
        raise ShapeError(
            f"missing key {key!r} (saw: {sorted(payload)[:10]})", url=url, saw=sorted(payload)
        )
    return payload[key]


def upstream_errors(payload: dict[str, Any]) -> list[str]:
    """DLP reports some 'successes' as HTTP 200 with a non-empty errors list."""
    errors = payload.get("errors")
    if not errors:
        return []
    return [str(e) for e in errors]
