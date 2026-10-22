"""Failure alerts: the half of the notifier that decides whether to trust silence.

A quiet tracker is only useful if "no message" means "no price move". So the
same channel that carries the digest also carries:

- three consecutive fetch failures for a source;
- an immediate note when the upstream answers in a shape we do not understand
  (a shape error is not a flaky network, it is the world changing);
- exactly one recovery message per outage.

Threshold alerts are edge-triggered: one message when a source crosses the
threshold, nothing while it keeps failing, one all-clear when it returns.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from app.alerts import state
from app.alerts.digest import last_success_text
from app.tracking.poller import PollResult
from app.tracking.store import utcnow

FAILURE_THRESHOLD = 3
SHAPE_THROTTLE = timedelta(hours=6)

LABELS = {
    "packages": "package API",
    "tickets": "ticket API",
    "plu": "ticket product codes",
    "internal": "poller",
}


def build_failure_alerts(
    conn: sqlite3.Connection, result: PollResult, *, now: datetime | None = None
) -> list[str]:
    now = now or utcnow()
    shape_sources = {
        error.split(" ", 1)[0] for error in result.shape_errors if " " in error
    }

    messages: list[str] = []
    messages.extend(_shape_alert(conn, result, now))
    messages.extend(_threshold_alerts(conn, now, suppress=shape_sources))
    return messages


def _shape_alert(
    conn: sqlite3.Connection, result: PollResult, now: datetime
) -> list[str]:
    if not result.shape_errors:
        return []

    # A shape error repeats on every poll until fixed. Once every six hours is
    # enough to be unmissable without becoming the reason you mute the bot.
    last = state.updated_at(conn, "shape_alert")
    if last is not None and now - last < SHAPE_THROTTLE:
        return []

    lines = result.shape_errors[:3]
    text = ["⚠️ DLP response shape changed — readings may be wrong"]
    text.extend(f"  {line[:200]}" for line in lines)
    if len(result.shape_errors) > len(lines):
        text.append(f"  … and {len(result.shape_errors) - len(lines)} more")
    if any(error.startswith("plu ") for error in result.shape_errors):
        text.append("Ticket product codes changed — the ticket reference line is disabled.")
    text.append("")
    text.append(f"Last successful check: {last_success_text(conn, now)}")
    state.set(conn, "shape_alert", "sent", now=now)
    return ["\n".join(text)]


def _ago(value: str | None, now: datetime) -> str:
    """How long since *this source* last succeeded, not the tracker as a whole."""
    if not value:
        return "never"
    last = datetime.fromisoformat(value)
    seconds = max(0, int((now - last).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _threshold_alerts(
    conn: sqlite3.Connection, now: datetime, *, suppress: set[str]
) -> list[str]:
    messages: list[str] = []

    for row in conn.execute("SELECT * FROM health ORDER BY source"):
        source = row["source"]
        if source == "telegram":
            continue  # cannot alert about the alert channel

        key = f"failure:{source}"
        failing = int(row["consecutive_failures"]) >= FAILURE_THRESHOLD
        already = state.get(conn, key) is not None

        if failing and not already and source not in suppress:
            label = LABELS.get(source, source)
            count = int(row["consecutive_failures"])
            text = [
                f"⚠️ DLP {label} failing — {count} consecutive failures",
                f"Last success: {_ago(row['last_success_at'], now)}",
            ]
            if row["last_error"]:
                text.append(f"Last error: {str(row['last_error'])[:200]}")
            messages.append("\n".join(text))
            state.set(conn, key, str(count), now=now)

        elif not failing and already:
            state.clear(conn, key)
            label = LABELS.get(source, source)
            messages.append(f"✅ DLP {label} recovered — {_ago(row['last_success_at'], now)}")

    return messages
