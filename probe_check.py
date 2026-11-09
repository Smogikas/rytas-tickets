import rytas
from collections import Counter, defaultdict
ev = rytas.load_events()
names = {e: m["name"] for e, m in ev["events"].items()}
eid = next(e for e in names if "Nanterre" in names[e])
P = rytas.read_csv(rytas.SNAPSHOTS)
new = {r["section_id"]: r["section"] for r in P if r["event_id"] == eid and r["section"].split()[0] in ("123", "124", "125", "126")}
print("sections:", new)
for e in names:
    seen = {r["section"] for r in P if r["event_id"] == e and r["section_id"] in new}
    if seen: print("OPEN IN", names[e][:60], sorted(seen))
tl = rytas.timelines(rytas.load_seats(eid))
res = rytas.analyse_all(ev)[eid]
for s in res["sections"]:
    if s.get("section_id") in new:
        print(s["section"], {k: s[k] for k in ("seats", "available", "season", "sold_seen", "pre_taken", "singles", "revenue")})
for sid in new:
    c = Counter(); ts = Counter()
    for seat, rows in tl.items():
        if rows[-1]["section_id"] == sid:
            c[(rows[0]["status"], rows[0]["price_class"], rows[-1]["status"])] += 1
            ts[rows[0]["ts_utc"][:16]] += 1
    print(new[sid], "(first status, first class, now status):", sorted(c.items(), key=str), "| first seen:", dict(ts))
print("totals singles/pre_taken/sold_seen:", res["totals"]["singles"], res["totals"]["pre_taken"], res["totals"]["sold_seen"])
