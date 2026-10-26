# Usage: alerts and dashboard

## Alerts

One message per poll, and only if there is something to say.

A line appears when, for an **active** track, on the **Spanish** market, for a
**shortlisted** hotel:

- the price moves by at least the track's `min_delta` (default €20) in either
  direction, or
- the price sets a new all-time low for that arrival, even below the threshold,
- or availability flips (sold out ↔ back available).

The first successful check of a new track sends one baseline message instead:
how many arrivals are priced, how many are sold out, and the cheapest current
arrival. It is not a price change and ignores `min_delta`.

`min_delta` is per track and editable.

```
🎢 Disneyland Paris — 22 Oct, 18:04  ·  3 changes

February half term · 2 nights · 3 park days · 2a1c
  ▼ 7 Feb   €690 → €640   (−€50, all-time low)
  ▲ 3 Feb   €612 → €648   (+€36)

Best arrival this window
  Cheyenne 7 Feb — package €640 · tickets €549 · implied hotel €91

Last successful check: 12 min ago
```

At most 12 change lines; the rest become `… and 9 more`.

Failure alerts are separate and immediate:

| Trigger | What you get |
|---|---|
| 3 consecutive failures for a source | one message, then silence until recovery |
| recovery | one all-clear |
| unrecognised response shape | immediately, throttled to once per 6 h |
| missing ticket product code | the ticket line is disabled, never the package prices |

Telegram send failures cannot be alerted over Telegram; they are recorded on
the health page with the API's own error, and the **Send test message** button
reports each chat's result.

**Silence is only allowed to mean "nothing moved".** If the tracker cannot see,
it says so, and the dashboard header says "last successful check: N hours ago".

---

## The dashboard

- **Tracks** — one row per track: party, window, nights, cheapest arrival now.
  Archived tracks sit in a collapsed section below.
- **Track** — per-hotel cheapest available arrival, all-time low, last seen, the
  alert toggle, and a strip of facts (markets, threshold, changes in the last
  24 h). Window trim/extend, Check now, stop, archive and delete live here.
  **Check now** forces this track's fetch regardless of cadence, with a
  60-second cooldown.
- **Hotel** — price history opens **by check-in date**, with the cheapest
  current date in the summary and #1/#2/#3 badges on the three cheapest
  arrivals. **Over time** is the alternative: x is when the price was checked,
  one stepped line per arrival date (the 12 cheapest, with history), with a
  7 day / 30 day / all selector. Market chips switch the chart and arrivals
  table; prices and the chart stay in that market's currency. **Arrivals**
  comes first: package, previous, all-time low, tickets, implied hotel cost,
  and when each arrival was last seen, with #1/#2/#3 on the cheapest dates.
  The **Markets** table follows, using ES as the reference and showing only EUR
  markets that differ; **Show all markets** opens the full matrix. GBP/CHF are
  shown natively and never get a delta. The same #1/#2/#3 ranks appear here.
- **Health** — per-source last success, consecutive failures, last error, recent
  failures and recent requests. The Telegram panel shows whether it is
  configured, the masked chat ids, the last API error, and a **Send test
  message** button.

Trimmed arrivals are hidden from the chart but their observations are untouched,
so extending the window again restores the lines exactly as they were.

### Archiving and deleting tracks

**Archive** hides a track, stops its polling and alerts, and keeps every link
and price. It can be restored; the track's own state (tracking or stopped) is
unchanged. **Stop** is still terminal: a stopped track stays in the list until
you archive or delete it.

**Delete** removes the track row and its hotel, market and date links. It does
not remove prices: a series is keyed by hotel, market, arrival date, stay length
and party, independently of any track, and observations belong to the series.
Ticket prices, parties and fetch history stay too. A future track for the same
holiday reuses those series and picks up their history, including observations
from before it was created.

---

