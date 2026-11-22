"""Ioniq6 build: curve log (where the car sits in its lane through left and right curves)."""
import json
import math
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from cereal import log
from openpilot.sunnypilot.selfdrive.settings_server import curves as cv
from openpilot.sunnypilot.selfdrive.settings_server.tests.helpers import Msg, fake_sm

W = 1_700_000_000.0
V = 25.0                          # 90 km/h
K = 1.0 / (V * V)                 # curvature for 1 m/s² at V
CTX = {"time": "t", "trip": "A", "controller": "sunnypilot", "model": "M"}
X = [0.0, 2.0, 5.0, 10.0, 20.0, 40.0]


def lanes(offset=0.0, width=3.6, prob=0.9, std=0.1):
  """modelV2 with straight lane lines, car `offset` m right of the lane centre."""
  c = -offset
  return Msg(laneLineProbs=[0.5, prob, prob, 0.5], laneLineStds=[0.2, std, std, 0.2],
             laneLines=[Msg(x=X, y=[c - 1.5 * width] * 6), Msg(x=X, y=[c - width / 2] * 6),
                        Msg(x=X, y=[c + width / 2] * 6), Msg(x=X, y=[c + 1.5 * width] * 6)])


def feed(tr, seconds, lat=0.0, offset=0.0, actual=None, lane=True, active=True, blinker=False, pressed=False,
         v=V, lc=(False, 0.0, 0.0), dt=0.1):
  rows = []
  for _ in range(round(seconds / dt)):
    r = tr.update(dt=dt, v=v, lat_active=active, blinker=blinker, pressed=pressed, desired_curv=lat / (v * v),
                  actual_curv=(lat if actual is None else actual) / (v * v),
                  lane=cv.lane_position(lanes(offset)) if lane else None, lc=lc, ctx=CTX)
    if r:
      rows.append(r)
  return rows


class TestConventions:
  def test_left_steering_is_negative_curvature(self):
    """controlsd: curvature = -VM.calc_curvature(angle); a positive (left) wheel angle must come out negative."""
    from opendbc.car.car_helpers import interfaces
    from opendbc.car.hyundai.values import CAR
    from opendbc.car.vehicle_model import VehicleModel
    vm = VehicleModel(interfaces[CAR.HYUNDAI_IONIQ_6].get_non_essential_params(CAR.HYUNDAI_IONIQ_6))
    assert -vm.calc_curvature(math.radians(10.0), V, 0.0) < 0

  def test_lane_status_path_matches_controlsd(self):
    from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering import status
    assert cv.LANE_STATUS == status.STATUS

  def test_reads_a_real_modelv2_message(self):
    m = log.ModelDataV2.new_message()
    m.laneLineProbs, m.laneLineStds = [0.5, 0.9, 0.9, 0.5], [0.2, 0.1, 0.1, 0.2]
    ll = m.init("laneLines", 4)
    for i, y in enumerate((-5.6, -2.1, 1.5, 5.0)):
      ll[i].x, ll[i].y = X, [y] * len(X)
    off, width = cv.lane_position(m.as_reader())
    assert off == pytest.approx(0.3) and width == pytest.approx(3.6)      # centre is 0.3 m to the left -> car sits right


class TestLanePosition:
  @pytest.mark.parametrize("offset", [0.0, 0.25, -0.4])
  def test_offset(self, offset):
    assert cv.lane_position(lanes(offset))[0] == pytest.approx(offset)

  @pytest.mark.parametrize("kw", [{"prob": 0.3}, {"std": 0.5}, {"width": 2.0}, {"width": 6.0}])
  def test_unclear_lines(self, kw):
    assert cv.lane_position(lanes(**kw)) is None

  def test_garbage(self):
    assert cv.lane_position(Msg(laneLineProbs=[], laneLineStds=[], laneLines=[])) is None
    assert cv.lane_position(None) is None


class TestLaneCenteringState:
  def test_fresh_and_stale(self):
    snap = {"mono": 100.0, "enabled": True, "failed": False, "correcting_pct": 80, "curvature": -0.0004, "lat_accel": 0.2}
    assert cv.lane_centering_state(snap, 101.0) == (True, 80.0, -0.2)
    assert cv.lane_centering_state(snap, 110.0) == (False, 0.0, 0.0)
    assert cv.lane_centering_state(None, 1.0) == (False, 0.0, 0.0)
    assert cv.lane_centering_state({**snap, "failed": True}, 101.0)[0] is False


class TestTracker:
  def test_right_curve(self):
    tr = cv.CurveTracker()
    feed(tr, 5)
    rows = feed(tr, 6, lat=1.2, offset=0.2) + feed(tr, 2)
    assert len(rows) == 1
    r = rows[0]
    assert r["dir"] == "right" and r["peak_lat"] == 1.2 and r["kph"] == 90 and r["seconds"] == pytest.approx(6.0, abs=0.15)
    assert r["off_mid"] == pytest.approx(0.2) and r["lane_pct"] == 100 and r["steer_gap"] == pytest.approx(0.0)
    assert r["takeover"] == 0 and r["controller"] == "sunnypilot"

  def test_left_curve_and_offset_sign(self):
    tr = cv.CurveTracker()
    r = (feed(tr, 6, lat=-1.0, offset=-0.15) + feed(tr, 2))[0]
    assert r["dir"] == "left" and r["off_mean"] == pytest.approx(-0.15)

  def test_entry_middle_exit(self):
    tr = cv.CurveTracker()
    rows = feed(tr, 2, lat=1.0, offset=0.0) + feed(tr, 2, lat=1.0, offset=0.3) + feed(tr, 2, lat=1.0, offset=0.1) + feed(tr, 2)
    r = rows[0]
    assert (r["off_entry"], r["off_mid"], r["off_exit"]) == (pytest.approx(0.0), pytest.approx(0.3), pytest.approx(0.1))

  def test_understeer_is_negative_gap_both_sides(self):
    for lat in (1.0, -1.0):
      tr = cv.CurveTracker()
      r = (feed(tr, 5, lat=lat, actual=0.8 * lat) + feed(tr, 2))[0]
      assert r["steer_gap"] == pytest.approx(-0.2)

  @pytest.mark.parametrize("seconds,lat", [(2.0, 1.2), (6.0, 0.6)])
  def test_too_short_or_too_gentle_is_skipped(self, seconds, lat):
    tr = cv.CurveTracker()
    assert feed(tr, seconds, lat=lat) + feed(tr, 2) == []

  def test_turn_signal_discards(self):
    tr = cv.CurveTracker()
    assert feed(tr, 4, lat=1.0) + feed(tr, 1, lat=1.0, blinker=True) + feed(tr, 2) == []

  def test_short_dip_does_not_split_the_curve(self):
    tr = cv.CurveTracker()
    rows = feed(tr, 3, lat=1.0) + feed(tr, 0.5, lat=0.1) + feed(tr, 3, lat=1.0) + feed(tr, 2)
    assert len(rows) == 1 and rows[0]["seconds"] == pytest.approx(6.5, abs=0.15)

  def test_trailing_straight_is_not_part_of_the_curve(self):
    tr = cv.CurveTracker()
    r = (feed(tr, 4, lat=1.0, offset=0.2) + feed(tr, 3, lat=0.0, offset=-0.5))[0]
    assert r["seconds"] == pytest.approx(4.0, abs=0.15) and r["off_exit"] == pytest.approx(0.2)

  def test_s_bend_gives_two_rows(self):
    tr = cv.CurveTracker()
    rows = feed(tr, 4, lat=1.0) + feed(tr, 4, lat=-1.0) + feed(tr, 2)
    assert [r["dir"] for r in rows] == ["right", "left"]

  def test_disengage_closes_and_keeps_the_curve(self):
    tr = cv.CurveTracker()
    rows = feed(tr, 4, lat=1.0, pressed=False) + feed(tr, 0.5, lat=1.0, pressed=True) + feed(tr, 1, lat=1.0, active=False)
    assert len(rows) == 1 and rows[0]["takeover"] == 1

  def test_slow_speed_ignored(self):
    tr = cv.CurveTracker()
    assert feed(tr, 6, lat=1.0, v=8.0) + feed(tr, 2, v=8.0) == []

  def test_no_lane_data_leaves_offsets_blank(self):
    tr = cv.CurveTracker()
    r = (feed(tr, 5, lat=1.0, lane=False) + feed(tr, 2))[0]
    assert r["off_mid"] == "" and r["lane_pct"] == 0

  def test_lane_centering_recorded(self):
    tr = cv.CurveTracker()
    r = (feed(tr, 5, lat=1.0, lc=(True, 60.0, -0.1)) + feed(tr, 2))[0]
    assert r["lc"] == 1 and r["lc_pct"] == 60 and r["lc_push"] == pytest.approx(-0.1)

  def test_straight_baseline_at_trip_end(self):
    tr = cv.CurveTracker()
    feed(tr, 40, offset=0.12)
    feed(tr, 5, lat=1.0)                                       # still open when the drive ends
    rows = tr.trip_end(CTX)
    assert [r["dir"] for r in rows] == ["right", "straight"]
    assert rows[1]["off_mean"] == pytest.approx(0.12) and rows[1]["seconds"] == 40
    assert tr.trip_end(CTX) == []                              # reset for the next drive


def row(side, off, controller="sunnypilot", lc=0, trip="A", gap=-0.1, takeover=0):
  key = "off_mean" if side == "straight" else "off_mid"
  return {"trip": trip, "dir": side, key: str(off), "off_entry": str(off), "off_exit": str(off), "steer_gap": str(gap),
          "controller": controller, "lc": str(lc), "takeover": str(takeover)}


class TestSummary:
  def test_not_enough(self):
    s = cv.summarize([row("right", 0.2)])
    assert s["verdict"].startswith("Not enough curves")

  def test_by_setup_and_side(self):
    rows = [row("right", 0.2)] * 4 + [row("left", -0.03)] * 3 + [row("straight", 0.05)] + \
           [row("right", 0.05, "starpilot_ioniq6", 1, "B")] * 3
    s = cv.summarize(rows)
    first = s["setups"][0]
    assert first["setup"] == "sunnypilot controller" and first["right"]["sits"] == pytest.approx(0.2)
    assert first["left"]["text"] == "0.03 m left" and first["straight"]["text"] == "0.05 m right"
    assert s["setups"][1]["setup"] == "StarPilot Ioniq 6 controller + lane centering"
    assert "right curves sit 0.20 m right (4); left curves sit 0.03 m left (3); straights 0.05 m right" in s["verdict"]

  def test_only_recent_drives(self):
    rows = [row("right", 0.5, trip=f"T{i}") for i in range(12)]
    assert cv.summarize(rows)["trips"] == 10


class TestTelemetry:
  @pytest.fixture
  def t(self, tm, monkeypatch):
    monkeypatch.setattr(tm, "send_notification", lambda *a: None)
    t = tm.Telemetry()
    t.cfg_read = float("inf")
    return t

  def drive(self, t, wall, seconds, lat=0.0, offset=0.0, started=True, dt=0.1):
    for _ in range(round(seconds / dt)):
      sm = fake_sm(started=started, v=V, dcurv=lat * K)
      sm["modelV2"] = lanes(offset)
      sm["carState"].leftBlinker = sm["carState"].rightBlinker = False
      t.step(sm, dt, wall - W, wall)
      wall += dt
    return wall

  def test_drive_writes_curves_and_baseline(self, tm, t, tmp_path):
    (tmp_path / "sp_steering_controller.json").write_text(json.dumps({"controller": "starpilot_ioniq6", "failed": False}))
    w = self.drive(t, W, 35, offset=0.05)
    (tmp_path / "sp_lane_centering.json").write_text(json.dumps({"mono": w - W, "enabled": True, "correcting_pct": 50,
                                                                 "curvature": 0.0003, "lat_accel": 0.15}))
    w = self.drive(t, w, 6, lat=1.1, offset=0.25)
    w = self.drive(t, w, 2)
    self.drive(t, w, 1, started=False)
    rows = tm.read_csv_all(tm.CURVES_CSV)
    assert [r["dir"] for r in rows] == ["right", "straight"]
    assert float(rows[0]["off_mid"]) == pytest.approx(0.25) and rows[0]["controller"] == "starpilot_ioniq6"
    assert rows[0]["lc"] == "1" and rows[0]["model"] == "TEST" and rows[0]["trip"] == rows[1]["trip"]
    assert "right curves" not in t.curves()["verdict"]       # one curve isn't enough to judge

  def test_a_curve_log_bug_never_breaks_telemetry(self, tm, t, monkeypatch):
    monkeypatch.setattr(cv.CurveTracker, "update", lambda self, **k: 1 / 0)
    w = self.drive(t, W, 3, lat=1.0)
    assert t.curves_broken and t.status["onroad"]
    w = self.drive(t, w, 1, started=False)
    monkeypatch.undo()
    self.drive(t, w, 1)
    assert not t.curves_broken                                 # the next drive logs again

  def test_keeps_last_30_drives(self, tm):
    for i in range(33):
      tm.append_csv(tm.CURVES_CSV, cv.FIELDS, {"trip": f"T{i:02d}", "dir": "right"})
    tm.trim_by_trip(tm.CURVES_CSV, cv.FIELDS, 30)
    assert [r["trip"] for r in tm.read_csv_all(tm.CURVES_CSV)][0] == "T03"


class TestHttp:
  @pytest.fixture
  def server(self, tm, monkeypatch):
    from openpilot.sunnypilot.selfdrive.settings_server import settings_server as ss
    monkeypatch.setattr(ss, "TELEMETRY", None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ss.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()

  def test_summary_and_download(self, server, tm):
    with urllib.request.urlopen(server + "/api/curves.csv") as r:
      assert r.read().decode().strip() == ",".join(cv.FIELDS)
    for _ in range(3):
      tm.append_csv(tm.CURVES_CSV, cv.FIELDS, {**row("right", 0.2), "time": "t"})
    with urllib.request.urlopen(server + "/api/curves") as r:
      assert "right curves sit 0.20 m right (3)" in json.loads(r.read())["verdict"]
    with urllib.request.urlopen(server + "/api/curves.csv") as r:
      assert r.headers["Content-Type"] == "text/csv" and r.read().decode().count("\n") == 4
