import rytas
from collections import Counter, defaultdict
ev = rytas.load_events()
names = {e: m["name"] for e, m in ev["events"].items()}
eid = next(e for e in names if "Nanterre" in names[e])
tls = {e: rytas.timelines(rytas.load_seats(e)) for e in names}
groups = rytas.season_groups(names)
season = rytas.season_seats(groups[eid], tls)
print("group size:", len(groups[eid]), "season set:", len(season))
P = rytas.read_csv(rytas.SNAPSHOTS)
secname = {r["section_id"]: r["section"].split()[0] for r in P}
tl = tls[eid]
bysec = Counter(); when = Counter(); now = Counter(); cls = Counter(); rowsn = Counter()
for seat in season:
    rows = tl.get(seat)
    if not rows: continue
    free = [r for r in rows if str(r["status"]) == "1"]
    if free:
        bysec[secname.get(rows[-1]["section_id"], rows[-1]["section_id"])] += 1
        when[free[0]["ts_utc"][:16]] += 1
        now[str(rows[-1]["status"])] += 1
        cls[(rows[0]["price_class"], free[0]["price_class"])] += 1
        rowsn[len(rows)] += 1
print("season seats seen free in Nanterre, by section:", dict(bysec))
print("first seen free at:", dict(sorted(when.items())))
print("status now (1 free, 0 taken):", dict(now))
print("(first class, class when free):", dict(cls))
print("rows per seat:", dict(rowsn))
ex = next(s for s in season if tl.get(s) and any(str(r["status"]) == "1" for r in tl[s]))
print("example:", [(r["ts_utc"][5:16], r["status"], r["price_class"]) for r in tl[ex]])
