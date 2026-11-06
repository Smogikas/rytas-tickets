"""Offline tests for rytas.py. No network: the bilietai.lt API is simulated."""
import copy
import json
import sys
import tempfile
import unittest
import unittest.mock
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rytas  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
PRICES = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))
EVENT = json.loads((FIXTURES / "event.json").read_text(encoding="utf-8"))
SERIES = json.loads((FIXTURES / "series.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)

# section id -> number of seats (as in the real seat plans of these sections)
SECTION_SEATS = {"83548": 28, "94921": 16, "72935": 40}


class FakeSite:
    """Mutable stand-in for bilietai.lt. Seat ids are shared by all games,
    like the real venue layout."""

    def __init__(self):
        self.series = copy.deepcopy(SERIES)
        self.prices = {e["id"]: copy.deepcopy(PRICES) for e in self.series["events"]}
        # seat status per event: seat_id -> 1/0. Seats 0..5 of every section
        # are season tickets: taken in every game.
        self.seats = {}
        for e in self.series["events"]:
            self.seats[e["id"]] = {
                sid: {f"{sid}-{i}": (0 if i < 6 else 1) for i in range(n)}
                for sid, n in SECTION_SEATS.items()}
        self.requests = []

    def sell(self, eid, sid, n):
        free = [s for s, st in self.seats[eid][sid].items() if st == 1]
        for s in free[:n]:
            self.seats[eid][sid][s] = 0

    def set_price(self, eid, sid, price):
        for s in self.prices[eid]["eventSections"]:
            if s["id"] == sid:
                s["priceZones"][0]["ticketTypes"][0]["price"] = price

    def close_section(self, eid, sid):
        self.prices[eid]["eventSections"] = [s for s in self.prices[eid]["eventSections"] if s["id"] != sid]

    def get_json(self, url):
        self.requests.append(url)
        path = url.split("/api/v1/")[1].split("?")[0]
        parts = path.split("/")
        if parts[0] == "series":
            return copy.deepcopy(self.series)
        if parts[0] == "events":
            e = next((x for x in self.series["events"] if x["id"] == parts[1]), None)
            if not e:
                raise rytas.urllib.error.HTTPError(url, 404, "not found", {}, None)
            return dict(EVENT, id=e["id"], name=e["title"], eventStartAt=e["eventStartAt"],
                        seriesId=self.series["id"])
        if parts[:2] == ["store", "events"] and parts[3] == "prices":
            p = copy.deepcopy(self.prices[parts[2]])
            for s in p["eventSections"]:  # prices API caps availability at 22
                n = min(22, sum(self.seats[parts[2]][s["id"]].values()))
                s["availableTickets"] = n
                for z in s["priceZones"]:
                    for t in z["ticketTypes"]:
                        t["availableTickets"] = n
            return p
        if parts[:2] == ["store", "events"] and parts[3] == "sections":
            eid, sid = parts[2], parts[4]
            if sid not in {s["id"] for s in self.prices[eid]["eventSections"]}:
                raise rytas.urllib.error.HTTPError(url, 400, "closed", {}, None)
            return {"seatMapTickets": [
                {"seatId": seat, "row": "1", "place": seat.split("-")[1], "priceClass": 1, "status": st,
                 "x": 10 + 16 * int(seat.split("-")[1]), "y": 20}
                for seat, st in self.seats[eid][sid].items()]}
        raise AssertionError(f"unexpected URL {url}")

    def get_text(self, url):
        self.requests.append(url)
        assert url.endswith("/store/seat-map/1769507581.svg"), url
        return ("<svg>\r\n<image x='0'/>"
                "<polygon id=\"section83548\" points=\"524,632 622,632 622,612 523,611 \" style=\"fill:#ccc\"/>\r\n"
                "\t<polygon id='section94921' points=\"164,342 178,342\r\n 178,304 163,304 \"\r\n\tstyle=\"fill:#ccc\"/>"
                "<polygon points=\"1,1 2,2 3,3\" id='section72935'/></svg>")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, value in {
            "DATA": self.tmp / "data", "SNAPSHOTS": self.tmp / "data" / "snapshots.csv",
            "SEATS_DIR": self.tmp / "data" / "seats", "ATTENDANCE": self.tmp / "data" / "attendance.csv",
            "EVENTS": self.tmp / "events.json", "PAUSE": 0,
            "LAYOUT": self.tmp / "data" / "layout.csv", "SHAPES": self.tmp / "data" / "shapes.csv",
        }.items():
            p = unittest.mock.patch.object(rytas, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.site = FakeSite()
        for name in ("get_json", "get_text"):
            p = unittest.mock.patch.object(rytas, name, getattr(self.site, name))
            p.start()
            self.addCleanup(p.stop)
        self.hour = 10
        p = unittest.mock.patch.object(rytas, "now_utc", self._tick)
        p.start()
        self.addCleanup(p.stop)

    def _tick(self):
        self.hour += 1
        return f"2026-11-{6 + self.hour // 24:02d}T{self.hour % 24:02d}:00:00+00:00"

    def snapshot_all(self, seats=True):
        with unittest.mock.patch.object(rytas, "datetime", wraps=rytas.datetime) as dt:
            dt.now.return_value = NOW
            return rytas.main(["snapshot"] + (["--seats"] if seats else []))


class Prices(Base):
    def test_flatten(self):
        rows = rytas.flatten("EV", "t", PRICES)
        self.assertEqual(len(rows), 3)
        vip = [r for r in rows if r["section_id"] == "94921"][0]
        self.assertEqual((vip["price"], vip["fee"], vip["available"]), (189.0, 180.0, 8))

    def test_only_changes_logged_and_vanished_section_is_zero(self):
        _, w = rytas.take_snapshot("EV", PRICES)
        self.assertEqual(len(w), 3)
        _, w = rytas.take_snapshot("EV", PRICES)
        self.assertEqual(w, [])
        p = copy.deepcopy(PRICES)
        p["eventSections"] = [s for s in p["eventSections"] if s["id"] != "83548"]
        _, w = rytas.take_snapshot("EV", p)
        self.assertEqual([(r["section_id"], r["available"]) for r in w], [("83548", 0)])
        _, w = rytas.take_snapshot("EV", p)
        self.assertEqual(w, [])


class Seats(Base):
    E = "PNJMMUNJYB"

    def test_seat_snapshot_logs_all_then_only_changes(self):
        cur, w, n = rytas.take_seat_snapshot(self.E, self.site.get_json(f"x/api/v1/store/events/{self.E}/prices"))
        self.assertEqual((len(cur), len(w), n), (84, 84, 3))
        self.assertEqual(sum(r["status"] for r in cur.values()), 84 - 18)  # exact, not capped at 22
        _, w, _ = rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.assertEqual(w, [])
        self.site.sell(self.E, "72935", 5)
        _, w, _ = rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.assertEqual([(r["section_id"], r["status"]) for r in w], [("72935", 0)] * 5)

    def test_closed_section_remaining_seats_marked_taken_and_not_refetched(self):
        rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.site.close_section(self.E, "94921")  # 10 seats were still available
        _, w, n = rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.assertEqual(n, 3)  # tried the vanished section once more
        self.assertEqual(len(w), 10)
        self.assertTrue(all(r["section_id"] == "94921" and r["status"] == 0 for r in w))
        _, w, n = rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.assertEqual((n, w), (2, []))  # nothing left to check there

    def test_soldout_vs_withdrawn_sections(self):
        prices = rytas.fetch_prices(self.E)
        rytas.take_snapshot(self.E, prices)
        rytas.take_seat_snapshot(self.E, prices)
        # VIP leaves the list with 10 left (an exact count): sold out
        # 401 leaves the list with 34 left ("20+"): withdrawn, nothing counted as sold
        self.site.close_section(self.E, "94921")
        self.site.close_section(self.E, "72935")
        prices = rytas.fetch_prices(self.E)
        rytas.take_snapshot(self.E, prices)
        _, w, _ = rytas.take_seat_snapshot(self.E, prices)
        self.assertEqual({r["section_id"] for r in w}, {"94921"})
        res = rytas.analyse_event(rytas.timelines(rytas.load_seats(self.E)), rytas.load_snapshots(self.E), set())
        by = {x["section_id"]: x for x in res["sections"]}
        self.assertEqual(by["94921"]["status"], "soldout")
        self.assertEqual(by["94921"]["available"], 0)
        self.assertEqual(by["72935"]["status"], "closed")
        self.assertEqual(by["72935"]["available"], 34)       # not counted as sold
        self.assertTrue(by["72935"]["closed_at"])
        self.assertEqual(by["83548"]["status"], "open")
        # the withdrawn section reopens: open again
        self.site.prices[self.E] = copy.deepcopy(PRICES)
        prices = rytas.fetch_prices(self.E)
        rytas.take_snapshot(self.E, prices)
        res = rytas.analyse_event(rytas.timelines(rytas.load_seats(self.E)), rytas.load_snapshots(self.E), set())
        self.assertEqual({x["section_id"]: x["status"] for x in res["sections"]}["72935"], "open")

    def test_section_opening_later_is_picked_up(self):
        self.site.close_section(self.E, "72935")
        cur, _, _ = rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.assertEqual(len(cur), 44)
        self.site.prices[self.E] = copy.deepcopy(PRICES)  # section opens
        _, w, _ = rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.assertEqual(len(w), 40)

    def test_seats_due_schedule(self):
        def due(hours_to_game, hours_since):
            start = NOW + rytas.timedelta(hours=hours_to_game)
            last = NOW - rytas.timedelta(hours=hours_since)
            return rytas.seats_due({"start_utc": start.isoformat(), "last_seats_utc": last.isoformat()}, NOW)
        self.assertTrue(rytas.seats_due({"start_utc": NOW.isoformat()}, NOW))  # never fetched
        self.assertTrue(due(24, 1))
        self.assertFalse(due(24, 0.5))
        self.assertFalse(due(24 * 5, 3))
        self.assertTrue(due(24 * 5, 6))
        self.assertFalse(due(24 * 60, 12))
        self.assertTrue(due(24 * 60, 23))


class Analysis(Base):
    def test_season_groups(self):
        names = {"a": "LKL: x", "b": "LKL: y", "c": "LKL: z", "d": "BCL: w"}
        g = rytas.season_groups(names)
        self.assertEqual(g["a"], ("a", "b", "c"))
        self.assertEqual(g["d"], ("a", "b", "c", "d"))  # <3 BCL games -> compare with all

    def test_numbers_season_sales_prices(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        E, J = "PNJMMUNJYB", "ZPHZTQITUH"
        self.site.sell(E, "83548", 2)          # sold before tracking (not season: free in other games)
        self.assertEqual(self.snapshot_all(), 0)
        self.site.sell(E, "83548", 3)          # 3 sold at 89
        self.snapshot_all()
        self.site.set_price(E, "83548", 99.0)
        self.site.sell(E, "83548", 1)          # 1 sold at the new price
        self.site.sell(J, "94921", 4)
        self.snapshot_all()

        res = rytas.analyse_all(rytas.load_events())
        by = {x["section_id"]: x for x in res[E]["sections"]}
        p2 = by["83548"]
        self.assertEqual((p2["seats"], p2["taken"], p2["season"]), (28, 12, 6))
        self.assertEqual((p2["sold_seen"], p2["pre_taken"], p2["singles"]), (4, 2, 6))
        # club revenue = ticket value = price - fee (fee 7 in this section)
        self.assertAlmostEqual(p2["revenue"], 3 * (89 - 7) + (99 - 7) + 2 * (89 - 7))
        self.assertAlmostEqual(p2["gross"], 3 * 89 + 99 + 2 * 89)
        self.assertEqual((p2["price"], p2["fee"]), (99.0, 7.0))
        self.assertEqual([h[1] for h in p2["history"]], [89.0, 99.0])  # price change recorded

        t = res[E]["totals"]
        self.assertEqual(t["seats"], 84)
        self.assertEqual(t["season"], 18)      # 6 per section, taken in every game
        self.assertEqual(t["taken"], 24)
        self.assertAlmostEqual(t["avg_price"], t["revenue"] / 6)
        self.assertEqual(res[J]["totals"]["sold_seen"], 4)

        for cmd in (["report"], ["report", E], ["season"]):
            self.assertEqual(rytas.main(cmd), 0)
        rytas.main(["attendance", E, "70"])
        self.assertEqual(rytas.main(["report", E]), 0)

    def test_seat_sold_in_one_game_before_tracking_is_not_season(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        self.site.sell("PNJMMUNJYB", "72935", 3)
        self.snapshot_all()
        res = rytas.analyse_all(rytas.load_events())
        t = res["PNJMMUNJYB"]["totals"]
        self.assertEqual((t["season"], t["pre_taken"]), (18, 3))


class Commands(Base):
    def test_add_series_discover_and_snapshot(self):
        self.assertEqual(rytas.main(["add-series", "https://www.bilietai.lt/renginiai/PNJMMUNJYB/x/seatmap"]), 0)
        ev = rytas.load_events()
        self.assertEqual(list(ev["series"]), ["Y4ISRKYEV3"])
        self.assertEqual(len(ev["events"]), 3)

        # a new game is published and one is rescheduled
        self.site.series["events"].append({"id": "NEWGAME001", "title": "LKL: Vilniaus ''Rytas'' - Kauno ''Žalgiris''",
                                           "eventStartAt": "2027-04-18T13:50:00Z", "status": "ON_SALE"})
        self.site.series["events"][1]["eventStartAt"] = "2026-11-16T17:00:00Z"
        self.site.prices["NEWGAME001"] = copy.deepcopy(PRICES)
        self.site.seats["NEWGAME001"] = copy.deepcopy(self.site.seats["PNJMMUNJYB"])
        with unittest.mock.patch("builtins.print") as out:
            self.assertEqual(rytas.main(["discover"]), 0)
        printed = "\n".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("NEW      NEWGAME001", printed)
        self.assertIn("UPDATED  ZPHZTQITUH", printed)
        ev = rytas.load_events()
        self.assertEqual(len(ev["events"]), 4)
        self.assertEqual(ev["events"]["ZPHZTQITUH"]["start_utc"], "2026-11-16T17:00:00Z")

        # first snapshot fetches seat plans for every game, next one within the hour does not
        self.assertEqual(self.snapshot_all(seats=False), 0)
        self.assertTrue(all(rytas.seat_path(e).exists() for e in ev["events"]))
        n = len(self.site.requests)
        self.snapshot_all(seats=False)
        self.assertEqual(len(self.site.requests) - n, 4)  # prices only, 1 per game

    def test_layout_and_shapes_stored_once_and_export(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        self.site.sell("PNJMMUNJYB", "83548", 2)
        self.assertEqual(self.snapshot_all(), 0)
        self.site.sell("PNJMMUNJYB", "83548", 1)
        self.snapshot_all()
        self.assertEqual(len(rytas.read_csv(rytas.LAYOUT)), 84)   # every seat once, shared by all games
        shapes = rytas.read_csv(rytas.SHAPES)
        self.assertEqual({s["section_id"] for s in shapes}, {"83548", "94921", "72935"})
        self.assertEqual([s["points"] for s in shapes if s["section_id"] == "94921"], ["164,342 178,342 178,304 163,304"])
        self.assertEqual(sum(u.endswith(".svg") for u in self.site.requests), 1)
        self.assertEqual(rytas.load_events()["events"]["PNJMMUNJYB"]["conf_id"], "1769507581")

        rytas.main(["attendance", "PNJMMUNJYB", "4100"])
        out = self.tmp / "site"
        self.assertEqual(rytas.main(["export", str(out)]), 0)
        data = json.loads((out / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["games"]), 3)
        g = next(x for x in data["games"] if x["id"] == "PNJMMUNJYB")
        self.assertEqual((g["comp"], g["opp"]), ("BCL", "Nantero „Nanterre 92“"))
        self.assertEqual((g["taken"], g["season"], g["singles"], g["seen"], g["before"]), (21, 18, 3, 1, 2))
        self.assertAlmostEqual(g["revenue"], 3 * (89 - 7))
        self.assertAlmostEqual(g["gross"], 3 * 89)
        self.assertEqual(g["attendance"], 4100)
        self.assertEqual(len(data["shapes"]["1769507581"]), 3)
        self.assertEqual(data["seasonSeats"], 18)
        sec = next(s for s in data["sections"]["PNJMMUNJYB"] if s["id"] == "83548")
        self.assertEqual((sec["seats"], sec["seen"], sec["fee"]), (28, 1, 7.0))
        seats = json.loads((out / "seats" / "PNJMMUNJYB.json").read_text(encoding="utf-8"))
        cats = Counter(s[4] for s in seats["83548"])
        self.assertEqual(cats, Counter({"A": 19, "S": 6, "B": 2, "O": 1}))
        sold = next(s for s in seats["83548"] if s[4] == "O")
        self.assertEqual((sold[5], sold[6]), (89.0, 7.0))
        self.assertTrue(sold[7])  # when the sale was seen

    def test_split_name(self):
        self.assertEqual(rytas.split_name("LKL: Vilniaus ''Rytas'' - Kauno ''Žalgiris''"), ("LKL", "Kauno „Žalgiris“"))

    def test_price_check_triggers_targeted_seat_checks(self):
        E = "PNJMMUNJYB"
        rytas.main(["add-series", E])
        self.snapshot_all(seats=True)               # full scan of every game
        seat_calls = lambda: [u for u in self.site.requests if "/sections/" in u]
        n0, s0 = len(self.site.requests), len(seat_calls())

        # nothing changed: hourly run = prices only
        self.snapshot_all(seats=False)
        self.assertEqual(len(self.site.requests) - n0, 3)

        # 3 sold in Parteris 2 (22 left -> 19, visible under the cap): only that section is read
        self.site.sell(E, "83548", 3)
        s1 = len(seat_calls())
        self.snapshot_all(seats=False)
        self.assertEqual([u.split("/sections/")[1].split("/")[0] for u in seat_calls()[s1:]], ["83548"])
        tl = rytas.timelines(rytas.load_seats(E))
        self.assertEqual(sum(1 for rows in tl.values() if rows[0]["status"] == 1 and rows[-1]["status"] == 0), 3)

        # 3 sold in 401 (34 left -> 31, shown as 22 both times): no signal, waits for the full scan
        self.site.sell(E, "72935", 3)
        s2 = len(seat_calls())
        self.snapshot_all(seats=False)
        self.assertEqual(seat_calls()[s2:], [])

        # VIP closes: its remaining seats are marked taken right away; reopening brings them back
        self.site.close_section(E, "94921")
        self.snapshot_all(seats=False)
        vip = [r for r in rytas.latest_by(rytas.load_seats(E), lambda r: r["seat_id"]).values() if r["section_id"] == "94921"]
        self.assertEqual(sum(r["status"] for r in vip), 0)
        self.site.prices[E]["eventSections"].append(copy.deepcopy(PRICES["eventSections"][1]))
        self.snapshot_all(seats=False)
        vip = [r for r in rytas.latest_by(rytas.load_seats(E), lambda r: r["seat_id"]).values() if r["section_id"] == "94921"]
        self.assertEqual(sum(r["status"] for r in vip), 10)

    def test_final_check_before_tipoff(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        self.snapshot_all(seats=True)
        start = datetime(2026, 11, 11, 17, 30, tzinfo=timezone.utc)
        now = start - rytas.timedelta(minutes=43)      # hourly run at 16:47
        ev = rytas.load_events()
        self.assertEqual([e for _s, e, _m in rytas.games_starting_soon(ev, now, 75)], ["PNJMMUNJYB"])
        self.assertEqual(rytas.games_starting_soon(ev, start - rytas.timedelta(minutes=80), 75), [])
        self.assertEqual(rytas.games_starting_soon(ev, start + rytas.timedelta(minutes=1), 75), [])

        out = self.tmp / "gh_output"
        with unittest.mock.patch.object(rytas, "datetime", wraps=rytas.datetime) as dt, \
             unittest.mock.patch.dict("os.environ", {"GITHUB_OUTPUT": str(out)}):
            dt.now.return_value = now
            self.assertEqual(rytas.main(["final", "--check"]), 0)
        self.assertEqual(out.read_text().strip(), "soon=true")

        sleeps = []
        n = len(self.site.requests)
        self.site.sell("PNJMMUNJYB", "72935", 3)                  # "20+" section: only a full read sees it
        with unittest.mock.patch.object(rytas, "datetime", wraps=rytas.datetime) as dt, \
             unittest.mock.patch.object(rytas.time, "sleep", side_effect=sleeps.append):
            dt.now.return_value = now
            self.assertEqual(rytas.main(["final"]), 0)
        self.assertEqual([round(x) for x in sleeps if x > 1], [33 * 60])   # waits until 17:20, 10 min before (ignore request pauses)
        seat_reads = [u for u in self.site.requests[n:] if "/sections/" in u]
        self.assertEqual(len(seat_reads), 3)                       # every open section of that game only
        tl = rytas.timelines(rytas.load_seats("PNJMMUNJYB"))
        self.assertEqual(sum(1 for r in tl.values() if r[0]["status"] == 1 and r[-1]["status"] == 0), 3)
        self.assertIn("final_check_utc", rytas.load_events()["events"]["PNJMMUNJYB"])

    def test_add_single_event(self):
        self.assertEqual(rytas.main(["add", "ZPHZTQITUH"]), 0)
        self.assertTrue(rytas.seat_path("ZPHZTQITUH").exists())
        self.assertEqual(rytas.load_events()["events"]["ZPHZTQITUH"]["series_id"], "Y4ISRKYEV3")

    def test_old_events_json_is_upgraded(self):
        rytas.EVENTS.write_text(json.dumps({"PNJMMUNJYB": {"name": "BCL: x", "start_utc": "2026-11-11T17:30:00Z"}}))
        ev = rytas.load_events()
        self.assertEqual(ev["series"], {})
        self.assertIn("PNJMMUNJYB", ev["events"])
        self.assertEqual(self.snapshot_all(), 0)

    def test_snapshot_failure_returns_nonzero_and_others_continue(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        orig = self.site.get_json

        def flaky(url):
            if "ZPHZTQITUH" in url:
                raise OSError("network down")
            return orig(url)
        with unittest.mock.patch.object(rytas, "get_json", flaky):
            self.assertEqual(self.snapshot_all(), 1)
        self.assertTrue(rytas.seat_path("PNJMMUNJYB").exists())
        self.assertTrue(rytas.seat_path("JSAIJZ5XW2").exists())

    def test_past_games_skipped(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        with unittest.mock.patch.object(rytas, "datetime", wraps=rytas.datetime) as dt:
            dt.now.return_value = datetime(2026, 11, 20, tzinfo=timezone.utc)
            rytas.main(["snapshot"])
        self.assertFalse(rytas.seat_path("PNJMMUNJYB").exists())
        self.assertTrue(rytas.seat_path("JSAIJZ5XW2").exists())

    def test_event_id_from_url(self):
        url = "https://www.bilietai.lt/renginiai/PNJMMUNJYB/bcl-vilniaus-rytas-nantero-nanterre-92/seatmap"
        self.assertEqual(rytas.event_id_from(url), "PNJMMUNJYB")
        self.assertEqual(rytas.event_id_from(" PNJMMUNJYB "), "PNJMMUNJYB")


class RealHttp(unittest.TestCase):
    def test_anonymous_session_cookie_and_closed_section(self):
        """Fake HTTP server: API returns 403 unless the login/express cookie is sent,
        and 400 for a closed section's seat plan."""
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
                    return self._send(200, {"uuid": "u", "authenticated": False}, "SESSION=abc; Path=/; HttpOnly")
                self._send(404, {})

            def do_GET(self):
                calls.append(("GET", self.path))
                if "SESSION=abc" not in (self.headers.get("Cookie") or ""):
                    return self._send(403, {"error": "forbidden"})
                if "/sections/CLOSED/" in self.path:
                    return self._send(400, {"error": "closed"})
                if "/seat-map" in self.path:
                    return self._send(200, {"seatMapTickets": [{"seatId": 1, "row": "1", "place": "1",
                                                                "priceClass": 9, "status": 1}]})
                self._send(200, PRICES if "/prices" in self.path else EVENT)

        srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with unittest.mock.patch.object(rytas, "API", f"http://127.0.0.1:{srv.server_port}/api/v1"), \
             unittest.mock.patch.object(rytas, "_opener", None), \
             unittest.mock.patch.dict("os.environ", {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            self.assertEqual(rytas.fetch_info("X")["eventStartAt"], "2026-11-11T17:30:00Z")
            self.assertEqual(len(rytas.fetch_prices("X")["eventSections"]), 3)
            self.assertEqual(len(rytas.fetch_section_seats("X", "OPEN")), 1)
            self.assertIsNone(rytas.fetch_section_seats("X", "CLOSED"))
        self.assertEqual(calls[0], ("POST", "/api/v1/login/express"))
        self.assertEqual([c[0] for c in calls].count("POST"), 1)


if __name__ == "__main__":
    unittest.main()
