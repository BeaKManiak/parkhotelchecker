"""Static configuration: endpoints, markets, horizons.

Everything here is a *constant of the outside world*, not user configuration.
User configuration (party, dates, hotels) belongs to the track model, not here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

# --- Endpoints -------------------------------------------------------------

PACKAGE_CALENDAR_URL = (
    "https://api.disneylandparis.com/prices-calendar/api/v1/prices/package-price-calendar"
)
TICKET_CALENDAR_URL = (
    "https://api.disneylandparis.com/prices-calendar/api/v2/prices/ticket-price-calendar"
)
TICKET_CATEGORY_URL = "https://tickets.disneylandparis.com/api/selectionDrawer/category"

# --- Identification --------------------------------------------------------

USER_AGENT = "parkhotelchecker/0.1 (personal use)"

# tickets.disneylandparis.com stalls without a browser-like UA; the price APIs
# accept anything. Keep the honest UA for the APIs, and this only where needed.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)

# --- Markets ---------------------------------------------------------------

# The package and ticket payloads do NOT carry a currency field. Currency is
# implied by the market, so this map is load-bearing: a missing key must fail
# loudly rather than default to EUR.
MARKET_CURRENCY: dict[str, str] = {
    "es-es": "EUR",
    "fr-fr": "EUR",
    "it-it": "EUR",
    "de-de": "EUR",
    "nl-nl": "EUR",
    "pt-pt": "EUR",
    "en-ie": "EUR",
    "fr-be": "EUR",
    "nl-be": "EUR",
    "de-at": "EUR",
    "de-ch": "EUR",
    "en-gb": "GBP",
    "fr-ch": "CHF",
    "en-usd": "USD",
    "da-dk": "DKK",
}

PRIMARY_MARKET = "es-es"
CANARY_MARKETS = ("en-gb", "fr-ch")

# --- Package shape ---------------------------------------------------------

# Disneyland Paris packages always include both parks. The ticket reference
# line therefore always prices a 2-park ticket.
PARKS = 2

# Night -> park days is always N + 1.
VALID_LOS = ("1n_2j", "2n_3j", "3n_4j", "4n_5j")
DEFAULT_LOS = "2n_3j"


def park_days(los: str) -> int:
    """'2n_3j' -> 3."""
    if los not in VALID_LOS:
        raise ValueError(f"unsupported los {los!r}; expected one of {VALID_LOS}")
    return int(los.split("_")[1].rstrip("j"))


# --- Horizon ---------------------------------------------------------------

# Observed on 2026-10-21: packages sell to 2028-03-30, ticket calendars to
# 2028-03-31. Treated as a soft ceiling: requests past it are skipped and
# logged, never treated as failures.
HORIZON_LATEST: date = date(2028, 3, 30)


def within_horizon(d: date) -> bool:
    return date.today() <= d <= HORIZON_LATEST


# --- Money -----------------------------------------------------------------

def as_money(value: float | int | str) -> Decimal:
    """Convert a JSON number to Decimal without binary float artefacts.

    Decimal(812.62) is wrong (it captures the float's binary error);
    Decimal(str(812.62)) is 812.62.
    """
    return Decimal(str(value)).quantize(Decimal("0.01"))
