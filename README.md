# rytas-tickets

Tracks ticket sales for Vilniaus „Rytas" home games on [bilietai.lt](https://www.bilietai.lt),
seat by seat: how many seats are on sale, how many are taken, how many of those are season
tickets, what was sold online and at what price, and how that compares with the real
attendance after the game.

Runs by itself on GitHub Actions every hour. No dependencies, Python 3.8+ standard library only.

## What bilietai.lt exposes

The website loads public JSON from `https://www.bilietai.lt/api/v1` (an anonymous session
cookie from `POST /login/express` is required, the script handles it):

| Endpoint | Gives |
|---|---|
| `/series/<id>` | all games of the season series |
| `/events/<id>` | game name, start time, series id |
| `/store/events/<id>/prices` | open sections: price, fee, availability (capped at 22, shows "20+") |
| `/store/events/<id>/sections/<section>/seat-map` | **every seat** of an open section: row, place, status (1 = available, 0 = taken) |

Seat ids are the same in every game (same arena layout), which makes the season-ticket
estimate below possible. Closed sections return no seat plan; sections open during the
sales, and the tracker records when each one first appears.

## How the numbers are worked out

For each game, from the seat plans:

- **Seats**: seats in sections that have been on sale
- **Available**: available now
- **Taken**: seats − available
- **Season** (estimate): taken seats that were **never available in any tracked game**
  (seen in at least 2 games). A seat sold for one game would be free in the others, so a
  seat that is always taken belongs to a season-ticket holder (or is permanently held by
  the club). Games of the same competition (LKL, BCL) are compared with each other; a
  competition with fewer than 3 tracked games is compared with all games.
- **Single** = taken − season: single-game tickets
  - **Seen**: seats observed going available → taken while tracked: exact online sales
  - **Before**: taken when first seen and not season: sold before tracking began, comps
- **Revenue** (singles, excl. fees): each seen sale at the section price at that moment;
  "before" seats at the first logged price. **Avg** = revenue / singles.

The more games are tracked, the better the season estimate: games late in the season, with
few single tickets sold, help most. A seat a season holder releases for resale shows up
as available and is then not counted as season.

After the game, `attendance − taken` shows no-shows (negative) or entries without a seat
(comps, children under 6) (positive).

## Usage

```bash
python3 rytas.py add-series https://www.bilietai.lt/renginiai/PNJMMUNJYB/bcl-vilniaus-rytas-nantero-nanterre-92
python3 rytas.py snapshot            # prices; seat plans when due
python3 rytas.py snapshot --seats    # seat plans now for every upcoming game
python3 rytas.py discover            # new / rescheduled games of tracked series
python3 rytas.py attendance PNJMMUNJYB 8450 --note "official figure"
python3 rytas.py report              # one line per game
python3 rytas.py report PNJMMUNJYB   # one game, per section
python3 rytas.py season              # season-ticket seats by section
python3 rytas.py add <url|id>        # a single game outside the series
```

## Automatic runs (GitHub Actions)

`.github/workflows/snapshot.yml`:

- **every hour (:17 UTC)**: tests, then `snapshot`. Prices are logged every hour (1 request
  per game). Seat plans cost one request per open section, so they are fetched **hourly in
  the last 48 h before a game, every 6 h within 14 days, daily otherwise**.
- **Mondays 05:41 UTC**: `discover` adds newly published games and updates rescheduled ones.
- data is committed back to the repo, and the run page shows the season summary.
- code pushes and pull requests run the tests only. If the tests fail, no snapshot is taken.

Manual run: Actions → *Ticket snapshot* → *Run workflow*. Options: track a series or a
single game, discover now, fetch seat plans now.

Next season: run the workflow with *add_series* set to any game of the new season.

GitHub may delay scheduled runs at busy times and disables schedules after 60 days without
repo activity (snapshot commits count as activity).

## Files

```
rytas.py                         the tracker
.github/workflows/snapshot.yml   hourly snapshots, weekly discovery, tests
tests/                           offline tests with a simulated bilietai.lt
events.json                      tracked series and games
data/snapshots.csv               section prices/availability, changes only
data/seats/<game id>.csv         seat status, changes only (first row per seat = initial state)
data/attendance.csv              recorded attendance
```

## Notes

Requests are spaced out (0.4 s between seat plans) and kept to what the analysis needs.
This is a personal analytics project, not affiliated with bilietai.lt or BC Rytas.
