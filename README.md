# rytas-tickets

Ticket sales tracker for Vilniaus „Rytas" home games on [bilietai.lt](https://www.bilietai.lt),
seat by seat, with a live web page:

**https://smogikas.github.io/rytas-tickets/**

- **Games**: every upcoming / played game with the expected crowd (season tickets + single
  tickets sold), available tickets, club revenue and average ticket value; actual attendance
  for played games.
- **Arena plan**: pick a game; sections coloured by % taken, % single tickets or price; click
  a section for its numbers, every seat (season ticket / sold and at what price / available)
  and its price history; a table of all sections below.
- **Season tickets** (Abonementai): season seats per section with the club's 2026/27 membership
  prices and a revenue range (low = holders renewed at the renewal price where there was one,
  high = all paid the open-sale price).
- **Insights** (Analizė): single-ticket sales curves by days before tip-off (7 / 14 / 30 days / all;
  „Rungtynių diena": the game day from 00:00 to tip-off, sold since midnight;
  hover any line for its game, days before tip-off and tickets sold), price changes marked on the
  curve and listed, and for played games expected vs attended and attendance by
  opponent, weekday, tip-off time and competition (once attendance is typed into the Sheet).
- **Sales map** (Pardavimų žemėlapis): replay how a game's sections filled up over the last 7 / 14 / 30 days or
  all of tracking (slider / play), and section popularity over played games (average fill rate incl. season
  tickets, and average seats taken; far-away games are left out so they don't pull the averages down).
- Game cards show tickets sold in the last 24 h (▲/▼ against the 24 h before).
- Lithuanian / English, light / dark (follows the system by default).

Everything runs on GitHub Actions; the collected data is stored in a Google Sheet, not in
this repository.

## How the numbers are worked out

bilietai.lt's own JSON (`https://www.bilietai.lt/api/v1`, anonymous session from
`POST /login/express`):

| Endpoint | Gives |
|---|---|
| `/series/<id>` | all games of the season |
| `/events/<id>` | game name, start, venue layout id |
| `/store/events/<id>/prices` | open sections: price, fee |
| `/store/events/<id>/sections/<s>/seat-map` | every seat: row, place, position, status (1 available / 0 taken) |
| `/store/seat-map/<layout>.svg` | section outlines for the arena map |

Seat ids are the same in every game, so for each game:

- **Taken** = seats − available = **expected crowd** if every season holder comes
- **Season tickets** (estimate): seats seen in 3+ games (2 while fewer games have seat data) and
  **taken when first seen in every one** (games of the same competition; all games if it has
  fewer than 3). The set does not shrink during the season: a season seat given back later stays
  a season seat, and in that game counts as available, or as a single ticket once resold.
  Far-away games, with few single tickets sold yet, keep seats sold to single buyers out.
  They cluster in the central sideline sections (108, 116–120, lounges 102–104), mostly in
  the middle rows, in blocks of 2–10 seats.
- **Single tickets** = taken − season
  - **sold while tracked**: seen going available → taken, priced at the price in force then
    in the seat's own price zone (a seat's price class = the zone id; a section can have
    several zones, e.g. 21-ta kaina 79 €, 22-ta kaina 69 €)
  - **sold before tracking began** (5 Nov 2026): priced at the first logged price (estimate)
- **Club revenue** = **ticket value = price − fee**. The bilietai.lt price includes fees
  (booking, Arena Vilnius, venue administration, lounge), which do not go to the club:
  e.g. a 45 € lounge ticket = 30 € ticket value + 15 € fees.
- Prices are logged hourly whenever they change, so price changes before a game are kept.
  Only changed rows are written, so each zone's last price is carried forward to rebuild the
  full price list at any moment; a zone selling out is not a price change.
- Only sections sold online are counted; sections that open later are picked up.
- Placeholder seats (place "NĖRA" = none; 9 in 403–404 row 9) are not real seats: left out of every
  count, grey in the seat plan. Seats without a price class but real (a block in 119–120 rows 16–19)
  count like any seat: taken in every game, they are season tickets.
- A section off the sale list still returns a seat plan, but with every seat "taken" (= can't be
  bought), so it is not read; e.g. 409 is off sale for Nanterre and Žalgiris while almost empty
  in other games. The sold-out / withdrawn rule decides instead.
- **B Tribūna** (110–111, the ultras' sections) is not sold on bilietai.lt: its full capacity (from
  the venue plan) is added to the expected crowd as an estimate, unless it appears on sale.
  For some games (Žalgiris) B Tribūna covers 109–112: sections 109 and 112 count as B Tribūna (full)
  in a game where they are not on sale online but are on sale in other games.
- **107** (137 seats) was sold out as memberships (club posts, Aug 2026) and is never on single sale:
  all its seats count as season tickets. (107 AMUSNET Lounge, the 20-seat corner, is read normally.)
- **BCL Top 8** games are not covered by memberships: every seat is a normal ticket there, no
  season tickets are counted.
- Season seats in a section that is not on sale online for a game still count (as season tickets,
  taken, expected crowd) in that game.
- The full venue plan gives the seat count of every section and the arena capacity (Sheet tab
  **Talpa**; `rytas.py capacity`) and seat positions of sections never on sale.
- A section leaving the sale list is **sold out** (last seats counted as sold, striped on the
  map) if it had an exact count (< 22) left, or the game is one whose sections sell out
  (Žalgiris), or it sold at least as many seats in the last 24 h as it had left. Otherwise it
  was **withdrawn** (nothing counted as sold, dashed outline, closing date shown); free seats in
  withdrawn sections are not counted as available tickets.
- Price changes: the section's price history card (first → now, and every change with ▲/▼);
  ▲/▼ next to the price in the sections table.
- Tracking of a game stops at tip-off, so sections closing at the start are not seen as withdrawn.
  Seats already seen sold always stay sold, also in a withdrawn section.

## Schedule (GitHub Actions, `.github/workflows/snapshot.yml`)

- **hourly (:17 UTC)**: tests → load data from the Sheet → prices for every upcoming game;
  full seat plans hourly in the last 5 days before a game, every 6 h within 14 days, daily
  otherwise; **hot games** (Žalgiris, BCL, or any game selling 50+ seats in 24 h) hourly
  however far away → save new rows to the Sheet → rebuild and publish the page.
  Between full scans, when the hourly price check sees a section open or close, a price
  change, or fewer tickets left in a section showing under 22, that section's seat plan is
  read right away (sections showing "22+" give no signal and wait for the full scan).
- **10 minutes before tip-off**: a final full check (every seat and price) of that game, in
  its own job started by the hourly run that sees the game starting within 75 minutes.
- **daily at 23:17 (Vilnius)**: the hourly run also looks for new or rescheduled games of the
  tracked season.
- **1st and 15th of the month**: the workflow re-enables itself, since GitHub switches off
  schedules of public repositories after 60 days without repository activity (the data goes to
  the Sheet, not the repo).
- **code push**: tests, then the page is rebuilt (no data collection).
- **manual** (Actions → *Ticket snapshot* → *Run workflow*): track a season or a game,
  look for new games, fetch seat plans now.

Played games are kept: tracking stops at tip-off, the data stays.

**Warnings**: a small ⚠ badge next to "Duomenys atnaujinti" on the page (tap it for the list) when
the page has not updated for 3+ hours, a game could not be read from bilietai.lt, saving to the
Sheet or the new-game check failed, or a game's seat plans are missing or overdue. Details are in
the run logs (GitHub → Actions). GitHub also emails the repository owner when a run fails.

## Data: Google Sheet

Tabs: **Rungtynės** (overview, one row per game), **one tab per game** (e.g. `2026-11-11 Nanterre 92`: every price change and seat change, „Įrašas" = kaina / vieta) **Talpa** (seats per section and arena capacity) and hidden technical tabs (`_events`, `_series`, `_layout`, `_shapes`, `_capacity`).
Game rows are only appended, never changed. **Actual attendance**: type it into the last
column „Lankomumas (įrašykite)" of the Rungtynės tab (the job never overwrites that column); it shows on the page
after the next hourly run.

Setup (once): a Google Cloud service account with the Sheets API enabled, its JSON key in
the repository secret `GOOGLE_SA_KEY`, and the Sheet shared with the account as Editor.
The Sheet id is in the workflow file.

## Local use

```bash
pip install google-auth requests                  # only for sheets.py
export SHEET_ID=... GOOGLE_SA_KEY="$(cat key.json)"
python3 sheets.py pull                            # Sheet -> data/, events.json
python3 rytas.py report                           # one line per game
python3 rytas.py report PNJMMUNJYB                # one game, per section
python3 rytas.py season                           # season-ticket seats by section
python3 rytas.py export _site && cp site/index.html _site/   # the web page, locally
python3 -m unittest discover -s tests             # tests (no network)
```

Other commands: `add-series <url>`, `add <url>`, `discover`, `snapshot [--seats]`,
`attendance <id> <number>`.

Requests are spaced out (0.4 s between seat plans). Personal analytics project, not
affiliated with bilietai.lt or BC Rytas.
