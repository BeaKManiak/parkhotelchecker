"""The 16 hotels sold on the Disneyland Paris site, in two sets.

Codes are validated live (2026-10-21/22). A hotel code is 4 letters; an
unknown-but-well-formed code returns HTTP 200 with
errors=["no_scored_prices_calendar_found"], not a 4xx.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HotelKind(StrEnum):
    ONSITE = "onsite"
    PARTNER = "partner"


@dataclass(frozen=True, slots=True)
class Hotel:
    code: str
    name: str
    kind: HotelKind
    seasonal: bool = False


HOTELS: tuple[Hotel, ...] = (
    # --- On-site ---
    Hotel("DDLH", "Disneyland Hotel", HotelKind.ONSITE),
    Hotel("DNYH", "Disney Hotel New York - The Art of Marvel", HotelKind.ONSITE),
    Hotel("DNBH", "Disney Newport Bay Club", HotelKind.ONSITE),
    Hotel("DSLH", "Disney Sequoia Lodge", HotelKind.ONSITE),
    Hotel("DCHH", "Disney Hotel Cheyenne", HotelKind.ONSITE),
    Hotel("DSFH", "Disney Hotel Santa Fe", HotelKind.ONSITE),
    Hotel("DDCH", "Disney Davy Crockett Ranch", HotelKind.ONSITE),
    # --- Partner ---
    Hotel("ELVE", "Hotel l'Elysee Val d'Europe", HotelKind.PARTNER),
    Hotel("STAY", "Staycity Aparthotels Marne-la-Vallee", HotelKind.PARTNER),
    Hotel("KISP", "Ki Space Hotel & Spa", HotelKind.PARTNER),
    Hotel("ALLS", "All Suites Appart Hotel", HotelKind.PARTNER),
    Hotel("PIVA", "Aparthotel Adagio Val d'Europe", HotelKind.PARTNER),
    Hotel("KYMA", "Campanile Val de France", HotelKind.PARTNER),
    Hotel("BBHO", "B&B Hotel", HotelKind.PARTNER),
    Hotel("MYTE", "Explorers Fabulous Hotels Group", HotelKind.PARTNER),
    Hotel("VVNP", "Les Villages Nature Paris", HotelKind.PARTNER, seasonal=True),
)

BY_CODE: dict[str, Hotel] = {h.code: h for h in HOTELS}

ONSITE_CODES: tuple[str, ...] = tuple(h.code for h in HOTELS if h.kind is HotelKind.ONSITE)
PARTNER_CODES: tuple[str, ...] = tuple(h.code for h in HOTELS if h.kind is HotelKind.PARTNER)


def label(code: str) -> str:
    """Human label for a code; falls back to the code so an unknown hotel is
    still printable in an alert rather than raising inside the notifier."""
    hotel = BY_CODE.get(code)
    return hotel.name if hotel else code
