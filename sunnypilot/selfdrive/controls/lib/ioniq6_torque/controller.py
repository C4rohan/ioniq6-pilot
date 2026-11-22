"""
Ioniq6 build: run StarPilot's Ioniq 6 steering controller inside sunnypilot's controlsd.

Off unless /data/sunnypilot_ioniq6_steering.json says {"enabled": true}. The choice is made when
controlsd starts, so a change takes effect on the next drive, never mid-drive (as in StarPilot).

When on, it replaces sunnypilot's torque controller (and NNLC) with StarPilot's Ioniq6TorquePolicy.

Torque scale: StarPilot raises the CAN-FD steering limit to 409 units (its own panda safety); this
build keeps sunnypilot's 270, which the panda enforces and a prebuilt branch can't change. The
policy's output is a fraction of that limit, so StarPilot's tune is rescaled to ask for the same
torque in units: the lateral-accel factor is multiplied by 270/409 (3.0 -> 1.98, x1.22 in the policy
-> 2.42), and friction divided by it (0.09 -> 0.136). The PID and feedforward live in lateral-accel
units and reach torque through the factor; friction is a torque fraction (opendbc get_friction
multiplies it by the factor), hence the opposite scaling. Same torque as StarPilot until it hits 270.

Live learning, as in StarPilot (controlsd -> apply_native_refinement -> update_torque_parameters):
learned factor/offset/friction from torqued are used when they sit within StarPilot's bounds around
the base tune (factor 0.7-1.3x, |offset| <= min(0.5, 0.5 x factor), friction 0.5x-max(0.1, 1.5x)),
with the factor times 1.22; values outside are ignored. torqued learns on this car's own 270 scale,
so they're compared with the rescaled base and used as they are.

sunnypilot's normal controller is kept ready: it keeps receiving the learned values and the
request history, and takes over for the rest of the drive if the StarPilot controller ever raises
an error or returns a non-finite torque.
"""
import json
import math
import os
import time
from types import SimpleNamespace

from opendbc.car.hyundai.values import CarControllerParams
from opendbc.car.lateral import get_friction
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.controls.lib.latcontrol import LatControl
from openpilot.common.pid import PIDController
from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque.ioniq6_policy import (
  IONIQ_6_BASE_LAT_ACCEL_FACTOR_MULT, Ioniq6TorquePolicy, is_ioniq_6_2025_model,
)
from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque.torque_shaping import KI, KP

DATA = os.environ.get("SUNNYPILOT_DATA", "/data")
CONFIG = os.path.join(DATA, "sunnypilot_ioniq6_steering.json")
SHM = os.environ.get("SUNNYPILOT_SHM", "/dev/shm")
STATUS = os.path.join(SHM, "sp_steering_controller.json")

STARPILOT = "starpilot_ioniq6"
STANDARD = "sunnypilot"

# key: (default, lowest, highest). lat_accel_factor and friction are in StarPilot's units (its
# 409-unit torque limit), so the numbers match StarPilot's; both are rescaled to this car's limit
# (see above), and the policy then multiplies the factor by 1.22 exactly once.
LIMITS = {
  "enabled": (False, None, None),
  "live_learning": (True, None, None),
  "lat_accel_factor": (3.0, 2.0, 4.5),
  "friction": (0.09, 0.0, 0.2),
}

STARPILOT_STEER_MAX = 409.0             # StarPilot's CAN-FD torque-steering limit (opendbc hyundai/values.py)
LEARN_FACTOR = (0.7, 1.3)               # StarPilot native_baseline.native_source bounds
LEARN_FRICTION = (0.5, 1.5, 0.1)
STATUS_REFRESH_S = 10.0


def torque_scale(CP) -> float:
  """This car's torque limit over StarPilot's: how much bigger a fraction of it the same torque is."""
  return float(CarControllerParams(CP).STEER_MAX) / STARPILOT_STEER_MAX


def learned_ok(base_factor: float, base_friction: float, factor: float, offset: float, friction: float) -> bool:
  values = (factor, offset, friction)
  return (all(math.isfinite(v) for v in values) and
          LEARN_FACTOR[0] * base_factor <= factor <= LEARN_FACTOR[1] * base_factor and
          abs(offset) <= min(0.5, 0.5 * base_factor) and
          LEARN_FRICTION[0] * base_friction <= friction <= max(LEARN_FRICTION[2], LEARN_FRICTION[1] * base_friction))


def validate(cfg) -> dict | None:
  if not isinstance(cfg, dict):
    return None
  out = {}
  for key, (default, lo, hi) in LIMITS.items():
    v = cfg.get(key, default)
    if lo is None:
      if type(v) is not bool:
        return None
    elif isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
      return None
    out[key] = float(v) if lo is not None else v
  return out


def read_config(path: str | None = None) -> tuple[dict | None, str]:
  """(settings, problem). Missing file -> off; unreadable or out of range -> off with a reason."""
  try:
    with open(path or CONFIG) as f:
      text = f.read()
  except FileNotFoundError:
    return None, ""
  except Exception:
    return None, "unreadable settings file"
  if not text.strip():
    return None, ""
  try:
    cfg = validate(json.loads(text))
  except Exception:
    return None, "settings file is not valid JSON"
  if cfg is None:
    return None, "a setting is missing a valid value or is out of range"
  return cfg, ""


def ineligible_reason(CP) -> str:
  if str(CP.carFingerprint) != "HYUNDAI_IONIQ_6":
    return f"not an Ioniq 6 ({CP.carFingerprint})"
  if CP.lateralTuning.which() != "torque":
    return "car is not on torque steering"
  if str(CP.steerControlType) != "torque":
    return "car is not on torque steering"
  if CP.dashcamOnly or CP.notCar:
    return "dashcam mode"
  return ""


def write_status(snap: dict, path: str | None = None) -> None:
  try:
    tmp = (path or STATUS) + ".tmp"
    with open(tmp, "w") as f:
      f.write(json.dumps({**snap, "mono": time.monotonic()}))
    os.replace(tmp, path or STATUS)
  except Exception:
    cloudlog.exception("ioniq6 steering: can't write status file")


class _ExtensionProxy:
  """controlsd calls LaC.extension.*: keep feeding sunnypilot's controller so it's ready as a fallback."""
  def __init__(self, fallback):
    self._fallback_ext = getattr(fallback, "extension", None)

  def __getattr__(self, name):
    target = getattr(self._fallback_ext, name, None)
    if callable(target):
      def call(*args, **kwargs):
        try:
          return target(*args, **kwargs)
        except Exception:
          cloudlog.exception(f"ioniq6 steering: fallback extension {name} failed")
      return call
    return lambda *a, **k: None


class LatControlIoniq6(LatControl):
  def __init__(self, CP, CP_SP, CI, dt, fallback, cfg: dict, status_path: str | None = None, scale: float | None = None):
    super().__init__(CP, CP_SP, CI, dt)
    self.fallback = fallback
    self.extension = _ExtensionProxy(fallback)
    self.status_path = status_path
    self.failed = False
    self.scale = torque_scale(CP) if scale is None else float(scale)
    self.base_factor = cfg["lat_accel_factor"] * self.scale      # raw, on this car's torque scale
    self.base_friction = cfg["friction"] / self.scale          # a torque fraction: opposite scaling
    self.learning = bool(cfg.get("live_learning", True))
    self.learned = False
    self.status_written = time.monotonic()

    # The parent interface Ioniq6TorquePolicy expects from StarPilot's LatControlTorque.
    self.torque_params = CP.lateralTuning.torque.as_builder()
    self.torque_params.latAccelFactor = self.base_factor
    self.torque_params.friction = self.base_friction
    self.torque_params.latAccelOffset = 0.0
    self.torque_from_lateral_accel = CI.torque_from_lateral_accel()
    self.lateral_accel_from_torque = CI.lateral_accel_from_torque()
    self.pid = PIDController(KP, KI, rate=1 / self.dt)        # replaced by the policy with its own flat-gain PID
    self.steering_angle_deadzone_deg = self.torque_params.steeringAngleDeadzoneDeg

    policy_cp = SimpleNamespace(carFingerprint=CP.carFingerprint, carFw=CP.carFw, minSteerSpeed=CP.minSteerSpeed,
                                lateralTuning=SimpleNamespace(torque=SimpleNamespace(latAccelFactor=self.base_factor)))
    self.policy = Ioniq6TorquePolicy(self, policy_cp)           # applies the x1.22 factor and limits
    self.variant = "2025" if is_ioniq_6_2025_model(CP) else "2023-24"
    self.snapshot = {"controller": STARPILOT, "variant": self.variant, "failed": False,
                     "torque_scale": round(self.scale, 4), "live_learning": self.learning, "learned": False}
    self._refresh_snapshot()
    cloudlog.info(f"ioniq6 steering: StarPilot Ioniq 6 controller active ({self.snapshot})")
    write_status(self.snapshot, status_path)

  def _refresh_snapshot(self):
    self.snapshot.update({"lat_accel_factor": round(float(self.torque_params.latAccelFactor), 4),
                          "friction": round(float(self.torque_params.friction), 4),
                          "offset": round(float(self.torque_params.latAccelOffset), 4), "learned": self.learned})

  # --- the parent methods the policy calls -------------------------------------------------
  def update_limits(self):
    self.pid.set_limits(self.lateral_accel_from_torque(self.steer_max, self.torque_params),
                        self.lateral_accel_from_torque(-self.steer_max, self.torque_params))

  def friction(self, error, deadzone, threshold, CS, setpoint):
    # StarPilot's LatControlTorque.friction for the Ioniq 6 (its HKG CAN-FD highway shaping excludes the Ioniq 6)
    return get_friction(error, deadzone, threshold, self.torque_params)

  # --- what controlsd calls ----------------------------------------------------------------
  def update_live_torque_params(self, latAccelFactor, latAccelOffset, friction):
    try:
      self.fallback.update_live_torque_params(latAccelFactor, latAccelOffset, friction)
    except Exception:
      cloudlog.exception("ioniq6 steering: fallback update_live_torque_params failed")
    if not self.learning or self.failed:
      return
    try:
      factor, offset, fric = float(latAccelFactor), float(latAccelOffset), float(friction)
      if not learned_ok(self.base_factor, self.base_friction, factor, offset, fric):
        return                                 # StarPilot ignores learned values outside its bounds
      self.torque_params.latAccelFactor = factor * IONIQ_6_BASE_LAT_ACCEL_FACTOR_MULT
      self.torque_params.latAccelOffset = offset
      self.torque_params.friction = fric
      self.update_limits()
      self.learned = True
      now = time.monotonic()
      if now - self.status_written >= STATUS_REFRESH_S:
        self.status_written = now
        self._refresh_snapshot()
        write_status(self.snapshot, self.status_path)
    except Exception:
      cloudlog.exception("ioniq6 steering: applying learned torque values failed")

  def reset(self):
    super().reset()
    self.fallback.reset()

  def _feed_fallback(self, CS, desired_curvature) -> None:
    """Keep the fallback's request history current, so a mid-curve takeover doesn't start from zeros."""
    buf = getattr(self.fallback, "lat_accel_request_buffer", None)
    if buf is not None:
      try:
        buf.append(float(desired_curvature) * float(CS.vEgo) ** 2)
      except Exception:
        pass

  def _fail(self, why: str) -> None:
    cloudlog.error(f"ioniq6 steering: {why}; using sunnypilot's controller for the rest of this drive")
    self.failed = True
    self.snapshot = {**self.snapshot, "failed": True, "error": why}
    write_status(self.snapshot, self.status_path)

  def update(self, active, CS, VM, params, steer_limited_by_safety, desired_curvature, calibrated_pose, curvature_limited, lat_delay):
    if not self.failed:
      try:
        torque, angle, log = self.policy.update(active, CS, VM, params, steer_limited_by_safety, desired_curvature,
                                                curvature_limited, lat_delay)
        self._feed_fallback(CS, desired_curvature)
        if math.isfinite(torque) and abs(torque) <= self.steer_max + 1e-6:
          return torque, angle, log
        self._fail(f"bad torque output {torque!r}")
      except Exception:
        cloudlog.exception("ioniq6 steering: policy raised")
        self._fail("controller error")
    return self.fallback.update(active, CS, VM, params, steer_limited_by_safety, desired_curvature, calibrated_pose,
                                curvature_limited, lat_delay)


def select_lateral_control(CP, CP_SP, CI, dt, current, config_path: str | None = None, status_path: str | None = None):
  """Called once by controlsd after sunnypilot picks its controller. Returns the controller to use."""
  try:
    cfg, problem = read_config(config_path)
    if cfg is None or not cfg["enabled"]:
      write_status({"controller": STANDARD, "failed": False, "problem": problem}, status_path)
      return current
    reason = ineligible_reason(CP)
    if reason:
      cloudlog.warning(f"ioniq6 steering: enabled but not used: {reason}")
      write_status({"controller": STANDARD, "failed": False, "problem": f"not used: {reason}"}, status_path)
      return current
    return LatControlIoniq6(CP, CP_SP, CI, dt, current, cfg, status_path)
  except Exception:
    cloudlog.exception("ioniq6 steering: setup failed, keeping sunnypilot's controller")
    write_status({"controller": STANDARD, "failed": True, "error": "setup failed"}, status_path)
    return current


# --- phone page --------------------------------------------------------------------------
def _label(controller: str | None, variant: str | None = None) -> str:
  if controller == STARPILOT:
    return f"StarPilot Ioniq 6 ({(variant or '2023-24').replace('-', '–')} tune)"
  return "sunnypilot standard"


def describe(status: dict | None, cfg: dict | None, problem: str) -> dict:
  """One line for the Live status card. status = what controlsd chose at the start of the last drive."""
  want = STARPILOT if cfg and cfg.get("enabled") else STANDARD
  if problem:
    return {"level": "bad", "text": f"sunnypilot standard (ioniq6_steering settings: {problem})"}
  if not isinstance(status, dict) or "controller" not in status:
    suffix = ", from your next drive" if want == STARPILOT else ""
    return {"level": "mut", "text": _label(want) + suffix}
  running = status.get("controller")
  if running == STARPILOT and status.get("failed"):
    return {"level": "bad", "text": "StarPilot Ioniq 6 hit an error; sunnypilot took over for that drive"}
  if running != want:
    return {"level": "warn", "text": f"{_label(running, status.get('variant'))} this drive; {_label(want)} from the next drive"}
  if want == STARPILOT:
    tune = f"factor {status.get('lat_accel_factor')}, friction {status.get('friction')}"
    if status.get("learned"):
      tune += ", using live learning"
    elif status.get("live_learning"):
      tune += ", live learning not in range yet"
    return {"level": "on", "text": f"{_label(running, status.get('variant'))}, {tune}"}
  note = status.get("problem")
  return {"level": "mut", "text": _label(STANDARD) + (f" ({note})" if note else "")}


def read_status(config_path: str | None = None, status_path: str | None = None) -> dict:
  cfg, problem = read_config(config_path)
  try:
    with open(status_path or STATUS) as f:
      status = json.load(f)
  except Exception:
    status = None
  return describe(status, cfg, problem)
