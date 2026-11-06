#!/usr/bin/env python3
"""Rytas ticket tracker.

Logs bilietai.lt prices and seat-by-seat availability for Vilniaus "Rytas"
home games, and reports seats taken, season-ticket estimate, online sales,
revenue and average price, compared with real attendance.

Commands:
  add-series <url|event id|series id>  track every game of a season series
  add <url|id>                         track a single game
  discover                             add new / update rescheduled games of tracked series
  snapshot [--seats]                   log prices (+ seat plans when due) for upcoming games
  attendance <id> <number>             record actual attendance after the game
  report [<id>]                        season summary, or one game in detail
  season                               season-ticket seat estimate
  capacity                             seats per section (full venue plan) and arena capacity
  final [--check]                      full check 10 min before a game starting within 75 min
"""
import argparse
import csv
import http.cookiejar
import json
import os
import re
import sys
import time
from bisect import bisect_right
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://www.bilietai.lt/api/v1"
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots.csv"       # section prices + capped availability
SEATS_DIR = DATA / "seats"               # one CSV per game, seat status changes
ATTENDANCE = DATA / "attendance.csv"
LAYOUT = DATA / "layout.csv"             # seat positions for drawing the seat plan
SHAPES = DATA / "shapes.csv"             # section outlines of the arena overview map
CAPACITY = DATA / "capacity.csv"         # seats per section in the full venue plan
EVENTS = ROOT / "events.json"
RUN_STATUS = DATA / "run_status.json"    # what failed in this run, for the page's warnings
# Local working copies. The GitHub job loads them from / saves them to the
# Google Sheet (sheets.py), so no data is stored in the repository.

# The prices API never reports more than 22 available per section ("22+").
# Exact numbers come from the seat plans instead.
CAP = 22
PAUSE = 0.4  # seconds between seat-plan requests, be polite to the site

SNAP_FIELDS = ["ts_utc", "event_id", "section_id", "section", "zone",
               "ticket_type_id", "price", "fee", "available", "zone_id"]
SEAT_FIELDS = ["ts_utc", "section_id", "seat_id", "row", "place", "price_class", "status"]
ATT_FIELDS = ["event_id", "attendance", "recorded_utc", "note"]
LAYOUT_FIELDS = ["seat_id", "section_id", "row", "place", "x", "y"]
SHAPE_FIELDS = ["conf_id", "section_id", "points"]
CAP_FIELDS = ["conf_id", "section_id", "seats"]

# B Tribūna: Rytas fan sectors 110-111 (ultras), not sold on bilietai.lt. Assumed full: their whole
# capacity is added to the expected crowd unless they appear on sale (then real
# seat data is used). Section id -> name.
FAN_SECTIONS = {"79993": "110", "72934": "111"}
# For some games (Žalgiris) B Tribūna is extended to the neighbouring sections 109 and 112:
# such a section counts as B Tribūna (full) in a game where it is not on sale online
# while it is on sale in other games. Section numbers (start of the section title).
FAN_EXTRA = ("109", "112")
# Sections sold out as season memberships and never on single sale: every seat is a
# season ticket in each game memberships cover (club posts, Aug 2026: 107 "išparduota").
# Section id -> name. (107 AMUSNET Lounge, the 20-seat corner, is on sale and read normally.)
SEASON_SECTIONS = {"87884": "107"}
# Games memberships don't cover (BCL Top 8; a league-run final four): every seat is a
# normal ticket there, so no season tickets are counted.
NO_SEASON = re.compile(r"top\s*-?\s*8|ketvirtfinal|quarter.?final|final\s*four|finalo\s+ketvert", re.I)
# Games whose sections sell out rather than get withdrawn: a section of such a game
# leaving the sale list counts as sold out even with "22+" left at the last check.
SELLOUT_OPPONENTS = ("Žalgiris",)
# Games that can sell fast: full seat plans every hour, however far away the game is.
# Any game selling HOT_SALES+ seats in 24 h is treated the same.
HOT_OPPONENTS = ("Žalgiris",)
HOT_COMPETITIONS = ("BCL",)
HOT_SALES = 50
# Season-ticket estimate: a seat must be seen in this many games (fewer if fewer
# games have seat data, but at least 2) and never be available in any of them.
SEASON_MIN_GAMES = 3
# Ticket price on bilietai.lt = ticket value (club) + fees (booking, arena,
# venue administration, lounge). The API gives "price" (total) and "fee";
# the club's revenue is price - fee.


# ---------------------------------------------------------------- fetching

HEADERS = {
    "User-Agent": "rytas-tickets/0.3 (personal project)",
    "Accept": "application/json",
}
_opener = None


def opener():
    """URL opener with an anonymous bilietai.lt session.

    The store API answers 403 without a session cookie. The website gets one
    by POSTing (empty body) to /login/express, which returns
    {"authenticated": false} and sets the cookie. We do the same once per run.
    """
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        req = urllib.request.Request(f"{API}/login/express", data=b"", method="POST", headers=HEADERS)
        with _opener.open(req, timeout=30) as r:
            r.read()
    return _opener


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with opener().open(req, timeout=30) as r:
        return json.load(r)


def get_text(url):
    req = urllib.request.Request(url, headers=dict(HEADERS, Accept="*/*"))
    with opener().open(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def fetch_shapes(conf_id):
    """Section outlines of the arena overview map: [(section_id, "x,y x,y ...")]
    in a 1000x1000 space, from the seat-map SVG the website draws."""
    svg = get_text(f"{API}/store/seat-map/{conf_id}.svg")
    out = []
    for tag in re.findall(r"<polygon\b[^>]*>", svg):
        sid = re.search(r"""id=['"]section(\d+)['"]""", tag)
        pts = re.search(r"""points=['"]([^'"]+)['"]""", tag)
        if sid and pts:
            out.append((sid.group(1), " ".join(pts.group(1).split())))
    return out


def fetch_info(event_id):
    return get_json(f"{API}/events/{event_id}?language=lt")


def fetch_prices(event_id):
    return get_json(f"{API}/store/events/{event_id}/prices?language=lt")


def fetch_series(series_id):
    return get_json(f"{API}/series/{series_id}?language=lt")


def fetch_section_seats(event_id, section_id):
    """Seat list of one section, or None if the section is not on sale (HTTP 4xx).

    Each seat: seatId (same id in every game at this venue layout), row,
    place, priceClass (= price zone id), status (1 = available, 0 = taken).
    """
    try:
        j = get_json(f"{API}/store/events/{event_id}/sections/{section_id}/seat-map?language=lt")
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500 and e.code not in (403, 429):
            return None
        raise
    return j.get("seatMapTickets") or []


# ----------------------------------------------------------------- storage

def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_csv(path, fields, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    if not new:
        with path.open(newline="", encoding="utf-8") as f:
            head = next(csv.reader(f), [])
        if head != list(fields):          # older file without a new column: rewrite it
            old = read_csv(path)
            path.unlink()
            append_csv(path, fields, old)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_events():
    """events.json: {"series": {id: title}, "events": {id: {...}}}.
    Older files were a flat {event_id: {...}} dict; upgrade them."""
    raw = load_json(EVENTS, {})
    if "events" not in raw:
        raw = {"series": {}, "events": raw}
    raw.setdefault("series", {})
    return raw


def save_events(ev):
    ev["events"] = dict(sorted(ev["events"].items(), key=lambda kv: kv[1].get("start_utc", "")))
    save_json(EVENTS, ev)


def load_snapshots(event_id):
    rows = []
    for r in read_csv(SNAPSHOTS):
        if r["event_id"] != event_id:
            continue
        r["price"] = float(r["price"])
        r["fee"] = float(r["fee"])
        r["available"] = int(r["available"])
        rows.append(r)
    return rows


def seat_path(event_id):
    return SEATS_DIR / f"{event_id}.csv"


def load_seats(event_id):
    rows = read_csv(seat_path(event_id))
    for r in rows:
        r["status"] = int(r["status"])
    return rows


def latest_attendance(event_id):
    found = None
    for r in read_csv(ATTENDANCE):
        if r["event_id"].strip() == event_id:
            try:
                found = int(float(str(r["attendance"]).replace(" ", "").replace(",", ".")))
            except ValueError:
                pass
    return found


def latest_by(rows, key):
    last = {}
    for r in sorted(rows, key=lambda r: r["ts_utc"]):
        last[key(r)] = r
    return last


def event_id_from(text):
    m = re.search(r"/renginiai/([A-Za-z0-9]+)", text)
    return m.group(1) if m else text.strip()


# --------------------------------------------------------------- snapshots

def norm_id(v):
    """Ids may come back from the Sheet as numbers: 1370.0 -> '1370'."""
    v = str(v if v is not None else "").strip()
    return v[:-2] if v.endswith(".0") else v


def price_key(r):
    """A price row's identity: section + price zone + ticket type (zones of one
    section share the ticket type id, so the zone is needed)."""
    return (r["section_id"], r.get("zone", ""), r["ticket_type_id"])


def flatten(event_id, ts, prices):
    """One row per (section, price zone, ticket type)."""
    rows = []
    for sec in prices.get("eventSections", []):
        for zone in sec.get("priceZones", []):
            for tt in zone.get("ticketTypes", []):
                rows.append({
                    "ts_utc": ts, "event_id": event_id,
                    "section_id": str(sec["id"]), "section": sec["title"],
                    "zone": zone.get("description", ""), "ticket_type_id": str(tt["id"]),
                    "price": tt["price"], "fee": tt.get("fee", 0),
                    "available": tt["availableTickets"], "zone_id": norm_id(zone.get("id", "")),
                })
    return rows


def take_snapshot(event_id, prices):
    """Log section prices, writing only rows that changed since the last log.

    A section that was on sale but is missing from the API now is logged as
    available=0 (sold out / withdrawn). Returns (current rows, rows written).
    """
    ts = now_utc()
    rows = flatten(event_id, ts, prices)
    last = latest_by(load_snapshots(event_id), price_key)
    current = {price_key(r) for r in rows}
    for key, prev in last.items():
        if key not in current and prev["available"] > 0:
            rows.append(dict(prev, ts_utc=ts, available=0))
    written = [
        r for r in rows
        if (k := price_key(r)) not in last
        or (float(r["price"]), float(r["fee"]), int(r["available"]))
        != (last[k]["price"], last[k]["fee"], last[k]["available"])
        or (r.get("zone_id") and not last[k].get("zone_id"))   # once: rows logged before zone ids were kept
    ]
    append_csv(SNAPSHOTS, SNAP_FIELDS, written)
    return rows, written


def recent_sales(rows, since, section=None):
    """Seats seen going available -> taken at or after `since` (ISO ts)."""
    n = 0
    for seat_rows in timelines(r for r in rows if section is None or r["section_id"] == section).values():
        for a, b in zip(seat_rows, seat_rows[1:]):
            if a["status"] == 1 and b["status"] == 0 and b["ts_utc"] >= since:
                n += 1
                break
    return n


def take_seat_snapshot(event_id, prices, only=None, sellout=False):
    """Fetch the seat plan of every open section (or only the sections in
    `only`) and log seats whose status changed.

    Sections only appear in the prices list while on sale; some open later.
    A section seen before that no longer returns a seat plan has its
    remaining available seats logged as taken (sold out / withdrawn).
    Returns (current seat rows, rows written, sections fetched).
    """
    ts = now_utc()
    log = load_seats(event_id)
    last = latest_by(log, lambda r: r["seat_id"])
    open_ids = [str(s["id"]) for s in prices.get("eventSections", [])]
    had_available = {r["section_id"] for r in last.values() if r["status"] == 1}
    to_fetch = open_ids + sorted(had_available - set(open_ids))
    if only is not None:
        to_fetch = [sid for sid in to_fetch if sid in only]

    current = {}
    known_layout = {r["seat_id"] for r in read_csv(LAYOUT)}
    new_layout = []
    for i, sid in enumerate(to_fetch):
        if i and sid in open_ids:
            time.sleep(PAUSE)
        # A section off the sale list still returns a seat plan, but with every seat
        # "taken" (= can't be bought, not sold), so it is not read: the rule below decides.
        tickets = None if sid not in open_ids else fetch_section_seats(event_id, sid)
        if tickets is None:
            if sid not in open_ids:
                # The section left the sale list. It sold out (the last seats were sold) if
                # - fewer than CAP seats were left (an exact count, watched hourly), or
                # - the game is one whose sections sell out, not get withdrawn (Žalgiris), or
                # - in the last 24 h it sold at least as many seats as it had left.
                # Otherwise it was withdrawn: nothing is counted as sold.
                remaining = [r for r in last.values() if r["section_id"] == sid and r["status"] == 1]
                day_ago = (parse_ts(ts) - timedelta(hours=24)).isoformat()
                if (len(remaining) < CAP or sellout
                        or recent_sales(log, day_ago, sid) >= len(remaining)):
                    for r in remaining:
                        current[r["seat_id"]] = dict(r, ts_utc=ts, status=0)
            continue
        for t in tickets:
            seat = str(t["seatId"])
            current[seat] = {
                "ts_utc": ts, "section_id": sid, "seat_id": seat,
                "row": t.get("row", ""), "place": t.get("place", ""),
                "price_class": str(t.get("priceClass", "")),
                "status": 1 if t.get("status") == 1 else 0,
            }
            if seat not in known_layout and "x" in t:
                known_layout.add(seat)
                new_layout.append({"seat_id": seat, "section_id": sid, "row": t.get("row", ""),
                                   "place": t.get("place", ""), "x": t["x"], "y": t["y"]})
    written = [r for k, r in current.items() if k not in last or last[k]["status"] != r["status"]]
    append_csv(seat_path(event_id), SEAT_FIELDS, written)
    append_csv(LAYOUT, LAYOUT_FIELDS, new_layout)
    return current, written, len(to_fetch)


def fetch_venue_map(conf_id):
    """The full venue plan: every seat of the layout with its section, row,
    place and position (also sections never on sale)."""
    return get_json(f"{API}/store/seat-map/{conf_id}")


def load_capacity(conf_id):
    return {r["section_id"]: int(float(r["seats"])) for r in read_csv(CAPACITY) if r["conf_id"] == str(conf_id)}


def ensure_capacity(conf_id):
    """Once per venue layout: seats per section from the full venue plan, and
    seat positions for arena-map sections not seen in any seat plan yet (so
    sections that are never on sale, like the fan sectors, can be drawn)."""
    if not conf_id or load_capacity(conf_id):
        return 0
    seats = fetch_venue_map(conf_id).get("seats") or []
    counts = Counter(str(x["section"]) for x in seats)
    append_csv(CAPACITY, CAP_FIELDS, [{"conf_id": str(conf_id), "section_id": sid, "seats": n}
                                      for sid, n in sorted(counts.items())])
    map_sections = {r["section_id"] for r in read_csv(SHAPES) if r["conf_id"] == str(conf_id)}
    known = {r["seat_id"]: r for r in read_csv(LAYOUT)}
    overlap = [x for x in seats if str(x["code"]) in known]
    same = sum(1 for x in overlap if (int(float(known[str(x["code"])]["x"])), int(float(known[str(x["code"])]["y"])))
               == (int(x["x"]), int(x["y"])))
    new = [{"seat_id": str(x["code"]), "section_id": str(x["section"]), "row": x.get("row", ""),
            "place": x.get("place", ""), "x": x["x"], "y": x["y"]}
           for x in seats if str(x["section"]) in map_sections and str(x["code"]) not in known]
    if overlap and same < 0.95 * len(overlap):
        print(f"venue plan {conf_id}: positions differ from seat plans ({same}/{len(overlap)}), not using them")
        new = []
    append_csv(LAYOUT, LAYOUT_FIELDS, new)
    print(f"venue plan {conf_id}: {len(counts)} sections, {len(seats)} seats; "
          f"positions match {same}/{len(overlap)} known seats; +{len(new)} seat positions")
    return len(counts)


def ensure_shapes(conf_id):
    """Store the arena map's section outlines once per venue layout."""
    if not conf_id or any(r["conf_id"] == str(conf_id) for r in read_csv(SHAPES)):
        return 0
    rows = [{"conf_id": str(conf_id), "section_id": s, "points": p} for s, p in fetch_shapes(conf_id)]
    append_csv(SHAPES, SHAPE_FIELDS, rows)
    return len(rows)


def opponent_in(name, teams):
    _comp, opp = split_name(name)
    return any(t.lower() in opp.lower() for t in teams)


def is_hot(meta, seat_rows, now):
    """A game that can sell out fast: a big opponent or competition, or 50+ seats
    sold in the last 24 h."""
    if opponent_in(meta.get("name", ""), HOT_OPPONENTS) or competition(meta.get("name", "")) in HOT_COMPETITIONS:
        return True
    return recent_sales(seat_rows, (now - timedelta(hours=24)).isoformat()) >= HOT_SALES


def seats_due(meta, now, hot=False):
    """Seat plans cost one request per section, so fetch them less often for
    games far away: hourly in the last 48 h (and always for hot games), every 6 h
    within 14 days, else daily."""
    last = meta.get("last_seats_utc")
    if not last:
        return True
    hours_left = (parse_ts(meta["start_utc"]) - now).total_seconds() / 3600
    interval = 1 if hours_left <= 48 or hot else 6 if hours_left <= 24 * 14 else 24
    return now - parse_ts(last) >= timedelta(hours=interval * 0.9)


def snapshot_event(event_id, meta, now, force_seats=False):
    if not meta.get("conf_id"):  # venue layout id, looked up once per game
        meta["conf_id"] = str(fetch_info(event_id).get("confId") or "")
    ensure_shapes(meta["conf_id"])
    ensure_capacity(meta["conf_id"])
    prices = fetch_prices(event_id)
    rows, written = take_snapshot(event_id, prices)
    msg = f"prices: {len(written)} changed"
    sellout = opponent_in(meta.get("name", ""), SELLOUT_OPPONENTS)
    hot = is_hot(meta, load_seats(event_id), now)
    if hot:
        msg += " [hot: hourly seat plans]"
    if force_seats or seats_due(meta, now, hot):
        current, changed, n = take_seat_snapshot(event_id, prices, sellout=sellout)
        meta["last_seats_utc"] = now_utc()
        avail = sum(r["status"] for r in current.values())
        msg += f"; seats: {len(current)} in {n} sections, {avail} available, {len(changed)} changed"
    elif written:
        # Between full scans: the hourly price check saw something change in
        # these sections (opened, closed, price changed, or tickets left
        # changed while under the 22 cap = sales), so read just their seat plans now.
        changed_sections = {r["section_id"] for r in written}
        current, changed, n = take_seat_snapshot(event_id, prices, only=changed_sections, sellout=sellout)
        msg += f"; seats of {n} changed section(s): {len(changed)} seats changed"
    return msg


# ---------------------------------------------------------------- analysis

def competition(name):
    """'LKL: Vilniaus Rytas - ...' -> 'LKL'."""
    return name.split(":")[0].strip() if ":" in name else ""


def timelines(seat_rows):
    tl = defaultdict(list)
    for r in sorted(seat_rows, key=lambda r: r["ts_utc"]):
        tl[r["seat_id"]].append(r)
    return tl


def season_groups(names):
    """event_id -> tuple of event_ids to compare with for the season-ticket estimate.

    Games of the same competition are compared (season tickets might not cover
    every competition); a competition with fewer than 3 tracked games is
    compared with all games."""
    covered = {eid: n for eid, n in names.items() if season_covered(n)}
    by_comp = defaultdict(list)
    for eid, name in covered.items():
        by_comp[competition(name)].append(eid)
    everyone = tuple(sorted(covered))
    groups = {eid: tuple(sorted(by_comp[competition(n)])) if len(by_comp[competition(n)]) >= 3 else everyone
              for eid, n in covered.items()}
    groups.update({eid: () for eid in names if eid not in covered})   # not covered: no season tickets
    return groups


def season_covered(name):
    """Do season memberships cover this game? Not BCL Top 8 (or a league-run final four)."""
    return not ("BCL" in competition(name).upper() and NO_SEASON.search(name))


def season_seats(group, tls):
    """Seats observed in at least SEASON_MIN_GAMES games of the group (fewer if fewer
    games have seat data, but at least 2) and taken the first time they were seen in
    every one of them. These were taken before single-game sales could reach them:
    season tickets (and any seats held in advance for every game). Far-away games,
    with few single tickets sold yet, keep seats sold to single buyers out.

    A season seat that becomes free later (the holder gives it back) stays a season
    seat, so the set does not shrink during the season; in that game it counts as
    available, or as a single ticket once resold (classify_seats)."""
    with_data = sum(1 for eid in group if tls.get(eid))
    need = max(2, min(SEASON_MIN_GAMES, with_data))
    seen, ever_free = Counter(), set()
    for eid in group:
        for seat, rows in tls.get(eid, {}).items():
            if blocked(rows[-1]):
                continue
            seen[seat] += 1
            if rows[0]["status"] == 1:         # free when first seen: not a season seat
                ever_free.add(seat)
    return {s for s, n in seen.items() if n >= need and s not in ever_free}


def zone_names(price_rows):
    """zone id -> zone name ('1370' -> '21-ta kaina') from every row that has the id.
    Zone ids are the event's price levels and the same in every game, so a zone that
    sold out before ids were logged is still matched through the other games."""
    return {norm_id(r["zone_id"]): r.get("zone", "") for r in price_rows if r.get("zone_id")}


def price_lookup(price_rows, names=None):
    """section_id -> {"times": [ts], "states": [{zone: (price, fee, available)}], "ids": {zone_id: zone}}.

    Rows are logged only when they change, so the full price list in force at a
    moment is rebuilt by carrying every zone's last row forward. `names` (zone id ->
    name, from all games) fills in ids this game's rows don't have."""
    per = defaultdict(list)
    for r in sorted(price_rows, key=lambda r: r["ts_utc"]):
        per[r["section_id"]].append(r)
    out = {}
    for sid, rows in per.items():
        state, times, states, ids = {}, [], [], {}
        for r in rows:
            zone = r.get("zone", "")
            state[(zone, r["ticket_type_id"])] = (r["price"], r["fee"], r["available"])
            if r.get("zone_id"):
                ids[norm_id(r["zone_id"])] = zone
            zones = {}
            for (z, _tt), (p, f, a) in state.items():      # a zone's price: its cheapest ticket type
                if z not in zones or p < zones[z][0]:
                    zones[z] = (p, f, max(a, zones.get(z, (0, 0, 0))[2]))
            if times and times[-1] == r["ts_utc"]:
                states[-1] = zones
            else:
                times.append(r["ts_utc"])
                states.append(zones)
        seen = {r.get("zone", "") for r in rows}
        for zid, zname in (names or {}).items():
            if zid not in ids and zname in seen:
                ids[zid] = zname
        out[sid] = {"times": times, "states": states, "ids": ids}
    return out


def state_at(sec, ts):
    """The section's zones in force at ts (the first logged ones if ts is earlier)."""
    i = bisect_right(sec["times"], ts) - 1
    return sec["states"][max(i, 0)]


def price_at(prices, sid, ts, zone_id=None):
    """(price, fee) in force at ts for the seat's price zone (its priceClass); without
    a known zone the cheapest zone still on sale. (None, None) without prices."""
    sec = prices.get(sid)
    if not sec or not sec["states"]:
        return None, None
    zones = state_at(sec, ts)
    z = sec["ids"].get(norm_id(zone_id)) if zone_id not in (None, "") else None
    if z is None and len(zones) == 1:
        z = next(iter(zones))
    if z in zones:
        return zones[z][0], zones[z][1]
    pick = [v for v in zones.values() if v[2] > 0] or list(zones.values())
    p, f, _a = min(pick)
    return p, f


def zone_histories(prices, sid):
    """zone -> [(ts, price, fee)]: only the moments that zone's price or fee changed."""
    out = {}
    sec = prices.get(sid)
    if not sec:
        return out
    for t, zones in zip(sec["times"], sec["states"]):
        for z, (p, f, _a) in zones.items():
            h = out.setdefault(z, [])
            if not h or (h[-1][1], h[-1][2]) != (p, f):
                h.append((t, p, f))
    return out


def current_zones(prices, sid):
    """[(zone, price, fee, on sale)] now, cheapest first."""
    sec = prices.get(sid)
    if not sec or not sec["states"]:
        return []
    return sorted(((z, p, f, a > 0) for z, (p, f, a) in sec["states"][-1].items()), key=lambda x: (x[1], x[0]))


def price_history(prices, sid):
    """Changes of the section's headline price (the zone that is cheapest now)."""
    zs = current_zones(prices, sid)
    if not zs:
        return []
    open_zs = [z for z in zs if z[3]] or zs
    return zone_histories(prices, sid).get(open_zs[0][0], [])


# seat categories
SEASON, SOLD_SEEN, SOLD_BEFORE, AVAILABLE, BLOCKED = "S", "O", "B", "A", "X"


def blocked(row):
    """A placeholder in the seat plan, not a real seat (place "NĖRA" = none, e.g. 403-404
    row 9): left out of every count. Other seats without a price class (price class 0,
    e.g. a block in 119-120 rows 16-19) are counted like any seat: taken in every game,
    they are season tickets."""
    return "NĖRA" in str(row.get("place", "")).upper()


def classify_seats(tl, prices, season):
    """seat_id -> (section_id, category, price, fee, ts).

    S  season ticket (taken when first seen in every compared game, never free in this one)
    O  sold while tracked: price/fee in force when the sale was seen (also a season
       seat given back and resold in this game)
    B  taken before tracking began, not season: first logged price (estimate)
    A  available: current price
    """
    out = {}
    for seat, rows in tl.items():
        first, last = rows[0], rows[-1]
        sid = last["section_id"]
        if blocked(last):
            out[seat] = (sid, BLOCKED, None, None, None)
            continue
        if last["status"] == 1:
            cat, ts = AVAILABLE, "9999"
        elif seat in season and not any(r["status"] == 1 for r in rows):
            cat, ts = SEASON, last["ts_utc"]
        elif any(r["status"] == 1 for r in rows):
            cat, ts = SOLD_SEEN, last["ts_utc"]   # changes-only log: last row = the sale
        else:
            cat, ts = SOLD_BEFORE, first["ts_utc"]
        p, f = price_at(prices, sid, ts, last.get("price_class"))
        out[seat] = (sid, cat, p, f, last["ts_utc"] if cat == SOLD_SEEN else None)
    return out


def analyse_event(tl, price_rows, season, names=None):
    """Seat-level figures for one game.

    seats       seats in sections that have been on sale (from seat plans)
    available   available now
    taken       seats - available = expected crowd if every season holder comes
    season      taken seats in the season-ticket estimate
    sold_seen   seats seen going available -> taken while tracked: exact online sales
    pre_taken   taken when first seen and not season: sold before tracking began
    singles     taken - season: single-game tickets (estimate)
    revenue     club revenue = ticket value (price - fee) of the singles; seen sales
                at the price in force when seen, earlier ones at the first logged price
    gross       what buyers paid incl. fees
    """
    prices = price_lookup(price_rows, names)
    titles = {r["section_id"]: r["section"] for r in sorted(price_rows, key=lambda r: r["ts_utc"])}
    first_ts = min((rows[0]["ts_utc"] for rows in tl.values()), default=None)
    first_seen = {}
    for rows in tl.values():
        sid = rows[-1]["section_id"]
        if sid not in first_seen or rows[0]["ts_utc"] < first_seen[sid]:
            first_seen[sid] = rows[0]["ts_utc"]

    keys = ["seats", "available", "taken", "season", "sold_seen", "pre_taken"]
    sec = defaultdict(lambda: dict({k: 0 for k in keys}, revenue=0.0, gross=0.0))
    count_key = {SEASON: "season", SOLD_SEEN: "sold_seen", SOLD_BEFORE: "pre_taken", AVAILABLE: "available"}
    for seat, (sid, cat, p, f, _ts) in classify_seats(tl, prices, season).items():
        if cat == BLOCKED:
            continue
        x = sec[sid]
        x["seats"] += 1
        x[count_key[cat]] += 1
        if cat != AVAILABLE:
            x["taken"] += 1
        if cat in (SOLD_SEEN, SOLD_BEFORE) and p is not None:
            x["revenue"] += p - (f or 0)
            x["gross"] += p

    # is the section in the sale list now? Only when none of its zones is left: a zone can
    # sell out or be replaced (renamed, same price) while the section stays on sale.
    # -> (on sale, since when it has been off sale)
    listed = {}
    for sid, sec_p in prices.items():
        since = None
        for ts, zones in zip(sec_p["times"], sec_p["states"]):
            if any(a > 0 for _p, _f, a in zones.values()):
                since = None
            elif since is None:
                since = ts
        listed[sid] = (since is None, since)

    sections = []
    for sid, x in sec.items():
        x["singles"] = x["taken"] - x["season"]
        later = first_ts and parse_ts(first_seen[sid]) - parse_ts(first_ts) > timedelta(hours=1)
        zs = current_zones(prices, sid)
        open_zs = [z for z in zs if z[3]] or zs
        p, f = (open_zs[0][1], open_zs[0][2]) if open_zs else (None, None)
        hist = zone_histories(prices, sid)
        on_sale, since = listed.get(sid, (True, None))
        if x["seats"] and x["available"] == 0:
            status = "soldout"        # every seat taken
        elif not on_sale:
            status = "closed"         # withdrawn from sale with seats left
        else:
            status = "open"
        sections.append(dict(x, section_id=sid, section=titles.get(sid, sid), price=p, fee=f,
                             price_max=max((z[1] for z in open_zs), default=None),
                             zones=[{"name": z, "price": zp, "fee": zf, "open": zo, "history": hist.get(z, [])}
                                    for z, zp, zf, zo in zs],
                             opened_later=first_seen[sid] if later else None,
                             status=status, closed_at=since if status != "open" and not on_sale else None,
                             history=hist.get(open_zs[0][0], []) if open_zs else []))
    sections.sort(key=lambda x: x["section"])
    tot = {k: sum(x[k] for x in sections) for k in keys + ["singles", "revenue", "gross"]}
    # free seats in withdrawn sections cannot be bought: not counted as available
    tot["withdrawn_free"] = sum(x["available"] for x in sections if x["status"] == "closed")
    tot["available"] -= tot["withdrawn_free"]
    tot["avg_price"] = tot["revenue"] / tot["singles"] if tot["singles"] else None
    tot["avg_paid"] = tot["gross"] / tot["singles"] if tot["singles"] else None
    tot["sections"] = len(sections)
    tot["first_ts"] = first_ts
    tot["last_ts"] = max((rows[-1]["ts_utc"] for rows in tl.values()), default=None)
    return {"sections": sections, "totals": tot}


def analyse_all(ev, with_seats=False):
    names = {eid: m["name"] for eid, m in ev["events"].items()}
    tls = {eid: timelines(load_seats(eid)) for eid in names}
    groups = season_groups(names)
    cache, out = {}, {}
    all_prices = read_csv(SNAPSHOTS)
    zn = zone_names(all_prices)
    sec_titles = {r["section_id"]: r["section"] for r in all_prices}
    seat_sec = {seat: rows[-1]["section_id"] for tl in tls.values() for seat, rows in tl.items()}
    sold_somewhere = set(seat_sec.values())
    extra_fans = {sid: (title.split() or [""])[0] for sid, title in sec_titles.items()
                  if (title.split() or [""])[0] in FAN_EXTRA}
    for eid in names:
        g = groups[eid]
        if g not in cache:
            cache[g] = season_seats(g, tls)
        price_rows = load_snapshots(eid)
        out[eid] = analyse_event(tls[eid], price_rows, cache[g], zn)
        out[eid]["season_group_size"] = len(g)
        out[eid]["season_total"] = len(cache[g])
        if with_seats:
            out[eid]["seats"] = classify_seats(tls[eid], price_lookup(price_rows, zn), cache[g])
        # B Tribūna not on sale for this game: assume full (their whole capacity).
        # 110-111 always; 109 / 112 when on sale in other games but not in this one.
        cap = load_capacity(ev["events"][eid].get("conf_id", ""))
        on_sale = {rows[-1]["section_id"] for rows in tls[eid].values()}
        fan_ids = dict(FAN_SECTIONS)
        fan_ids.update({sid: num for sid, num in extra_fans.items() if sid in sold_somewhere})
        fans = sorted(((sid, name, cap[sid]) for sid, name in fan_ids.items() if sid in cap and sid not in on_sale),
                      key=lambda x: x[1])
        fan_set = {sid for sid, _n, _c in fans}
        # season seats in other sections not on sale online for this game: the holders
        # still have their seats, so they count as season tickets / taken / expected crowd
        seen = tls[eid]
        off = Counter(seat_sec[s] for s in cache[g] if s not in seen and seat_sec[s] not in fan_set)
        t = out[eid]["totals"]
        t["season_off"] = sum(off.values())
        for k in ("seats", "taken", "season"):
            t[k] += t["season_off"]
        out[eid]["season_off_sections"] = sorted((sid, sec_titles.get(sid, sid), n) for sid, n in off.items())
        # sections sold out as memberships, never on single sale (107): all season tickets
        whole = [(sid, name, cap[sid]) for sid, name in SEASON_SECTIONS.items()
                 if g and sid in cap and sid not in on_sale] if g else []
        n_whole = sum(c for _s, _n, c in whole)
        for k in ("seats", "taken", "season"):
            t[k] += n_whole
        out[eid]["season_total"] += n_whole
        out[eid]["season_sections"] = whole
        for sid, name, c in whole:
            out[eid]["sections"].append(dict(
                section_id=sid, section=name, seats=c, available=0, taken=c, season=c, sold_seen=0,
                pre_taken=0, singles=0, revenue=0.0, gross=0.0, price=None, fee=None, price_max=None,
                zones=[], opened_later=None, status="soldout", closed_at=None, history=[], whole=True))
        out[eid]["fan_sections"] = fans
        out[eid]["totals"]["fans_est"] = sum(n for _s, _n, n in fans)
        out[eid]["totals"]["expected"] = out[eid]["totals"]["taken"] + out[eid]["totals"]["fans_est"]
    return out


def capacity_table(ev, res=None):
    """Seats per section (full venue plan) with how often each was on sale.
    Returns (rows, totals) for the Sheet's capacity tab and `rytas.py capacity`."""
    res = res or analyse_all(ev)
    confs = sorted({m.get("conf_id", "") for m in ev["events"].values()} - {""})
    titles = {}
    for eid in ev["events"]:
        for r in load_snapshots(eid):
            titles[r["section_id"]] = r["section"]
    titles.update({sid: f"{name} (B Tribūna)" for sid, name in FAN_SECTIONS.items()})
    titles.update({sid: f"{name} (abonementai)" for sid, name in SEASON_SECTIONS.items()})
    seen = defaultdict(list)          # section -> seats on sale per game
    for eid, r in res.items():
        for x in r["sections"]:
            if not x.get("whole"):
                seen[x["section_id"]].append(x["seats"])
    rows, totals = [], {}
    for conf in confs:
        cap = load_capacity(conf)
        on_map = {r["section_id"] for r in read_csv(SHAPES) if r["conf_id"] == conf}
        for sid in sorted(cap, key=lambda k: (k not in on_map, titles.get(k, "~" + k))):
            rows.append({"conf_id": conf, "section_id": sid, "title": titles.get(sid, ""), "seats": cap[sid],
                         "max_on_sale": max(seen[sid], default=0), "games_on_sale": len(seen[sid]),
                         "on_map": sid in on_map})
        totals[conf] = {"plan": sum(cap.values()), "map": sum(n for k, n in cap.items() if k in on_map),
                        "ever_on_sale": sum(n for k, n in cap.items() if seen[k])}
    return rows, totals


# ---------------------------------------------------------------- commands

def money(x):
    return "-" if x is None else f"{x:,.0f}"


def add_event(ev, eid, name, start, series_id=None, status=None):
    meta = ev["events"].setdefault(eid, {})
    new = not meta
    changed = new or meta.get("start_utc") != start or meta.get("name") != name
    meta.update(name=name, start_utc=start)
    if series_id:
        meta["series_id"] = series_id
    if status:
        meta["status"] = status
    return new, changed


def cmd_add(args):
    eid = event_id_from(args.event)
    info = fetch_info(eid)
    ev = load_events()
    add_event(ev, eid, info["name"], info["eventStartAt"], info.get("seriesId"), info.get("status"))
    meta = ev["events"][eid]
    meta["conf_id"] = str(info.get("confId") or "")
    print(f"Tracking {eid}: {info['name']} ({info['eventStartAt']})")
    print(snapshot_event(eid, meta, datetime.now(timezone.utc), force_seats=True))
    save_events(ev)


def sync_series(ev, series_id):
    s = fetch_series(series_id)
    ev["series"][series_id] = s.get("title", series_id)
    report = []
    for e in s.get("events", []):
        new, changed = add_event(ev, e["id"], e["title"], e["eventStartAt"], series_id, e.get("status"))
        if new:
            report.append(f"NEW      {e['id']}  {e['eventStartAt']}  {e['title']}")
        elif changed:
            report.append(f"UPDATED  {e['id']}  {e['eventStartAt']}  {e['title']}")
    return s, report


def cmd_add_series(args):
    target = event_id_from(args.target)
    try:
        series_id = fetch_info(target).get("seriesId") or target
    except urllib.error.HTTPError:
        series_id = target  # not an event id: treat as a series id
    ev = load_events()
    s, report = sync_series(ev, series_id)
    save_events(ev)
    print(f"Series {series_id}: {s.get('title')} - {len(s.get('events', []))} games")
    print("\n".join(report) or "No new games.")
    print("Run `snapshot` (or wait for the hourly job) to log them.")


def note_status(key, value):
    """Record what failed in this run (data/run_status.json) for the page's warnings."""
    st = load_json(RUN_STATUS, {})
    st[key] = value
    save_json(RUN_STATUS, st)


def cmd_discover(args):
    ev = load_events()
    if not ev["series"]:
        print("No series tracked. Use: rytas.py add-series <url>", file=sys.stderr)
        return 1
    failed = False
    for sid in list(ev["series"]):
        try:
            s, report = sync_series(ev, sid)
            print(f"{sid} ({s.get('title')}): {len(s.get('events', []))} games")
            for line in report:
                print("  " + line)
        except Exception as e:
            failed = True
            print(f"{sid}: failed: {e}", file=sys.stderr)
            note_status("discover", str(e)[:200])
    save_events(ev)
    return 1 if failed else 0


def cmd_snapshot(args):
    ev = load_events()
    if not ev["events"]:
        print("No events tracked. Use: rytas.py add-series <url>", file=sys.stderr)
        return 1
    failed, tried = [], 0
    now = datetime.now(timezone.utc)
    try:
        for eid, meta in ev["events"].items():
            if parse_ts(meta["start_utc"]) < now:
                continue  # game started, sales are over
            tried += 1
            try:
                print(f"{eid} {meta['start_utc'][:10]}: " + snapshot_event(eid, meta, now, args.seats))
            except Exception as e:  # keep going for other events
                failed.append({"id": eid, "error": str(e)[:200]})
                print(f"{eid}: failed: {e}", file=sys.stderr)
    finally:
        save_events(ev)
        note_status("snapshot", {"tried": tried, "failed": failed, "at": now_utc()})
    return 1 if failed else 0


def games_starting_soon(ev, now, window_min):
    """Upcoming games that start within the next `window_min` minutes, earliest first."""
    out = []
    for eid, meta in ev["events"].items():
        start = parse_ts(meta["start_utc"])
        if now < start <= now + timedelta(minutes=window_min):
            out.append((start, eid, meta))
    return sorted(out)


def cmd_final(args):
    """Final check before tip-off: wait until `lead` minutes before a game that
    starts within `window` minutes, then read every seat plan and price of it.
    With --check only report (for the workflow) whether such a game exists."""
    ev = load_events()
    soon = games_starting_soon(ev, datetime.now(timezone.utc), args.window)
    if args.check:
        line = f"soon={'true' if soon else 'false'}"
        print(line + "".join(f"  {eid} {meta['start_utc']}" for _s, eid, meta in soon))
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(line + "\n")
        return 0
    if not soon:
        print("No game starts soon.")
        return 0
    failed = False
    try:
        for start, eid, meta in soon:
            wait = (start - timedelta(minutes=args.lead) - datetime.now(timezone.utc)).total_seconds()
            if wait > 0:
                print(f"{eid}: waiting {wait / 60:.0f} min for the final check ({args.lead} min before {meta['start_utc']})", flush=True)
                time.sleep(wait)
            try:
                print(f"{eid} final check: " + snapshot_event(eid, meta, datetime.now(timezone.utc), force_seats=True), flush=True)
                meta["final_check_utc"] = now_utc()
            except Exception as e:
                failed = True
                print(f"{eid}: final check failed: {e}", file=sys.stderr)
    finally:
        save_events(ev)
    return 1 if failed else 0


def cmd_capacity(args):
    rows, totals = capacity_table(load_events())
    for conf, tot in totals.items():
        print(f"Layout {conf}: full plan {tot['plan']} seats, arena-map sections {tot['map']}, "
              f"sections ever on sale {tot['ever_on_sale']}")
    print(f"\n{'Section':<30}{'Seats':>7}{'Max on sale':>13}{'Games':>7}  Map")
    for r in rows:
        if r["on_map"] or r["games_on_sale"]:
            print(f"{(r['title'] or r['section_id'])[:29]:<30}{r['seats']:>7}{r['max_on_sale']:>13}{r['games_on_sale']:>7}  {'yes' if r['on_map'] else ''}")
    return 0


def cmd_attendance(args):
    eid = event_id_from(args.event)
    append_csv(ATTENDANCE, ATT_FIELDS, [{
        "event_id": eid, "attendance": args.number,
        "recorded_utc": now_utc(), "note": args.note or "",
    }])
    print(f"Recorded attendance {args.number} for {eid}")


def print_summary(ev, res):
    print(f"{'Date':<11}{'Game':<44}{'Seats':>7}{'Avail':>7}{'Taken':>7}{'Season':>8}"
          f"{'Single':>8}{'Seen':>6}{'Revenue':>10}{'Avg':>6}{'BTrib':>6}{'Expect':>7}{'Attend':>8}")
    for eid, meta in ev["events"].items():
        t = res[eid]["totals"]
        att = latest_attendance(eid)
        name = meta["name"].replace("Vilniaus ''Rytas'' - ", "").replace("''", "\"")
        if not t["sections"]:
            print(f"{meta['start_utc'][:10]:<11}{name[:43]:<44}{'no seat data yet':>30}")
            continue
        print(f"{meta['start_utc'][:10]:<11}{name[:43]:<44}{t['seats']:>7}{t['available']:>7}{t['taken']:>7}"
              f"{t['season']:>8}{t['singles']:>8}{t['sold_seen']:>6}{money(t['revenue']):>10}"
              f"{money(t['avg_price']):>6}{t.get('fans_est', 0):>6}{t.get('expected', t['taken']):>7}"
              f"{(att if att is not None else '-'):>8}")
    for eid, meta in ev["events"].items():
        fans = res[eid].get("fan_sections") or []
        if len(fans) > 2:
            print(f"{meta['start_utc'][:10]} B Tribūna: " + ", ".join(f"{name} {n}" for _sid, name, n in fans))
        off = res[eid].get("season_off_sections") or []
        if off:
            print(f"{meta['start_utc'][:10]} season seats in sections not on sale: "
                  + ", ".join(f"{title} {n}" for _sid, title, n in off))
    print("\nSeason = seats never available in any tracked game (season tickets). Single = Taken - Season."
          "\nSeen = online sales observed while tracking. Revenue/Avg in EUR, excl. fees, singles only."
          "\nDetails: rytas.py report <id>")


def print_detail(eid, meta, r):
    t = r["totals"]
    print(f"\n=== {meta['name']} [{eid}] {meta['start_utc']} ===")
    if not t["sections"]:
        print("No seat data yet.")
        return
    print(f"Seat data {t['first_ts']} -> {t['last_ts']}; season estimate compares "
          f"{r['season_group_size']} games")
    print(f"\n{'Section':<24}{'Price':>7}{'Seats':>7}{'Avail':>7}{'Taken':>7}{'Season':>8}"
          f"{'Seen':>6}{'Before':>8}{'Revenue':>9}  Opened")
    for x in r["sections"]:
        price = "-" if x["price"] is None else f"{x['price']:.0f}"
        print(f"{x['section'][:23]:<24}{price:>7}{x['seats']:>7}{x['available']:>7}{x['taken']:>7}"
              f"{x['season']:>8}{x['sold_seen']:>6}{x['pre_taken']:>8}{money(x['revenue']):>9}"
              f"  {(x['opened_later'] or '')[:16]}")
    print(f"""
Expected crowd:              {t['expected']}  (taken {t['taken']} + B Tribūna {t['fans_est']}, est.)
Seats in open sections:      {t['seats']}  ({t['sections']} sections)
Available now:               {t['available']}  (+ {t['withdrawn_free']} in withdrawn sections)
Taken:                       {t['taken']}
  season tickets (est.):     {t['season']}
  single tickets (est.):     {t['singles']}
    sold online, seen:       {t['sold_seen']}
    taken before tracking:   {t['pre_taken']}
Club revenue, singles (est.): {money(t['revenue'])} EUR (ticket value = price - fees)
Buyers paid incl. fees:      {money(t['gross'])} EUR
Average ticket value:        {money(t['avg_price'])} EUR (buyers paid {money(t['avg_paid'])} EUR)""")
    att = latest_attendance(eid)
    if att is not None:
        print(f"\nActual attendance:           {att}")
        print(f"Attendance - seats taken:    {att - t['taken']:+d}  "
              f"(negative: no-shows; positive: comps, kids under 6, sections not tracked)")


def cmd_report(args):
    ev = load_events()
    if not ev["events"]:
        print("No events tracked.", file=sys.stderr)
        return 1
    res = analyse_all(ev)
    if args.event:
        eid = event_id_from(args.event)
        if eid not in ev["events"]:
            print(f"{eid} is not tracked.", file=sys.stderr)
            return 1
        print_detail(eid, ev["events"][eid], res[eid])
    else:
        print_summary(ev, res)
    return 0


def cmd_season(args):
    ev = load_events()
    names = {eid: m["name"] for eid, m in ev["events"].items()}
    tls = {eid: timelines(load_seats(eid)) for eid in names}
    for group in sorted(set(season_groups(names).values())):
        seats = season_seats(group, tls)
        comps = sorted({competition(names[e]) for e in group})
        with_data = sum(1 for e in group if tls[e])
        print(f"\n{'/'.join(comps)}: {len(group)} games ({with_data} with seat data) -> "
              f"{len(seats)} season-ticket seats (est.)")
        titles, seat_sec = {}, {}
        for e in group:
            for r in load_snapshots(e):
                titles[r["section_id"]] = r["section"]
            for seat, rows in tls[e].items():
                seat_sec[seat] = rows[-1]["section_id"]
        by_sec = Counter(seat_sec[x] for x in seats)
        cap = {}
        for e in group:
            cap.update(load_capacity(ev["events"][e].get("conf_id", "")))
        print(f"  {'Section':<32}{'Season':>7}{'Seats':>7}")
        for sid, n in sorted(by_sec.items(), key=lambda kv: (titles.get(kv[0], kv[0]))):
            print(f"  {titles.get(sid, sid)[:30]:<32}{n:>7}{cap.get(sid, ''):>7}")
        if group:
            for sid, name in SEASON_SECTIONS.items():
                if sid in cap:
                    print(f"  {name + ' (whole section, memberships)':<32}{cap[sid]:>7}{cap[sid]:>7}")
    print(f"\nA seat counts once it is seen in {SEASON_MIN_GAMES}+ games (2+ while fewer games have data) "
          "and is taken the first time it is seen in every one. More games tracked = better estimate.")
    return 0


def split_name(name):
    """'LKL: Vilniaus ''Rytas'' - Utenos ''Juventus''' -> ('LKL', 'Utenos „Juventus“')."""
    comp = competition(name)
    rest = name.split(":", 1)[1].strip() if ":" in name else name
    opp = rest.split(" - ", 1)[1] if " - " in rest else rest
    return comp, re.sub(r"''(.+?)''", "„\\1“", opp).strip()


def r2(x):
    return None if x is None else round(x, 2)


def health_warnings(ev, res, now):
    """Problems for the page's warning badge: [{code, game?, detail?, since?}].

    From this run: games that could not be read (snapshot), the new-game check, saving to
    the Sheet (RUN_OUTCOMES from the workflow). From the data: upcoming games without seat
    data or whose seat plans are overdue. (A page that stops updating altogether is
    detected by the page itself from `updated`.)"""
    out = []
    st = load_json(RUN_STATUS, {})
    opp = {eid: split_name(m["name"])[1] for eid, m in ev["events"].items()}
    snap = st.get("snapshot") or {}
    failed = snap.get("failed") or []
    if failed and len(failed) == snap.get("tried") and len(failed) > 1:
        out.append({"code": "readAll", "detail": failed[0]["error"]})
    else:
        out += [{"code": "read", "game": opp.get(f["id"], f["id"]), "detail": f["error"]} for f in failed]
    if st.get("discover"):
        out.append({"code": "discover", "detail": st["discover"]})
    outcomes = dict(x.split("=", 1) for x in os.environ.get("RUN_OUTCOMES", "").split() if "=" in x)
    if outcomes.get("save") == "failure":
        out.append({"code": "save"})
    if outcomes.get("snap") == "failure" and not failed:
        out.append({"code": "readAll", "detail": ""})
    for eid, meta in ev["events"].items():
        start = parse_ts(meta["start_utc"])
        if start <= now:
            continue
        if not res[eid]["totals"]["sections"]:
            out.append({"code": "nodata", "game": opp[eid]})
            continue
        last = meta.get("last_seats_utc")
        if last:
            hours_left = (start - now).total_seconds() / 3600
            interval = 1 if hours_left <= 48 else 6 if hours_left <= 24 * 14 else 24
            if now - parse_ts(last) > timedelta(hours=2 * interval + 1):
                out.append({"code": "stale", "game": opp[eid], "since": last})
    return out


def cmd_export(args):
    """Write the website's data: <dir>/data.json (games, sections, map) and
    <dir>/seats/<game>.json (every seat with its category and price)."""
    out = Path(args.dir)
    (out / "seats").mkdir(parents=True, exist_ok=True)
    ev = load_events()
    res = analyse_all(ev, with_seats=True)
    layout = {r["seat_id"]: r for r in read_csv(LAYOUT)}
    attendance = {}
    for row in read_csv(ATTENDANCE):  # also typed by hand in the Sheet: be lenient
        try:
            attendance[row["event_id"].strip()] = int(float(str(row["attendance"]).replace(" ", "").replace(",", ".")))
        except (ValueError, KeyError):
            pass
    now = datetime.now(timezone.utc)
    games, sections = [], {}
    for eid, meta in ev["events"].items():
        r, t = res[eid], res[eid]["totals"]
        comp, opp = split_name(meta["name"])
        games.append({
            "id": eid, "name": meta["name"], "comp": comp, "opp": opp, "start": meta["start_utc"],
            "played": parse_ts(meta["start_utc"]) <= now, "conf": meta.get("conf_id", ""),
            "seats": t["seats"], "available": t["available"], "withdrawn": t["withdrawn_free"],
            "taken": t["taken"], "season": t["season"], "seasonOff": t["season_off"],
            "singles": t["singles"], "seen": t["sold_seen"], "before": t["pre_taken"],
            "fans": t["fans_est"], "expected": t["expected"],
            "fanSections": [{"id": sid, "title": name, "seats": n} for sid, name, n in r["fan_sections"]],
            "revenue": r2(t["revenue"]), "gross": r2(t["gross"]),
            "avg": r2(t["avg_price"]), "avgPaid": r2(t["avg_paid"]),
            "attendance": attendance.get(eid), "dataFrom": t["first_ts"], "dataTo": t["last_ts"],
        })
        sections[eid] = [{
            "id": x["section_id"], "title": x["section"], "price": x["price"], "fee": x["fee"],
            "priceMax": x["price_max"],
            "zones": [{"name": z["name"], "price": z["price"], "fee": z["fee"], "open": z["open"],
                       "history": [[h[0], h[1], h[2]] for h in z["history"]]} for z in x["zones"]],
            "seats": x["seats"], "available": x["available"], "season": x["season"], "singles": x["singles"],
            "seen": x["sold_seen"], "before": x["pre_taken"], "revenue": r2(x["revenue"]), "gross": r2(x["gross"]),
            "openedLater": x["opened_later"], "status": x["status"], "closedAt": x["closed_at"],
            "history": [[h[0], h[1], h[2]] for h in x["history"]],
        } for x in r["sections"]]
        # seats per section: [row, place, x, y, category, price, fee, sold_at]
        per = defaultdict(list)
        for seat, (sid, cat, p, f, ts) in r["seats"].items():
            lo = layout.get(seat)
            if lo:
                per[sid].append([lo["row"], lo["place"], int(float(lo["x"])), int(float(lo["y"])), cat, p, f, ts])
        fan_ids = {sid for sid, _n, _c in r["fan_sections"]}
        whole_ids = {sid for sid, _n, _c in r.get("season_sections", [])}
        for lo in layout.values():          # B Tribūna / membership sections: seats from the venue plan
            if lo["section_id"] in fan_ids or lo["section_id"] in whole_ids:
                cat = "F" if lo["section_id"] in fan_ids else "S"
                per[lo["section_id"]].append([lo["row"], lo["place"], int(float(lo["x"])), int(float(lo["y"])), cat, None, None, None])
        (out / "seats" / f"{eid}.json").write_text(json.dumps(per, ensure_ascii=False, separators=(",", ":")),
                                                  encoding="utf-8")
    shapes = defaultdict(list)
    for s in read_csv(SHAPES):
        shapes[s["conf_id"]].append([s["section_id"], s["points"]])
    any_res = next(iter(res.values()), {})
    # total arena capacity: seats of every section drawn on the arena map (full venue plan)
    arena_cap = {}
    for conf, shp in shapes.items():
        cap = load_capacity(conf)
        arena_cap[conf] = sum(cap.get(sid, 0) for sid, _pts in shp)
    data = {"updated": now_utc(), "seasonSeats": any_res.get("season_total", 0),
            "games": games, "sections": sections, "shapes": shapes, "arenaCap": arena_cap,
            "warnings": health_warnings(ev, res, now)}
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Exported {len(games)} games to {out}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("event"); a.set_defaults(fn=cmd_add)
    a = sub.add_parser("add-series"); a.add_argument("target"); a.set_defaults(fn=cmd_add_series)
    sub.add_parser("discover").set_defaults(fn=cmd_discover)
    s = sub.add_parser("snapshot")
    s.add_argument("--seats", action="store_true", help="fetch seat plans now, even if not due")
    s.set_defaults(fn=cmd_snapshot)
    t = sub.add_parser("attendance")
    t.add_argument("event"); t.add_argument("number", type=int); t.add_argument("--note")
    t.set_defaults(fn=cmd_attendance)
    r = sub.add_parser("report"); r.add_argument("event", nargs="?"); r.set_defaults(fn=cmd_report)
    sub.add_parser("season").set_defaults(fn=cmd_season)
    sub.add_parser("capacity", help="seats per section and arena capacity").set_defaults(fn=cmd_capacity)
    f = sub.add_parser("final", help="final full check shortly before tip-off")
    f.add_argument("--window", type=int, default=75, help="look for games starting within N minutes (default 75)")
    f.add_argument("--lead", type=int, default=10, help="check N minutes before tip-off (default 10)")
    f.add_argument("--check", action="store_true", help="only report whether a game starts soon")
    f.set_defaults(fn=cmd_final)
    x = sub.add_parser("export"); x.add_argument("dir"); x.set_defaults(fn=cmd_export)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
