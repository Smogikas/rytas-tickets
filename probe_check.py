import rytas
ev = rytas.load_events()
eid = next(e for e, m in ev["events"].items() if "Nanterre" in m["name"])
live = rytas.flatten(eid, rytas.now_utc(), rytas.fetch_prices(eid))
ours = {}
for r in rytas.read_csv(rytas.SNAPSHOTS):
    if r["event_id"] == eid:
        ours[r["section"]] = r
for r in live:
    if r["section"].split()[0] in ("123", "124", "125", "126", "116", "108", "122"):
        o = ours.get(r["section"])
        print("LIVE", r["section"], "|", r["zone"], r["zone_id"], r["price"], "€ avail", r["available"],
              "|| OURS last:", (o["ts_utc"][:16], o["zone"], o["price"], o["available"]) if o else "never seen")
print("live sections:", len({r['section'] for r in live}), "ours ever:", len(ours))
