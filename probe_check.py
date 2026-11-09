import rytas
from collections import defaultdict
ev = rytas.load_events()
rows = rytas.read_csv(rytas.SNAPSHOTS)
names = {e: m["name"] for e, m in ev["events"].items()}
# current state per (event, section, zone): last row
last = {}
for r in sorted(rows, key=lambda r: r["ts_utc"]):
    last[(r["event_id"], r["section_id"], r["zone"])] = r
by = defaultdict(lambda: defaultdict(set))      # event -> zone -> {(price, fee, section)}
for (e, sid, z), r in last.items():
    by[e][z].add((float(r["price"]), r["section"].split()[0]))
mixed = 0
for e, zs in by.items():
    for z, s in sorted(zs.items()):
        prices = {p for p, _ in s}
        if len(prices) > 1:
            mixed += 1
            print("MIXED", rytas.opponent(names[e]) if hasattr(rytas, "opponent") else e, "|", z, "|", sorted(s))
print("levels per game checked:", sum(len(z) for z in by.values()), "mixed:", mixed)
# one game: level -> price, sections
e = next(x for x in names if "Nanterre" in names[x])
for z, s in sorted(by[e].items(), key=lambda kv: min(p for p, _ in kv[1])):
    print("NANTERRE", z, "|", sorted({p for p, _ in s}), "|", " ".join(sorted({x for _, x in s})))
# same level across games
acr = defaultdict(set)
for e, zs in by.items():
    for z, s in zs.items():
        acr[z] |= {p for p, _ in s}
print("ACROSS GAMES:", {z: sorted(p) for z, p in sorted(acr.items()) if "kaina" in z})
