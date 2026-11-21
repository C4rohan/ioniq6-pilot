"""
Our port reproduces StarPilot's frozen Ioniq 6 controller traces number for number.

Same setup as StarPilot's test_ioniq6_policy.py::test_actual_frozen_controller_sequences_and_pure_calibration
(source commit in ioniq6_torque.STARPILOT_COMMIT): matched vehicle parameters, roll 0.03, the
original speed-dependent proportional gain table, low-speed turn assist admitted, and the later
standard friction-threshold increase patched to 0 (it postdates the frozen vectors).
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cereal import car
from opendbc.car.car_helpers import interfaces
from opendbc.car.hyundai.values import CAR
from opendbc.car.vehicle_model import VehicleModel
from openpilot.common.realtime import DT_CTRL
from openpilot.selfdrive.car.helpers import convert_to_capnp
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.sunnypilot.selfdrive.car import interfaces as sunnypilot_interfaces
from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque import ioniq6_policy as tune
from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque.controller import LatControlIoniq6
from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque.torque_shaping import INTERP_SPEEDS

TESTDATA = Path(__file__).parent / "testdata"
SEQUENCE = ([(False, 0.3, 10.0, -0.001, False)] * 2 +
            [(True, 0.3, 10.0, -0.001, False)] * 4 +
            [(True, 2.0, 5.0, -0.001, False)] * 4 +
            [(True, 20.0, 2.0, 0.0003, False)] * 4 +
            [(True, 20.0, 2.0, -0.0003, True)] * 2 +
            [(False, 0.0, 8.0, -0.001, False)] * 2 +
            [(True, 3.0, 8.0, -0.001, False)] * 4)
HELPER_SAMPLES = ((0.3, .1, .2, 8., 3., -.2), (12., -.5, -1.3, -6., -10., .2), (27., .02, .8, 1., 0., -.1))


def ioniq6_cp(profile="2023", matched=True):
  cp = interfaces[CAR.HYUNDAI_IONIQ_6].get_non_essential_params(CAR.HYUNDAI_IONIQ_6)
  if matched:
    for name, value in (("mass", 2084.0), ("wheelbase", 2.97), ("centerToFront", 1.188),
                        ("steerRatio", 14.26), ("tireStiffnessFactor", 0.65)):
      setattr(cp, name, value)
    cp.lateralTuning.torque.steeringAngleDeadzoneDeg = 0.0
  if profile == "2025":
    versions = cp.init("carFw", 2)
    versions[0].fwVersion = b"IONIQ6-230915"
    versions[1].fwVersion = b"IONIQ6-240206"
  return cp


def build(cp, cfg=None, tmp_path=None):
  cp_sp = interfaces[CAR.HYUNDAI_IONIQ_6].get_non_essential_params_sp(cp, CAR.HYUNDAI_IONIQ_6)
  ci = interfaces[CAR.HYUNDAI_IONIQ_6](cp, cp_sp)
  sunnypilot_interfaces.setup_interfaces(ci)
  cp_sp = convert_to_capnp(cp_sp)
  fallback = LatControlTorque(cp.as_reader(), cp_sp.as_reader(), ci, DT_CTRL)
  cfg = cfg or {"enabled": True, "lat_accel_factor": 3.0, "friction": 0.09}
  status = str(tmp_path / "status.json") if tmp_path else "/nonexistent/status.json"
  return LatControlIoniq6(cp.as_reader(), cp_sp.as_reader(), ci, DT_CTRL, fallback, cfg, status), VehicleModel(cp)


def run(controller, vm, sequence, roll=0.03, lat_delay=0.1):
  cs = car.CarState.new_message()
  cs.gearShifter = "drive"
  params = SimpleNamespace(angleOffsetDeg=0.0, roll=roll)
  rows = []
  for active, speed, angle, curvature, pressed in sequence:
    cs.vEgo, cs.steeringAngleDeg, cs.steeringPressed = speed, angle, pressed
    output, _, state = controller.update(active, cs, vm, params, False, curvature, None, False, lat_delay)
    rows.append([output, state.desiredLateralAccel, state.desiredLateralJerk, state.p, state.i, state.f])
  return rows


@pytest.mark.parametrize("profile", ["2023", "2025"])
def test_frozen_starpilot_traces(profile, tmp_path, monkeypatch):
  monkeypatch.setattr(tune, "IONIQ_6_STANDARD_FRICTION_THRESHOLD_INCREASE", 0.0)
  frozen = json.loads((TESTDATA / f"ioniq6_frozen_{profile}.json").read_text())
  controller, vm = build(ioniq6_cp(profile), tmp_path=tmp_path)
  # as in StarPilot's frozen run: turn assist admitted, original gain table
  controller.policy.turn_assist = True
  controller.turn_assist_maneuver = True
  controller.pid._k_p = [INTERP_SPEEDS, tune.KP_INTERP]
  assert controller.policy.is_2025 == (profile == "2025")
  assert controller.torque_params.latAccelFactor == pytest.approx(frozen["factor"], abs=1e-6)
  rows = run(controller, vm, SEQUENCE)
  assert not controller.failed
  for i, (row, expected) in enumerate(zip(rows, frozen["rows"], strict=True)):
    assert row == pytest.approx(expected, abs=1e-6), f"row {i}"


def test_frozen_helper_functions():
  frozen = json.loads((TESTDATA / "ioniq6_frozen_2023.json").read_text())
  for (speed, accel, jerk, angle, actual, torque), expected in zip(HELPER_SAMPLES, frozen["helpers"], strict=True):
    got = [tune.get_ioniq_6_ff_scale(accel, jerk, speed), tune.get_ioniq_6_directional_taper_scale(accel, jerk, speed),
           tune.get_ioniq_6_friction_threshold(speed, accel, jerk), tune.get_ioniq_6_center_taper_scale(accel, speed),
           tune.get_ioniq_6_low_speed_angle_assist_torque(angle, actual, torque, speed), tune.get_ioniq_6_2025_center_output_scale(accel, speed)]
    assert got == pytest.approx(expected, abs=1e-6)


def test_firmware_gate():
  assert not tune.is_ioniq_6_2025_model(ioniq6_cp("2023"))
  cp = ioniq6_cp("2023")
  one = cp.init("carFw", 1)
  one[0].fwVersion = b"IONIQ6-230915"
  assert not tune.is_ioniq_6_2025_model(cp)
  assert tune.is_ioniq_6_2025_model(ioniq6_cp("2025"))
