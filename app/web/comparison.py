"""Presentation model for the market comparison table.

Prices are compared only within the same currency. A EUR market that differs
from ES by at least one euro gets a column in the default view; identical EUR
markets collapse into a count. Non-EUR markets are always shown natively and
never get a delta, because there is no FX source.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app import config
from app.web.queries import ComparisonRow

DELTA = Decimal("1.00")


@dataclass(frozen=True, slots=True)
class Column:
    market: str
    currency: str
    status: str  # reference | differs | matched | unpriced | other


@dataclass(frozen=True, slots=True)
class ComparisonView:
    reference: str
    reference_currency: str
    columns: tuple[Column, ...]
    all_columns: tuple[Column, ...]
    differing: tuple[str, ...]
    matched: tuple[str, ...]
    unpriced: tuple[str, ...]
    has_comparison_markets: bool
    has_data: bool


def build(
    rows: list[ComparisonRow],
    markets,
    *,
    reference: str = config.PRIMARY_MARKET,
) -> ComparisonView:
    entries = [entry["market"] for entry in markets]
    if not entries:
        entries = [reference]
    if reference not in entries:
        reference = entries[0]

    currencies = {
        market: config.MARKET_CURRENCY.get(market, "EUR") for market in entries
    }
    reference_currency = currencies[reference]

    def priced(market: str) -> list:
        return [
            cell
            for row in rows
            if (cell := row.cells.get(market)) is not None
            and cell.observed
            and cell.price is not None
        ]

    def differs_from_reference(market: str) -> bool:
        if currencies[market] != reference_currency:
            return False
        for row in rows:
            ref = row.cells.get(reference)
            cell = row.cells.get(market)
            if ref is None or cell is None:
                continue
            if not ref.observed or not cell.observed:
                continue
            if ref.price is None or cell.price is None:
                continue
            if abs(cell.price - ref.price) >= DELTA:
                return True
        return False

    columns: dict[str, Column] = {}
    differing: list[str] = []
    matched: list[str] = []
    unpriced: list[str] = []

    for market in entries:
        if market == reference:
            status = "reference"
        elif currencies[market] != reference_currency:
            status = "other"
        elif not priced(market):
            status = "unpriced"
            unpriced.append(market)
        elif differs_from_reference(market):
            status = "differs"
            differing.append(market)
        else:
            status = "matched"
            matched.append(market)
        columns[market] = Column(market, currencies[market], status)

    eur_markets = [
        market
        for market in entries
        if market != reference and currencies[market] == reference_currency
    ]
    other_markets = [
        market
        for market in entries
        if market != reference and currencies[market] != reference_currency
    ]
    ordered = [reference] + eur_markets + other_markets
    default = [reference] + [
        market for market in eur_markets if columns[market].status == "differs"
    ] + other_markets

    return ComparisonView(
        reference=reference,
        reference_currency=reference_currency,
        columns=tuple(columns[market] for market in default),
        all_columns=tuple(columns[market] for market in ordered),
        differing=tuple(differing),
        matched=tuple(matched),
        unpriced=tuple(unpriced),
        has_comparison_markets=len(entries) > 1,
        has_data=any(priced(market) for market in entries if market != reference),
    )
