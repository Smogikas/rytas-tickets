"""Offline tests for rytas.py using a trimmed sample of the real API response."""
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rytas  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))
EVENT = json.loads((FIXTURES / "event.json").read_text(encoding="utf-8"))


def fake_get_json(url):
    """Stand-in for the bilietai.lt API (no network in tests)."""
    if "/prices" in url:
        return json.loads(json.dumps(FIXTURE))
    if "/events/" in url:
        return dict(EVENT)
    raise AssertionError(f"unexpected URL {url}")


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

    def test_add_snapshot_report_end_to_end(self):
        orig = rytas.get_json
        rytas.get_json = fake_get_json
        try:
            url = "https://www.bilietai.lt/renginiai/PNJMMUNJYB/bcl-vilniaus-rytas-nantero-nanterre-92/seatmap"
            self.assertEqual(rytas.main(["add", url]), 0)
            events = json.loads(rytas.EVENTS.read_text(encoding="utf-8"))
            self.assertEqual(events["PNJMMUNJYB"]["start_utc"], "2026-11-11T17:30:00Z")

            # future event -> snapshot taken; past event -> skipped
            events["OLDGAME"] = {"name": "old", "start_utc": "2020-01-01T00:00:00Z"}
            rytas.EVENTS.write_text(json.dumps(events), encoding="utf-8")
            with unittest.mock.patch.object(rytas, "datetime", wraps=rytas.datetime) as dt:
                dt.now.return_value = rytas.datetime(2026, 11, 6, 12, tzinfo=rytas.timezone.utc)
                self.assertEqual(rytas.main(["snapshot"]), 0)
            # nothing changed between the two snapshots -> no new rows
            self.assertEqual(len(rytas.load_snapshots("PNJMMUNJYB")), 3)
            self.assertEqual(rytas.load_snapshots("OLDGAME"), [])
            self.assertEqual(rytas.main(["report"]), 0)
        finally:
            rytas.get_json = orig

    def test_only_changes_logged_and_vanished_section_is_sold_out(self):
        hours = iter(range(10, 20))
        patcher = unittest.mock.patch.object(
            rytas, "now_utc", side_effect=lambda: f"2026-11-06T{next(hours)}:00:00+00:00")
        patcher.start()
        self.addCleanup(patcher.stop)
        rows, written = rytas.take_snapshot("EV", FIXTURE)
        self.assertEqual(len(written), 3)
        rows, written = rytas.take_snapshot("EV", FIXTURE)
        self.assertEqual(written, [])  # identical -> nothing written

        # VIP sells 2, Parteris 2 (14 left) disappears from the API entirely
        p = with_available(FIXTURE, "94921", 6)
        p["eventSections"] = [s for s in p["eventSections"] if s["id"] != "83548"]
        rows, written = rytas.take_snapshot("EV", p)
        got = {r["section_id"]: int(r["available"]) for r in written}
        self.assertEqual(got, {"94921": 6, "83548": 0})

        # it stays at 0 on later snapshots without being re-logged
        rows, written = rytas.take_snapshot("EV", p)
        self.assertEqual(written, [])

        res = rytas.analyse(rytas.load_snapshots("EV"), {"83548": {"seats": 20}, "94921": {"seats": 10}})
        by = {x["section_id"]: x for x in res["sections"]}
        self.assertEqual(by["83548"]["sold"], 20)
        self.assertEqual(by["83548"]["observed_sold"], 14)
        self.assertEqual(by["94921"]["observed_sold"], 2)

    def test_snapshot_failure_returns_nonzero(self):
        def boom(url):
            raise OSError("network down")
        rytas.EVENTS.write_text(json.dumps(
            {"X": {"name": "x", "start_utc": "2099-01-01T00:00:00Z"}}), encoding="utf-8")
        orig = rytas.get_json
        rytas.get_json = boom
        try:
            self.assertEqual(rytas.main(["snapshot"]), 1)
        finally:
            rytas.get_json = orig

    def test_real_http_uses_anonymous_session_cookie(self):
        """Fake bilietai.lt: API returns 403 unless the login/express cookie is sent."""
        import http.server
        import threading

        calls = []

        class Fake(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, cookie=None):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                calls.append(("POST", self.path))
                if self.path == "/api/v1/login/express":
                    self._send(200, {"uuid": "u", "authenticated": False}, "SESSION=abc; Path=/; HttpOnly")
                else:
                    self._send(404, {})

            def do_GET(self):
                calls.append(("GET", self.path))
                if "SESSION=abc" not in (self.headers.get("Cookie") or ""):
                    return self._send(403, {"error": "forbidden"})
                self._send(200, FIXTURE if "/prices" in self.path else EVENT)

        srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with unittest.mock.patch.object(rytas, "API", f"http://127.0.0.1:{srv.server_port}/api/v1"), \
             unittest.mock.patch.object(rytas, "_opener", None), \
             unittest.mock.patch.dict("os.environ", {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            info, prices = rytas.fetch_event("PNJMMUNJYB")
        self.assertEqual(info["eventStartAt"], "2026-11-11T17:30:00Z")
        self.assertEqual(len(prices["eventSections"]), 3)
        self.assertEqual(calls[0], ("POST", "/api/v1/login/express"))  # session first, once
        self.assertEqual([c[0] for c in calls].count("POST"), 1)

    def test_event_id_from_url(self):
        url = "https://www.bilietai.lt/renginiai/PNJMMUNJYB/bcl-vilniaus-rytas-nantero-nanterre-92/seatmap"
        self.assertEqual(rytas.event_id_from(url), "PNJMMUNJYB")
        self.assertEqual(rytas.event_id_from("PNJMMUNJYB"), "PNJMMUNJYB")


if __name__ == "__main__":
    unittest.main()
