import rytas
from collections import Counter, defaultdict
ev = rytas.load_events()
names = {e: m["name"] for e, m in ev["events"].items()}
eid = next(e for e in names if "Nanterre" in names[e])
P = [r for r in rytas.read_csv(rytas.SNAPSHOTS) if r["event_id"] == eid]
secname = {r["section_id"]: r["section"] for r in P}
tl = rytas.timelines(rytas.load_seats(eid))
first = defaultdict(lambda: Counter())
for seat, rows in tl.items():
    sid = rows[0]["section_id"]
    if rows[0]["ts_utc"] >= "2026-11-09":
        first[(rows[0]["ts_utc"][:16], secname.get(sid, sid))][(str(rows[0]["status"]), rows[0]["price_class"])] += 1
for k in sorted(first): print("NEW", k, dict(first[k]))
print("PRICE ROWS today for 107 / new sections:")
for r in P:
    if r["ts_utc"] >= "2026-11-09T10" and (r["section"].startswith("107") or r["section_id"] not in {x["section_id"] for x in P if x["ts_utc"] < "2026-11-09"}):
        print("PR", r["ts_utc"][:16], r["section"], "|", r["zone"], r["price"], "avail", r["available"])
res = rytas.analyse_all(ev)[eid]
for s in res["sections"]:
    if str(s.get("section", "")).startswith("107") or s.get("whole"):
        print("SEC", {k: v for k, v in s.items() if not isinstance(v, (list, dict))})
print("TOT", {k: res["totals"][k] for k in ("season", "singles", "sold_seen", "pre_taken") if k in res["totals"]})
