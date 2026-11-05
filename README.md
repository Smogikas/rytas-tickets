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
sold = section capacity - available
```

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
| Sold | `Seats - Avail`, or `<=N` upper bound while capped |
| Seen sold | Tickets observed leaving between snapshots |
| Revenue | Estimated, excl. fees. Observed sales are priced at the price listed at that time; the rest at the first logged price |

Attendance minus online sold shows season tickets, club allocations, other sales channels
and complimentary tickets, i.e. everything not sold through bilietai.lt.

## Scheduling

Sales speed up before the game, so snapshot more often near match day. Cron example
(hourly):

```cron
0 * * * * cd /path/to/rytas-tickets && python3 rytas.py snapshot >> snapshot.log 2>&1
```

Or use a GitHub Actions schedule that commits the CSV back to the repo.

## Files

```
rytas.py          the tracker
events.json       tracked events (created by `add`)
capacity.json     seats per section (fill in by hand)
data/snapshots.csv   every snapshot: time, section, price, fee, available
data/attendance.csv  recorded attendance
```

## Notes

Be polite to the site: one snapshot per hour per game is plenty. This is a personal
analytics project and is not affiliated with bilietai.lt or BC Rytas.
