#!/usr/bin/env python
"""Read-only sanity probe against the live DLP APIs.

This is the M1 checkpoint. It prints what the API actually says, so a human can
judge whether the data means what we think it means. It never writes to a
database, and it is never run by the test suite.

    python scripts/live_probe.py packages --hotel DCHH --month 2027-01
    python scripts/live_probe.py packages --hotel DCHH --month 2027-01 --child 2020-01-01
    python scripts/live_probe.py tickets  --month 2026-11 --child 2020-01-01

Exit code: 0 on a usable answer, 1 on an upstream/shape failure.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal

from app import config
from app.dlp import hotels, packages, tickets
from app.dlp.http import ShapeError, UpstreamError, create_client


def _month(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}") from None


def _day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


async def probe_packages(args: argparse.Namespace) -> int:
    children = tuple(args.child or ())
    async with create_client() as client:
        calendar = await packages.fetch(
            client,
            hotel=args.hotel,
            market=args.market,
            los=args.los,
            adults=args.adult,
            child_birthdates=children,
            month=args.month,
        )

    print(
        f"{hotels.label(calendar.hotel)} ({calendar.hotel})  "
        f"{calendar.market} {calendar.los}  "
        f"{args.adult}a{len(children)}c  {calendar.month:%Y-%m}"
    )
    print(f"currency: {calendar.currency} (from market, not payload)")
    print(f"errors:   {list(calendar.errors) or 'none'}")
    print(f"days:     {len(calendar.days)}")
    print()

    if not calendar.days:
        print("  (no sellable dates for this party - occupancy, or no inventory)")
        return 0

    print(f"  {'arrival':<12} {'price':>10}  {'room':<5} {'rate':<5} best")
    print(f"  {'-' * 12} {'-' * 10}  {'-' * 5} {'-' * 5} ----")
    prices: list[Decimal] = []
    for arrival in sorted(calendar.days):
        day = calendar.days[arrival]
        prices.append(day.price)
        print(
            f"  {arrival.isoformat():<12} {day.price:>10}  "
            f"{day.room_code:<5} {day.rate_category:<5} {'yes' if day.is_best_price else ''}"
        )

    cheapest = min(calendar.days, key=lambda d: calendar.days[d].price)
    print()
    print(f"  cheapest: {cheapest.isoformat()} at {calendar.days[cheapest].price}")
    print(f"  range:    {min(prices)} .. {max(prices)}")
    return 0


async def probe_tickets(args: argparse.Namespace) -> int:
    duration = config.park_days(args.los)
    children = tuple(args.child or ())
    end = date(
        args.month.year + (1 if args.month.month == 12 else 0),
        1 if args.month.month == 12 else args.month.month + 1,
        1,
    )

    async with create_client(slow=True) as client:
        print(f"discovering product codes ({args.market}) ...")
        products = await tickets.discover_product_codes(
            client, market=args.market, unique_id=str(uuid.uuid4())
        )
        product = tickets.select_product(products, duration_days=duration)

        print(
            f"  using {product.product_type}  "
            f"{product.duration_days}d/{product.parks}p  "
            f"adult={product.adult_code} child={product.child_code}"
        )
        calendar = await tickets.fetch_calendar(
            client,
            market=args.market,
            start=args.month,
            end=end,
            product=product,
            adults=args.adult,
            children=len(children),
        )

    print()
    print(
        f"ticket reference  {calendar.market} {calendar.currency}  "
        f"{product.duration_days}d/{product.parks}p  {args.adult}a{len(children)}c  "
        f"{args.month:%Y-%m}"
    )
    print(f"days: {len(calendar.days)}")
    print()
    print(f"  {'visit':<12} {'adult':>8} {'child':>8} {'party':>9} avail")
    print(f"  {'-' * 12} {'-' * 8} {'-' * 8} {'-' * 9} -----")
    for visit in sorted(calendar.days)[: args.limit]:
        day = calendar.days[visit]
        total = day.total(args.adult, len(children))
        print(
            f"  {visit.isoformat():<12} "
            f"{(day.adult if day.adult is not None else '-'):>8} "
            f"{(day.child if day.child is not None else '-'):>8} "
            f"{(total if total is not None else '-'):>9} "
            f"{'yes' if day.available else 'no'}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only DLP price probe")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--market", default=config.PRIMARY_MARKET)
    common.add_argument("--month", type=_month, required=True, help="YYYY-MM")
    common.add_argument("--adult", type=int, default=2)
    common.add_argument(
        "--child",
        action="append",
        metavar="YYYY-MM-DD",
        help="child birthdate; repeat for each child (under-3s are free: omit them)",
    )
    common.add_argument("--los", default=config.DEFAULT_LOS)

    pkgs = sub.add_parser("packages", parents=[common], help="hotel + park days")
    pkgs.add_argument("--hotel", default="DCHH")

    tix = sub.add_parser("tickets", parents=[common], help="ticket-only reference")
    tix.add_argument("--limit", type=int, default=10)

    return parser


async def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "packages":
            return await probe_packages(args)
        return await probe_tickets(args)
    except ShapeError as exc:
        print(f"\nSHAPE ERROR: {exc}", file=sys.stderr)
        print("The upstream answered, but not in a shape we understand.", file=sys.stderr)
        return 1
    except UpstreamError as exc:
        print(f"\nUPSTREAM ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
