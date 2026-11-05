"""Tests for sheets.py against an in-memory fake of the Google Sheets API."""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import rytas  # noqa: E402
import sheets  # noqa: E402


class FakeSheets:
    """Just the endpoints sheets.py uses. Values are stored as given (RAW)."""

    def __init__(self):
        self.tabs = {"Sheet1": []}
        self.calls = []

    @staticmethod
    def _tab(rng):
        return urllib.parse.unquote(rng).split("!")[0].strip("'")

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path))
        if method == "GET" and path == "":
            return {"sheets": [{"properties": {"title": t}} for t in self.tabs]}
        if path == ":batchUpdate":
            for r in body["requests"]:
                title = r["addSheet"]["properties"]["title"]
                assert title not in self.tabs, f"tab {title} exists"
                self.tabs[title] = []
            return {}
        if path == "/values:batchGet":
            return {"valueRanges": [{"values": [list(r) for r in self.tabs[self._tab(n)]]} for n in params["ranges"]]}
        if path.endswith(":append"):
            self.tabs[self._tab(path[len("/values/"):-len(":append")])].extend(list(r) for r in body["values"])
            return {}
        if path.endswith(":clear"):
            self.tabs[self._tab(path[len("/values/"):-len(":clear")])] = []
            return {}
        if method == "PUT":
            tab = self._tab(path[len("/values/"):])
            assert path.endswith("!A1")
            self.tabs[tab] = [list(r) for r in body["values"]] + self.tabs[tab][len(body["values"]):]
            return {}
        raise AssertionError((method, path))


class SheetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = {
            "DATA": self.tmp / "data", "SNAPSHOTS": self.tmp / "data" / "snapshots.csv",
            "SEATS_DIR": self.tmp / "data" / "seats", "ATTENDANCE": self.tmp / "data" / "attendance.csv",
            "LAYOUT": self.tmp / "data" / "layout.csv", "SHAPES": self.tmp / "data" / "shapes.csv",
            "EVENTS": self.tmp / "events.json",
        }
        for name, value in self.patches.items():
            p = unittest.mock.patch.object(rytas, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = unittest.mock.patch.object(sheets, "STATE", self.tmp / "data" / ".sheet_state.json")
        p.start()
        self.addCleanup(p.stop)
        self.fake = FakeSheets()
        p = unittest.mock.patch.object(sheets, "request", self.fake.request)
        p.start()
        self.addCleanup(p.stop)

    def write_local_data(self):
        rytas.save_json(rytas.EVENTS, {"series": {"S1": "Season"}, "events": {
            "G1": {"name": "LKL: A - B", "start_utc": "2026-11-15T17:00:00Z", "series_id": "S1", "conf_id": "77",
                   "last_seats_utc": "2026-11-05T22:40:00+00:00"}}})
        rytas.append_csv(rytas.seat_path("G1"), rytas.SEAT_FIELDS, [
            {"ts_utc": "t1", "section_id": "10", "seat_id": str(i), "row": "1", "place": str(i),
             "price_class": "5", "status": i % 2} for i in range(12)])
        rytas.append_csv(rytas.SNAPSHOTS, rytas.SNAP_FIELDS, [
            {"ts_utc": "t1", "event_id": "G1", "section_id": "10", "section": "108", "zone": "z",
             "ticket_type_id": "1", "price": 16.0, "fee": 6.0, "available": 6}])

    def test_first_push_migrates_local_data_then_pull_restores_it(self):
        self.write_local_data()
        sheets.pull()                      # empty sheet: local files untouched
        self.assertEqual(len(rytas.load_seats("G1")), 12)
        sheets.push()
        self.assertEqual(len(self.fake.tabs["seats_G1"]), 13)      # header + 12
        self.assertEqual(self.fake.tabs["prices"][0], rytas.SNAP_FIELDS)
        self.assertEqual(self.fake.tabs["events"][1][:3], ["G1", "LKL: A - B", "2026-11-15T17:00:00Z"])
        self.assertNotIn("layout", self.fake.tabs)                 # nothing to upload, no empty tab

        # a later run starts with no local files at all (nothing in the repo)
        for p in self.tmp.rglob("*"):
            if p.is_file():
                p.unlink()
        sheets.pull()
        ev = rytas.load_events()
        self.assertEqual(ev["series"], {"S1": "Season"})
        self.assertEqual(ev["events"]["G1"]["conf_id"], "77")
        self.assertEqual(len(rytas.load_seats("G1")), 12)
        self.assertEqual(rytas.load_snapshots("G1")[0]["fee"], 6.0)

        # only new rows are appended
        rytas.append_csv(rytas.seat_path("G1"), rytas.SEAT_FIELDS, [
            {"ts_utc": "t2", "section_id": "10", "seat_id": "1", "row": "1", "place": "1", "price_class": "5", "status": 0}])
        n_calls = len(self.fake.calls)
        sheets.push()
        self.assertEqual(len(self.fake.tabs["seats_G1"]), 14)
        self.assertEqual(len(self.fake.tabs["prices"]), 2)
        appends = [c for c in self.fake.calls[n_calls:] if c[1].endswith(":append")]
        self.assertEqual(len(appends), 1)

    def test_attendance_typed_in_the_sheet_is_used(self):
        self.write_local_data()
        sheets.pull()
        sheets.push()
        # the user adds a row by hand in the Sheet: new tab with header + a number cell
        self.fake.tabs["attendance"] = [["event_id", "attendance", "recorded_utc", "note"], ["G1", 4100.0]]
        sheets.pull()
        self.assertEqual(rytas.latest_attendance("G1"), 4100)
        sheets.push()                       # nothing new locally: the hand-typed row is not duplicated
        self.assertEqual(len(self.fake.tabs["attendance"]), 2)

    def test_large_append_is_chunked(self):
        with unittest.mock.patch.object(sheets, "CHUNK", 5):
            self.write_local_data()
            sheets.pull()
            sheets.push()
        self.assertEqual(sum(1 for c in self.fake.calls if c[1] == "/values/seats_G1!A1:append"), 3)
        self.assertEqual(len(self.fake.tabs["seats_G1"]), 13)

    def test_cli_requires_credentials(self):
        import subprocess
        env = {k: v for k, v in os.environ.items() if k not in ("SHEET_ID", "GOOGLE_SA_KEY")}
        r = subprocess.run([sys.executable, str(Path(sheets.__file__)), "pull"], env=env,
                           capture_output=True, text=True, cwd=str(self.tmp))
        self.assertEqual(r.returncode, 2)
        self.assertIn("SHEET_ID", r.stderr)


if __name__ == "__main__":
    unittest.main()
