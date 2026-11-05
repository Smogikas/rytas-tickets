"""Offline tests for rytas.py using a trimmed sample of the real API response."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rytas  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "prices.json").read_text(encoding="utf-8"))


def with_available(prices, section_id, n, price=None):
    out = json.loads(json.dumps(prices))
    for s in out["eventSections"]:
        if s["id"] == section_id:
            for z in s["priceZones"]:
                for t in z["ticketTypes"]:
                    t["availableTickets"] = n
                    if price is not None:
                        t["price"] = price
    return out


class Tracker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        rytas.DATA = self.tmp / "data"
        rytas.SNAPSHOTS = rytas.DATA / "snapshots.csv"
        rytas.ATTENDANCE = rytas.DATA / "attendance.csv"
        rytas.EVENTS = self.tmp / "events.json"
        rytas.CAPACITY = self.tmp / "capacity.json"

    def test_flatten(self):
        rows = rytas.flatten("EV", "t", FIXTURE)
        self.assertEqual(len(rows), 3)
        vip = [r for r in rows if r["section_id"] == "94921"][0]
        self.assertEqual((vip["price"], vip["fee"], vip["available"]), (189.0, 180.0, 8))

    def test_sales_and_revenue(self):
        # snapshot 1, then 4 tickets sell in Parteris 2 (14 -> 10), VIP 8 -> 7 at a new price
        rows1 = rytas.flatten("EV", "2026-11-06T10:00:00+00:00", FIXTURE)
        p2 = with_available(FIXTURE, "83548", 10)
        p2 = with_available(p2, "94921", 7, price=199.0)
        rows2 = rytas.flatten("EV", "2026-11-07T10:00:00+00:00", p2)
        caps = {"83548": {"seats": 20}, "94921": {"seats": 10}, "72935": {"seats": 100}}
        res = rytas.analyse(rows1 + rows2, caps)
        by = {x["section_id"]: x for x in res["sections"]}

        self.assertEqual(by["83548"]["sold"], 10)          # 20 - 10
        self.assertEqual(by["83548"]["observed_sold"], 4)  # 14 -> 10
        # 4 observed at 89 + 6 earlier (unobserved) at first logged price 89
        self.assertAlmostEqual(by["83548"]["revenue_est"], 10 * 89.0)

        self.assertEqual(by["94921"]["sold"], 3)
        # 1 observed at the earlier price 189, 2 unobserved at first price 189
        self.assertAlmostEqual(by["94921"]["revenue_est"], 3 * 189.0)
        self.assertEqual(by["94921"]["price"], 199.0)      # latest price kept

        # capped section: exact sold unknown, only an upper bound
        self.assertIsNone(by["72935"]["sold"])
        self.assertEqual(by["72935"]["sold_max"], 78)      # 100 - 22

        t = res["totals"]
        self.assertEqual(t["sold"], 13)
        self.assertAlmostEqual(t["revenue_est"], 10 * 89 + 3 * 189)

    def test_multiple_ticket_types_share_seats(self):
        # a second, pricier ticket type in the VIP section must not double count capacity
        p = json.loads(json.dumps(FIXTURE))
        vip = [s for s in p["eventSections"] if s["id"] == "94921"][0]
        vip["priceZones"][0]["ticketTypes"].append(
            {"id": "9999", "name": "Su maistu", "price": 250.0, "fee": 200.0, "availableTickets": 8})
        rows = rytas.flatten("EV", "t", p)
        res = rytas.analyse(rows, {"94921": {"seats": 10}})
        by = {x["section_id"]: x for x in res["sections"]}
        self.assertEqual(len(res["sections"]), 3)
        self.assertEqual(by["94921"]["sold"], 2)
        self.assertEqual(by["94921"]["price"], 189.0)  # cheapest type

    def test_no_capacity_means_no_exact_sold(self):
        rows = rytas.flatten("EV", "t", FIXTURE)
        res = rytas.analyse(rows, {})
        self.assertEqual(res["totals"]["sold"], 0)
        self.assertIsNone(res["totals"]["avg_price"])

    def test_snapshot_creates_capacity_template_and_attendance(self):
        rytas.take_snapshot("EV", FIXTURE)
        caps = json.loads(rytas.CAPACITY.read_text(encoding="utf-8"))
        self.assertEqual(set(caps), {"83548", "94921", "72935"})
        self.assertTrue(all(v["seats"] is None for v in caps.values()))
        rytas.main(["attendance", "EV", "8450", "--note", "official"])
        self.assertEqual(rytas.latest_attendance("EV"), 8450)
        self.assertEqual(len(rytas.load_snapshots("EV")), 3)

    def test_event_id_from_url(self):
        url = "https://www.bilietai.lt/renginiai/PNJMMUNJYB/bcl-vilniaus-rytas-nantero-nanterre-92/seatmap"
        self.assertEqual(rytas.event_id_from(url), "PNJMMUNJYB")
        self.assertEqual(rytas.event_id_from("PNJMMUNJYB"), "PNJMMUNJYB")


if __name__ == "__main__":
    unittest.main()
