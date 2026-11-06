"""One-off: show the price-zone structure and how seat priceClass maps to zones."""
import json, sys, time
from collections import Counter
sys.path.insert(0, ".")
import rytas
for eid in sys.argv[1:]:
    p = rytas.fetch_prices(eid)
    print("=== event", eid, "top keys", list(p))
    multi = 0
    for s in p.get("eventSections", []):
        zones = s.get("priceZones", [])
        tts = sum(len(z.get("ticketTypes", [])) for z in zones)
        if len(zones) > 1 or tts > len(zones):
            multi += 1
            if multi <= 4:
                print("SECTION", s["id"], s["title"], "keys", [k for k in s if k != "priceZones"])
                for z in zones:
                    print("  zone", {k: v for k, v in z.items() if k != "ticketTypes"})
                    for t in z.get("ticketTypes", []):
                        print("    tt", t)
                seats = rytas.fetch_section_seats(eid, str(s["id"])) or []
                print("  seat priceClass counts", Counter((t.get("priceClass"), t.get("status")) for t in seats))
                if seats: print("  seat sample", {k: v for k, v in seats[0].items()})
                time.sleep(0.5)
    print("sections", len(p.get("eventSections", [])), "with several zones/types", multi)
    names = Counter(t.get("name") or t.get("title") or t.get("description") for s in p["eventSections"] for z in s["priceZones"] for t in z["ticketTypes"])
    print("ticket type names", names.most_common(10))
