"""Ticket-only prices, and discovery of the seasonal product codes.

Two things matter here:

1. Prices are per **visit start date**, and for multi-day tickets the price is
   the **total for the whole ticket** (not per day, not per park).
2. Product codes rotate between seasons. They are discovered from the shop's
   own category endpoint and never hardcoded. A season change that we fail to
   notice must raise, not silently draw an empty line.

Verified 2026-10-22: the 3-day/2-park product is ``TKITL34NP`` (an earlier
research note said ``TKITL3NP`` and was wrong - the API happened to answer
anyway, which is precisely why discovery is mandatory).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

from app import config
from app.dlp import http as dlp_http
from app.dlp.http import ShapeError


@dataclass(frozen=True, slots=True)
class TicketDay:
    visit_start: date
    adult: Decimal | None
    child: Decimal | None
    available: bool

    def total(self, adults: int, children: int) -> Decimal | None:
        """Party total for this visit date, or None if not fully priced."""
        if not self.available:
            return None
        if adults and self.adult is None:
            return None
        if children and self.child is None:
            return None
        adult_part = (self.adult or Decimal("0.00")) * adults
        child_part = (self.child or Decimal("0.00")) * children
        return (adult_part + child_part).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class TicketCalendar:
    market: str
    currency: str
    product_type: str
    duration_days: int
    parks: int
    days: Mapping[date, TicketDay]


@dataclass(frozen=True, slots=True)
class ProductCode:
    product_type: str
    adult_code: str
    child_code: str
    duration_days: int
    parks: int
    travel_from: date | None
    travel_to: date | None


# --- Product code discovery ------------------------------------------------


def build_category_payload(*, market: str, unique_id: str) -> dict[str, Any]:
    return {
        "categoryCode": "DATED",
        "market": market,
        "channel": "b2c",
        "slug": "tickets",
        "uniqueId": unique_id,
        "membershipType": "",
        "userId": None,
    }


async def discover_product_codes(
    client: httpx.AsyncClient, *, market: str, unique_id: str
) -> tuple[ProductCode, ...]:
    payload = build_category_payload(market=market, unique_id=unique_id)
    body = await dlp_http.post_json(client, config.TICKET_CATEGORY_URL, payload)
    return parse_category_payload(body)


def parse_category_payload(payload: Mapping[str, Any]) -> tuple[ProductCode, ...]:
    url = config.TICKET_CATEGORY_URL
    selection = payload.get("selectionConfig")
    if not isinstance(selection, dict):
        raise ShapeError(
            f"category payload has no 'selectionConfig' (saw: {sorted(payload)[:10]})", url=url
        )
    durations = selection.get("durations")
    if not isinstance(durations, list):
        raise ShapeError(
            f"'durations' is {type(durations).__name__}, expected list", url=url
        )

    products: list[ProductCode] = []
    for duration in durations:
        if not isinstance(duration, dict):
            continue
        for raw in duration.get("products", []) or []:
            if not isinstance(raw, dict):
                continue
            try:
                travel_from, travel_to = _travel_window(raw.get("travelDates"))
                products.append(
                    ProductCode(
                        product_type=str(raw["packageCode"]),
                        adult_code=str(raw["pluAdult"]),
                        child_code=str(raw["pluChild"]),
                        duration_days=int(raw["lengthOfStay"]["min"]),
                        parks=int(raw["numberOfParks"]),
                        travel_from=travel_from,
                        travel_to=travel_to,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ShapeError(f"unparseable product entry: {raw!r}", url=url) from exc

    if not products:
        raise ShapeError("category payload listed no products", url=url)
    return tuple(products)


def _travel_window(travel_dates: Any) -> tuple[date | None, date | None]:
    """First travel window, as (from, to). Absent or malformed -> (None, None)."""
    if not isinstance(travel_dates, list) or not travel_dates:
        return None, None
    first = travel_dates[0]
    if not isinstance(first, dict):
        return None, None
    return _parse_iso(first.get("from")), _parse_iso(first.get("to"))


def _parse_iso(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def select_product(
    products: Iterable[ProductCode], *, duration_days: int, parks: int = config.PARKS
) -> ProductCode:
    """Pick the product matching our package shape, or fail loudly."""
    matches = [p for p in products if p.duration_days == duration_days and p.parks == parks]
    if not matches:
        available = sorted({(p.duration_days, p.parks) for p in products})
        raise ShapeError(
            f"no product for {duration_days} day(s)/{parks} park(s); "
            f"upstream offers {available}"
        )
    # If a new season adds a duplicate duration/park pair, prefer the one with
    # the latest travel window rather than silently taking the first.
    return max(matches, key=lambda p: p.travel_from or date.min)


# --- Ticket calendar -------------------------------------------------------


def build_calendar_payload(
    *,
    market: str,
    start: date,
    end: date,
    product: ProductCode,
    adults: int,
    children: int,
) -> dict[str, Any]:
    return {
        "market": market,
        "currency": config.MARKET_CURRENCY[market],
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "products": [
            {
                "productType": product.product_type,
                "adultProductCode": product.adult_code,
                "childProductCode": product.child_code,
            }
        ],
    }


async def fetch_calendar(
    client: httpx.AsyncClient,
    *,
    market: str,
    start: date,
    end: date,
    product: ProductCode,
    adults: int,
    children: int,
) -> TicketCalendar:
    payload = build_calendar_payload(
        market=market, start=start, end=end, product=product, adults=adults, children=children
    )
    body = await dlp_http.post_json(client, config.TICKET_CALENDAR_URL, payload)
    return parse_calendar_payload(
        body,
        market=market,
        product_type=product.product_type,
        duration_days=product.duration_days,
        parks=product.parks,
    )


def parse_calendar_payload(
    payload: Mapping[str, Any],
    *,
    market: str,
    product_type: str,
    duration_days: int,
    parks: int = config.PARKS,
) -> TicketCalendar:
    url = config.TICKET_CALENDAR_URL
    calendar = payload.get("calendar")
    if not isinstance(calendar, list):
        raise ShapeError(
            f"ticket payload has no 'calendar' list (saw: {sorted(payload)[:10]})", url=url
        )

    days: dict[date, TicketDay] = {}
    for entry in calendar:
        if not isinstance(entry, dict) or "date" not in entry:
            raise ShapeError(f"unparseable calendar entry: {entry!r}", url=url)
        try:
            visit_start = datetime.strptime(str(entry["date"]), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ShapeError(f"unparseable ticket date {entry['date']!r}", url=url) from exc

        products = entry.get("products")
        if not isinstance(products, dict):
            raise ShapeError(f"calendar entry has no 'products': {entry!r}", url=url)
        day = products.get(product_type)
        if not isinstance(day, dict):
            raise ShapeError(
                f"calendar entry does not price {product_type} (saw: {sorted(products)})", url=url
            )

        available = bool(day.get("available", False))
        adult = day.get("priceAdult")
        child = day.get("priceChild")
        days[visit_start] = TicketDay(
            visit_start=visit_start,
            adult=config.as_money(adult) if adult is not None else None,
            child=config.as_money(child) if child is not None else None,
            available=available,
        )

    return TicketCalendar(
        market=market,
        currency=config.MARKET_CURRENCY[market],
        product_type=product_type,
        duration_days=duration_days,
        parks=parks,
        days=days,
    )
