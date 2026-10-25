#!/usr/bin/env python
"""Read-only package probe across markets.

v1.1 compares several markets, but only es-es and the two canaries have ever
been exercised. This asks each market for one hotel-month and prints a compact
table, so an empty or mislabelled market is obvious before it is added to a
track.

    python scripts/probe_markets.py
    python scripts/probe_markets.py --markets fr-fr,de-de,it-it --month 2027-02
    python scripts/probe_markets.py --hotel DSFH --child 2020-01-01

Exit code: 0 if every market answered with the expected currency, 1 on a hard
error. An empty month is data, not a failure: the hotel may simply not sell the
party on those dates.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta

from app import config
from app.dlp import packages
from app.dlp.http import ShapeError, UpstreamError, create_client


def _month(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}") from None


def _default_month() -> date:
    return (date.today() + timedelta(days=60)).replace(day=1)


def _market_list(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(config.MARKET_CURRENCY)
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe the package API across markets (read-only)"
    )
    parser.add_argument(
        "--markets",
        default="all",
        help="comma-separated market codes, or 'all' (default)",
    )
    parser.add_argument("--hotel", default="DCHH")
    parser.add_argument("--month", type=_month, default=_default_month())
    parser.add_argument("--los", default=config.DEFAULT_LOS)
    parser.add_argument("--adult", type=int, default=2)
    parser.add_argument(
        "--child",
        action="append",
        metavar="YYYY-MM-DD",
        help="child birthdate; repeat for each child (omit under-3s)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="seconds between requests (default 0.4)",
    )
    return parser


async def probe(args: argparse.Namespace) -> int:
    markets = _market_list(args.markets)
    unknown = [market for market in markets if market not in config.MARKET_CURRENCY]
    if unknown:
        print(f"unknown market(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    children = tuple(args.child or ())
    rows = []
    hard_errors = 0

    async with create_client() as client:
        for index, market in enumerate(markets):
            expected = config.MARKET_CURRENCY[market]
            try:
                calendar = await packages.fetch(
                    client,
                    hotel=args.hotel,
                    market=market,
                    los=args.los,
                    adults=args.adult,
                    child_birthdates=children,
                    month=args.month,
                )
                days = calendar.days
                if not days:
                    status = "empty"
                    cheapest = ""
                else:
                    arrival = min(days, key=lambda day: days[day].price)
                    cheapest = f"{arrival.isoformat()} {days[arrival].price:,.2f}"
                    status = "ok" if calendar.currency == expected else "CURRENCY?"
                if calendar.errors:
                    status = "ERRORS"
                if calendar.currency != expected:
                    status = "CURRENCY?"
                rows.append(
                    (
                        market,
                        expected,
                        calendar.currency,
                        str(len(days)),
                        cheapest,
                        status,
                        ",".join(calendar.errors)[:60],
                    )
                )
                if status not in {"ok", "empty"}:
                    hard_errors += 1
            except (ShapeError, UpstreamError) as exc:
                hard_errors += 1
                rows.append((market, expected, "—", "—", "—", "ERROR", str(exc)[:60]))
            except Exception as exc:  # noqa: BLE001 - keep the table going
                hard_errors += 1
                rows.append((market, expected, "—", "—", "—", "ERROR", str(exc)[:60]))

            if index < len(markets) - 1 and args.delay:
                await asyncio.sleep(args.delay)

    print(
        f"{args.hotel} · {args.month:%Y-%m} · {args.los} · "
        f"{args.adult}a{len(children)}c"
    )
    print()
    header = ("market", "want", "got", "days", "cheapest", "status", "detail")
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(7)]
    line = "  ".join(str(value).ljust(width) for value, width in zip(header, widths))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print(
            "  ".join(str(value).ljust(width) for value, width in zip(row, widths))
        )

    ok = sum(1 for row in rows if row[5] == "ok")
    empty = sum(1 for row in rows if row[5] == "empty")
    print()
    print(f"{ok} ok · {empty} empty · {len(rows) - ok - empty} problem(s)")
    return 1 if hard_errors else 0


async def main() -> int:
    args = build_parser().parse_args()
    return await probe(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
