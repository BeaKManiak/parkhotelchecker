"""Package price calendar: hotel + park days, priced per check-in date.

Request shape (verified 2026-10-22):

    POST /prices-calendar/api/v1/prices/package-price-calendar
    {"market":"es-es","los":"2n_3j","hotel":"DCHH","adult":2,
     "child":["2020-01-01"],"startDate":"2027-01","endDate":"2027-01"}

Response: ``dates`` is a **dict keyed "dd-mm-yyyy"**, not a list. Dates that are
absent from the dict are not sellable for that party/date — never assume the
month is complete.

Currency is not in the payload; it comes from the market (see config).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

from app import config
from app.dlp import http as dlp_http
from app.dlp.http import ShapeError

DATE_KEY_FORMAT = "%d-%m-%Y"


@dataclass(frozen=True, slots=True)
class Room:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class PackageDay:
    arrival: date
    price: Decimal
    room_code: str
    rate_category: str
    is_best_price: bool


@dataclass(frozen=True, slots=True)
class PackageCalendar:
    hotel: str
    market: str
    los: str
    currency: str
    month: date  # first day of the requested month
    days: Mapping[date, PackageDay]
    rooms: tuple[Room, ...]
    errors: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.days


def party_mix(adults: int, children: int) -> str:
    """The upstream's own party notation, e.g. '2a_1c'."""
    return f"{adults}a_{children}c"


def build_payload(
    *,
    hotel: str,
    market: str,
    los: str,
    adults: int,
    child_birthdates: tuple[str, ...],
    month: date,
) -> dict[str, Any]:
    if market not in config.MARKET_CURRENCY:
        raise ValueError(f"unknown market {market!r}")
    if los not in config.VALID_LOS:
        raise ValueError(f"unsupported los {los!r}")
    month_key = month.strftime("%Y-%m")
    return {
        "market": market,
        "los": los,
        "hotel": hotel,
        "adult": adults,
        "child": list(child_birthdates),
        "startDate": month_key,
        "endDate": month_key,
    }


async def fetch(
    client: httpx.AsyncClient,
    *,
    hotel: str,
    market: str,
    los: str,
    adults: int,
    child_birthdates: tuple[str, ...] = (),
    month: date,
) -> PackageCalendar:
    payload = build_payload(
        hotel=hotel,
        market=market,
        los=los,
        adults=adults,
        child_birthdates=child_birthdates,
        month=month,
    )
    body = await dlp_http.post_json(client, config.PACKAGE_CALENDAR_URL, payload)
    return parse_payload(body, hotel=hotel, market=market, los=los, month=month)


def parse_payload(
    payload: Mapping[str, Any],
    *,
    hotel: str,
    market: str,
    los: str,
    month: date,
) -> PackageCalendar:
    url = config.PACKAGE_CALENDAR_URL
    errors = tuple(dlp_http.upstream_errors(dict(payload)))

    if "dates" not in payload:
        raise ShapeError(
            f"package payload has no 'dates' key (saw: {sorted(payload)[:10]})",
            url=url,
            saw=sorted(payload),
        )

    raw_dates = payload["dates"]
    if not isinstance(raw_dates, dict):
        raise ShapeError(
            f"'dates' is {type(raw_dates).__name__}, expected object", url=url, saw=type(raw_dates)
        )

    days: dict[date, PackageDay] = {}
    for key, entry in raw_dates.items():
        try:
            arrival = datetime.strptime(key, DATE_KEY_FORMAT).date()
        except (TypeError, ValueError) as exc:
            raise ShapeError(f"unparseable date key {key!r}", url=url) from exc
        if not isinstance(entry, dict) or "price" not in entry:
            raise ShapeError(f"date entry {key!r} has no price (saw: {entry!r})", url=url)
        days[arrival] = PackageDay(
            arrival=arrival,
            price=config.as_money(entry["price"]),
            room_code=str(entry.get("roomType", "")),
            rate_category=str(entry.get("rateCategory", "")),
            is_best_price=bool(entry.get("bestPrice", False)),
        )

    _check_party_echo(payload, url=url)

    rooms = tuple(
        Room(code=str(r.get("roomCode", "")), name=str(r.get("name", "")).strip())
        for r in payload.get("rooms", [])
        if isinstance(r, dict)
    )

    return PackageCalendar(
        hotel=hotel,
        market=market,
        los=los,
        currency=config.MARKET_CURRENCY[market],
        month=month.replace(day=1),
        days=days,
        rooms=rooms,
        errors=errors,
    )


def _check_party_echo(payload: Mapping[str, Any], *, url: str) -> None:
    """The request echo tells us which party the upstream actually priced.

    If it disagrees with what we sent, our request was silently altered and
    every number in the response is about a different holiday.
    """
    request_echo = payload.get("request")
    if not isinstance(request_echo, dict) or "partyMix" not in request_echo:
        return
    echoed = str(request_echo["partyMix"])
    sent_adults = payload.get("requestV3", {}).get("adult")
    sent_children = payload.get("requestV3", {}).get("child")
    if isinstance(sent_adults, int) and isinstance(sent_children, list):
        expected = party_mix(sent_adults, len(sent_children))
        if echoed != expected:
            raise ShapeError(
                f"party echo mismatch: sent {expected}, upstream priced {echoed}", url=url
            )
