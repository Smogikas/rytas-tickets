"""Tests for sheets.py against an in-memory fake of the Google Sheets API."""
import os
import re
import sys
import tempfile
import unittest
import unittest.mock
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rytas  # noqa: E402
import sheets  # noqa: E402


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


class FakeSheets:
    """The endpoints sheets.py uses, on an in-memory grid per tab."""

    def __init__(self):
        self.tabs = {"Sheet1": []}
        self.props = {"Sheet1": {"sheetId": 0, "hidden": False, "gridProperties": {"rowCount": 1000, "columnCount": 26}}}
        self.order = ["Sheet1"]
        self.calls = []
        self.next_id = 1

    @staticmethod
    def parse(path, prefix="/values/", suffix=""):
        r = urllib.parse.unquote(path[len(prefix):len(path) - len(suffix)] if suffix else path[len(prefix):])
        m = re.match(r"^'(.+?)'(?:!([A-Z]+)(\d+)(?::[A-Z]+\d+)?)?$", r)
        assert m, f"bad range {r}"
        return m.group(1), (col_index(m.group(2)) if m.group(2) else 0), (int(m.group(3)) - 1 if m.group(3) else 0)

    def write(self, tab, col, row, values):
        cols = self.props[tab]["gridProperties"]["columnCount"]
        if any(col + len(v) > cols for v in values):     # Google: "exceeds grid limits" (rows grow, columns don't)
            raise RuntimeError(f"HTTP 400: Range exceeds grid limits. Max columns: {cols} ({tab})")
        grid = self.tabs[tab]
        for i, vals in enumerate(values):
            while len(grid) <= row + i:
                grid.append([])
            line = grid[row + i]
            while len(line) < col + len(vals):
                line.append("")
            for j, v in enumerate(vals):
                line[col + j] = v

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, urllib.parse.unquote(path)))
        if method == "GET" and path == "":
            return {"sheets": [{"properties": dict(self.props[t], title=t, index=i)} for i, t in enumerate(self.order)]}
        if path == ":batchUpdate":
            for r in body["requests"]:
                if "addSheet" in r:
                    p = r["addSheet"]["properties"]
                    assert p["title"] not in self.tabs
                    g = p.get("gridProperties", {})
                    # Google: "You can't freeze all visible rows on the sheet" (HTTP 400)
                    assert g.get("frozenRowCount", 0) < g.get("rowCount", 1000), "would freeze all rows"
                    # Google: a sheet can't hide its last visible tab
                    assert not p.get("hidden") or any(not v["hidden"] for v in self.props.values())
                    self.tabs[p["title"]] = []
                    self.props[p["title"]] = {"sheetId": self.next_id, "hidden": p.get("hidden", False),
                                              "gridProperties": dict(g)}
                    self.next_id += 1
                    self.order.insert(p.get("index", len(self.order)), p["title"])
                    return {"replies": [{"addSheet": {"properties": dict(self.props[p["title"]], title=p["title"])}}]}
                elif "appendDimension" in r:
                    d = r["appendDimension"]
                    t = next(t for t, v in self.props.items() if v["sheetId"] == d["sheetId"])
                    key = "columnCount" if d["dimension"] == "COLUMNS" else "rowCount"
                    self.props[t]["gridProperties"][key] += d["length"]
                elif "deleteSheet" in r:
                    t = next(t for t, v in self.props.items() if v["sheetId"] == r["deleteSheet"]["sheetId"])
                    del self.tabs[t], self.props[t]
                    self.order.remove(t)
            return {}
        if path == "/values:batchGet":
            return {"valueRanges": [{"values": [list(x) for x in self.tabs[self.parse("/values/" + n)[0]]]}
                                    for n in params["ranges"]]}
        if path.endswith(":append"):
            tab, _, _ = self.parse(path, suffix=":append")
            last = max((i for i, r in enumerate(self.tabs[tab]) if any(v != "" for v in r)), default=-1)
            self.write(tab, 0, last + 1, body["values"])
            return {}
        if path.endswith(":clear"):
            self.tabs[self.parse(path, suffix=":clear")[0]] = []
            return {}
        if method == "PUT":
            tab, col, row = self.parse(path)
            self.write(tab, col, row, body["values"])
            return {}
        raise AssertionError((method, path))


class SheetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, value in {
            "DATA": self.tmp / "data", "SNAPSHOTS": self.tmp / "data" / "snapshots.csv",
            "SEATS_DIR": self.tmp / "data" / "seats", "ATTENDANCE": self.tmp / "data" / "attendance.csv",
            "LAYOUT": self.tmp / "data" / "layout.csv", "SHAPES": self.tmp / "data" / "shapes.csv",
            "CAPACITY": self.tmp / "data" / "capacity.csv", "EVENTS": self.tmp / "events.json",
        }.items():
            p = unittest.mock.patch.object(rytas, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in {"STATE": self.tmp / "data" / ".sheet_state.json"}.items():
            p = unittest.mock.patch.object(sheets, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.fake = FakeSheets()
        p = unittest.mock.patch.object(sheets, "request", self.fake.request)
        p.start()
        self.addCleanup(p.stop)

    def write_local_data(self):
        rytas.save_json(rytas.EVENTS, {"series": {"S1": "Season"}, "events": {
            "G2": {"name": "LKL: Vilniaus ''Rytas'' - Kauno ''Žalgiris''", "start_utc": "2027-04-18T13:50:00Z",
                   "series_id": "S1", "conf_id": "77"},
            "G1": {"name": "BCL: Vilniaus ''Rytas'' - Nantero ''Nanterre 92''", "start_utc": "2026-11-11T17:30:00Z",
                   "series_id": "S1", "conf_id": "77", "last_seats_utc": "2026-11-05T22:40:00+00:00"}}})
        for eid in ("G1", "G2"):
            rytas.append_csv(rytas.seat_path(eid), rytas.SEAT_FIELDS, [
                {"ts_utc": "2026-11-05T22:40:00+00:00", "section_id": "10", "seat_id": str(i), "row": "7",
                 "place": str(i), "price_class": "5", "status": i % 2} for i in range(12)])
            rytas.append_csv(rytas.SNAPSHOTS, rytas.SNAP_FIELDS, [
                {"ts_utc": "2026-11-05T22:39:00+00:00", "event_id": eid, "section_id": "10", "section": "108",
                 "zone": "6-a kaina", "ticket_type_id": "6175", "price": 16.0, "fee": 6.0, "available": 6}])
        rytas.append_csv(rytas.LAYOUT, rytas.LAYOUT_FIELDS, [
            {"seat_id": "1", "section_id": "10", "row": "7", "place": "1", "x": 510, "y": 122}])

    def wipe_local(self):
        for p in self.tmp.rglob("*"):
            if p.is_file():
                p.unlink()

    def test_layout_tabs_and_round_trip(self):
        self.write_local_data()
        sheets.pull()                                   # empty Sheet: local files kept
        sheets.push()
        f = self.fake
        self.assertEqual(f.order[0], "Rungtynės")       # overview first
        self.assertNotIn("Sheet1", f.tabs)              # empty default tab removed
        self.assertIn("2026-11-11 Nanterre 92", f.tabs)
        self.assertIn("2027-04-18 Žalgiris", f.tabs)
        self.assertLess(f.order.index("2026-11-11 Nanterre 92"), f.order.index("2027-04-18 Žalgiris"))
        for t in ("_events", "_series", "_layout"):
            self.assertTrue(f.props[t]["hidden"])
        game = f.tabs["2026-11-11 Nanterre 92"]
        self.assertEqual(game[0], sheets.GAME_HEAD)
        self.assertEqual(game[1][:9], ["2026-11-05T22:39:00+00:00", "kaina", "10", "108", "6-a kaina", "6175", 16, 6, 6])
        self.assertEqual(len(game), 1 + 1 + 12)
        self.assertEqual(game[2][1], "vieta")
        self.assertEqual(game[2][3], "108")             # section title filled in on seat rows
        ov = f.tabs["Rungtynės"]
        self.assertEqual(ov[0], sheets.OVERVIEW_HEAD)
        self.assertEqual(ov[1][:5], ["G1", "2026-11-11 19:30", "BCL", "Nantero „Nanterre 92“", "Būsima"])
        self.assertEqual(ov[2][1], "2027-04-18 16:50")  # summer time in Vilnius

        # a later run starts with nothing local (nothing in the repo)
        self.wipe_local()
        sheets.pull()
        ev = rytas.load_events()
        self.assertEqual(ev["events"]["G1"]["tab"], "2026-11-11 Nanterre 92")
        self.assertEqual(ev["events"]["G1"]["conf_id"], "77")
        self.assertEqual(len(rytas.load_seats("G1")), 12)
        self.assertEqual(rytas.load_snapshots("G2")[0]["fee"], 6.0)
        self.assertEqual(len(rytas.read_csv(rytas.LAYOUT)), 1)

        # only new rows are appended, to that game's tab
        rytas.append_csv(rytas.seat_path("G1"), rytas.SEAT_FIELDS, [
            {"ts_utc": "2026-11-06T10:00:00+00:00", "section_id": "10", "seat_id": "2", "row": "7", "place": "2",
             "price_class": "5", "status": 0}])
        n = len(f.calls)
        sheets.push()
        appends = [c for c in f.calls[n:] if c[1].endswith(":append")]
        self.assertEqual(appends, [("POST", "/values/'2026-11-11 Nanterre 92'!A1:append")])
        self.assertEqual(len(f.tabs["2026-11-11 Nanterre 92"]), 15)

    def test_typed_attendance_is_kept_and_used(self):
        self.write_local_data()
        sheets.pull()
        sheets.push()
        # the user types attendance next to the Žalgiris row (row 3), as a number
        self.fake.write("Rungtynės", 12, 2, [[9800]])
        self.wipe_local()
        sheets.pull()
        self.assertEqual(rytas.latest_attendance("G2"), 9800)
        # a new game is discovered: appended at the bottom, typed value stays on its game
        ev = rytas.load_events()
        ev["events"]["G0"] = {"name": "LKL: Vilniaus ''Rytas'' - Utenos ''Juventus''", "start_utc": "2026-11-01T17:00:00Z"}
        rytas.save_json(rytas.EVENTS, ev)
        sheets.push()
        ov = self.fake.tabs["Rungtynės"]
        self.assertEqual([r[0] for r in ov[1:]], ["G1", "G2", "G0"])
        self.assertEqual(ov[2][12], 9800)
        self.assertEqual(ov[3][4], "Sužaista")

    def test_attendance_from_cli_goes_to_sheet(self):
        self.write_local_data()
        sheets.pull()
        rytas.main(["attendance", "G1", "4100"])
        sheets.push()
        self.assertEqual(self.fake.tabs["Rungtynės"][1][12], 4100)
        n = len(self.fake.calls)
        sheets.push()                                   # not written again
        self.assertFalse(any("!L" in c[1] and "L1" not in c[1] for c in self.fake.calls[n:]))

    def test_capacity_tab(self):
        self.write_local_data()
        rytas.append_csv(rytas.CAPACITY, rytas.CAP_FIELDS, [
            {"conf_id": "77", "section_id": "10", "seats": 139}, {"conf_id": "77", "section_id": "72934", "seats": 246},
            {"conf_id": "77", "section_id": "99999", "seats": 8}])
        rytas.append_csv(rytas.SHAPES, rytas.SHAPE_FIELDS, [
            {"conf_id": "77", "section_id": "10", "points": "1,1 2,2 3,3"}, {"conf_id": "77", "section_id": "72934", "points": "4,4 5,5 6,6"}])
        sheets.pull()
        sheets.push()
        f = self.fake
        self.assertEqual(f.order[:2], ["Rungtynės", "Talpa"])
        cap = f.tabs["Talpa"]
        self.assertEqual(cap[0], sheets.CAPACITY_HEAD)
        self.assertIn(["10", "108", 139, 12, 2, "taip"], cap)
        self.assertIn(["72934", "111 (B Tribūna)", 246, "", "", "taip"], cap)
        self.assertIn(["", "Visa arena (žemėlapio sektoriai)", 385], cap)
        self.assertIn(["", "Visas salės planas (su ložėmis ir kt.)", 393], cap)
        ov = f.tabs["Rungtynės"]
        self.assertEqual(ov[0][6], "iš jų B Tribūna (110–111, įvert.)")
        self.assertEqual((ov[1][5], ov[1][6]), (6 + 246, 246))       # expected = taken 6 + fan sector 111 (246)
        # the capacity table survives a fresh start (hidden _capacity tab)
        self.wipe_local(); sheets.pull()
        self.assertEqual(rytas.load_capacity("77"), {"10": 139, "72934": 246, "99999": 8})

    def test_overview_tab_is_widened_for_a_new_column(self):
        """Regression: a Rungtynės tab made with 12 columns before the B Tribūna column
        existed must be widened, not fail with 'exceeds grid limits'."""
        self.write_local_data()
        sheets.pull()
        sheets.push()
        f = self.fake
        f.props["Rungtynės"]["gridProperties"]["columnCount"] = 12          # as created by the older version
        f.tabs["Rungtynės"] = [r[:12] for r in f.tabs["Rungtynės"]]
        self.wipe_local()
        sheets.pull()
        sheets.push()
        self.assertEqual(f.props["Rungtynės"]["gridProperties"]["columnCount"], len(sheets.OVERVIEW_HEAD))
        self.assertEqual(f.tabs["Rungtynės"][0], sheets.OVERVIEW_HEAD)

    def test_tab_title(self):
        m = {"name": "LKL: Vilniaus ''Rytas'' - Kėdainių ''Nevėžis-Paskolų klubas''", "start_utc": "2027-01-24T17:10:00Z"}
        self.assertEqual(sheets.game_tab_title(m, set()), "2027-01-24 Nevėžis-Paskolų klubas")
        self.assertEqual(sheets.game_tab_title(m, {"2027-01-24 Nevėžis-Paskolų klubas"}), "2027-01-24 Nevėžis-Paskolų klubas (2)")

    def test_vilnius_time(self):
        self.assertEqual(sheets.vilnius(datetime(2026, 11, 11, 17, 30, tzinfo=timezone.utc)), "2026-11-11 19:30")
        self.assertEqual(sheets.vilnius(datetime(2027, 3, 28, 0, 59, tzinfo=timezone.utc)), "2027-03-28 02:59")
        self.assertEqual(sheets.vilnius(datetime(2027, 3, 28, 1, 0, tzinfo=timezone.utc)), "2027-03-28 04:00")

    def test_large_append_is_chunked(self):
        with unittest.mock.patch.object(sheets, "CHUNK", 5):
            self.write_local_data()
            sheets.pull()
            sheets.push()
        self.assertEqual(sum(1 for c in self.fake.calls if c[1] == "/values/'2026-11-11 Nanterre 92'!A1:append"), 3)
        self.assertEqual(len(self.fake.tabs["2026-11-11 Nanterre 92"]), 14)

    def test_cli_requires_credentials(self):
        import subprocess
        env = {k: v for k, v in os.environ.items() if k not in ("SHEET_ID", "GOOGLE_SA_KEY")}
        r = subprocess.run([sys.executable, str(Path(sheets.__file__)), "pull"], env=env,
                           capture_output=True, text=True, cwd=str(self.tmp))
        self.assertEqual(r.returncode, 2)
        self.assertIn("SHEET_ID", r.stderr)


if __name__ == "__main__":
    unittest.main()
