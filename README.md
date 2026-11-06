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
- **Season tickets** (estimate): seats seen in 2+ games and **never available in any**.
  They cluster in the central sideline sections (108, 116–120, lounges 102–104), mostly in
  the middle rows, in blocks of 2–10 seats.
- **Single tickets** = taken − season
  - **sold while tracked**: seen going available → taken, priced at the price in force then
  - **sold before tracking began** (5 Nov 2026): priced at the first logged price (estimate)
- **Club revenue** = **ticket value = price − fee**. The bilietai.lt price includes fees
  (booking, Arena Vilnius, venue administration, lounge), which do not go to the club:
  e.g. a 45 € lounge ticket = 30 € ticket value + 15 € fees.
- Prices are logged hourly whenever they change, so price changes before a game are kept.
- Only sections sold online are counted; sections that open later are picked up.

## Schedule (GitHub Actions, `.github/workflows/snapshot.yml`)

- **hourly (:17 UTC)**: tests → load data from the Sheet → prices for every upcoming game;
  seat plans hourly in the last 48 h before a game, every 6 h within 14 days, daily otherwise
  → save new rows to the Sheet → rebuild and publish the page.
- **Mondays 05:41 UTC**: look for new or rescheduled games of the tracked season.
- **code push**: tests, then the page is rebuilt (no data collection).
- **manual** (Actions → *Ticket snapshot* → *Run workflow*): track a season or a game,
  look for new games, fetch seat plans now.

Played games are kept: tracking stops at tip-off, the data stays.

## Data: Google Sheet

Tabs: **Rungtynės** (overview, one row per game), **one tab per game** (e.g. `2026-11-11 Nanterre 92`: every price change and seat change, „Įrašas" = kaina / vieta) and hidden technical tabs (`_events`, `_series`, `_layout`, `_shapes`).
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
