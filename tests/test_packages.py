"""Package calendar parsing against recorded live responses.

The numbers here are frozen snapshots taken 2026-10-22 (es-es). They are the
contract: if a fixture no longer parses, the upstream changed, and that is a
finding - never edit the fixture to make this pass.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.dlp import packages
from app.dlp.http import ShapeError

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def parse(name: str, *, adults: int, children: int) -> packages.PackageCalendar:
    return packages.parse_payload(
        load(name),
        hotel="DCHH",
        market="es-es",
        los="2n_3j",
        month=date(2027, 1, 1),
    )


def test_couple_anchor_price() -> None:
    """2 adults, Cheyenne, 2n/3j, es-es, arriving 2027-01-14 = EUR 812.62."""
    cal = parse("packages_dchh_es_2a_2027-01.json", adults=2, children=0)

    assert len(cal.days) == 31
    assert cal.currency == "EUR"
    day = cal.days[date(2027, 1, 14)]
    assert day.price == Decimal("812.62")
    assert day.room_code == "HD"
    assert day.is_best_price is True


def test_couple_with_child_priced_differently() -> None:
    cal = parse("packages_dchh_es_2a1c_2027-01.json", adults=2, children=1)

    # A child is charged, not ignored: the same night costs meaningfully more.
    assert cal.days[date(2027, 1, 14)].price == Decimal("1007.92")
    assert cal.days[date(2027, 1, 14)].price > Decimal("812.62")


def test_dates_is_a_dict_not_a_list() -> None:
    """Guards the exact mistake the payload invites: `dates` is keyed dd-mm-yyyy."""
    payload = load("packages_dchh_es_2a_2027-01.json")
    assert isinstance(payload["dates"], dict)
    assert "14-01-2027" in payload["dates"]


def test_rooms_are_parsed() -> None:
    cal = parse("packages_dchh_es_2a_2027-01.json", adults=2, children=0)
    assert cal.rooms
    assert cal.rooms[0].code == "HD"


def test_missing_dates_key_is_a_shape_error() -> None:
    with pytest.raises(ShapeError):
        packages.parse_payload({}, hotel="DCHH", market="es-es", los="2n_3j", month=date(2027, 1, 1))


def test_dates_as_list_is_a_shape_error() -> None:
    with pytest.raises(ShapeError):
        packages.parse_payload(
            {"dates": []}, hotel="DCHH", market="es-es", los="2n_3j", month=date(2027, 1, 1)
        )


def test_unparseable_date_key_is_a_shape_error() -> None:
    with pytest.raises(ShapeError):
        packages.parse_payload(
            {"dates": {"2027-01-14": {"price": 1.0}}},
            hotel="DCHH",
            market="es-es",
            los="2n_3j",
            month=date(2027, 1, 1),
        )


def test_unknown_market_is_rejected_before_requesting() -> None:
    with pytest.raises(ValueError):
        packages.build_payload(
            hotel="DCHH", market="xx-xx", los="2n_3j", adults=2, child_birthdates=(), month=date(2027, 1, 1)
        )


def test_party_echo_mismatch_is_a_shape_error() -> None:
    payload = load("packages_dchh_es_2a_2027-01.json")
    # Pretend we sent 2 adults + 1 child and upstream priced a couple.
    payload["requestV3"] = {"market": "es-es", "adult": 2, "child": ["2020-01-01"]}
    with pytest.raises(ShapeError):
        packages.parse_payload(
            payload, hotel="DCHH", market="es-es", los="2n_3j", month=date(2027, 1, 1)
        )
