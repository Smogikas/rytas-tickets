import rytas
from collections import Counter, defaultdict
ev = rytas.load_events()
eid = next(e for e, m in ev["events"].items() if "Nanterre" in m["name"])
P = [r for r in rytas.read_csv(rytas.SNAPSHOTS) if r["event_id"] == eid]
print("=== all Nanterre price changes on 11-09 ===")
for r in P:
    if r["ts_utc"].startswith("2026-11-09"):
        print(r["ts_utc"][:16], r["section"], "|", r["zone"], r["zone_id"], "|", r["price"], "€ avail", r["available"])
print("=== other games: price rows changed 2026-11-09T09 ===")
for r in rytas.read_csv(rytas.SNAPSHOTS):
    if r["event_id"] != eid and r["ts_utc"].startswith("2026-11-09T09"):
        print(r["event_id"], r["section"], "|", r["zone"], "|", r["price"], "€ avail", r["available"])
tl = rytas.timelines(rytas.load_seats(eid))
sid = "73017"
rows_by = defaultdict(list)
for seat, rows in tl.items():
    if rows[-1]["section_id"] == sid and rows[-1]["price_class"] == "1353":
        rows_by[rows[-1]["row"]].append(int(rows[-1]["place"]))
        hist = [(r["ts_utc"][5:16], r["price_class"], r["status"]) for r in rows]
print("1353 seats by row:", {k: sorted(v) for k, v in sorted(rows_by.items(), key=lambda x: int(x[0]))})
print("example history:", hist)
free = [(r[-1]["row"], r[-1]["place"]) for s, r in tl.items() if r[-1]["section_id"] == sid and str(r[-1]["status"]) == "1"]
print("free now:", sorted(free, key=lambda x: (int(x[0]), int(x[1]))))
res = rytas.analyse_all(ev)[eid]
for s in res["sections"]:
    if s.get("section_id") in ("72936", sid) or "112" in str(s.get("section", "")):
        print({k: v for k, v in s.items() if not isinstance(v, (list, dict))})
