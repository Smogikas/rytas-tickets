# rytas-tickets

Tracks ticket sales for Vilniaus „Rytas" home games on [bilietai.lt](https://www.bilietai.lt):
how many tickets are still on sale, how many have sold, at what price, and how that compares
with the real attendance after the game.

No dependencies, Python 3.8+ standard library only.

## How it works

bilietai.lt exposes two public JSON endpoints that the site itself uses:

- `GET /api/v1/events/<id>` – event name, start time, venue
- `GET /api/v1/store/events/<id>/prices` – per section: price, fee, **tickets still available**

The API only reports what is *left*, not what was sold. So:

```
seats taken = section capacity - available
```

Important: "seats taken" includes season tickets and seats held back by the club, not only
tickets sold on bilietai.lt. The real online sales are the tickets the tracker *sees* disappear
between two snapshots ("Seen sold" / "Online sales seen by tracker"), so snapshot regularly
from the moment sales open.

Every run of `snapshot` appends the current price and availability of every section to
`data/snapshots.csv`. Because prices are logged with each snapshot, you can later compute
average ticket price and revenue, and see how prices changed over time.

### Known limitation: availability is capped at 22

The API never reports more than 22 available tickets per section (the site shows "20+").
While a section still has 22+ left, the exact sold count is unknown (the report shows
`<=N` as an upper bound). Once it drops below 22, the numbers are exact. Early in the
sales, most sections are capped; closer to the game more of them become exact.

## Usage

```bash
# 1. start tracking a game (paste the URL or just the id) – takes a first snapshot
python3 rytas.py add https://www.bilietai.lt/renginiai/PNJMMUNJYB/bcl-vilniaus-rytas-nantero-nanterre-92/seatmap

# 2. fill in section capacities in capacity.json ("seats": null -> number)

# 3. take snapshots regularly (see "Scheduling" below)
python3 rytas.py snapshot

# 4. after the game, record real attendance
python3 rytas.py attendance PNJMMUNJYB 8450 --note "official figure"

# 5. report
python3 rytas.py report            # all tracked games
python3 rytas.py report PNJMMUNJYB # one game
```

### Section capacities

`capacity.json` is created automatically with every section listed and `"seats": null`.
Fill in the seat count of each section (from the arena seat map / club figures).
Until a section has a capacity, it can only contribute "seen sold" numbers, which are
tickets observed leaving between two snapshots.

### Report columns

| Column | Meaning |
|---|---|
| Price / Fee | Latest ticket price and service fee (EUR) |
| Avail | Tickets available now (`22+` = capped, exact number unknown) |
| Seats | Capacity from `capacity.json` |
| Sold | Seats taken: `Seats - Avail`, or `<=N` upper bound while capped |
| Seen sold | Online sales observed between snapshots |
| Revenue | Estimated, excl. fees. Observed sales are priced at the price listed at that time; the rest at the first logged price (so season-ticket seats are valued at single-game price, an overestimate) |

If a section has several ticket types, they share the same seats: availability is taken once
per section and the cheapest ticket type's price is used.

After the game, `attendance - seats taken` shows no-shows (negative) or uncounted entries
such as complimentary tickets and children under 6 (positive). Only sections with an exact
count are included, so the comparison is most meaningful once most sections are below 22.

## Automatic snapshots (GitHub Actions)

`.github/workflows/snapshot.yml` runs **every hour at :17 UTC** on GitHub, so it works even
when your computer is off:

1. runs the test suite. If the tests fail, no snapshot is taken.
2. `python rytas.py snapshot` for every tracked game that hasn't started yet
3. commits `data/`, `events.json` and `capacity.json` back to the repo

Only changes are logged: a row is written when a section's price, fee or availability
changes. A section that disappears from the API is logged as `available = 0` (sold out or
withdrawn).

**Track a new game from the browser:** Actions → *Ticket snapshot* → *Run workflow* →
paste the bilietai.lt URL into *add_event*. Or run `python3 rytas.py add <url>` locally and
push `events.json`.

Every push that changes code also runs the tests (no snapshot). A failed run sends a
GitHub email notification.

Note: GitHub may delay scheduled runs at busy times, and disables schedules in repos with
no activity for 60 days (the snapshot commits count as activity while a game is tracked).

To run locally instead, a cron example:

```cron
17 * * * * cd /path/to/rytas-tickets && python3 rytas.py snapshot >> snapshot.log 2>&1
```

## Files

```
rytas.py          the tracker
.github/workflows/snapshot.yml   hourly snapshot + tests
tests/            offline tests (no network, simulated API)
events.json       tracked events (created by `add`)
capacity.json     seats per section (fill in by hand)
data/snapshots.csv   every snapshot: time, section, price, fee, available
data/attendance.csv  recorded attendance
```

## Notes

Be polite to the site: one snapshot per hour per game is plenty. This is a personal
analytics project and is not affiliated with bilietai.lt or BC Rytas.
