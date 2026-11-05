#!/usr/bin/env python3
"""Google Sheet storage for rytas.py, so collected data is not kept in the repo.

  python sheets.py pull   Sheet -> local working files (data/, events.json)
  python sheets.py push   new local rows -> Sheet (append); events/series rewritten

Tabs: events, series, prices, layout, shapes, attendance, seats_<game id>.
Rows are only ever appended (except events/series, which are small and
rewritten). You can type actual attendance straight into the "attendance" tab.

Needs env SHEET_ID and GOOGLE_SA_KEY (a service account's JSON key; the
sheet must be shared with that account as Editor) and `pip install google-auth requests`.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import rytas

API = "https://sheets.googleapis.com/v4/spreadsheets"
STATE = rytas.DATA / ".sheet_state.json"   # rows per tab at pull time
CHUNK = 10000                              # rows per append request

EVENT_FIELDS = ["id", "name", "start_utc", "series_id", "status", "conf_id", "last_seats_utc"]
SERIES_FIELDS = ["id", "title"]


def tables(events=()):
    """tab name -> (local file, columns) for append-only tables."""
    t = {
        "prices": (rytas.SNAPSHOTS, rytas.SNAP_FIELDS),
        "layout": (rytas.LAYOUT, rytas.LAYOUT_FIELDS),
        "shapes": (rytas.SHAPES, rytas.SHAPE_FIELDS),
        "attendance": (rytas.ATTENDANCE, rytas.ATT_FIELDS),
    }
    for eid in events:
        t[f"seats_{eid}"] = (rytas.seat_path(eid), rytas.SEAT_FIELDS)
    return t


# ------------------------------------------------------------------ HTTP

_token = None


def token():
    global _token
    if _token is None:
        from google.oauth2 import service_account
        import google.auth.transport.requests
        info = json.loads(os.environ["GOOGLE_SA_KEY"])
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        creds.refresh(google.auth.transport.requests.Request())
        _token = creds.token
    return _token


def request(method, path, params=None, body=None):
    """One Sheets API call; path is relative to the spreadsheet."""
    url = f"{API}/{os.environ['SHEET_ID']}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(6):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 429 = per-minute quota (60 writes/min), 5xx = transient: wait and retry
            if (e.code == 429 or e.code >= 500) and attempt < 5:
                time.sleep(min(64, 2 ** (attempt + 2)))
                continue
            raise


def tab_names():
    meta = request("GET", "", {"fields": "sheets.properties.title"})
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def add_tab(name, columns):
    request("POST", ":batchUpdate", body={"requests": [{"addSheet": {"properties": {
        "title": name, "gridProperties": {"rowCount": 1, "columnCount": len(columns), "frozenRowCount": 1}}}}]})
    request("PUT", f"/values/{urllib.parse.quote(name)}!A1", {"valueInputOption": "RAW"},
            {"values": [columns]})


def read_tabs(names):
    """name -> list of rows (lists of strings), header included."""
    if not names:
        return {}
    res = request("GET", "/values:batchGet", {"ranges": [f"'{n}'" for n in names],
                                              "valueRenderOption": "UNFORMATTED_VALUE"})
    out = {}
    for n, vr in zip(names, res.get("valueRanges", [])):
        out[n] = [["" if v is None else str(v) for v in row] for row in vr.get("values", [])]
    return out


def append_rows(name, rows):
    for i in range(0, len(rows), CHUNK):
        request("POST", f"/values/{urllib.parse.quote(name)}!A1:append",
                {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                {"values": rows[i:i + CHUNK]})


def rewrite(name, columns, rows):
    request("POST", f"/values/{urllib.parse.quote(name)}:clear", body={})
    request("PUT", f"/values/{urllib.parse.quote(name)}!A1", {"valueInputOption": "RAW"},
            {"values": [columns] + rows})


# --------------------------------------------------------------- pull/push

def to_dicts(rows, columns):
    """Sheet rows -> dicts by the sheet's own header (columns missing there stay empty)."""
    if not rows:
        return []
    head = rows[0]
    return [{c: (r[head.index(c)] if c in head and head.index(c) < len(r) else "") for c in columns}
            for r in rows[1:] if any(r)]


def write_local(path, columns, dicts):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rytas.append_csv(path, columns, dicts)


def pull():
    names = tab_names()
    ours = [n for n in names if n in ("events", "series") or n in tables() or n.startswith("seats_")]
    data = read_tabs(ours)
    state = {}
    if "events" in data:
        ev = {"series": {s["id"]: s["title"] for s in to_dicts(data.get("series", []), SERIES_FIELDS)},
              "events": {}}
        for e in to_dicts(data["events"], EVENT_FIELDS):
            ev["events"][e["id"]] = {k: e[k] for k in EVENT_FIELDS[1:] if e[k] != ""}
        rytas.save_json(rytas.EVENTS, ev)
    seat_tabs = [n[len("seats_"):] for n in data if n.startswith("seats_")]
    for name, (path, columns) in tables(seat_tabs).items():
        if name in data:
            dicts = to_dicts(data[name], columns)
            write_local(path, columns, dicts)
            state[name] = len(dicts)
        else:
            state[name] = 0          # first run: local rows (if any) get uploaded by push
    rytas.DATA.mkdir(parents=True, exist_ok=True)
    rytas.save_json(STATE, state)
    print(f"Pulled {len(data)} tabs: " + ", ".join(f"{n} {len(v) - 1 if v else 0}" for n, v in data.items()))


def push():
    state = rytas.load_json(STATE, {})
    names = set(tab_names())
    ev = rytas.load_events()
    pushed = []
    for name, (path, columns) in tables(ev["events"]).items():
        rows = rytas.read_csv(path)
        new = rows[state.get(name, 0):]
        if not new:
            continue
        if name not in names:
            add_tab(name, columns)
        append_rows(name, [[r.get(c, "") for c in columns] for r in new])
        state[name] = len(rows)
        pushed.append(f"{name} +{len(new)}")
    for name, columns, rows in (
        ("events", EVENT_FIELDS, [[eid] + [str(m.get(c, "")) for c in EVENT_FIELDS[1:]] for eid, m in ev["events"].items()]),
        ("series", SERIES_FIELDS, [[sid, title] for sid, title in ev["series"].items()]),
    ):
        if name not in names:
            add_tab(name, columns)
        rewrite(name, columns, rows)
    rytas.save_json(STATE, state)
    print("Pushed: " + (", ".join(pushed) or "no new rows") + f"; events {len(ev['events'])}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("pull", "push"):
        print(__doc__)
        sys.exit(2)
    for var in ("SHEET_ID", "GOOGLE_SA_KEY"):
        if not os.environ.get(var):
            print(f"Missing environment variable {var}", file=sys.stderr)
            sys.exit(2)
    pull() if cmd == "pull" else push()
