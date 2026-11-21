"""Ioniq6 build: hosting StarPilot's Ioniq 6 controller (selection, fallback, isolation, direction)."""
import json
import math

import pytest

from opendbc.car.car_helpers import interfaces
from opendbc.car.hyundai.values import CAR
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.car.helpers import convert_to_capnp
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.sunnypilot.selfdrive.car import interfaces as sunnypilot_interfaces
from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque import controller as ctl
from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque.tests.test_parity import build, ioniq6_cp, run


@pytest.fixture
def files(tmp_path):
  cfg, status = tmp_path / "cfg.json", tmp_path / "status.json"

  def write(obj):
    cfg.write_text(obj if isinstance(obj, str) else json.dumps(obj))
  write.cfg, write.status = str(cfg), status
  return write


def parts(car=CAR.HYUNDAI_IONIQ_6, cp=None):
  cp = cp or interfaces[car].get_non_essential_params(car)
  cp_sp = interfaces[car].get_non_essential_params_sp(cp, car)
  ci = interfaces[car](cp, cp_sp)
  sunnypilot_interfaces.setup_interfaces(ci)
  cp_sp = convert_to_capnp(cp_sp)
  return cp.as_reader(), cp_sp.as_reader(), ci, LatControlTorque(cp.as_reader(), cp_sp.as_reader(), ci, DT_CTRL)


def select(files, car=CAR.HYUNDAI_IONIQ_6, cp=None):
  cp, cp_sp, ci, current = parts(car, cp)
  chosen = ctl.select_lateral_control(cp, cp_sp, ci, DT_CTRL, current, config_path=files.cfg, status_path=str(files.status))
  return chosen, current, json.loads(files.status.read_text())


class TestConfig:
  @pytest.mark.parametrize("content,enabled,problem", [
    (None, None, ""), ("", None, ""), ({}, False, ""), ({"enabled": True}, True, ""),
    ({"enabled": True, "lat_accel_factor": 3.2, "friction": 0.1}, True, ""),
    ({"enabled": True, "lat_accel_factor": 9}, None, "out of range"), ({"enabled": "yes"}, None, "out of range"),
    ({"enabled": True, "friction": True}, None, "out of range"), ("{nope", None, "not valid JSON"),
  ])
  def test_read_config(self, files, content, enabled, problem):
    if content is not None:
      files(content)
    cfg, why = ctl.read_config(files.cfg)
    assert (cfg["enabled"] if cfg else None) == enabled and problem in why


class TestSelection:
  def test_off_by_default(self, files):
    chosen, current, status = select(files)
    assert chosen is current and status["controller"] == ctl.STANDARD

  def test_enabled_on_ioniq6(self, files):
    files({"enabled": True})
    chosen, current, status = select(files)
    assert isinstance(chosen, ctl.LatControlIoniq6) and chosen.fallback is current
    assert status["controller"] == ctl.STARPILOT and status["variant"] == "2023-24"
    assert status["lat_accel_factor"] == pytest.approx(3.66) and status["friction"] == pytest.approx(0.09)
    assert chosen.pid._k_p == [[0.0], [0.6]]                           # StarPilot's flat gain for this policy

  def test_custom_factor_gets_multiplier_once(self, files):
    files({"enabled": True, "lat_accel_factor": 3.2, "friction": 0.1})
    chosen, _, _ = select(files)
    assert chosen.torque_params.latAccelFactor == pytest.approx(3.2 * 1.22) and chosen.torque_params.friction == pytest.approx(0.1)

  def test_other_car_is_never_switched(self, files):
    files({"enabled": True})
    chosen, current, status = select(files, CAR.HYUNDAI_IONIQ_5)
    assert chosen is current and "not an Ioniq 6" in status["problem"]

  def test_invalid_config_keeps_sunnypilot(self, files):
    files({"enabled": True, "friction": 5})
    chosen, current, status = select(files)
    assert chosen is current and "out of range" in status["problem"]

  def test_setup_error_keeps_sunnypilot(self, files, monkeypatch):
    files({"enabled": True})
    monkeypatch.setattr(ctl, "Ioniq6TorquePolicy", lambda *a, **k: 1 / 0)
    chosen, current, status = select(files)
    assert chosen is current and status["failed"]


class TestRuntime:
  def test_policy_error_switches_to_fallback_for_the_drive(self, tmp_path, monkeypatch):
    controller, vm = build(ioniq6_cp(), tmp_path=tmp_path)
    run(controller, vm, [(True, 20.0, 0.0, 0.0005, False)] * 50)
    monkeypatch.setattr(controller.policy, "update", lambda *a: 1 / 0)
    calls = []
    real = controller.fallback.update
    monkeypatch.setattr(controller.fallback, "update", lambda *a: calls.append(1) or real(*a))
    rows = run(controller, vm, [(True, 20.0, 0.0, 0.0005, False)] * 10)
    assert controller.failed and len(calls) == 10 and all(math.isfinite(r[0]) for r in rows)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["failed"] and status["error"] == "controller error"

  def test_non_finite_output_switches_to_fallback(self, tmp_path, monkeypatch):
    controller, vm = build(ioniq6_cp(), tmp_path=tmp_path)
    good = controller.policy.update
    monkeypatch.setattr(controller.policy, "update", lambda *a: (float("nan"), 0.0, good(*a)[2]))
    rows = run(controller, vm, [(True, 20.0, 0.0, 0.0005, False)] * 5)
    assert controller.failed and all(math.isfinite(r[0]) for r in rows)

  def test_learned_params_go_to_fallback_only(self, tmp_path):
    controller, _ = build(ioniq6_cp(), tmp_path=tmp_path)
    before = (controller.torque_params.latAccelFactor, controller.torque_params.friction, (controller.pid.pos_limit, controller.pid.neg_limit))
    controller.update_live_torque_params(2.1, 0.05, 0.2)
    assert (controller.torque_params.latAccelFactor, controller.torque_params.friction, (controller.pid.pos_limit, controller.pid.neg_limit)) == before
    assert controller.fallback.torque_params.latAccelFactor == pytest.approx(2.1)

  def test_extension_calls_reach_fallback_and_unknown_ones_are_harmless(self, tmp_path, monkeypatch):
    controller, _ = build(ioniq6_cp(), tmp_path=tmp_path)
    seen = []
    monkeypatch.setattr(controller.fallback.extension, "update_lateral_lag", lambda lag: seen.append(lag))
    controller.extension.update_lateral_lag(0.2)
    controller.extension.does_not_exist(1, 2)
    assert seen == [0.2]

  @pytest.mark.parametrize("curvature,sign", [(0.001, -1), (-0.001, +1)])
  def test_steers_into_the_curve(self, tmp_path, curvature, sign):
    """Positive curvature is a right turn; actuator torque is left-positive, so a right curve needs negative torque."""
    controller, vm = build(ioniq6_cp(), tmp_path=tmp_path)
    rows = run(controller, vm, [(True, 20.0, 0.0, curvature, False)] * 200, roll=0.0)
    assert rows[-1][0] * sign > 0.05

  def test_inactive_outputs_zero(self, tmp_path):
    controller, vm = build(ioniq6_cp(), tmp_path=tmp_path)
    assert all(r[0] == 0.0 for r in run(controller, vm, [(False, 20.0, 0.0, 0.001, False)] * 20))

  def test_output_within_steer_limits_across_a_drive(self, tmp_path):
    controller, vm = build(ioniq6_cp(), tmp_path=tmp_path)
    seq = []
    for speed in (3.0, 8.0, 15.0, 25.0, 33.0):
      for k in (-0.02, -0.004, -0.0005, 0.0, 0.0005, 0.004, 0.02):
        seq += [(True, speed, 0.0, k, False)] * 30 + [(True, speed, 0.0, k, True)] * 5
    rows = run(controller, vm, seq)
    assert not controller.failed and all(math.isfinite(r[0]) and abs(r[0]) <= 1.0 for r in rows)


class TestPhoneStatus:
  ON = {"enabled": True, "lat_accel_factor": 3.0, "friction": 0.09}
  RUNNING = {"controller": ctl.STARPILOT, "variant": "2023-24", "failed": False, "lat_accel_factor": 3.66, "friction": 0.09}

  @pytest.mark.parametrize("status,cfg,problem,level,text", [
    (None, None, "", "mut", "sunnypilot standard"),
    (None, ON, "", "mut", "StarPilot Ioniq 6 (2023–24 tune), from your next drive"),
    (RUNNING, ON, "", "on", "StarPilot Ioniq 6 (2023–24 tune), factor 3.66, friction 0.09"),
    ({"controller": ctl.STANDARD}, ON, "", "warn", "sunnypilot standard this drive; StarPilot Ioniq 6 (2023–24 tune) from the next drive"),
    (RUNNING, None, "", "warn", "StarPilot Ioniq 6 (2023–24 tune) this drive; sunnypilot standard from the next drive"),
    ({**RUNNING, "failed": True}, ON, "", "bad", "StarPilot Ioniq 6 hit an error; sunnypilot took over for that drive"),
    ({"controller": ctl.STANDARD, "problem": "not used: not an Ioniq 6 (X)"}, None, "", "mut", "sunnypilot standard (not used: not an Ioniq 6 (X))"),
    (None, None, "settings file is not valid JSON", "bad", "sunnypilot standard (ioniq6_steering settings: settings file is not valid JSON)"),
  ])
  def test_describe(self, status, cfg, problem, level, text):
    assert ctl.describe(status, cfg, problem) == {"level": level, "text": text}

  def test_reads_what_controlsd_wrote(self, files):
    files({"enabled": True})
    select(files)
    d = ctl.read_status(config_path=files.cfg, status_path=str(files.status))
    assert d["level"] == "on" and d["text"].startswith("StarPilot Ioniq 6 (2023–24 tune)")
