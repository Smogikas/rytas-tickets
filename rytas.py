#!/usr/bin/env python3
"""Rytas ticket tracker.

Logs bilietai.lt availability + prices for tracked events, and reports
tickets sold / revenue / average price, compared with real attendance.

Commands:
  add <url|id>              start tracking an event (and take a first snapshot)
  snapshot                  log current availability + prices for all upcoming tracked events
  attendance <id> <number>  record actual attendance after the game
  report [<id>]             sales report (all tracked events if no id)
"""
import argparse
import csv
import json
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

API = "https://www.bilietai.lt/api/v1"
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots.csv"
ATTENDANCE = DATA / "attendance.csv"
EVENTS = ROOT / "events.json"
CAPACITY = ROOT / "capacity.json"

# The API never reports more than 22 available tickets per section (the site
# shows "20+"). A value >= CAP means "22 or more", not an exact count.
CAP = 22

SNAP_FIELDS = ["ts_utc", "event_id", "section_id", "section", "zone",
               "ticket_type_id", "price", "fee", "available"]
ATT_FIELDS = ["event_id", "attendance", "recorded_utc", "note"]


# ---------------------------------------------------------------- fetching

def get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "rytas-tickets/0.1 (personal project)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_event(event_id):
    info = get_json(f"{API}/events/{event_id}?language=lt")
    prices = get_json(f"{API}/store/events/{event_id}/prices?language=lt")
    return info, prices


def flatten(event_id, ts, prices):
    """One row per (section, price zone, ticket type)."""
    rows = []
    for sec in prices.get("eventSections", []):
        for zone in sec.get("priceZones", []):
            for tt in zone.get("ticketTypes", []):
                rows.append({
                    "ts_utc": ts,
                    "event_id": event_id,
                    "section_id": str(sec["id"]),
                    "section": sec["title"],
                    "zone": zone.get("description", ""),
                    "ticket_type_id": str(tt["id"]),
                    "price": tt["price"],
                    "fee": tt.get("fee", 0),
                    "available": tt["availableTickets"],
                })
    return rows


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
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def load_snapshots(event_id):
    if not SNAPSHOTS.exists():
        return []
    rows = []
    with SNAPSHOTS.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["event_id"] != event_id:
                continue
            r["price"] = float(r["price"])
            r["fee"] = float(r["fee"])
            r["available"] = int(r["available"])
            rows.append(r)
    return rows


def latest_attendance(event_id):
    if not ATTENDANCE.exists():
        return None
    found = None
    with ATTENDANCE.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["event_id"] == event_id:
                found = int(r["attendance"])
    return found


def last_state(event_id):
    """Latest logged row per (section_id, ticket_type_id) for an event."""
    last = {}
    for r in sorted(load_snapshots(event_id), key=lambda r: r["ts_utc"]):
        last[(r["section_id"], r["ticket_type_id"])] = r
    return last


def take_snapshot(event_id, prices):
    """Log the current state, writing only rows that changed since the last log.

    A section that was on sale before but is missing from the API now is
    logged as available=0 (sold out / withdrawn), so its sales are not lost.
    Returns (all current rows, rows actually written).
    """
    ts = now_utc()
    rows = flatten(event_id, ts, prices)
    last = last_state(event_id)
    current = {(r["section_id"], r["ticket_type_id"]) for r in rows}
    for key, prev in last.items():
        if key not in current and prev["available"] > 0:
            rows.append(dict(prev, ts_utc=ts, available=0))
    changed_rows = [
        r for r in rows
        if (k := (r["section_id"], r["ticket_type_id"])) not in last
        or (float(r["price"]), float(r["fee"]), int(r["available"]))
        != (last[k]["price"], last[k]["fee"], last[k]["available"])
    ]
    append_csv(SNAPSHOTS, SNAP_FIELDS, changed_rows)
    # make sure every section has a slot in capacity.json for the user to fill in
    caps = load_json(CAPACITY, {})
    changed = False
    for r in rows:
        if r["section_id"] not in caps:
            caps[r["section_id"]] = {"title": r["section"], "seats": None}
            changed = True
    if changed:
        save_json(CAPACITY, caps)
    return rows, changed_rows


def event_id_from(text):
    m = re.search(r"/renginiai/([A-Za-z0-9]+)", text)
    return m.group(1) if m else text.strip()


# ---------------------------------------------------------------- analysis

def analyse(rows, caps):
    """Per-section and total sales figures from a list of snapshot rows.

    sold            seats no longer available online = capacity - available.
                    Exact only when available < CAP. NOTE: this includes season
                    tickets / club-held seats, not only bilietai.lt sales.
    sold_max        upper bound when available is capped (capacity - CAP)
    observed_*      tickets seen leaving between two snapshots where both were
                    uncensored, priced at the price listed in the earlier one.
                    These are real online sales.
    revenue_est     observed revenue + (sold - observed) priced at the first
                    logged price; only when sold is exact

    Ticket types within a section share the same seats, so each section is
    reduced to one row per snapshot: available = max over its ticket types,
    price/fee/zone = those of the cheapest ticket type.
    """
    per_snap = defaultdict(list)
    for r in rows:
        per_snap[(r["section_id"], r["ts_utc"])].append(r)

    series = defaultdict(list)
    for (sid, _ts), rs in per_snap.items():
        base = min(rs, key=lambda r: r["price"])
        series[sid].append(dict(base, available=max(r["available"] for r in rs)))

    sections = []
    for sid, s in series.items():
        s.sort(key=lambda r: r["ts_utc"])
        first, last = s[0], s[-1]
        seats = (caps.get(sid) or {}).get("seats")
        censored = last["available"] >= CAP

        obs_sold, obs_rev = 0, 0.0
        for a, b in zip(s, s[1:]):
            if a["available"] < CAP and b["available"] < CAP and a["available"] > b["available"]:
                n = a["available"] - b["available"]
                obs_sold += n
                obs_rev += n * a["price"]

        sold = sold_max = revenue_est = None
        if seats is not None:
            if censored:
                sold_max = max(0, seats - CAP)
            else:
                sold = max(0, seats - last["available"])
                revenue_est = obs_rev + max(0, sold - obs_sold) * first["price"]

        sections.append({
            "section_id": sid, "section": last["section"], "zone": last["zone"],
            "price": last["price"], "fee": last["fee"], "available": last["available"],
            "censored": censored, "seats": seats, "sold": sold, "sold_max": sold_max,
            "observed_sold": obs_sold, "observed_revenue": obs_rev,
            "revenue_est": revenue_est,
        })

    known = [x for x in sections if x["sold"] is not None]
    sold_total = sum(x["sold"] for x in known)
    rev_total = sum(x["revenue_est"] for x in known)
    obs_sold = sum(x["observed_sold"] for x in sections)
    obs_rev = sum(x["observed_revenue"] for x in sections)
    totals = {
        "sections": len(sections),
        "sections_with_exact_sold": len(known),
        "sold": sold_total,
        "revenue_est": rev_total,
        "avg_price": rev_total / sold_total if sold_total else None,
        "observed_sold": obs_sold,
        "observed_revenue": obs_rev,
        "observed_avg_price": obs_rev / obs_sold if obs_sold else None,
        "reported_available": sum(x["available"] for x in sections),
    }
    sections.sort(key=lambda x: x["section"])
    return {"sections": sections, "totals": totals}


# ---------------------------------------------------------------- commands

def money(x):
    return "-" if x is None else f"{x:,.2f}"


def cmd_add(args):
    event_id = event_id_from(args.event)
    info, prices = fetch_event(event_id)
    events = load_json(EVENTS, {})
    events[event_id] = {"name": info["name"], "start_utc": info["eventStartAt"]}
    save_json(EVENTS, events)
    rows, written = take_snapshot(event_id, prices)
    print(f"Tracking {event_id}: {info['name']} ({info['eventStartAt']})")
    print(f"Snapshot: {len(rows)} sections/ticket types, {len(written)} rows logged. "
          f"Fill in section seat counts in capacity.json to get 'sold' numbers.")


def cmd_snapshot(args):
    events = load_json(EVENTS, {})
    if not events:
        print("No events tracked. Use: rytas.py add <url>", file=sys.stderr)
        return 1
    failed = False
    now = datetime.now(timezone.utc)
    for eid, meta in events.items():
        if parse_ts(meta["start_utc"]) < now:
            continue  # game already started, sales are over
        try:
            _info, prices = fetch_event(eid)
            rows, written = take_snapshot(eid, prices)
            print(f"{eid}: {len(rows)} sections/ticket types, {len(written)} changed, "
                  f"{sum(int(r['available']) for r in rows)} tickets shown available")
        except Exception as e:  # keep going for other events
            failed = True
            print(f"{eid}: failed: {e}", file=sys.stderr)
    return 1 if failed else 0


def cmd_attendance(args):
    event_id = event_id_from(args.event)
    append_csv(ATTENDANCE, ATT_FIELDS, [{
        "event_id": event_id, "attendance": args.number,
        "recorded_utc": now_utc(), "note": args.note or "",
    }])
    print(f"Recorded attendance {args.number} for {event_id}")


def print_report(event_id, name, caps):
    rows = load_snapshots(event_id)
    print(f"\n=== {name or event_id} [{event_id}] ===")
    if not rows:
        print("No snapshots yet.")
        return
    stamps = sorted({r["ts_utc"] for r in rows})
    print(f"{len(stamps)} snapshots with changes, {stamps[0]} -> {stamps[-1]}")
    res = analyse(rows, caps)
    t = res["totals"]

    print(f"\n{'Section':<24}{'Price':>8}{'Fee':>7}{'Avail':>7}{'Seats':>7}{'Sold':>8}{'Seen sold':>10}{'Revenue':>11}")
    for x in res["sections"]:
        avail = f"{x['available']}+" if x["censored"] else str(x["available"])
        if x["sold"] is not None:
            sold = str(x["sold"])
        elif x["sold_max"] is not None:
            sold = f"<={x['sold_max']}"
        else:
            sold = "?"
        print(f"{x['section'][:23]:<24}{x['price']:>8.2f}{x['fee']:>7.2f}{avail:>7}"
              f"{(x['seats'] if x['seats'] is not None else '?'):>7}{sold:>8}"
              f"{x['observed_sold']:>10}{money(x['revenue_est']):>11}")

    print(f"\nSections with exact sold count: {t['sections_with_exact_sold']}/{t['sections']}"
          f"  (rest need seats in capacity.json or have >= {CAP} still available)")
    print(f"Seats taken (exact sections):   {t['sold']}  (incl. season tickets / club-held seats)")
    print(f"Revenue estimate:               {money(t['revenue_est'])} EUR (excl. fees)")
    print(f"Average ticket price:           {money(t['avg_price'])} EUR")
    print(f"Online sales seen by tracker:   {t['observed_sold']} tickets, "
          f"{money(t['observed_revenue'])} EUR, avg {money(t['observed_avg_price'])} EUR")
    print(f"Still on sale (lower bound):    {t['reported_available']}")

    att = latest_attendance(event_id)
    if att is not None:
        print(f"\nActual attendance:              {att}")
        if t["sold"]:
            diff = att - t["sold"]
            print(f"Attendance - seats taken:       {diff:+d}  (only exact sections counted; "
                  f"negative = no-shows / unused tickets, positive = sections not yet exact, "
                  f"comps, kids under 6)")


def cmd_report(args):
    events = load_json(EVENTS, {})
    caps = load_json(CAPACITY, {})
    ids = [event_id_from(args.event)] if args.event else list(events)
    if not ids:
        print("No events tracked.", file=sys.stderr)
        return 1
    for eid in ids:
        print_report(eid, events.get(eid, {}).get("name"), caps)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add"); a.add_argument("event"); a.set_defaults(fn=cmd_add)
    s = sub.add_parser("snapshot"); s.set_defaults(fn=cmd_snapshot)
    t = sub.add_parser("attendance")
    t.add_argument("event"); t.add_argument("number", type=int); t.add_argument("--note")
    t.set_defaults(fn=cmd_attendance)
    r = sub.add_parser("report"); r.add_argument("event", nargs="?"); r.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
