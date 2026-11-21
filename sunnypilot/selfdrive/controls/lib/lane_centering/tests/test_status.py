"""Ioniq6 build: lane-centering status line for the phone page."""
import json
from types import SimpleNamespace

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering import runtime as rt, status as st
from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering.tests.test_runtime import BASE, Clock, SM, cs, model


def drive(r, clock, m, ticks, CS=None, latActive=True):
  """controlsd's call order, with the car steering exactly as commanded."""
  sm, CS, CC = SM(), CS or cs(), SimpleNamespace(latActive=latActive)
  for i in range(ticks):
    sm.logMonoTime["modelV2"] = 1 + i // 5
    clock.t += 0.01
    out = r.pre_limit(sm, CS, CC, m, BASE, BASE)
    if r.correcting:
      r.post_limit(m, BASE, out, BASE, CS.vEgo)


@pytest.fixture
def cfg(tmp_path):
  path = tmp_path / "sunnypilot_lane_centering.json"

  def write(obj):
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(path)
  write.path = str(path)
  return write


def snap(status_file):
  return json.loads(status_file.read_text())


class TestWriter:
  def test_summary_counts_correcting_share_and_direction(self):
    w = st.StatusWriter()
    for _ in range(30):
      w.add(lat_active=True, reason="qualified", applied=-0.0002, v_ego=25.0)
    for _ in range(70):
      w.add(lat_active=True, reason="qualified", applied=0.0, v_ego=25.0)
    s = w.summary(5.0, enabled=True, failed=False)
    assert s["correcting_pct"] == 30 and s["curvature"] == pytest.approx(-0.0002) and s["lat_accel"] == pytest.approx(-0.125)

  def test_writes_once_a_second_atomically(self, status_file):
    w = st.StatusWriter()
    w.add(lat_active=True, reason="unqualified_model", applied=0.0, v_ego=20.0)
    w.flush(10.0, enabled=True, failed=False)
    w.add(lat_active=True, reason="qualified", applied=0.001, v_ego=20.0)
    w.flush(10.5, enabled=True, failed=False)                 # too soon: not written
    assert snap(status_file)["reason"] == "unqualified_model"
    w.flush(11.0, enabled=True, failed=False)
    assert snap(status_file)["correcting_pct"] == 100 and not (status_file.parent / "sp_lane_centering.json.tmp").exists()

  def test_unwritable_path_just_stops_status(self, tmp_path):
    w = st.StatusWriter(str(tmp_path / "missing_dir" / "s.json"))
    w.flush(1.0, enabled=True, failed=False)
    assert w.broken
    w.flush(5.0, enabled=True, failed=False)                  # no retry, no raise


class TestDescribe:
  def line(self, now=10.0, **kw):
    s = {"mono": 9.5, "enabled": True, "failed": False, "reason": "qualified", "correcting_pct": 0, "curvature": 0.0, "lat_accel": 0.0}
    s.update(kw)
    return st.describe(s, now, config_enabled=True)

  def test_correcting_left(self):
    d = self.line(correcting_pct=85, curvature=-0.0002, lat_accel=-0.13)
    assert d["state"] == "correcting" and d["side"] == "left" and d["text"] == "correcting ← left, 0.13 m/s², 85% of the last second"

  def test_correcting_right(self):
    assert self.line(correcting_pct=10, curvature=0.0001, lat_accel=0.06)["side"] == "right"

  @pytest.mark.parametrize("reason,state,level", [("qualified", "centred", "on"), ("unqualified_model", "waiting", "warn"),
                                                  ("driver_override", "paused", "warn"), ("inactive", "standby", "mut"),
                                                  ("signal_release", "paused", "warn"), ("error", "error", "bad"),
                                                  ("something_new", "paused", "warn")])
  def test_reasons(self, reason, state, level):
    d = self.line(reason=reason)
    assert (d["state"], d["level"]) == (state, level) and d["text"]

  def test_error_beats_correcting(self):
    assert self.line(failed=True, reason="error", correcting_pct=50)["state"] == "error"

  def test_stale_file_means_not_driving(self):
    assert st.describe({"mono": 1.0, "enabled": True}, 10.0, config_enabled=True)["text"] == "on, starts when you drive"
    assert st.describe(None, 10.0, config_enabled=False)["text"] == "off"

  def test_invalid_config(self):
    assert st.describe(None, 10.0, config_enabled=False, config_invalid=True)["level"] == "bad"


class TestReadStatus:
  @pytest.mark.parametrize("content,expect", [(None, "off"), ("", "off"), ({}, "off"), ({"enabled": False}, "off"),
                                              ({"enabled": True}, "on, starts when you drive"),
                                              ({"enabled": True, "offset_m": 2}, "invalid"), ("{broken", "invalid")])
  def test_config_states_when_not_driving(self, cfg, status_file, content, expect):
    path = cfg(content) if content is not None else cfg.path
    assert expect in st.read_status(10.0, config_path=path)["text"]


class TestRuntimeStatus:
  def test_runtime_reports_correction_direction(self, cfg, status_file):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    drive(r, clock, model(left=-2.1, right=1.5), 400)            # 0.3 m right of centre
    s = snap(status_file)
    assert s["enabled"] and s["correcting_pct"] > 90 and s["curvature"] < 0 and s["lat_accel"] < 0
    d = st.read_status(clock.t, config_path=cfg.path)
    assert d["state"] == "correcting" and d["side"] == "left"

  def test_runtime_reports_waiting_and_off(self, cfg, status_file):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    drive(r, clock, model(prob=0.3), 300)
    assert st.read_status(clock.t, config_path=cfg.path)["state"] == "waiting"
    cfg({"enabled": False})
    drive(r, clock, model(prob=0.3), 300)
    assert st.read_status(clock.t, config_path=cfg.path)["text"] == "off"

  def test_runtime_reports_error(self, cfg, status_file, monkeypatch):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    monkeypatch.setattr(r.controller, "update", lambda *a: 1 / 0)
    drive(r, clock, model(left=-2.1, right=1.5), 300)
    assert st.read_status(clock.t, config_path=cfg.path)["state"] == "error"

  def test_status_failure_never_changes_steering(self, cfg, tmp_path):
    clock = Clock()
    good = rt.LaneCenteringRuntime(cfg({"enabled": True}), Clock())
    bad = rt.LaneCenteringRuntime(cfg.path, clock, status_path=str(tmp_path / "nope" / "s.json"))
    m, sm, CS, CC = model(left=-2.1, right=1.5), SM(), cs(), SimpleNamespace(latActive=True)
    for i in range(300):
      sm.logMonoTime["modelV2"] = 1 + i // 5
      good.clock.t += 0.01
      clock.t += 0.01
      assert good.pre_limit(sm, CS, CC, m, BASE, BASE) == bad.pre_limit(sm, CS, CC, m, BASE, BASE)
    assert bad.status.broken and not bad.failed and bad.correcting
