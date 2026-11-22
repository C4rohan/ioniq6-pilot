"""Ioniq6 build: hosting StarPilot's highway and lane-change smoothing in controlsd."""
import json
import math
from types import SimpleNamespace

import numpy as np
import pytest

from cereal import log
from openpilot.sunnypilot.selfdrive.controls.lib.smoothing import runtime as rt
from openpilot.sunnypilot.selfdrive.controls.lib.smoothing.tests.test_highway_correction_gain import _V_HWY, _fit_sine, _weave

LCS = log.LaneChangeState


class Clock:
  def __init__(self):
    self.t = 100.0

  def __call__(self):
    return self.t


class SM(dict):
  def __init__(self, maneuver=False, model_ok=False):
    super().__init__()
    self.valid = {"lateralManeuverPlan": maneuver}
    self.model_ok = model_ok

  def all_checks(self, names):
    return self.model_ok            # False: no model prediction, exercises the measured-deviation path


def cs(**kw):
  return SimpleNamespace(**{"vEgo": _V_HWY, "leftBlinker": False, "rightBlinker": False, "steeringPressed": False,
                            "canValid": True, "canTimeout": False, **kw})


def model(state=LCS.off):
  return SimpleNamespace(meta=SimpleNamespace(laneChangeState=state))


@pytest.fixture
def make(tmp_path):
  cfg, status = tmp_path / "smoothing.json", tmp_path / "status.json"

  def build(settings=None):
    if settings is not None:
      cfg.write_text(json.dumps(settings))
    clock = Clock()
    r = rt.SmoothingRuntime(str(cfg), str(status), clock)
    r.clock_ = clock
    r.cfg_path, r.status_file = str(cfg), status
    return r
  build.cfg = cfg
  return build


def highway(r, curvatures, CS=None, sm=None, state=LCS.off, lat=True):
  CS, CC, m = CS or cs(), SimpleNamespace(latActive=lat), model(state)
  sm = sm if sm is not None else SM()       # an empty SM is falsy
  out = []
  for c in curvatures:
    r.clock_.t += 0.01
    out.append(r.smooth_highway(sm, CS, CC, m, float(c)))
    r.report()
  return np.array(out)


class TestSettings:
  @pytest.mark.parametrize("cfg,ok", [
    ({}, True), ({"highway_smoothing": 5}, True), ({"highway_smoothing": 9, "lane_change_smoothing": True, "lane_change_seconds": 8}, True),
    ({"highway_smoothing": 10}, False), ({"highway_smoothing": 2.5}, False), ({"highway_smoothing": True}, False),
    ({"lane_change_smoothing": "yes"}, False), ({"lane_change_seconds": 2.9}, False), ([], False),
  ])
  def test_validate(self, cfg, ok):
    assert (rt.validate(cfg) is not None) == ok

  def test_strength_maps_to_starpilot_gain(self):
    assert rt.highway_gain(0) == 1.0 and rt.highway_gain(9) == pytest.approx(0.1) and rt.highway_gain(3) == pytest.approx(0.7)

  def test_bad_file_means_off(self, make):
    make.cfg.write_text("{nope")
    cfg, problem = rt.read_config(str(make.cfg))
    assert cfg["highway_smoothing"] == 0 and not cfg["lane_change_smoothing"] and "JSON" in problem


class TestHighway:
  def test_off_is_exact_pass_through(self, make):
    raw = _weave()
    assert np.array_equal(highway(make(), raw), raw)

  def test_on_damps_the_weave(self, make):
    raw = _weave()
    out = highway(make({"highway_smoothing": 5}), raw)
    tail = slice(len(raw) // 2, None)
    assert _fit_sine(out[tail], 0.6)[0] < 0.6 * _fit_sine(raw[tail], 0.6)[0]

  def test_turn_signal_and_hands_on_wheel_pass_through(self, make):
    raw = _weave()
    for CS in (cs(leftBlinker=True), cs(steeringPressed=True)):
      out = highway(make({"highway_smoothing": 9}), raw, CS=CS)
      assert np.allclose(out, raw)

  def test_lane_change_and_maneuver_pass_through(self, make):
    raw = _weave()
    assert np.allclose(highway(make({"highway_smoothing": 9}), raw, state=LCS.laneChangeStarting), raw)
    assert np.allclose(highway(make({"highway_smoothing": 9}), raw, sm=SM(maneuver=True)), raw)

  def test_slow_roads_untouched(self, make):
    raw = _weave(v_ego=12.0)
    assert np.allclose(highway(make({"highway_smoothing": 9}), raw, CS=cs(vEgo=12.0)), raw)

  def test_switching_on_mid_drive_does_not_step(self, make):
    r = make()
    raw = _weave(bias=0.4 / _V_HWY ** 2)
    first = highway(r, raw[:1000])
    make.cfg.write_text(json.dumps({"highway_smoothing": 9}))
    second = highway(r, raw[1000:])
    assert abs(second[0] - first[-1]) < 2 * np.max(np.abs(np.diff(raw)))

  def test_error_passes_through_for_the_rest_of_the_drive(self, make, monkeypatch):
    r = make({"highway_smoothing": 5})
    monkeypatch.setattr(r.highway, "update", lambda *a, **k: 1 / 0)
    raw = _weave(seconds=3)
    assert np.array_equal(highway(r, raw), raw) and r.highway_failed
    assert "error" in rt.read_status(r.clock_.t, str(make.cfg), str(r.status_file))["text"]


def lane_change(r, ticks, state, desired, speed=25.0, previous=0.0):
  sm, CS, CC, m = SM(model_ok=True), cs(vEgo=speed), SimpleNamespace(latActive=True), model(state)
  out = []
  for _ in range(ticks):
    r.clock_.t += 0.01
    r.smooth_highway(sm, CS, CC, m, desired)              # refreshes settings, as in controlsd
    f = r.lane_change_factor(sm, CS, CC, m, previous, desired)
    previous = r.limit(speed, previous, desired, f)
    out.append((f, previous))
    r.report()
  return out


class TestLaneChange:
  def test_off_never_limits(self, make):
    out = lane_change(make(), 50, LCS.laneChangeStarting, 0.002)
    assert all(f == 1.0 for f, _ in out) and out[0][1] == 0.002

  def test_on_eases_a_lane_change(self, make):
    out = lane_change(make({"lane_change_smoothing": True}), 50, LCS.laneChangeStarting, 0.002)
    base = math.pi ** 3 * 3.5 / rt.DEFAULT_LANE_CHANGE_SECONDS ** 3 * 1.3 / 5.0
    assert out[0][0] == pytest.approx(base) and out[0][1] < 0.002
    assert all(b[1] >= a[1] for a, b in zip(out, out[1:], strict=False))          # still moves toward the request

  def test_normal_driving_untouched(self, make):
    out = lane_change(make({"lane_change_smoothing": True}), 50, LCS.off, 0.002)
    assert all(f == 1.0 for f, _ in out)

  def test_setting_change_waits_for_the_lane_change_to_end(self, make):
    r = make({"lane_change_smoothing": True})
    lane_change(r, 5, LCS.laneChangeStarting, 0.002)
    make.cfg.write_text(json.dumps({"lane_change_smoothing": False}))
    r.clock_.t += 2.0
    assert lane_change(r, 5, LCS.laneChangeStarting, 0.003, previous=0.0005)[-1][0] < 1.0
    lane_change(r, 120, LCS.off, 0.0)
    assert not r.cfg["lane_change_smoothing"]

  def test_error_passes_through(self, make, monkeypatch):
    r = make({"lane_change_smoothing": True})
    monkeypatch.setattr(r.lane_change, "factor", lambda **k: 1 / 0)
    out = lane_change(r, 5, LCS.laneChangeStarting, 0.002)
    assert all(f == 1.0 for f, _ in out) and r.lane_change_failed


class TestPhone:
  NOW = 500.0

  @pytest.mark.parametrize("snap,cfg,problem,level,text", [
    (None, {}, "", "mut", "off"),
    (None, {"highway_smoothing": 4}, "", "on", "highway 4/9"),
    ({"mono": NOW, "highway_active_pct": 40}, {"highway_smoothing": 4}, "", "on", "highway 4/9, smoothing now"),
    ({"mono": NOW, "lane_change_min_factor": 0.3}, {"lane_change_smoothing": True}, "", "on", "lane changes on, easing one now"),
    (None, {"highway_smoothing": 2, "lane_change_smoothing": True}, "", "on", "highway 2/9 · lane changes on (5.8 s)"),
    ({"mono": NOW, "highway_failed": True}, {"highway_smoothing": 2}, "", "bad", "highway: error, off for this drive"),
    (None, {}, "settings file is not valid JSON", "bad", "off (smoothing settings: settings file is not valid JSON)"),
  ])
  def test_describe(self, snap, cfg, problem, level, text):
    assert rt.describe(snap, rt.validate(cfg), problem, self.NOW) == {"level": level, "text": text}

  def test_status_written_once_a_second_and_read_back(self, make):
    r = make({"highway_smoothing": 5})
    highway(r, _weave(seconds=20))
    d = rt.read_status(r.clock_.t, str(make.cfg), str(r.status_file))
    assert d == {"level": "on", "text": "highway 5/9, smoothing now"}
