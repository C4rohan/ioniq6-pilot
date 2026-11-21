"""Ioniq6 build: lane centering runtime (config, safe fallbacks, direction, controlsd wiring)."""
import json
from types import SimpleNamespace

import pytest

import cereal.messaging as messaging
from openpilot.selfdrive.controls.lib.drive_helpers import clip_curvature
from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering import runtime as rt
from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering.lane_centering import MAX_CORRECTION

XS = [float(i) for i in range(51)]
BASE = 0.0002
DT = 0.01


def model(left=-1.8, right=1.8, prob=0.9, std=0.1):
  m = messaging.new_message("modelV2").modelV2
  m.init("laneLines", 4)
  for line, y in zip(m.laneLines, (-5.4, left, right, 5.4), strict=True):
    line.x, line.y = XS, [y] * len(XS)
  m.laneLineProbs, m.laneLineStds = [0.0, prob, prob, 0.0], [0.0, std, std, 0.0]
  m.position.x, m.position.y, m.position.yStd = XS, [0.0] * len(XS), [0.1] * len(XS)
  return m


class SM(dict):
  def __init__(self, healthy=True, delay=0.15):
    super().__init__(liveDelay=SimpleNamespace(lateralDelay=delay))
    self.healthy = healthy
    self.logMonoTime = {"modelV2": 1}

  def all_checks(self, names):
    return self.healthy


def cs(**kw):
  return SimpleNamespace(**{"canValid": True, "canTimeout": False, "vEgo": 25.0, "leftBlinker": False, "rightBlinker": False,
                            "steeringPressed": False, **kw})


class Clock:
  def __init__(self):
    self.t = 100.0

  def __call__(self):
    return self.t


@pytest.fixture
def cfg(tmp_path):
  path = tmp_path / "sunnypilot_lane_centering.json"

  def write(obj):
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return str(path)
  write.path = str(path)
  return write


def run(r, clock, m, ticks=300, sm=None, CS=None, latActive=True, base=BASE):
  sm, CS, CC = sm or SM(), CS or cs(), SimpleNamespace(latActive=latActive)
  out = base
  for i in range(ticks):
    sm.logMonoTime["modelV2"] = 1 + i // 5           # new model frame every 5 control ticks (20 Hz)
    clock.t += DT
    out = r.pre_limit(sm, CS, CC, m, base, base)
  return out


class TestSettings:
  def test_missing_file_is_off(self, cfg):
    assert rt.read_settings(cfg.path).enabled is False

  def test_valid_file(self, cfg):
    s = rt.read_settings(cfg({"enabled": True, "offset_m": -0.1, "strength": 2.0}))
    assert s.enabled and s.offset_m == -0.1 and s.strength == 2.0 and s.e2e_authority == 0.5 and s.pause_on_signal

  @pytest.mark.parametrize("bad", [{"enabled": True, "offset_m": 0.5}, {"enabled": True, "strength": 9},
                                   {"enabled": True, "strength": True}, {"enabled": "yes"}, {"enabled": True, "e2e_authority": -1},
                                   {"enabled": True, "offset_m": float("nan")}, "not json", "[1, 2]"])
  def test_bad_file_is_off(self, cfg, bad):
    assert rt.read_settings(cfg(bad)).enabled is False


class TestRuntime:
  def test_disabled_returns_model_curvature(self, cfg):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg.path, clock)
    assert run(r, clock, model(left=-2.1, right=1.5)) == BASE and not r.correcting and r.reason == "disabled"

  @pytest.mark.parametrize("left,right,sign", [(-2.1, 1.5, -1), (-1.5, 2.1, +1)])
  def test_steers_toward_lane_centre(self, cfg, left, right, sign):
    """Car right of centre (centre at -0.3 m) -> steer left (negative curvature), and the reverse."""
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    out = run(r, clock, model(left=left, right=right))
    nudge = out - BASE
    assert r.correcting and r.reason == "qualified"
    assert nudge * sign > 0 and abs(nudge) <= MAX_CORRECTION + 1e-12

  def test_centred_car_gets_no_nudge(self, cfg):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    assert abs(run(r, clock, model()) - BASE) < 1e-9

  def test_offset_moves_target(self, cfg):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True, "offset_m": 0.3}), clock)
    assert run(r, clock, model()) > BASE                    # + offset = sit further right

  @pytest.mark.parametrize("kw,reason", [({"steeringPressed": True}, "driver_override"), ({"canValid": False}, "can_invalid"),
                                         ({"vEgo": 3.0}, "inactive")])
  def test_lets_go(self, cfg, kw, reason):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    m = model(left=-2.1, right=1.5)
    run(r, clock, m)
    assert run(r, clock, m, ticks=1, CS=cs(**kw)) == BASE and r.reason == reason

  def test_not_steering_means_no_nudge(self, cfg):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    assert run(r, clock, model(left=-2.1, right=1.5), latActive=False) == BASE

  def test_turn_signal_fades_out(self, cfg):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    m = model(left=-2.1, right=1.5)
    full = run(r, clock, m) - BASE
    faded = run(r, clock, m, ticks=100, CS=cs(leftBlinker=True)) - BASE
    assert r.reason == "signal_release" and abs(faded) < abs(full) * 0.01

  def test_poor_lane_lines_fade_out(self, cfg):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    full = run(r, clock, model(left=-2.1, right=1.5)) - BASE
    faded = run(r, clock, model(left=-2.1, right=1.5, prob=0.3), ticks=100) - BASE
    assert r.reason == "unqualified_model" and abs(faded) < abs(full) * 0.01      # 0.2 s release

  def test_unhealthy_model_is_ignored(self, cfg):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    assert run(r, clock, model(left=-2.1, right=1.5), sm=SM(healthy=False)) == BASE

  def test_config_switch_takes_effect_within_a_second(self, cfg):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": False}), clock)
    m = model(left=-2.1, right=1.5)
    run(r, clock, m, ticks=10)
    cfg({"enabled": True})
    run(r, clock, m, ticks=50)
    assert not r.settings.enabled                            # not re-read yet
    assert run(r, clock, m, ticks=300) < BASE and r.settings.enabled
    cfg({"enabled": False})
    assert run(r, clock, m, ticks=110) == BASE

  def test_error_turns_it_off_and_returns_model_curvature(self, cfg, monkeypatch):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    m = model(left=-2.1, right=1.5)
    run(r, clock, m)

    def boom(*a, **k):
      raise RuntimeError("bug")
    monkeypatch.setattr(r.controller, "update", boom)
    assert run(r, clock, m, ticks=1) == BASE and r.failed and not r.correcting
    monkeypatch.undo()
    assert run(r, clock, m) == BASE                          # stays off for the rest of the drive
    assert r.post_limit(m, BASE, BASE + 0.001, BASE, 25.0) == BASE + 0.001

  def test_post_limit_error_returns_baseline(self, cfg, monkeypatch):
    clock = Clock()
    r = rt.LaneCenteringRuntime(cfg({"enabled": True}), clock)
    m = model(left=-2.1, right=1.5)
    run(r, clock, m)
    monkeypatch.setattr(r.controller, "guard_applied", lambda *a: 1 / 0)
    assert r.post_limit(m, BASE, BASE - 0.0005, BASE, 25.0) == BASE and r.failed


def test_controlsd_wiring_stays_within_native_limits(cfg):
  """Same sequence as controlsd: pre_limit -> clip_curvature -> post_limit."""
  clock = Clock()
  r = rt.LaneCenteringRuntime(cfg({"enabled": True, "strength": 3.0}), clock)
  m, sm, CS, CC = model(left=-2.4, right=1.2), SM(), cs(), SimpleNamespace(latActive=True)
  desired, measured = 0.0, 0.0
  for i in range(500):
    sm.logMonoTime["modelV2"] = 1 + i // 5
    clock.t += DT
    candidate = r.pre_limit(sm, CS, CC, m, BASE, measured)
    previous = desired
    desired, _ = clip_curvature(CS.vEgo, previous, candidate, 0.0)
    if r.correcting:
      baseline, _ = clip_curvature(CS.vEgo, previous, BASE, 0.0)
      guarded = r.post_limit(m, baseline, desired, measured, CS.vEgo)
      assert min(baseline, desired) - 1e-12 <= guarded <= max(baseline, desired) + 1e-12
      desired = guarded
    assert abs(desired - BASE) <= MAX_CORRECTION + 1e-9
    measured = desired                                       # car follows the command
  assert desired < BASE                                      # car right of centre -> steering left
