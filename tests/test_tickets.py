"""Ticket prices and seasonal product-code discovery, against live fixtures."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.dlp import tickets
from app.dlp.http import ShapeError

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- discovery -------------------------------------------------------------


def test_discovery_finds_the_three_day_two_park_product() -> None:
    products = tickets.parse_category_payload(load("category_es.json"))
    product = tickets.select_product(products, duration_days=3)

    # The product code is seasonal; this is only the current value.
    assert product.product_type == "TKITL34NP"
    assert product.adult_code == "TKITHS003A"
    assert product.child_code == "TKITHS003C"
    assert product.parks == 2


def test_discovery_offers_every_duration_we_support() -> None:
    products = tickets.parse_category_payload(load("category_es.json"))
    durations = {p.duration_days for p in products if p.parks == 2}
    assert {1, 2, 3, 4} <= durations


def test_missing_duration_raises_rather_than_guessing() -> None:
    products = tickets.parse_category_payload(load("category_es.json"))
    with pytest.raises(ShapeError):
        tickets.select_product(products, duration_days=7)


def test_missing_selection_config_is_a_shape_error() -> None:
    with pytest.raises(ShapeError):
        tickets.parse_category_payload({"header": {}})


# --- calendar --------------------------------------------------------------


def test_single_day_two_park_prices_and_party_total() -> None:
    cal = tickets.parse_calendar_payload(
        load("tickets_es_1d2p_2026-11.json"), market="es-es", product_type="TKITL14DJ", duration_days=1
    )
    day = cal.days[date(2026, 11, 9)]

    assert day.adult == Decimal("109.00")
    assert day.child == Decimal("104.00")
    # 109 + 109 + 104 = 322.00 for 2 adults + 1 child.
    assert day.total(2, 1) == Decimal("322.00")
    assert cal.currency == "EUR"


def test_three_day_total_is_the_whole_ticket() -> None:
    cal = tickets.parse_calendar_payload(
        load("tickets_es_3d2p_2026-11-12.json"), market="es-es", product_type="TKITL34NP", duration_days=3
    )
    day = cal.days[date(2026, 11, 9)]

    assert day.adult == Decimal("280.00")
    assert day.child == Decimal("265.00")
    # Per person, for the whole 3-day ticket - not per day.
    assert day.total(2, 1) == Decimal("825.00")


def test_party_total_uses_both_rates() -> None:
    cal = tickets.parse_calendar_payload(
        load("tickets_es_1d2p_2026-11.json"), market="es-es", product_type="TKITL14DJ", duration_days=1
    )
    day = cal.days[date(2026, 11, 9)]
    assert day.total(2, 0) == Decimal("218.00")
    assert day.total(1, 1) == Decimal("213.00")


def test_missing_calendar_is_a_shape_error() -> None:
    with pytest.raises(ShapeError):
        tickets.parse_calendar_payload({}, market="es-es", product_type="TKITL14DJ", duration_days=1)


def test_unpriced_product_is_a_shape_error() -> None:
    payload = {"calendar": [{"date": "2026-11-09", "products": {"OTHER": {}}}]}
    with pytest.raises(ShapeError):
        tickets.parse_calendar_payload(payload, market="es-es", product_type="TKITL14DJ", duration_days=1)
