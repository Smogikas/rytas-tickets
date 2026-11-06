#!/usr/bin/env python3
"""Google Sheet storage for rytas.py, so collected data is not kept in the repo.

  python sheets.py pull   Sheet -> local working files (data/, events.json)
  python sheets.py push   new local rows -> Sheet

Tabs
  Rungtynės               overview, one row per game (rewritten each run). The last
                          column "Lankomumas (įrašykite)" is yours: type actual
                          attendance there; the job never overwrites it.
  <date> <opponent>       one tab per game, e.g. "2026-11-11 Nanterre 92": every price
                          change ("kaina" rows) and seat change ("vieta" rows), append-only
  _events _series _layout _shapes   hidden technical tabs

Needs env SHEET_ID and GOOGLE_SA_KEY (a service account's JSON key; the sheet
shared with that account as Editor) and `pip install google-auth requests`.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import rytas

API = "https://sheets.googleapis.com/v4/spreadsheets"
STATE = rytas.DATA / ".sheet_state.json"
CHUNK = 10000                     # rows per append request

OVERVIEW = "Rungtynės"
OVERVIEW_HEAD = ["ID", "Data (Vilnius)", "Turnyras", "Varžovas", "Būsena", "Laukiama žiūrovų",
                 "iš jų B Tribūna (110–111, įvert.)",
                 "Abonementai", "Pavieniai bilietai", "Laisvi bilietai", "Klubo pajamos €",
                 "Vid. bilieto kaina € (su mokesčiais)", "Lankomumas (įrašykite)"]
ATT_COL = chr(ord("A") + len(OVERVIEW_HEAD) - 1)   # attendance column (last), typed by the user
CAPACITY_TAB = "Talpa"
CAPACITY_HEAD = ["Sektoriaus ID", "Sektorius", "Vietų (pilnas planas)", "Daugiausia vietų pardavime",
                 "Parduodamas rungtynėse", "Žemėlapyje"]
GAME_HEAD = ["Laikas (UTC)", "Įrašas", "Sektoriaus ID", "Sektorius", "Zona / kainų klasė",
             "Bilieto tipas", "Kaina €", "Mokesčiai €", "Laisva sektoriuje (iki 22)",
             "Vietos ID", "Eilė", "Vieta", "Būsena (1 laisva, 0 užimta)", "Zonos ID"]
PRICE, SEAT = "kaina", "vieta"
EVENT_FIELDS = ["id", "name", "start_utc", "series_id", "status", "conf_id", "last_seats_utc", "final_check_utc", "tab"]
SERIES_FIELDS = ["id", "title"]
HIDDEN = {"_events": EVENT_FIELDS, "_series": SERIES_FIELDS,
          "_layout": rytas.LAYOUT_FIELDS, "_shapes": rytas.SHAPE_FIELDS, "_capacity": rytas.CAP_FIELDS}
DEFAULT_TABS = ("Sheet1", "Lapas1")


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
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"Sheets API {method} {path}: HTTP {e.code}: {detail}") from e


def rng(tab, cells=""):
    """A1 range for a tab, quoted (titles have spaces)."""
    return f"'{tab}'" + (f"!{cells}" if cells else "")


def vpath(r, suffix=""):
    return "/values/" + urllib.parse.quote(r, safe="") + suffix


def sheet_props():
    meta = request("GET", "", {"fields": "sheets.properties(sheetId,title,index,hidden,gridProperties(rowCount,columnCount))"})
    return {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}


def add_tab(title, head, hidden=False, index=None):
    props = {"title": title, "hidden": hidden,
             # header row frozen; Google refuses to freeze all rows, so start with 2
             "gridProperties": {"rowCount": 2, "columnCount": len(head), "frozenRowCount": 1}}
    if index is not None:
        props["index"] = index
    res = request("POST", ":batchUpdate", body={"requests": [{"addSheet": {"properties": props}}]})
    request("PUT", vpath(rng(title, "A1")), {"valueInputOption": "RAW"}, {"values": [head]})
    try:
        return res["replies"][0]["addSheet"]["properties"]
    except (KeyError, IndexError, TypeError):
        return {"title": title, "gridProperties": props["gridProperties"]}


def ensure_columns(tabs, title, cols):
    """Writing past a tab's last column fails (Google only adds rows by itself), so
    widen the tab first when a layout change needs more columns."""
    props = tabs.get(title) or {}
    have = (props.get("gridProperties") or {}).get("columnCount")
    if have is None or have >= cols or props.get("sheetId") is None:
        return
    request("POST", ":batchUpdate", body={"requests": [{"appendDimension": {
        "sheetId": props["sheetId"], "dimension": "COLUMNS", "length": cols - have}}]})
    props.setdefault("gridProperties", {})["columnCount"] = cols


def read_tabs(titles):
    """title -> list of rows (lists of strings), header included."""
    if not titles:
        return {}
    res = request("GET", "/values:batchGet", {"ranges": [rng(t) for t in titles],
                                              "valueRenderOption": "UNFORMATTED_VALUE"})
    return {t: [["" if v is None else str(v) for v in row] for row in vr.get("values", [])]
            for t, vr in zip(titles, res.get("valueRanges", []))}


def append_rows(title, rows):
    for i in range(0, len(rows), CHUNK):
        request("POST", vpath(rng(title, "A1"), ":append"),
                {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, {"values": rows[i:i + CHUNK]})


def rewrite(title, head, rows):
    request("POST", vpath(rng(title), ":clear"), body={})
    request("PUT", vpath(rng(title, "A1")), {"valueInputOption": "RAW"}, {"values": [head] + rows})


# --------------------------------------------------------------- helpers

def to_dicts(rows, columns):
    """Rows of a hidden tab -> dicts by that tab's own header."""
    if not rows:
        return []
    head = rows[0]
    return [{c: (r[head.index(c)] if c in head and head.index(c) < len(r) else "") for c in columns}
            for r in rows[1:] if any(r)]


def cell(row, i):
    return row[i] if i < len(row) else ""


def num(v):
    """Numbers as numbers in the Sheet (nicer to read and sum)."""
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


def game_tab_title(meta, taken):
    """'2026-11-11 Nanterre 92' from the start date and the opponent's team name."""
    _comp, opp = rytas.split_name(meta["name"])
    m = re.search(r"„(.+?)“", opp)
    short = re.sub(r"[\[\]'*?:/\\\"„“]", "", m.group(1) if m else opp).strip()
    title = f"{meta['start_utc'][:10]} {short}"[:90]
    n, base = 2, title
    while title in taken:
        title, n = f"{base} ({n})", n + 1
    return title


def price_to_row(r):
    return [r["ts_utc"], PRICE, r["section_id"], r["section"], r["zone"], r["ticket_type_id"],
            num(r["price"]), num(r["fee"]), num(r["available"]), "", "", "", "", r.get("zone_id", "")]


def seat_to_row(r, titles):
    return [r["ts_utc"], SEAT, r["section_id"], titles.get(r["section_id"], ""), r["price_class"], "",
            "", "", "", r["seat_id"], r["row"], r["place"], num(r["status"]), ""]


def split_game_rows(eid, rows):
    """Game tab rows -> (price dicts, seat dicts), by column position."""
    prices, seats = [], []
    for r in rows[1:]:
        kind = cell(r, 1)
        if kind == PRICE:
            prices.append({"ts_utc": cell(r, 0), "event_id": eid, "section_id": cell(r, 2), "section": cell(r, 3),
                           "zone": cell(r, 4), "ticket_type_id": cell(r, 5), "price": cell(r, 6),
                           "fee": cell(r, 7), "available": cell(r, 8), "zone_id": rytas.norm_id(cell(r, 13))})
        elif kind == SEAT:
            seats.append({"ts_utc": cell(r, 0), "section_id": cell(r, 2), "seat_id": cell(r, 9), "row": cell(r, 10),
                          "place": cell(r, 11), "price_class": cell(r, 4), "status": cell(r, 12)})
    return prices, seats


def write_local(path, columns, dicts):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rytas.append_csv(path, columns, dicts)


# --------------------------------------------------------------- pull/push

def pull():
    tabs = sheet_props()
    state = {"order": [], "att": {}, "counts": {}}
    if "_events" not in tabs:
        # empty Sheet (first run): keep any local files; push uploads them all
        rytas.DATA.mkdir(parents=True, exist_ok=True)
        rytas.save_json(STATE, state)
        print("Sheet has no data yet")
        return
    head = read_tabs([t for t in ("_events", "_series", "_layout", "_shapes", "_capacity", OVERVIEW) if t in tabs])
    events = to_dicts(head["_events"], EVENT_FIELDS)
    ev = {"series": {s["id"]: s["title"] for s in to_dicts(head.get("_series", []), SERIES_FIELDS)},
          "events": {e["id"]: {k: e[k] for k in EVENT_FIELDS[1:] if e[k] != ""} for e in events}}
    rytas.save_json(rytas.EVENTS, ev)
    for name, path in (("_layout", rytas.LAYOUT), ("_shapes", rytas.SHAPES), ("_capacity", rytas.CAPACITY)):
        dicts = to_dicts(head.get(name, []), HIDDEN[name])
        write_local(path, HIDDEN[name], dicts)
        state["counts"][name] = len(dicts)

    game_tabs = {eid: m["tab"] for eid, m in ev["events"].items() if m.get("tab") in tabs}
    data = read_tabs(list(game_tabs.values()))
    all_prices = []
    for eid, title in game_tabs.items():
        prices, seats = split_game_rows(eid, data.get(title, []))
        all_prices += prices
        write_local(rytas.seat_path(eid), rytas.SEAT_FIELDS, seats)
        state["counts"][f"prices:{eid}"] = len(prices)
        state["counts"][f"seats:{eid}"] = len(seats)
    write_local(rytas.SNAPSHOTS, rytas.SNAP_FIELDS, all_prices)

    att_rows = []
    for r in head.get(OVERVIEW, [])[1:]:
        eid = cell(r, 0).strip()
        if not eid:
            continue
        state["order"].append(eid)
        a = cell(r, len(OVERVIEW_HEAD) - 1).strip()
        if a:
            state["att"][eid] = a
            att_rows.append({"event_id": eid, "attendance": a, "recorded_utc": "", "note": "Sheet"})
    write_local(rytas.ATTENDANCE, rytas.ATT_FIELDS, att_rows)
    rytas.save_json(STATE, state)
    print(f"Pulled {len(ev['events'])} games, {len(all_prices)} price rows, "
          f"{sum(state['counts'].get(f'seats:{e}', 0) for e in game_tabs)} seat rows")


def overview_rows(ev, order):
    res = rytas.analyse_all(ev)
    now = rytas.datetime.now(rytas.timezone.utc)
    rows = []
    for eid in order:
        m = ev["events"][eid]
        t = res[eid]["totals"]
        comp, opp = rytas.split_name(m["name"])
        start = rytas.parse_ts(m["start_utc"])
        rows.append([eid, vilnius(start), comp, opp, "Sužaista" if start <= now else "Būsima",
                     t["expected"], t["fans_est"], t["season"], t["singles"], t["available"],
                     round(t["revenue"], 2), round(t["avg_paid"], 2) if t["avg_paid"] else ""])
    return rows


def vilnius(dt):
    """UTC -> Europe/Vilnius wall time (EET/EEST), without needing tz data."""
    y = dt.year
    def last_sunday(month):
        d = rytas.datetime(y, month + 1, 1, tzinfo=rytas.timezone.utc) - rytas.timedelta(days=1)
        return d - rytas.timedelta(days=(d.weekday() + 1) % 7)
    summer = last_sunday(3).replace(hour=1) <= dt < last_sunday(10).replace(hour=1)
    return (dt + rytas.timedelta(hours=3 if summer else 2)).strftime("%Y-%m-%d %H:%M")


def push():
    state = rytas.load_json(STATE, {"order": [], "att": {}, "counts": {}})
    counts = state.setdefault("counts", {})
    tabs = sheet_props()
    ev = rytas.load_events()
    pushed = []

    for name, path in (("_layout", rytas.LAYOUT), ("_shapes", rytas.SHAPES), ("_capacity", rytas.CAPACITY)):
        rows = rytas.read_csv(path)
        new = rows[counts.get(name, 0):]
        if new:
            if name not in tabs:
                tabs[name] = add_tab(name, HIDDEN[name], hidden=True)
            append_rows(name, [[num(r[c]) if c in ("x", "y", "seats") else r[c] for c in HIDDEN[name]] for r in new])
            counts[name] = len(rows)
            pushed.append(f"{name} +{len(new)}")

    all_prices = rytas.read_csv(rytas.SNAPSHOTS)
    for eid, meta in sorted(ev["events"].items(), key=lambda kv: kv[1]["start_utc"]):
        prices = [r for r in all_prices if r["event_id"] == eid]
        seats = rytas.read_csv(rytas.seat_path(eid))
        new_p = prices[counts.get(f"prices:{eid}", 0):]
        new_s = seats[counts.get(f"seats:{eid}", 0):]
        if not (new_p or new_s):
            continue
        if not meta.get("tab") or meta["tab"] not in tabs:
            meta["tab"] = meta.get("tab") or game_tab_title(meta, set(tabs))
            if meta["tab"] not in tabs:
                tabs[meta["tab"]] = add_tab(meta["tab"], GAME_HEAD)
        titles = {r["section_id"]: r["section"] for r in prices}
        ensure_columns(tabs, meta["tab"], len(GAME_HEAD))
        if (tabs.get(meta["tab"]) or {}).get("sheetId") is not None and meta.get("_head") != len(GAME_HEAD):
            pass
        rows = [price_to_row(r) for r in new_p] + [seat_to_row(r, titles) for r in new_s]
        rows.sort(key=lambda r: r[0])
        append_rows(meta["tab"], rows)
        counts[f"prices:{eid}"], counts[f"seats:{eid}"] = len(prices), len(seats)
        pushed.append(f"{meta['tab']} +{len(new_p)} kainos +{len(new_s)} vietos")

    for name, head, rows in (
        ("_events", EVENT_FIELDS, [[eid] + [str(m.get(c, "")) for c in EVENT_FIELDS[1:]] for eid, m in ev["events"].items()]),
        ("_series", SERIES_FIELDS, [[sid, title] for sid, title in ev["series"].items()]),
    ):
        if name not in tabs:
            tabs[name] = add_tab(name, head, hidden=True)
        rewrite(name, head, rows)

    # overview: stable row order (so typed attendance stays on its game), new games appended
    order = [e for e in state.get("order", []) if e in ev["events"]]
    order += [e for e, _ in sorted(ev["events"].items(), key=lambda kv: kv[1]["start_utc"]) if e not in order]
    if OVERVIEW not in tabs:
        tabs[OVERVIEW] = add_tab(OVERVIEW, OVERVIEW_HEAD, index=0)
    ensure_columns(tabs, OVERVIEW, len(OVERVIEW_HEAD))
    rows = overview_rows(ev, order)
    last_col = chr(ord("A") + len(OVERVIEW_HEAD) - 2)
    request("PUT", vpath(rng(OVERVIEW, f"A1:{last_col}{len(rows) + 1}")), {"valueInputOption": "RAW"},
            {"values": [OVERVIEW_HEAD[:-1]] + rows})
    request("PUT", vpath(rng(OVERVIEW, f"{ATT_COL}1")), {"valueInputOption": "RAW"}, {"values": [[OVERVIEW_HEAD[-1]]]})
    # seats per section and arena capacity (visible tab, rewritten)
    cap_rows, cap_tot = rytas.capacity_table(ev)
    if cap_rows:
        if CAPACITY_TAB not in tabs:
            tabs[CAPACITY_TAB] = add_tab(CAPACITY_TAB, CAPACITY_HEAD, index=1)
        body = [[r["section_id"], r["title"], r["seats"], r["max_on_sale"] or "", r["games_on_sale"] or "",
                 "taip" if r["on_map"] else ""] for r in cap_rows]
        for conf, tot in cap_tot.items():
            body += [[], ["", "Visa arena (žemėlapio sektoriai)", tot["map"]],
                     ["", "Sektoriai, kurie buvo parduodami", tot["ever_on_sale"]],
                     ["", "Visas salės planas (su ložėmis ir kt.)", tot["plan"]]]
        rewrite(CAPACITY_TAB, CAPACITY_HEAD, body)

    # attendance recorded locally (rytas.py attendance) and not yet in the Sheet
    for i, eid in enumerate(order):
        local = rytas.latest_attendance(eid)
        if local is not None and str(state.get("att", {}).get(eid, "")).split(".")[0] != str(local):
            request("PUT", vpath(rng(OVERVIEW, f"{ATT_COL}{i + 2}")), {"valueInputOption": "RAW"}, {"values": [[local]]})
            state.setdefault("att", {})[eid] = str(local)
    state["order"] = order

    for t in DEFAULT_TABS:   # the empty first tab of a new spreadsheet
        if t in tabs and tabs[t].get("sheetId") is not None and len(tabs) > 1:
            if not any(any(r) for r in read_tabs([t]).get(t, [])):
                request("POST", ":batchUpdate", body={"requests": [{"deleteSheet": {"sheetId": tabs[t]["sheetId"]}}]})

    rytas.save_json(rytas.EVENTS, ev)
    rytas.save_json(STATE, state)
    print("Pushed: " + ("; ".join(pushed) or "no new rows") + f"; overview {len(order)} games")


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
