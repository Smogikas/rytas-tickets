import rytas
from collections import Counter
ev = rytas.load_events()
eid = next(e for e, m in ev["events"].items() if "Nanterre" in m["name"])
P = [r for r in rytas.read_csv(rytas.SNAPSHOTS) if r["event_id"] == eid]
for sec in ("112", "122"):
    print(f"=== price rows for section {sec} (all changes) ===")
    for r in P:
        if r["section"].split()[0] == sec:
            print(r["ts_utc"][:16], r["section"], "|", r["zone"], r["zone_id"], "| type", r["ticket_type_id"], "|", r["price"], "€ avail", r["available"])
res = rytas.analyse_all(ev)[eid]
for s in res["sections"]:
    if str(s.get("title", "")).split()[:1] in (["112"], ["122"]):
        print({k: v for k, v in s.items() if k not in ("seats",)})
# section 122 seats by price class and status (latest)
tl = rytas.timelines(rytas.load_seats(eid))
for sec in ("112", "122"):
    sid = next((r["section_id"] for r in P if r["section"].split()[0] == sec), None)
    c = Counter()
    for seat, rows in tl.items():
        if rows[-1]["section_id"] == sid:
            c[(rows[-1]["price_class"], rows[-1]["status"])] += 1
    print(sec, sid, "latest (price_class, status):", sorted(c.items()))
    # when did each price class's seats get sold (free->taken) by day
    s = Counter()
    for seat, rows in tl.items():
        if rows[-1]["section_id"] == sid:
            for a, b in zip(rows, rows[1:]):
                if str(a["status"]) == "1" and str(b["status"]) == "0":
                    s[(b["price_class"], b["ts_utc"][:13])] += 1
    print(sec, "sales by class/hour:", sorted(s.items()))
