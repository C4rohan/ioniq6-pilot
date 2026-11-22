"""Ioniq6 build: Galaxy-style drive stats from the trip log."""
import json
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

from openpilot.sunnypilot.selfdrive.settings_server import stats as ds
from openpilot.sunnypilot.selfdrive.settings_server.tests.helpers import Msg, fake_sm

TODAY = date(2026, 11, 21)          # a Saturday; the week runs Mon 16 .. Sun 22


def trip(start, km=20.0, minutes=30.0, engaged=80, model="M", steer="", aol="", distracted="", unresponsive="", dis=0):
  return {"start": start, "km": str(km), "minutes": str(minutes), "engaged_pct": str(engaged), "model": model,
          "steer_pct": str(steer), "aol_min": str(aol), "distracted": str(distracted), "unresponsive": str(unresponsive),
          "disengagements": str(dis), "max_kph": "100"}


def rec(s, rid):
  return next(r for r in s["records"] if r["id"] == rid)


class TestSummary:
  def test_empty(self):
    s = ds.summarize([], [], TODAY)
    assert s["totals"]["drives"] == 0 and s["records"] == [] and s["last_drive"] is None and len(s["week"]["days"]) == 7

  def test_totals_weight_by_time(self):
    s = ds.summarize([trip("2026-11-20 08:00", km=10, minutes=60, engaged=100), trip("2026-11-20 18:00", km=30, minutes=20, engaged=0)],
                     [{}] * 2, TODAY)
    t = s["totals"]
    assert (t["drives"], t["km"], t["hours"], t["engaged_pct"]) == (2, 40.0, 1.3, 75.0)
    assert t["takeovers_per_100km"] == 5.0

  def test_this_week_is_monday_to_sunday(self):
    rows = [trip("2026-11-16 08:00", km=12), trip("2026-11-21 08:00", km=30), trip("2026-11-21 18:00", km=5),
            trip("2026-11-15 08:00", km=99)]                     # Sunday of last week
    w = ds.summarize(rows, [], TODAY)["week"]
    assert [d["label"] for d in w["days"]] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert [d["km"] for d in w["days"]] == [12.0, 0, 0, 0, 0, 35.0, 0] and w["drives"] == 3 and w["km"] == 47.0

  def test_new_fields_skip_old_drives(self):
    rows = [trip("2026-11-20 08:00", minutes=60, steer=90, aol=6), trip("2026-11-19 08:00", minutes=60)]
    t = ds.summarize(rows, [], TODAY)["totals"]
    assert t["steer_pct"] == 90.0 and t["aol_min"] == 6.0           # not averaged with the blank drive

  def test_records(self):
    rows = [
      trip("2026-11-02 08:00", km=50, minutes=40, engaged=60, distracted=0, unresponsive=0),
      trip("2026-11-03 08:00", km=10, minutes=15, engaged=95, distracted=2, unresponsive=0),
      trip("2026-11-04 08:00", km=10, minutes=70, engaged=70, distracted=0, unresponsive=1),
      trip("2026-11-05 08:00", km=10, minutes=20, engaged=70, distracted=0, unresponsive=0),
      trip("2026-11-06 08:00", km=10, minutes=20, engaged=70),                          # logged before alerts existed
      trip("2026-11-07 08:00", km=10, minutes=5, engaged=100, distracted=0, unresponsive=0),   # too short for best day
      trip("2026-11-20 08:00", km=30, minutes=30, engaged=50, distracted=0, unresponsive=0),
    ]
    s = ds.summarize(rows, [], TODAY)
    assert rec(s, "longest_drive")["value"] == 50.0 and rec(s, "longest_drive")["when"] == "2026-11-02"
    assert rec(s, "most_engaged_day")["value"] == 95.0
    assert rec(s, "best_week")["value"] == 100.0 and rec(s, "best_week")["when"] == "week of 2026-11-02"
    assert rec(s, "highest_streak")["value"] == 6
    assert rec(s, "longest_undistracted")["value"] == 70
    assert rec(s, "clean_streak")["value"] == 3                      # 05, 07, 20 (06 has no data, skipped)

  def test_models_and_recent(self):
    rows = [trip(f"2026-11-{d:02d} 08:00", km=10, model="A" if d % 2 else "B") for d in range(10, 17)]
    s = ds.summarize(rows, [], TODAY)
    assert [m["name"] for m in s["models"]] == ["B", "A"] and s["models"][0]["drives"] == 4     # even days 10-16 are B
    assert len(s["recent"]) == 5 and s["recent"][0]["start"] == "2026-11-16 08:00" and s["last_drive"]["km"] == 10.0

  def test_bad_rows_ignored(self):
    assert ds.summarize([{"start": "garbage"}, trip("2026-11-20 08:00", km="x")], [], TODAY)["totals"]["drives"] == 0


class TestTripLogging:
  @pytest.fixture
  def t(self, tm, monkeypatch):
    monkeypatch.setattr(tm, "send_notification", lambda *a: None)
    t = tm.Telemetry()
    t.cfg_read = float("inf")
    return t

  def drive(self, t, wall, seconds, events=(), started=True, lat=True, enabled=True, dt=0.5):
    for _ in range(round(seconds / dt)):
      sm = fake_sm(started=started, v=20.0, lat=lat, enabled=enabled)
      sm["onroadEvents"] = [Msg(name=n) for n in events]
      t.step(sm, dt, wall, wall)
      wall += dt
    return wall

  def test_steering_only_time_and_alerts(self, tm, t):
    w = self.drive(t, 1_700_000_000.0, 60)
    w = self.drive(t, w, 60, enabled=False)                            # steering without cruise
    w = self.drive(t, w, 3, events=["driverDistracted2"])
    w = self.drive(t, w, 3)
    w = self.drive(t, w, 3, events=["driverDistracted2", "driverUnresponsive3"])
    w = self.drive(t, w, 60, lat=False)
    self.drive(t, w, 1, started=False)
    row = tm.read_csv_all(tm.TRIPS_CSV)[-1]
    assert row["distracted"] == "2" and row["unresponsive"] == "1"
    assert float(row["aol_min"]) == 1.0 and row["steer_pct"] == str(round(100 * 129 / 189))    # 129 s steering of 189 s

  def test_old_trip_file_gets_new_columns(self, tm, t):
    old = ["start", "end", "minutes", "km", "max_kph", "engaged_pct", "disengagements", "model"]
    tm.append_csv(tm.TRIPS_CSV, old, {"start": "2026-11-01 08:00", "km": "5", "minutes": "10", "engaged_pct": "50"})
    w = self.drive(t, 1_700_000_000.0, 30)
    self.drive(t, w, 1, started=False)
    rows = tm.read_csv_all(tm.TRIPS_CSV)
    assert len(rows) == 2 and rows[0]["distracted"] == "" and rows[1]["distracted"] == "0"
    assert t.drive_stats()["totals"]["drives"] == 2


def test_http(tm, monkeypatch):
  from openpilot.sunnypilot.selfdrive.settings_server import settings_server as ss
  tm.append_csv(tm.TRIPS_CSV, tm.TRIP_FIELDS, trip("2026-11-20 08:00", km=42))
  monkeypatch.setattr(ss, "TELEMETRY", None)
  httpd = ThreadingHTTPServer(("127.0.0.1", 0), ss.Handler)
  threading.Thread(target=httpd.serve_forever, daemon=True).start()
  try:
    with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/stats") as r:
      d = json.loads(r.read())
    assert d["totals"]["km"] == 42.0 and d["records"][0]["id"] == "longest_drive"
  finally:
    httpd.shutdown()
    httpd.server_close()
