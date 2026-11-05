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
"""
import argparse
import csv
import http.cookiejar
import json
import re
import sys
import time
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
EVENTS = ROOT / "events.json"
# Local working copies. The GitHub job loads them from / saves them to the
# Google Sheet (sheets.py), so no data is stored in the repository.

# The prices API never reports more than 22 available per section ("20+").
# Exact numbers come from the seat plans instead.
CAP = 22
PAUSE = 0.4  # seconds between seat-plan requests, be polite to the site

SNAP_FIELDS = ["ts_utc", "event_id", "section_id", "section", "zone",
               "ticket_type_id", "price", "fee", "available"]
SEAT_FIELDS = ["ts_utc", "section_id", "seat_id", "row", "place", "price_class", "status"]
ATT_FIELDS = ["event_id", "attendance", "recorded_utc", "note"]
LAYOUT_FIELDS = ["seat_id", "section_id", "row", "place", "x", "y"]
SHAPE_FIELDS = ["conf_id", "section_id", "points"]
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
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
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
                    "available": tt["availableTickets"],
                })
    return rows


def take_snapshot(event_id, prices):
    """Log section prices, writing only rows that changed since the last log.

    A section that was on sale but is missing from the API now is logged as
    available=0 (sold out / withdrawn). Returns (current rows, rows written).
    """
    ts = now_utc()
    rows = flatten(event_id, ts, prices)
    last = latest_by(load_snapshots(event_id), lambda r: (r["section_id"], r["ticket_type_id"]))
    current = {(r["section_id"], r["ticket_type_id"]) for r in rows}
    for key, prev in last.items():
        if key not in current and prev["available"] > 0:
            rows.append(dict(prev, ts_utc=ts, available=0))
    written = [
        r for r in rows
        if (k := (r["section_id"], r["ticket_type_id"])) not in last
        or (float(r["price"]), float(r["fee"]), int(r["available"]))
        != (last[k]["price"], last[k]["fee"], last[k]["available"])
    ]
    append_csv(SNAPSHOTS, SNAP_FIELDS, written)
    return rows, written


def take_seat_snapshot(event_id, prices):
    """Fetch the seat plan of every open section and log seats whose status changed.

    Sections only appear in the prices list while on sale; some open later.
    A section seen before that no longer returns a seat plan has its
    remaining available seats logged as taken (sold out / withdrawn).
    Returns (current seat rows, rows written, sections fetched).
    """
    ts = now_utc()
    last = latest_by(load_seats(event_id), lambda r: r["seat_id"])
    open_ids = [str(s["id"]) for s in prices.get("eventSections", [])]
    had_available = {r["section_id"] for r in last.values() if r["status"] == 1}
    to_fetch = open_ids + sorted(had_available - set(open_ids))

    current = {}
    known_layout = {r["seat_id"] for r in read_csv(LAYOUT)}
    new_layout = []
    for i, sid in enumerate(to_fetch):
        if i:
            time.sleep(PAUSE)
        tickets = fetch_section_seats(event_id, sid)
        if tickets is None:
            if sid not in open_ids:
                for r in last.values():
                    if r["section_id"] == sid and r["status"] == 1:
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


def ensure_shapes(conf_id):
    """Store the arena map's section outlines once per venue layout."""
    if not conf_id or any(r["conf_id"] == str(conf_id) for r in read_csv(SHAPES)):
        return 0
    rows = [{"conf_id": str(conf_id), "section_id": s, "points": p} for s, p in fetch_shapes(conf_id)]
    append_csv(SHAPES, SHAPE_FIELDS, rows)
    return len(rows)


def seats_due(meta, now):
    """Seat plans cost one request per section, so fetch them less often for
    games far away: hourly in the last 48 h, every 6 h within 14 days, else daily."""
    last = meta.get("last_seats_utc")
    if not last:
        return True
    hours_left = (parse_ts(meta["start_utc"]) - now).total_seconds() / 3600
    interval = 1 if hours_left <= 48 else 6 if hours_left <= 24 * 14 else 24
    return now - parse_ts(last) >= timedelta(hours=interval * 0.9)


def snapshot_event(event_id, meta, now, force_seats=False):
    if not meta.get("conf_id"):  # venue layout id, looked up once per game
        meta["conf_id"] = str(fetch_info(event_id).get("confId") or "")
    ensure_shapes(meta["conf_id"])
    prices = fetch_prices(event_id)
    rows, written = take_snapshot(event_id, prices)
    msg = f"prices: {len(written)} changed"
    if force_seats or seats_due(meta, now):
        current, changed, n = take_seat_snapshot(event_id, prices)
        meta["last_seats_utc"] = now_utc()
        avail = sum(r["status"] for r in current.values())
        msg += f"; seats: {len(current)} in {n} sections, {avail} available, {len(changed)} changed"
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
    by_comp = defaultdict(list)
    for eid, name in names.items():
        by_comp[competition(name)].append(eid)
    everyone = tuple(sorted(names))
    return {eid: tuple(sorted(by_comp[competition(n)])) if len(by_comp[competition(n)]) >= 3 else everyone
            for eid, n in names.items()}


def season_seats(group, tls):
    """Seats observed in at least 2 games of the group and never available in any.
    These are taken before single-game sales could reach them: season tickets
    (and any permanently held seats)."""
    seen, ever_free = Counter(), set()
    for eid in group:
        for seat, rows in tls.get(eid, {}).items():
            seen[seat] += 1
            if any(r["status"] == 1 for r in rows):
                ever_free.add(seat)
    return {s for s, n in seen.items() if n >= 2 and s not in ever_free}


def price_lookup(price_rows):
    """section_id -> sorted [(ts, price, fee)] of the cheapest ticket type, from the price snapshots."""
    per = defaultdict(dict)
    for r in price_rows:
        cur = per[r["section_id"]].get(r["ts_utc"])
        if cur is None or r["price"] < cur[0]:
            per[r["section_id"]][r["ts_utc"]] = (r["price"], r["fee"])
    return {sid: [(t, p, f) for t, (p, f) in sorted(d.items())] for sid, d in per.items()}


def price_at(prices, sid, ts):
    """(price, fee) in force at ts (the first logged one if ts is earlier), or (None, None)."""
    series = prices.get(sid)
    if not series:
        return None, None
    p, f = series[0][1], series[0][2]
    for t, price, fee in series:
        if t <= ts:
            p, f = price, fee
        else:
            break
    return p, f


def price_history(prices, sid):
    """Only the moments the price or fee changed: [(ts, price, fee)]."""
    out = []
    for t, p, f in prices.get(sid, []):
        if not out or (out[-1][1], out[-1][2]) != (p, f):
            out.append((t, p, f))
    return out


# seat categories
SEASON, SOLD_SEEN, SOLD_BEFORE, AVAILABLE = "S", "O", "B", "A"


def classify_seats(tl, prices, season):
    """seat_id -> (section_id, category, price, fee, ts).

    S  season ticket (never available in any compared game)
    O  sold while tracked: price/fee in force when the sale was seen
    B  taken before tracking began, not season: first logged price (estimate)
    A  available: current price
    """
    out = {}
    for seat, rows in tl.items():
        first, last = rows[0], rows[-1]
        sid = last["section_id"]
        if last["status"] == 1:
            cat, ts = AVAILABLE, "9999"
        elif seat in season:
            cat, ts = SEASON, last["ts_utc"]
        elif first["status"] == 1:
            cat, ts = SOLD_SEEN, last["ts_utc"]   # changes-only log: last row = the sale
        else:
            cat, ts = SOLD_BEFORE, first["ts_utc"]
        p, f = price_at(prices, sid, ts)
        out[seat] = (sid, cat, p, f, last["ts_utc"] if cat == SOLD_SEEN else None)
    return out


def analyse_event(tl, price_rows, season):
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
    prices = price_lookup(price_rows)
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
        x = sec[sid]
        x["seats"] += 1
        x[count_key[cat]] += 1
        if cat != AVAILABLE:
            x["taken"] += 1
        if cat in (SOLD_SEEN, SOLD_BEFORE) and p is not None:
            x["revenue"] += p - (f or 0)
            x["gross"] += p

    sections = []
    for sid, x in sec.items():
        x["singles"] = x["taken"] - x["season"]
        later = first_ts and parse_ts(first_seen[sid]) - parse_ts(first_ts) > timedelta(hours=1)
        p, f = price_at(prices, sid, "9999")
        sections.append(dict(x, section_id=sid, section=titles.get(sid, sid), price=p, fee=f,
                             opened_later=first_seen[sid] if later else None,
                             history=price_history(prices, sid)))
    sections.sort(key=lambda x: x["section"])
    tot = {k: sum(x[k] for x in sections) for k in keys + ["singles", "revenue", "gross"]}
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
    for eid in names:
        g = groups[eid]
        if g not in cache:
            cache[g] = season_seats(g, tls)
        price_rows = load_snapshots(eid)
        out[eid] = analyse_event(tls[eid], price_rows, cache[g])
        out[eid]["season_group_size"] = len(g)
        out[eid]["season_total"] = len(cache[g])
        if with_seats:
            out[eid]["seats"] = classify_seats(tls[eid], price_lookup(price_rows), cache[g])
    return out


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
    save_events(ev)
    return 1 if failed else 0


def cmd_snapshot(args):
    ev = load_events()
    if not ev["events"]:
        print("No events tracked. Use: rytas.py add-series <url>", file=sys.stderr)
        return 1
    failed = False
    now = datetime.now(timezone.utc)
    try:
        for eid, meta in ev["events"].items():
            if parse_ts(meta["start_utc"]) < now:
                continue  # game started, sales are over
            try:
                print(f"{eid} {meta['start_utc'][:10]}: " + snapshot_event(eid, meta, now, args.seats))
            except Exception as e:  # keep going for other events
                failed = True
                print(f"{eid}: failed: {e}", file=sys.stderr)
    finally:
        save_events(ev)
    return 1 if failed else 0


def cmd_attendance(args):
    eid = event_id_from(args.event)
    append_csv(ATTENDANCE, ATT_FIELDS, [{
        "event_id": eid, "attendance": args.number,
        "recorded_utc": now_utc(), "note": args.note or "",
    }])
    print(f"Recorded attendance {args.number} for {eid}")


def print_summary(ev, res):
    print(f"{'Date':<11}{'Game':<44}{'Seats':>7}{'Avail':>7}{'Taken':>7}{'Season':>8}"
          f"{'Single':>8}{'Seen':>6}{'Revenue':>10}{'Avg':>6}{'Attend':>8}")
    for eid, meta in ev["events"].items():
        t = res[eid]["totals"]
        att = latest_attendance(eid)
        name = meta["name"].replace("Vilniaus ''Rytas'' - ", "").replace("''", "\"")
        if not t["sections"]:
            print(f"{meta['start_utc'][:10]:<11}{name[:43]:<44}{'no seat data yet':>30}")
            continue
        print(f"{meta['start_utc'][:10]:<11}{name[:43]:<44}{t['seats']:>7}{t['available']:>7}{t['taken']:>7}"
              f"{t['season']:>8}{t['singles']:>8}{t['sold_seen']:>6}{money(t['revenue']):>10}"
              f"{money(t['avg_price']):>6}{(att if att is not None else '-'):>8}")
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
Seats in open sections:      {t['seats']}  ({t['sections']} sections)
Available now:               {t['available']}
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
        by_sec = Counter()
        titles = {}
        for e in group:
            for r in load_snapshots(e):
                titles[r["section_id"]] = r["section"]
            for seat, rows in tls[e].items():
                if seat in seats:
                    by_sec[rows[-1]["section_id"]] += 1
        for sid, n in sorted(by_sec.items(), key=lambda kv: -kv[1]):
            print(f"  {titles.get(sid, sid)[:30]:<32}{n // max(1, with_data):>6}")
    print("\nA seat counts once it is seen in 2+ games and never available. "
          "More games tracked = better estimate.")
    return 0


def split_name(name):
    """'LKL: Vilniaus ''Rytas'' - Utenos ''Juventus''' -> ('LKL', 'Utenos „Juventus“')."""
    comp = competition(name)
    rest = name.split(":", 1)[1].strip() if ":" in name else name
    opp = rest.split(" - ", 1)[1] if " - " in rest else rest
    return comp, re.sub(r"''(.+?)''", "„\\1“", opp).strip()


def r2(x):
    return None if x is None else round(x, 2)


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
            "seats": t["seats"], "available": t["available"], "taken": t["taken"], "season": t["season"],
            "singles": t["singles"], "seen": t["sold_seen"], "before": t["pre_taken"],
            "revenue": r2(t["revenue"]), "gross": r2(t["gross"]),
            "avg": r2(t["avg_price"]), "avgPaid": r2(t["avg_paid"]),
            "attendance": attendance.get(eid), "dataFrom": t["first_ts"], "dataTo": t["last_ts"],
        })
        sections[eid] = [{
            "id": x["section_id"], "title": x["section"], "price": x["price"], "fee": x["fee"],
            "seats": x["seats"], "available": x["available"], "season": x["season"], "singles": x["singles"],
            "seen": x["sold_seen"], "before": x["pre_taken"], "revenue": r2(x["revenue"]), "gross": r2(x["gross"]),
            "openedLater": x["opened_later"], "history": [[h[0], h[1], h[2]] for h in x["history"]],
        } for x in r["sections"]]
        # seats per section: [row, place, x, y, category, price, fee, sold_at]
        per = defaultdict(list)
        for seat, (sid, cat, p, f, ts) in r["seats"].items():
            lo = layout.get(seat)
            if lo:
                per[sid].append([lo["row"], lo["place"], int(float(lo["x"])), int(float(lo["y"])), cat, p, f, ts])
        (out / "seats" / f"{eid}.json").write_text(json.dumps(per, ensure_ascii=False, separators=(",", ":")),
                                                  encoding="utf-8")
    shapes = defaultdict(list)
    for s in read_csv(SHAPES):
        shapes[s["conf_id"]].append([s["section_id"], s["points"]])
    any_res = next(iter(res.values()), {})
    data = {"updated": now_utc(), "seasonSeats": any_res.get("season_total", 0),
            "games": games, "sections": sections, "shapes": shapes}
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
    x = sub.add_parser("export"); x.add_argument("dir"); x.set_defaults(fn=cmd_export)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
