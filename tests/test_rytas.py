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
        self.classes = {}                      # seat_id -> price level (priceClass), default 1

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
        if parts[:2] == ["store", "seat-map"]:                 # full venue plan
            seats = [{"code": seat, "x": 10 + 16 * int(seat.split("-")[1]), "y": 20, "row": "1",
                      "place": seat.split("-")[1], "section": sid}
                     for sid, n in SECTION_SEATS.items() for seat in (f"{sid}-{i}" for i in range(n))]
            seats += [{"code": f"F1-{i}", "x": 16 * i, "y": 40, "row": "1", "place": str(i), "section": "79993"} for i in range(8)]
            seats += [{"code": f"F2-{i}", "x": 16 * i, "y": 40, "row": "1", "place": str(i), "section": "72934"} for i in range(10)]
            seats += [{"code": f"BOX-{i}", "x": i, "y": 0, "row": "1", "place": str(i), "section": "99999"} for i in range(5)]
            return {"seats": seats}
        if parts[:2] == ["store", "events"] and parts[3] == "sections":
            eid, sid = parts[2], parts[4]
            if sid not in {s["id"] for s in self.prices[eid]["eventSections"]}:
                raise rytas.urllib.error.HTTPError(url, 400, "closed", {}, None)
            return {"seatMapTickets": [
                {"seatId": seat, "row": "1", "place": seat.split("-")[1], "priceClass": self.classes.get(seat, 1),
                 "status": st, "x": 10 + 16 * int(seat.split("-")[1]), "y": 20}
                for seat, st in self.seats[eid][sid].items()]}
        raise AssertionError(f"unexpected URL {url}")

    def get_text(self, url):
        self.requests.append(url)
        assert url.endswith("/store/seat-map/1769507581.svg"), url
        return ("<svg>\r\n<image x='0'/>"
                "<polygon id=\"section83548\" points=\"524,632 622,632 622,612 523,611 \" style=\"fill:#ccc\"/>\r\n"
                "\t<polygon id='section94921' points=\"164,342 178,342\r\n 178,304 163,304 \"\r\n\tstyle=\"fill:#ccc\"/>"
                "<polygon points=\"1,1 2,2 3,3\" id='section72935'/>"
                "<polygon id='section79993' points='4,4 5,5 6,6'/><polygon id='section72934' points='7,7 8,8 9,9'/></svg>")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, value in {
            "DATA": self.tmp / "data", "SNAPSHOTS": self.tmp / "data" / "snapshots.csv",
            "SEATS_DIR": self.tmp / "data" / "seats", "ATTENDANCE": self.tmp / "data" / "attendance.csv",
            "EVENTS": self.tmp / "events.json", "PAUSE": 0,
            "LAYOUT": self.tmp / "data" / "layout.csv", "SHAPES": self.tmp / "data" / "shapes.csv",
            "CAPACITY": self.tmp / "data" / "capacity.csv",
            "RUN_STATUS": self.tmp / "data" / "run_status.json",
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

    def test_free_seat_moved_to_another_price_level_is_logged(self):
        rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        sec = self.site.seats[self.E]["72935"]
        free = [x for x, st in sec.items() if st == 1]
        taken = [x for x, st in sec.items() if st == 0]
        for x in free + taken:                  # the club reprices the section: only free seats matter
            self.site.classes[x] = 2
        _, w, _ = rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.assertEqual(sorted(r["seat_id"] for r in w), sorted(free))
        self.assertTrue(all(r["status"] == 1 and r["price_class"] == "2" for r in w))
        _, w, _ = rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.assertEqual(w, [])                 # logged once
        self.site.sell(self.E, "72935", 1)
        _, w, _ = rytas.take_seat_snapshot(self.E, rytas.fetch_prices(self.E))
        self.assertEqual([(r["status"], r["price_class"]) for r in w], [(0, "2")])
        tl = rytas.timelines(rytas.load_seats(self.E))
        rows = tl[w[0]["seat_id"]]
        self.assertEqual([r["status"] for r in rows], [1, 1, 0])
        cls = rytas.classify_seats(tl, {}, set())[w[0]["seat_id"]]
        self.assertEqual((cls[1], cls[4]), (rytas.SOLD_SEEN, w[0]["ts_utc"]))   # sale time = the sale, not the repricing
        self.assertEqual(rytas.recent_sales(rytas.load_seats(self.E), "0"), 1)

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

    def test_withdrawn_seats_are_not_available_and_sellout_games(self):
        prices = rytas.fetch_prices(self.E)
        rytas.take_snapshot(self.E, prices)
        rytas.take_seat_snapshot(self.E, prices)
        self.site.close_section(self.E, "72935")                  # 34 left ("22+"): withdrawn
        prices = rytas.fetch_prices(self.E)
        rytas.take_snapshot(self.E, prices)
        rytas.take_seat_snapshot(self.E, prices)
        t = rytas.analyse_event(rytas.timelines(rytas.load_seats(self.E)), rytas.load_snapshots(self.E), set())["totals"]
        self.assertEqual(t["withdrawn_free"], 34)
        self.assertEqual(t["available"], (28 - 6) + (16 - 6))     # only seats that can be bought
        # a game whose sections sell out (Žalgiris): the same section leaving counts as sold out
        J = "ZPHZTQITUH"
        prices = rytas.fetch_prices(J)
        rytas.take_snapshot(J, prices)
        rytas.take_seat_snapshot(J, prices)
        self.site.close_section(J, "72935")
        prices = rytas.fetch_prices(J)
        rytas.take_snapshot(J, prices)
        _, w, _ = rytas.take_seat_snapshot(J, prices, sellout=True)
        self.assertEqual(len([r for r in w if r["section_id"] == "72935" and r["status"] == 0]), 34)
        res = rytas.analyse_event(rytas.timelines(rytas.load_seats(J)), rytas.load_snapshots(J), set())
        self.assertEqual({x["section_id"]: x["status"] for x in res["sections"]}["72935"], "soldout")

    def test_off_sale_section_seat_plan_is_not_read(self):
        """bilietai.lt still returns the seat plan of a section off the sale list, with
        every seat 'taken' (= can't be bought). It must not count as sold."""
        prices = rytas.fetch_prices(self.E)
        rytas.take_snapshot(self.E, prices)
        rytas.take_seat_snapshot(self.E, prices)
        orig, asked = self.site.get_json, []

        def answers_off_sale(url):
            if "/sections/72935/seat-map" in url:
                asked.append(url)
                return {"seatMapTickets": [{"seatId": f"72935-{i}", "row": "1", "place": str(i), "priceClass": 1,
                                            "status": 0, "x": 0, "y": 0} for i in range(40)]}
            return orig(url)
        self.site.close_section(self.E, "72935")                  # 34 left: withdrawn
        with unittest.mock.patch.object(rytas, "get_json", answers_off_sale):
            prices = rytas.fetch_prices(self.E)
            rytas.take_snapshot(self.E, prices)
            _, w, _ = rytas.take_seat_snapshot(self.E, prices)
        self.assertEqual([r for r in w if r["section_id"] == "72935"], [])
        self.assertEqual(asked, [])                                # not even asked

    def test_placeholder_seats_are_not_counted(self):
        T1 = "2026-11-06T10:00:00+00:00"
        tl = {"a": [{"ts_utc": T1, "section_id": "S", "seat_id": "a", "status": 0, "price_class": "0", "place": "*NĖRA*"}],
              "d": [{"ts_utc": T1, "section_id": "S", "seat_id": "d", "status": 0, "price_class": "0", "place": "1"}],
              "b": [{"ts_utc": T1, "section_id": "S", "seat_id": "b", "status": 0, "price_class": "1334"}],
              "c": [{"ts_utc": T1, "section_id": "S", "seat_id": "c", "status": 1, "price_class": "1334"}]}
        tls = {"g1": tl, "g2": tl, "g3": tl}
        season = rytas.season_seats(("g1", "g2", "g3"), tls)
        self.assertEqual(season, {"b", "d"})          # placeholder a: no; price class 0 seat d: a season seat
        t = rytas.analyse_event(tl, [], season)["totals"]
        self.assertEqual((t["seats"], t["season"], t["available"], t["taken"]), (3, 2, 1, 2))
        self.assertEqual(rytas.classify_seats(tl, {}, season)["a"][1], rytas.BLOCKED)

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
        self.assertTrue(due(24 * 4, 1))          # 4 days away: hourly (last 5 days)
        self.assertTrue(due(24 * 5, 1))
        self.assertFalse(due(24 * 6, 3))         # 6 days away: every 6 h
        self.assertTrue(due(24 * 6, 6))
        self.assertFalse(due(24 * 60, 12))
        self.assertTrue(due(24 * 60, 23))


class Zones(unittest.TestCase):
    """Sections with several price zones; rows are logged only when they change."""

    def rows(self, spec, event="E"):
        return [{"ts_utc": ts, "event_id": event, "section_id": "S", "section": "Sec", "zone": z,
                 "ticket_type_id": "6175", "price": p, "fee": 7.0, "available": a, "zone_id": zid}
                for ts, z, zid, p, a in spec]

    def test_one_zone_changing_does_not_move_the_other(self):
        # T2: only the 79 zone is logged (tickets left changed). The 69 zone is unchanged.
        r = self.rows([("T1", "21-ta kaina", "1370", 79.0, 14), ("T1", "22-ta kaina", "1371", 69.0, 14),
                       ("T2", "21-ta kaina", "1370", 79.0, 10)])
        pl = rytas.price_lookup(r)
        h = rytas.zone_histories(pl, "S")
        self.assertEqual([x[1] for x in h["21-ta kaina"]], [79.0])
        self.assertEqual([x[1] for x in h["22-ta kaina"]], [69.0])
        self.assertEqual(rytas.price_history(pl, "S"), [("T1", 69.0, 7.0)])   # no false change
        self.assertEqual(rytas.price_at(pl, "S", "T2", "1370"), (79.0, 7.0))  # seat priced by its zone
        self.assertEqual(rytas.price_at(pl, "S", "T2", "1371.0"), (69.0, 7.0))
        self.assertEqual(rytas.price_at(pl, "S", "T2"), (69.0, 7.0))          # zone unknown: cheapest on sale
        self.assertEqual(rytas.current_zones(pl, "S")[0][:2], ("22-ta kaina", 69.0))

    def test_zone_sold_out_and_real_price_change(self):
        r = self.rows([("T1", "21-ta kaina", "1370", 79.0, 14), ("T1", "22-ta kaina", "1371", 69.0, 3),
                       ("T2", "22-ta kaina", "1371", 69.0, 0),                 # cheap zone sold out
                       ("T3", "21-ta kaina", "1370", 85.0, 9)])                # real price rise
        pl = rytas.price_lookup(r)
        self.assertEqual(rytas.price_at(pl, "S", "T2"), (79.0, 7.0))          # cheapest still on sale
        self.assertEqual(rytas.price_at(pl, "S", "T1", "1371"), (69.0, 7.0))  # seat sold at T1 in the 69 zone
        self.assertEqual([x[1] for x in rytas.price_history(pl, "S")], [79.0, 85.0])
        self.assertEqual([x[1] for x in rytas.zone_histories(pl, "S")["22-ta kaina"]], [69.0])

    def test_level_replaced_by_a_dearer_one(self):
        """122: '6-a kaina' 16 EUR ends and the unsold seats move to '15-ta kaina' 24 EUR."""
        r = self.rows([("T1", "6-a kaina", "1334", 16.0, 22),
                       ("T2", "15-ta kaina", "1353", 24.0, 13), ("T2", "6-a kaina", "1334", 16.0, 0)])
        pl = rytas.price_lookup(r)
        self.assertEqual(rytas.headline_history(pl, "S"),
                         [("T1", 16.0, 7.0, "6-a kaina"), ("T2", 24.0, 7.0, "15-ta kaina")])
        self.assertEqual(rytas.price_at(pl, "S", "T2", "1334"), (16.0, 7.0))               # sold seat: its level
        self.assertEqual(rytas.price_at(pl, "S", "T2", "1334", on_sale=True), (24.0, 7.0))  # free seat: what it sells at
        T1, T2 = "2026-11-06T00:23:00+00:00", "2026-11-09T09:17:00+00:00"
        for x in r:
            x["ts_utc"] = {"T1": T1, "T2": T2}[x["ts_utc"]]
        tl = {"x": [{"ts_utc": T1, "section_id": "S", "seat_id": "x", "status": 1, "price_class": "1334"}]}
        sec = rytas.analyse_event(tl, r, set())["sections"][0]
        self.assertEqual([h[1] for h in sec["history"]], [16.0, 24.0])
        self.assertEqual((sec["price"], sec["status"]), (24.0, "open"))
        ev = rytas.analyse_event(tl, r, set())["price_events"]
        self.assertEqual(ev, [{"ts": T2, "section": "Sec", "zone": "6-a kaina → 15-ta kaina", "from": 16.0, "to": 24.0}])
        self.assertEqual(sec["revenue"], 0)

    def test_seats_taken_in_a_section_opened_later_count_as_sold_that_day(self):
        T0, T3 = "2026-11-09T08:00:00+00:00", "2026-11-09T10:09:00+00:00"
        r = [dict(x, section_id=sid, section=sid) for sid, x in (("A", self.rows([(T0, "5-a kaina", "1", 20.0, 22)])[0]),
                                                                 ("B", self.rows([(T3, "23-čia kaina", "2", 8.0, 22)])[0]))]
        seat = lambda sid, i, ts, st, pc: {"ts_utc": ts, "section_id": sid, "seat_id": f"{sid}{i}", "status": st, "price_class": pc}
        tl = {"A1": [seat("A", 1, T0, 0, "1")],                                   # open from the start: before tracking
              "B1": [seat("B", 1, T3, 0, "0")], "B2": [seat("B", 2, T3, 0, "0")],  # taken when B opened
              "B3": [seat("B", 3, T3, 1, "2")]}
        res = rytas.analyse_event(tl, r, set())
        a, b = sorted(res["sections"], key=lambda x: x["section_id"])
        self.assertEqual((a["pre_taken"], a["sold_seen"]), (1, 0))
        self.assertEqual((b["pre_taken"], b["sold_seen"], b["singles"], b["available"]), (0, 2, 2, 1))
        self.assertEqual(res["sale_times"], [T3, T3])
        self.assertEqual(b["gross"], 16.0)                                        # 2 x 8 EUR

    def test_season_section_put_on_sale_keeps_its_taken_seats_as_season(self):
        """107 (sold out as season tickets) opens for Nanterre with 1 seat free."""
        T0, T3, T4 = "2026-11-09T08:00:00+00:00", "2026-11-09T15:00:00+00:00", "2026-11-09T16:00:00+00:00"
        sid = next(iter(rytas.SEASON_SECTIONS))
        r = [dict(x, section_id=k, section=k) for k, x in (("A", self.rows([(T0, "5-a kaina", "1", 20.0, 22)])[0]),
                                                           (sid, self.rows([(T3, "17-ta kaina", "2", 30.0, 2)])[0]))]
        seat = lambda k, i, ts, st: {"ts_utc": ts, "section_id": k, "seat_id": f"{k}-{i}", "status": st, "price_class": "2"}
        tl = {"A-1": [seat("A", 1, T0, 1)],
              f"{sid}-1": [seat(sid, 1, T3, 0)], f"{sid}-2": [seat(sid, 2, T3, 0)],
              f"{sid}-3": [seat(sid, 3, T3, 1), seat(sid, 3, T4, 0)]}            # the free one sells later
        res = rytas.analyse_event(tl, r, set())
        x = next(s for s in res["sections"] if s["section_id"] == sid)
        self.assertEqual((x["season"], x["singles"], x["sold_seen"], x["pre_taken"]), (2, 1, 1, 0))
        self.assertEqual(res["sale_times"], [T4])

    def test_zone_replaced_section_stays_open(self):
        """101 Lounge: zone 'Premium lounge 5' replaced by 'Premium lounge 6' (same price).
        The old zone going to 0 must not make the section 'withdrawn'."""
        T1, T2 = "2026-11-06T08:00:00+00:00", "2026-11-06T09:35:00+00:00"
        r = self.rows([(T1, "Premium lounge 5", "3838", 20.0, 22),
                       (T2, "Premium lounge 6", "4081", 20.0, 22), (T2, "Premium lounge 5", "3838", 20.0, 0)])
        tl = {"x": [{"ts_utc": T1, "section_id": "S", "seat_id": "x", "status": 1, "price_class": "3838"}]}
        sec = rytas.analyse_event(tl, r, set())["sections"][0]
        self.assertEqual(sec["status"], "open")
        self.assertIsNone(sec["closed_at"])
        r.append(dict(r[1], ts_utc="2026-11-06T11:00:00+00:00", available=0))   # now the whole section leaves
        sec = rytas.analyse_event(tl, r, set())["sections"][0]
        self.assertEqual((sec["status"], sec["closed_at"]), ("closed", "2026-11-06T11:00:00+00:00"))

    def test_zone_sold_out_before_ids_were_logged_is_matched_through_other_games(self):
        old = self.rows([("T1", "21-ta kaina", "", 99.0, 3), ("T1", "22-ta kaina", "", 89.0, 14),
                         ("T2", "21-ta kaina", "", 99.0, 0)])
        other = self.rows([("T1", "21-ta kaina", "1370", 79.0, 14)], event="J")
        pl = rytas.price_lookup(old, rytas.zone_names(old + other))
        self.assertEqual(rytas.price_at(pl, "S", "T1", "1370"), (99.0, 7.0))  # this game's 21-ta kaina
        self.assertEqual(rytas.price_at(rytas.price_lookup(old), "S", "T1", "1370"), (89.0, 7.0))  # without: cheapest

    def test_hourly_seat_scans_tolerate_early_runs(self):
        now = rytas.parse_ts("2026-11-11T00:42:00+00:00")
        near = {"start_utc": "2026-11-11T17:30:00+00:00", "last_seats_utc": "2026-11-10T23:49:00+00:00"}   # 53 min ago
        self.assertTrue(rytas.seats_due(near, now))
        near["last_seats_utc"] = "2026-11-11T00:20:00+00:00"                                             # 22 min ago
        self.assertFalse(rytas.seats_due(near, now))
        far = {"start_utc": "2026-11-20T17:30:00+00:00", "last_seats_utc": "2026-11-10T20:00:00+00:00"}   # 6-hourly, 4.7 h ago
        self.assertFalse(rytas.seats_due(far, now))

    def test_game_day_scan_times(self):
        # game 2026-11-11 19:00 Vilnius (17:00 UTC): local midnight = 22:00 UTC the day before
        meta = {"start_utc": "2026-11-11T17:00:00+00:00"}
        rytas.note_scan(meta, "2026-11-10T14:45:00+00:00")       # 16:45 the day before: too early
        self.assertNotIn("day_scans", meta)
        for ts in ("2026-11-10T16:45:00+00:00", "2026-11-10T21:44:00+00:00", "2026-11-10T22:50:00+00:00"):
            rytas.note_scan(meta, ts)
        self.assertEqual(meta["day_scans"].split(), ["2026-11-10T16:45:00+00:00", "2026-11-10T21:44:00+00:00",
                                                     "2026-11-10T22:50:00+00:00"])
        sales = ["2026-11-10T21:30:00+00:00", "2026-11-10T22:10:00+00:00", "2026-11-10T22:44:00+00:00"]
        self.assertEqual(rytas.day_reads(meta, 5, sales),
                         [["2026-11-10T16:45:00+00:00", 5], ["2026-11-10T21:44:00+00:00", 6],
                          ["2026-11-10T22:50:00+00:00", 8]])
        self.assertEqual(rytas.day_reads({"start_utc": meta["start_utc"]}, 5, sales), [])

    def test_sales_curve_and_price_events(self):
        curve = rytas.sales_curve("2026-11-05T22:23:12+00:00", 10,
                                  ["2026-11-06T08:05:00+00:00", "2026-11-06T08:40:00+00:00", "2026-11-06T11:00:00+00:00"])
        self.assertEqual(curve, [["2026-11-05T22:00:00Z", 10], ["2026-11-06T08:00:00Z", 12], ["2026-11-06T11:00:00Z", 13]])
        self.assertEqual(rytas.sales_curve(None, 0, []), [])
        r = self.rows([("2026-11-06T08:00:00+00:00", "21-ta kaina", "1370", 79.0, 14), ("2026-11-06T08:00:00+00:00", "22-ta kaina", "1371", 69.0, 14),
                       ("2026-11-06T10:00:00+00:00", "22-ta kaina", "1371", 69.0, 9),      # sales only
                       ("2026-11-06T12:00:00+00:00", "22-ta kaina", "1371", 59.0, 9)])     # price cut
        tl = {"x": [{"ts_utc": "2026-11-06T08:00:00+00:00", "section_id": "S", "seat_id": "x", "status": 1, "price_class": "1371"},
                    {"ts_utc": "2026-11-06T13:00:00+00:00", "section_id": "S", "seat_id": "x", "status": 0, "price_class": "1371"}]}
        res = rytas.analyse_event(tl, r, set())
        self.assertEqual(res["price_events"], [{"ts": "2026-11-06T12:00:00+00:00", "section": "Sec", "zone": "22-ta kaina",
                                                "from": 69.0, "to": 59.0}])
        self.assertEqual(res["sale_times"], ["2026-11-06T13:00:00+00:00"])

    def test_recent_sales(self):
        log = [{"ts_utc": "2026-11-06T10:00:00+00:00", "section_id": "S", "seat_id": str(i), "status": 1} for i in range(5)]
        log += [{"ts_utc": "2026-11-06T20:00:00+00:00", "section_id": "S", "seat_id": str(i), "status": 0} for i in range(3)]
        self.assertEqual(rytas.recent_sales(log, "2026-11-06T12:00:00+00:00"), 3)
        self.assertEqual(rytas.recent_sales(log, "2026-11-06T21:00:00+00:00"), 0)
        self.assertEqual(rytas.recent_sales(log, "2026-11-06T00:00:00+00:00", "T"), 0)

    def test_hot_games(self):
        now = NOW
        lkl = {"name": "LKL: Vilniaus ''Rytas'' - Jonavos ''Jonava Hipocredit''"}
        self.assertTrue(rytas.is_hot({"name": "LKL: Vilniaus ''Rytas'' - Kauno ''Žalgiris''"}, [], now))
        self.assertTrue(rytas.is_hot({"name": "BCL: Vilniaus ''Rytas'' - Nantero ''Nanterre 92''"}, [], now))
        self.assertFalse(rytas.is_hot(lkl, [], now))
        t0, t1 = (now - rytas.timedelta(hours=30)).isoformat(), (now - rytas.timedelta(hours=2)).isoformat()
        log = [{"ts_utc": t0, "section_id": "S", "seat_id": str(i), "status": 1} for i in range(60)]
        self.assertFalse(rytas.is_hot(lkl, log + [dict(r, ts_utc=t1, status=0) for r in log[:49]], now))
        self.assertTrue(rytas.is_hot(lkl, log + [dict(r, ts_utc=t1, status=0) for r in log[:50]], now))
        far = {"start_utc": (now + rytas.timedelta(days=60)).isoformat(),
               "last_seats_utc": (now - rytas.timedelta(hours=1)).isoformat()}
        self.assertFalse(rytas.seats_due(far, now))
        self.assertTrue(rytas.seats_due(far, now, hot=True))                 # hot: hourly, even far away

    def test_season_needs_three_games_once_three_have_data(self):
        def tl(*seats):
            return {s: [{"ts_utc": "T1", "section_id": "S", "seat_id": s, "status": st}] for s, st in seats}
        tls = {"a": tl(("x", 0), ("y", 0)), "b": tl(("x", 0), ("y", 0)), "c": tl(("x", 0))}
        self.assertEqual(rytas.season_seats(("a", "b", "c"), tls), {"x"})     # y seen in 2 of 3 only
        self.assertEqual(rytas.season_seats(("a", "b"), tls), {"x", "y"})     # 2 games with data: 2 suffice
        tls["c"] = tl(("x", 1))
        self.assertEqual(rytas.season_seats(("a", "b", "c"), tls), set())     # free in one game: not season


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

    def test_season_seats_count_in_a_game_where_their_section_is_not_on_sale(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        G3 = "JSAIJZ5XW2"
        self.site.close_section(G3, "72935")                  # never on sale online for game 3
        with unittest.mock.patch.object(rytas, "SEASON_MIN_GAMES", 2):
            self.assertEqual(self.snapshot_all(), 0)
            res = rytas.analyse_all(rytas.load_events())
        t1, t3 = res["PNJMMUNJYB"]["totals"], res[G3]["totals"]
        self.assertEqual(t3["season_off"], 6)                  # its 6 season seats still count
        self.assertEqual(t3["season"], t1["season"])
        self.assertEqual(t3["taken"] - t3["season"], t1["taken"] - t1["season"])   # singles unchanged
        self.assertEqual(res[G3]["season_off_sections"], [("72935", "401", 6)])
        self.assertEqual(t1["season_off"], 0)

    def test_b_tribuna_extended_to_109_112_when_not_on_sale(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        G3 = "JSAIJZ5XW2"
        self.site.close_section(G3, "83548")                  # stands in for 112: not on sale in game 3
        with unittest.mock.patch.object(rytas, "FAN_EXTRA", ("Parteris",)), \
                unittest.mock.patch.object(rytas, "SEASON_MIN_GAMES", 2):
            self.assertEqual(self.snapshot_all(), 0)
            res = rytas.analyse_all(rytas.load_events())
        f1 = {sid for sid, _n, _c in res["PNJMMUNJYB"]["fan_sections"]}
        f3 = {sid: n for sid, _n, n in res[G3]["fan_sections"]}
        self.assertEqual(f1, {"79993", "72934"})               # on sale in game 1: a normal section there
        self.assertEqual(f3, {"79993": 8, "72934": 10, "83548": 28})   # full capacity in game 3
        t3 = res[G3]["totals"]
        self.assertEqual(t3["fans_est"], 46)
        self.assertEqual(t3["season_off"], 0)                  # its season seats are inside B Tribūna, not counted twice
        self.assertEqual(t3["expected"], t3["taken"] + 46)

    def test_season_seat_given_back_later_stays_season(self):
        def tl(*seats):
            return {seat: [{"ts_utc": f"T{i}", "section_id": "S", "seat_id": seat, "status": st}
                           for i, st in enumerate(sts)] for seat, sts in seats}
        tls = {"a": tl(("x", [0]), ("y", [0])), "b": tl(("x", [0]), ("y", [0])),
               "c": tl(("x", [0, 1, 0]), ("y", [1, 0]))}    # x: given back, resold; y: free when first seen
        season = rytas.season_seats(("a", "b", "c"), tls)
        self.assertEqual(season, {"x"})                       # the set does not shrink
        cats = {seat: v[1] for seat, v in rytas.classify_seats(tls["c"], {}, season).items()}
        self.assertEqual(cats["x"], rytas.SOLD_SEEN)            # in game c: a resold single ticket
        self.assertEqual(rytas.classify_seats(tls["a"], {}, season)["x"][1], rytas.SEASON)
        tls["c"]["x"].append({"ts_utc": "T3", "section_id": "S", "seat_id": "x", "status": 1})
        self.assertEqual(rytas.classify_seats(tls["c"], {}, season)["x"][1], rytas.AVAILABLE)

    def test_returned_season_seat_is_free_only_in_that_game(self):
        """Holders can return their seat for one game (Nanterre: 114 r7 s5): free / resold there, season elsewhere."""
        def tl(*seats):
            return {seat: [{"ts_utc": f"T{i}", "section_id": "S", "seat_id": seat, "status": st}
                           for i, st in enumerate(sts)] for seat, sts in seats}
        tls = {"a": tl(("x", [0])), "b": tl(("x", [0])), "c": tl(("x", [0, 1]))}
        season = rytas.season_seats(("a", "b", "c"), tls)
        self.assertEqual(season, {"x"})                                          # still counted in the season total
        self.assertEqual(rytas.classify_seats(tls["a"], {}, season)["x"][1], rytas.SEASON)
        self.assertEqual(rytas.classify_seats(tls["c"], {}, season)["x"][1], rytas.AVAILABLE)
        tls["c"]["x"].append({"ts_utc": "T2", "section_id": "S", "seat_id": "x", "status": 0})
        self.assertEqual(rytas.classify_seats(tls["c"], {}, season)["x"][1], rytas.SOLD_SEEN)   # bought by someone else

    def test_membership_section_counts_as_season_and_bcl_top8_has_none(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        self.site.prices = {e: copy.deepcopy(p) for e, p in self.site.prices.items()}
        for e in self.site.prices:                             # 401 never on single sale (like 107)
            self.site.close_section(e, "72935")
        ev = rytas.load_events()
        ev["events"]["PNJMMUNJYB"]["name"] = "BCL TOP 8: Vilniaus ''Rytas'' - Nantero ''Nanterre 92''"
        rytas.save_events(ev)
        with unittest.mock.patch.object(rytas, "SEASON_SECTIONS", {"72935": "401"}), \
                unittest.mock.patch.object(rytas, "SEASON_MIN_GAMES", 2):
            self.assertEqual(self.snapshot_all(), 0)
            res = rytas.analyse_all(rytas.load_events())
        lkl = res["ZPHZTQITUH"]["totals"]
        self.assertEqual(res["ZPHZTQITUH"]["season_sections"], [("72935", "401", 40)])
        self.assertEqual(lkl["season"], 6 + 6 + 40)            # 6 season seats in each sold section + the whole of 401
        sec = {x["section_id"]: x for x in res["ZPHZTQITUH"]["sections"]}["72935"]
        self.assertEqual((sec["seats"], sec["season"], sec["status"]), (40, 40, "closed"))   # not on sale: shown closed
        self.assertEqual(res["ZPHZTQITUH"]["season_total"], 12 + 40)
        top8 = res["PNJMMUNJYB"]["totals"]
        self.assertEqual(top8["season"], 0)                    # Top 8: every taken seat is a single ticket
        self.assertEqual(top8["singles"], top8["taken"])
        self.assertEqual(res["PNJMMUNJYB"]["season_sections"], [])
        self.assertTrue(rytas.season_covered("BCL: Vilniaus ''Rytas'' - Nantero ''Nanterre 92''"))
        self.assertFalse(rytas.season_covered("BCL Top 8: Rytas - X"))
        self.assertTrue(rytas.season_covered("LKL: Rytas - X (ketvirtfinalis)"))   # LKL playoffs are covered

    def test_membership_section_put_on_sale_with_one_seat(self):
        """107 for Nanterre: 1 of its seats on single sale, the rest taken (memberships)."""
        rytas.main(["add-series", "PNJMMUNJYB"])
        self.site.prices = {e: copy.deepcopy(p) for e, p in self.site.prices.items()}
        games = list(self.site.prices)
        one = games[-1]
        for e in games[:-1]:
            self.site.close_section(e, "72935")
        self.site.seats.setdefault(one, {})["72935"] = {f"72935-{i}": (1 if i == 0 else 0) for i in range(40)}
        with unittest.mock.patch.object(rytas, "SEASON_SECTIONS", {"72935": "401"}), \
                unittest.mock.patch.object(rytas, "SEASON_MIN_GAMES", 2):
            self.assertEqual(self.snapshot_all(), 0)
            res = rytas.analyse_all(rytas.load_events(), with_seats=True)
            data_dir = Path(self.tmp) / "site"
            rytas.main(["export", str(data_dir)])
        covered = [e for e in games if res[e]["season_group_size"]]
        self.assertEqual({res[e]["season_total"] for e in covered}, {len(rytas.season_seats(
            rytas.season_groups({x: m["name"] for x, m in rytas.load_events()["events"].items()})[one],
            {x: rytas.timelines(rytas.load_seats(x)) for x in games})) + 40})          # all of 107, in every game
        sec = {x["section_id"]: x for x in res[one]["sections"]}["72935"]
        self.assertEqual((sec["seats"], sec["season"], sec["available"], sec["singles"]), (40, 39, 1, 0))
        cats = [c for (sid, c, *_rest) in res[one]["seats"].values() if sid == "72935"]
        self.assertEqual((cats.count("S"), cats.count("A"), cats.count("B")), (39, 1, 0))   # seat plan agrees with totals
        other = {x["section_id"]: x for x in res[games[0]]["sections"]}["72935"]
        self.assertEqual((other["seats"], other["season"], other["status"]), (40, 40, "closed"))   # the returned seat: season there
        seats = json.loads((data_dir / "seats" / f"{games[0]}.json").read_text(encoding="utf-8"))["72935"]
        self.assertEqual(sorted(q[4] for q in seats).count("S"), 40)

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
        self.assertEqual(len(rytas.read_csv(rytas.LAYOUT)), 84 + 18)   # every seat once (+ fan sectors from the venue plan)
        shapes = rytas.read_csv(rytas.SHAPES)
        self.assertEqual({s["section_id"] for s in shapes}, {"83548", "94921", "72935", "79993", "72934"})
        self.assertEqual([s["points"] for s in shapes if s["section_id"] == "94921"], ["164,342 178,342 178,304 163,304"])
        self.assertEqual(sum(u.endswith(".svg") for u in self.site.requests), 1)
        self.assertEqual(rytas.load_events()["events"]["PNJMMUNJYB"]["conf_id"], "1769507581")

        rytas.main(["attendance", "PNJMMUNJYB", "4100"])
        out = self.tmp / "site"
        self.assertEqual(rytas.main(["export", str(out)]), 0)
        data = json.loads((out / "data.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["games"]), 3)
        evs = rytas.load_events()["events"].values()
        self.assertTrue(all(m.get("last_prices_utc") for m in evs))
        checks = [m[k] for m in evs for k in ("last_prices_utc", "last_seats_utc", "final_check_utc") if m.get(k)]
        self.assertEqual(data["updated"], max(checks))          # time of the last data check, not of the build
        self.assertGreaterEqual(data["built"], data["updated"])
        g = next(x for x in data["games"] if x["id"] == "PNJMMUNJYB")
        self.assertEqual((g["comp"], g["opp"]), ("BCL", "Nantero „Nanterre 92“"))
        self.assertEqual((g["taken"], g["season"], g["singles"], g["seen"], g["before"]), (21, 18, 3, 1, 2))
        self.assertAlmostEqual(g["revenue"], 3 * (89 - 7))
        self.assertAlmostEqual(g["gross"], 3 * 89)
        self.assertEqual(g["attendance"], 4100)
        self.assertEqual(len(data["shapes"]["1769507581"]), 5)
        self.assertEqual(data["seasonSeats"], 18)
        sec = next(s for s in data["sections"]["PNJMMUNJYB"] if s["id"] == "83548")
        self.assertEqual((sec["seats"], sec["seen"], sec["fee"]), (28, 1, 7.0))
        seats = json.loads((out / "seats" / "PNJMMUNJYB.json").read_text(encoding="utf-8"))
        cats = Counter(s[4] for s in seats["83548"])
        self.assertEqual(cats, Counter({"A": 19, "S": 6, "B": 2, "O": 1}))
        sold = next(s for s in seats["83548"] if s[4] == "O")
        self.assertEqual((sold[5], sold[6]), (89.0, 7.0))
        self.assertTrue(sold[7])  # when the sale was seen

    def test_venue_plan_capacity_and_fan_sectors(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        self.assertEqual(self.snapshot_all(), 0)
        self.snapshot_all()
        self.assertEqual(sum(u.split("?")[0].endswith("/store/seat-map/1769507581") for u in self.site.requests), 1)  # once
        cap = rytas.load_capacity("1769507581")
        self.assertEqual(cap, {"83548": 28, "94921": 16, "72935": 40, "79993": 8, "72934": 10, "99999": 5})
        res = rytas.analyse_all(rytas.load_events())
        t = res["PNJMMUNJYB"]["totals"]
        self.assertEqual((t["taken"], t["fans_est"], t["expected"]), (18, 18, 36))   # fans assumed full
        rows, totals = rytas.capacity_table(rytas.load_events(), res)
        self.assertEqual(totals["1769507581"], {"plan": 107, "map": 102, "ever_on_sale": 84})
        fan = next(r for r in rows if r["section_id"] == "72934")
        self.assertEqual((fan["title"], fan["seats"], fan["games_on_sale"], fan["on_map"]), ("111 (B Tribūna)", 10, 0, True))
        p2 = next(r for r in rows if r["section_id"] == "83548")
        self.assertEqual((p2["max_on_sale"], p2["games_on_sale"]), (28, 3))
        out = self.tmp / "site"
        rytas.main(["export", str(out)])
        g = next(x for x in json.loads((out / "data.json").read_text(encoding="utf-8"))["games"] if x["id"] == "PNJMMUNJYB")
        self.assertEqual((g["fans"], g["expected"]), (18, 36))
        self.assertEqual({f["title"] for f in g["fanSections"]}, {"110", "111"})
        seats = json.loads((out / "seats" / "PNJMMUNJYB.json").read_text(encoding="utf-8"))
        self.assertEqual([s[4] for s in seats["72934"]], ["F"] * 10)
        self.assertEqual(rytas.main(["capacity"]), 0)

        # if a fan sector goes on sale, real seat data replaces the estimate
        self.site.prices["PNJMMUNJYB"]["eventSections"].append(
            dict(copy.deepcopy(PRICES["eventSections"][0]), id="72934", title="111"))
        self.site.seats["PNJMMUNJYB"]["72934"] = {f"F2-{i}": (0 if i < 4 else 1) for i in range(10)}
        self.snapshot_all()
        t = rytas.analyse_all(rytas.load_events())["PNJMMUNJYB"]["totals"]
        self.assertEqual((t["fans_est"], t["taken"]), (8, 22))

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

    def test_warnings_on_the_page(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        orig = self.site.get_json

        def flaky(url):
            if "ZPHZTQITUH" in url:
                raise OSError("network down")
            return orig(url)
        with unittest.mock.patch.object(rytas, "get_json", flaky):
            self.snapshot_all()

        def export(env=""):
            with unittest.mock.patch.dict(rytas.os.environ, {"RUN_OUTCOMES": env}), \
                    unittest.mock.patch.object(rytas, "datetime", wraps=rytas.datetime) as dt:
                dt.now.return_value = NOW
                rytas.main(["export", str(self.tmp / "site")])
            return json.loads((self.tmp / "site" / "data.json").read_text(encoding="utf-8"))
        d = export("snap=failure disc=skipped save=failure")
        codes = [(w["code"], w.get("game")) for w in d["warnings"]]
        self.assertIn(("read", "Jonavos „Jonava Hipocredit“"), codes)
        self.assertIn(("save", None), codes)
        self.assertIn(("nodata", "Jonavos „Jonava Hipocredit“"), codes)   # never read: no seat data
        self.assertEqual(d["arenaCap"], {"1769507581": 28 + 16 + 40 + 8 + 10})   # map sections only
        m = d["membership"]                                   # 6 season seats in each of the sections read
        self.assertEqual(m["presale"], rytas.MEMBERSHIPS_PRESALE)
        self.assertEqual(m["total"], sum(r["season"] for r in m["rows"]))
        self.assertLessEqual(m["low"], m["high"])
        g = next(x for x in d["games"] if x["id"] == "PNJMMUNJYB")
        self.assertTrue(g["sales"] and g["sales"][0][1] == g["before"])   # curve starts with pre-tracking sales
        self.assertIn("sold24", g)
        self.assertIn("priceEvents", g)
        self.assertEqual(d["popularity"], {})                       # no game played yet
        ev = rytas.load_events()
        res = rytas.analyse_all(ev)
        pop = rytas.popularity(res, ev, rytas.parse_ts("2026-11-12T00:00:00+00:00"))   # after the first game only
        sec = next(s for s in res["PNJMMUNJYB"]["sections"] if s["section_id"] == "72935")
        self.assertEqual(pop["72935"], {"title": sec["section"], "fill": round(100 * sec["taken"] / sec["seats"], 1),
                                        "taken": float(sec["taken"]), "seats": sec["seats"], "games": 1})
        # bilietai.lt answers nothing: one warning, not one per game
        with unittest.mock.patch.object(rytas, "get_json", side_effect=OSError("HTTP 403")):
            self.snapshot_all()
        d = export("snap=failure")
        self.assertEqual([w["code"] for w in d["warnings"] if w["code"].startswith("read")], ["readAll"])
        # all fine
        self.snapshot_all()
        self.assertEqual(export("snap=success disc=skipped save=success")["warnings"], [])

    def test_overdue_seat_plans_warning(self):
        rytas.main(["add-series", "PNJMMUNJYB"])
        self.snapshot_all()
        ev = rytas.load_events()
        ev["events"]["JSAIJZ5XW2"]["last_seats_utc"] = (NOW - rytas.timedelta(hours=50)).isoformat()  # game in 16 d: due daily
        rytas.save_events(ev)
        res = rytas.analyse_all(ev)
        w = rytas.health_warnings(ev, res, NOW)
        self.assertEqual([(x["code"], x["game"]) for x in w], [("stale", "Utenos „Juventus“")])
        ev["events"]["JSAIJZ5XW2"]["last_seats_utc"] = (NOW - rytas.timedelta(hours=10)).isoformat()
        self.assertEqual(rytas.health_warnings(ev, res, NOW), [])

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
