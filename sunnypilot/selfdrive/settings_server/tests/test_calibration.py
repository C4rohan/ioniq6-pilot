import json
import math

import pytest

from openpilot.sunnypilot.selfdrive.settings_server import calibration as cal
from openpilot.sunnypilot.selfdrive.settings_server.tests.helpers import Msg, fake_sm

W = 1_700_000_000.0
BAKED = {"laf": 3.173, "friction": 0.096}


def msgs(status="calibrated", pct=100, pitch=1.0, yaw=0.5, offset=0.2, laf=3.1, pts=5000, tvalid=True, pvalid=True):
  return (Msg(calStatus=status, calPerc=pct, rpyCalib=[0.0, math.radians(pitch), math.radians(yaw)], rpyCalibSpread=[0.0, 0.0, 0.0]),
          Msg(valid=pvalid, angleOffsetAverageDeg=offset, steerRatio=14.3, stiffnessFactor=1.0),
          Msg(liveValid=tvalid, latAccelFactorFiltered=laf, frictionCoefficientFiltered=0.1, totalBucketPoints=pts))


def snap(**kw):
  return cal.extract(*msgs(**kw))


def levels(rep):
  return [i["level"] for i in rep["issues"]]


class TestPure:
  @pytest.mark.parametrize("v,name", [(0, "uncalibrated"), (1, "calibrated"), (2, "invalid"), (3, "recalibrating"),
                                      ("recalibrating", "recalibrating"), ("bogus", "unknown"), (9, "unknown")])
  def test_status_name(self, v, name):
    assert cal.status_name(v) == name

  def test_extract_converts_to_degrees(self):
    s = snap(pitch=2.5, yaw=-1.25)
    assert s["pitch_deg"] == 2.5 and s["yaw_deg"] == -1.25 and s["laf"] == 3.1 and s["torque_valid"]

  def test_extract_tolerates_missing_fields(self):
    s = cal.extract(Msg(calStatus=1), None, None)
    assert s["status"] == "calibrated" and s["yaw_deg"] == 0.0 and not s["torque_valid"]

  def test_healthy(self):
    rep = cal.evaluate(snap(), BAKED, {"nnlc": False, "sliders": False, "live_learning": True})
    assert rep["level"] == "ok" and rep["issues"] == [] and rep["snap"]["laf_delta_pct"] == -2

  def test_no_data(self):
    assert cal.evaluate(None)["level"] == "wait"

  @pytest.mark.parametrize("status,level", [("invalid", "bad"), ("recalibrating", "warn"), ("uncalibrated", "wait")])
  def test_status_levels(self, status, level):
    rep = cal.evaluate(snap(status=status, pct=40), BAKED)
    assert rep["level"] == level and level in levels(rep)

  @pytest.mark.parametrize("kw,warn", [({"yaw": 3.2}, True), ({"yaw": -3.2}, True), ({"yaw": 2.0}, False),
                                       ({"pitch": 8.0}, True), ({"pitch": -4.5}, True), ({"pitch": -3.0}, False)])
  def test_near_limit(self, kw, warn):
    rep = cal.evaluate(snap(**kw), BAKED)
    assert (rep["level"] == "warn") == warn

  def test_angle_offset_only_when_params_valid(self):
    assert cal.evaluate(snap(offset=-2.0), BAKED)["level"] == "warn"
    assert cal.evaluate(snap(offset=-2.0, pvalid=False), BAKED)["level"] == "ok"

  def test_learned_strength_far_from_baked(self):
    assert cal.evaluate(snap(laf=2.2), BAKED)["level"] == "warn"           # -31%
    assert cal.evaluate(snap(laf=2.6), BAKED)["level"] == "ok"             # -18%

  def test_learning_progress(self):
    rep = cal.evaluate(snap(tvalid=False, pts=1000), BAKED)
    assert rep["level"] == "wait" and rep["torque_progress"] == 25

  def test_sliders_reported_and_suppress_learning_warnings(self):
    mode = {"nnlc": False, "sliders": True, "live_learning": False, "slider_laf": 2.5, "slider_friction": 0.1}
    rep = cal.evaluate(snap(laf=2.0, tvalid=False), BAKED, mode)
    assert levels(rep) == ["info"] and rep["level"] == "ok" and "sliders 2.5/0.1" in rep["mode_label"]

  def test_drift_needs_history_then_warns(self):
    trips = [{"event": "trip", "status": "calibrated", "yaw_deg": "0.5", "pitch_deg": "1.0"}] * 2
    assert cal.drift(snap(yaw=1.5), trips) is None
    trips += trips
    assert cal.drift(snap(yaw=1.5), trips) == {"yaw": 1.0, "pitch": 0.0}
    assert cal.evaluate(snap(yaw=1.5), BAKED, None, trips)["level"] == "warn"
    assert cal.evaluate(snap(yaw=0.7), BAKED, None, trips)["level"] == "ok"

  @pytest.mark.parametrize("toggles,expect", [
    ({}, {"nnlc": False, "sliders": False, "live_learning": True}),
    ({"NeuralNetworkLateralControl": True}, {"nnlc": True, "sliders": False, "live_learning": True}),
    ({"EnforceTorqueControl": True}, {"nnlc": False, "sliders": False, "live_learning": False}),
    ({"EnforceTorqueControl": True, "LiveTorqueParamsToggle": True}, {"nnlc": False, "sliders": False, "live_learning": True}),
    ({"CustomTorqueParams": True, "TorqueParamsOverrideEnabled": True}, {"nnlc": False, "sliders": False, "live_learning": True}),
  ])
  def test_tuning_mode(self, toggles, expect):
    assert cal.tuning_mode(lambda k: toggles.get(k, False), lambda k: 0.0) == expect

  def test_tuning_mode_sliders(self):
    t = {"EnforceTorqueControl": True, "CustomTorqueParams": True, "TorqueParamsOverrideEnabled": True, "LiveTorqueParamsToggle": True}
    m = cal.tuning_mode(lambda k: t.get(k, False), lambda k: {"TorqueParamsOverrideLatAccelFactor": 2.5}.get(k, 0.1))
    assert m == {"nnlc": False, "sliders": True, "live_learning": False, "slider_laf": 2.5, "slider_friction": 0.1}


class TestStarPilotController:
  RUNNING = {"controller": "starpilot_ioniq6", "variant": "2023-24", "failed": False, "lat_accel_factor": 2.416, "friction": 0.136,
             "live_learning": True, "learned": False}
  ON = {"NeuralNetworkLateralControl": True, "EnforceTorqueControl": True, "CustomTorqueParams": True, "TorqueParamsOverrideEnabled": True}

  def mode(self, steering):
    return cal.tuning_mode(lambda k: self.ON.get(k, False), lambda k: 2.5, steering)

  def test_running_controller_replaces_toggles(self):
    m = self.mode(self.RUNNING)
    assert m["controller"] == "starpilot_ioniq6" and not m["nnlc"] and not m["sliders"] and m["live_learning"]
    assert cal.mode_label(m) == "StarPilot Ioniq 6 (2023-24) · base tune 2.416/0.136"
    assert cal.mode_label(self.mode({**self.RUNNING, "learned": True})) == "StarPilot Ioniq 6 (2023-24) · live learning 2.416/0.136"

  def test_report_explains_and_skips_learned_strength_warning(self):
    rep = cal.evaluate(snap(laf=2.0), BAKED, self.mode(self.RUNNING))
    assert levels(rep) == ["info"] and rep["level"] == "ok" and "StarPilot" in rep["issues"][0]["text"]

  def test_standard_or_missing_status_uses_toggles(self):
    for steering in (None, {"controller": "sunnypilot", "failed": False}, {"controller": "sunnypilot", "failed": True}):
      m = self.mode(steering)
      assert "controller" not in m and "controller_failed" not in m and m["sliders"]

  def test_failed_controller_falls_back_to_toggles_and_warns(self):
    m = self.mode({**self.RUNNING, "failed": True})
    assert m["sliders"] and m["controller_failed"]
    assert "warn" in levels(cal.evaluate(snap(), BAKED, m))

  def test_reads_the_file_controlsd_writes(self, tmp_path):
    from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque import controller
    assert cal.STEERING_STATUS == controller.STATUS and cal.STARPILOT == controller.STARPILOT
    f = tmp_path / "s.json"
    controller.write_status(self.RUNNING, str(f))
    assert cal.read_steering_status(str(f))["controller"] == "starpilot_ioniq6"
    f.write_text("{bad")
    assert cal.read_steering_status(str(f)) is None and cal.read_steering_status(str(tmp_path / "none")) is None


def cal_sm(started=True, **kw):
  sm = fake_sm(started=started)
  sm["liveCalibration"], sm["liveParameters"], sm["liveTorqueParameters"] = msgs(**kw)
  return sm


class TestTelemetry:
  @pytest.fixture
  def t(self, tm, monkeypatch):
    sent = []
    monkeypatch.setattr(tm, "send_notification", sent.append)
    t = tm.Telemetry()
    t.cfg_read = float("inf")
    t.sent = sent
    t.cal_sent = lambda: [m for m in sent if m.startswith("📐")]   # trip summaries go to the same notifier
    return t

  def drive(self, t, start, seconds, **kw):
    for i in range(int(seconds * 5)):
      w = start + i * 0.2
      t.step(cal_sm(**kw), 0.2, w - W, w)
    return start + seconds

  def test_reset_mid_drive_alerts_once_and_logs(self, tm, t):
    w = self.drive(t, W, 5)
    w = self.drive(t, w, 5, status="recalibrating")
    w = self.drive(t, w, 2)
    self.drive(t, w, 2, status="recalibrating")
    assert len(t.cal_sent()) == 1 and "reset mid-drive" in t.cal_sent()[0]
    rows = tm.read_csv_all(tm.CAL_CSV)
    assert [r["event"] for r in rows] == ["recalibrating"]

  def test_damaged_history_row_ends_the_drive_once(self, tm, t):
    """A row cut short by a power loss used to make the end of a drive repeat every second."""
    for _ in range(3):
      tm.append_csv(tm.CAL_CSV, cal.CAL_FIELDS, {"event": "trip", "status": "calibrated", "yaw_deg": "0.5", "pitch_deg": "1.0"})
    with open(tm.CAL_CSV, "a") as f:
      f.write("2026-11-01 08:00,trip,calibrated\n")              # truncated: yaw/pitch missing
    w = self.drive(t, W, 60)
    for _ in range(5):
      w = self.drive(t, w, 1, started=False)
    trips = tm.read_csv_all(tm.TRIPS_CSV)
    assert len(trips) == 1 and len([m for m in t.sent if not m.startswith("📐")]) == 1
    assert [r["event"] for r in tm.read_csv_all(tm.CAL_CSV)].count("trip") == 5    # 3 + damaged + this drive

  def test_end_of_drive_steps_are_independent(self, tm, t, monkeypatch):
    monkeypatch.setattr(t, "_calibration_trip_end", lambda wall: 1 / 0)
    monkeypatch.setattr(t, "_curves_trip_end", lambda wall, trip=None: 1 / 0)
    w = self.drive(t, W, 60)
    self.drive(t, w, 3, started=False)
    assert len(tm.read_csv_all(tm.TRIPS_CSV)) == 1 and t.trip is None

  def test_no_alert_when_trip_starts_uncalibrated(self, tm, t):
    self.drive(t, W, 5, status="uncalibrated", pct=20)
    assert t.cal_sent() == [] and tm.read_csv_all(tm.CAL_CSV) == []

  def test_skips_services_never_received(self, tm, t):
    class SM(dict):
      recv_frame = {"liveCalibration": 0}
    sm = SM(cal_sm())
    t.step(sm, 0.2, 0, W)
    assert t.cal_snap is None

  def test_trip_end_row_last_file_and_drift_alert(self, tm, t):
    w = W
    for _ in range(3):
      w = self.drive(t, w, 60, yaw=0.5)
      t.step(cal_sm(started=False), 0.2, w - W, w)
      w += 10
    assert t.cal_sent() == []
    w = self.drive(t, w, 60, yaw=1.6)
    t.step(cal_sm(started=False), 0.2, w - W, w)
    assert len(t.cal_sent()) == 1 and "mount angle moved" in t.cal_sent()[0]
    rows = tm.read_csv_all(tm.CAL_CSV)
    assert [r["event"] for r in rows] == ["trip"] * 4 and rows[-1]["yaw_deg"] == "1.6"
    with open(tm.CAL_LAST) as f:
      last = json.load(f)
    assert last["snap"]["yaw_deg"] == 1.6

  def test_report_from_file_after_restart(self, tm, t):
    w = self.drive(t, W, 60)
    t.step(cal_sm(started=False), 0.2, w - W, w)
    rep = tm.calibration_report(from_file=True)
    assert rep["snap"]["status"] == "calibrated" and len(rep["history"]) == 1
    assert tm.Telemetry().cal_snap == rep["snap"]             # a restarted process shows the last drive
