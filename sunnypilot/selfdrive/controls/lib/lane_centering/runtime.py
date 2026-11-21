"""
Ioniq6 build: connects StarPilot's LaneCenteringController to controlsd.

Off unless /data/sunnypilot_lane_centering.json says {"enabled": true}. The file is re-read once
a second, so it can be switched on or off from the phone page during a drive.

The controller only nudges the curvature the model asked for, by at most 0.0012 1/m, and the
result still goes through openpilot's normal curvature limiter (clip_curvature). It lets go
on a turn signal, a lane change, a driver takeover, or when it can't see the lane lines well.
Any error inside this module turns lane centering off for the rest of the drive and returns
the model's curvature unchanged, so it can never stop openpilot from steering.
"""
import json
import math
import os
import time

from openpilot.common.realtime import DT_CTRL
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering.lane_centering import (
  ControlMode, LaneCenteringController, LaneCenteringInput, LaneCenteringResult, LaneCenteringSettings,
  STRENGTH_BASELINE, STRENGTH_MAX,
)
from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering.status import StatusWriter

DATA = os.environ.get("SUNNYPILOT_DATA", "/data")
CONFIG = os.path.join(DATA, "sunnypilot_lane_centering.json")

# key: (default, lowest, highest); booleans have no range
LIMITS = {
  "enabled": (False, None, None),
  "pause_on_signal": (True, None, None),
  "offset_m": (0.0, -0.3, 0.3),               # + moves the car right of centre, - left
  "strength": (STRENGTH_BASELINE, 0.5, STRENGTH_MAX),
  "e2e_authority": (0.5, 0.0, 1.0),           # how much a confident model path overrules centring
}


def validate(cfg) -> LaneCenteringSettings | None:
  """Settings from a parsed config, or None if anything is the wrong type or out of range."""
  if not isinstance(cfg, dict):
    return None
  values = {}
  for key, (default, lo, hi) in LIMITS.items():
    v = cfg.get(key, default)
    if lo is None:
      if type(v) is not bool:
        return None
    else:
      if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        return None
      v = float(v)
    values[key] = v
  return LaneCenteringSettings(enabled=values["enabled"], offset_m=values["offset_m"], e2e_authority=values["e2e_authority"],
                               pause_on_signal=values["pause_on_signal"], strength=values["strength"])


def read_settings(path: str | None = None) -> LaneCenteringSettings:
  """Anything missing, unreadable, or out of range means OFF: a bad file must never steer."""
  try:
    with open(path or CONFIG) as f:
      cfg = json.load(f)
  except FileNotFoundError:
    return LaneCenteringSettings()
  except Exception:
    cloudlog.warning("lane centering: unreadable config, staying off")
    return LaneCenteringSettings()
  return validate(cfg) or LaneCenteringSettings()


def lateral_delay(sm) -> float | None:
  try:
    if not sm.all_checks(['liveDelay']):
      return None
    value = float(sm['liveDelay'].lateralDelay)
  except Exception:
    return None
  return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


class LaneCenteringRuntime:
  REFRESH_S = 1.0
  MAX_TICK_S = 0.05

  def __init__(self, path: str | None = None, clock=time.monotonic, status_path: str | None = None):
    self.path = path
    self.status = StatusWriter(status_path)
    self._tick: dict | None = None    # last tick, folded into the status once post_limit has run
    self.clock = clock
    self.controller = LaneCenteringController()
    self.settings = LaneCenteringSettings()
    self.last_read: float | None = None
    self.last_tick: float | None = None
    self.delay: float | None = None
    self.result: LaneCenteringResult | None = None
    self.reason = "disabled"
    self.failed = False

  @property
  def correcting(self) -> bool:
    return not self.failed and self.result is not None and self.result.correction != 0.0

  def _refresh(self, now: float) -> None:
    if self.last_read is None or now < self.last_read or now - self.last_read >= self.REFRESH_S:
      settings = read_settings(self.path)
      if settings.enabled != self.settings.enabled:
        cloudlog.info(f"lane centering {'enabled' if settings.enabled else 'disabled'}")
      self.settings = settings
      self.last_read = now

  def _fail(self) -> None:
    cloudlog.exception("lane centering failed; off for the rest of this drive")
    self.failed = True
    self.result = None
    self.reason = "error"
    try:
      self.controller.reset()
    except Exception:
      pass

  def _inactive(self, reason: str, base: float) -> float:
    self.controller.reset()
    self.result = None
    self.reason = reason
    return base

  def pre_limit(self, sm, CS, CC, model_v2, base: float, measured: float) -> float:
    """Curvature to send to clip_curvature: the model's, plus the lane-centring nudge."""
    self._report()
    out = base
    if not self.failed:
      try:
        out = self._pre_limit(sm, CS, CC, model_v2, base, measured)
      except Exception:
        self._fail()
        out = base
    try:
      self._tick = {"lat_active": bool(CC.latActive), "v_ego": float(CS.vEgo), "reason": self.reason, "applied": 0.0}
    except Exception:
      self._tick = None
    return out

  def _report(self) -> None:
    """Phone-page status (status.py). Never affects steering; errors only stop the status file."""
    if self.status.broken:
      return
    try:
      if self._tick is not None:
        self.status.add(**self._tick)
      self.status.flush(self.clock(), enabled=self.settings.enabled, failed=self.failed)
    except Exception:
      self.status.broken = True

  def _pre_limit(self, sm, CS, CC, model_v2, base: float, measured: float) -> float:
    now = self.clock()
    previous, self.last_tick = self.last_tick, now
    discontinuity = previous is not None and not 0.0 < now - previous <= self.MAX_TICK_S
    dt = DT_CTRL if previous is None or discontinuity else now - previous
    self._refresh(now)
    self.result = None
    if not self.settings.enabled:
      return self._inactive("disabled", base)
    if not CS.canValid or CS.canTimeout:
      return self._inactive("can_invalid", base)
    if not sm.all_checks(['carState']):
      return self._inactive("carstate_unhealthy", base)
    self.delay = lateral_delay(sm)
    mode = ControlMode.LATERAL_ONLY if CC.latActive else ControlMode.OFF
    result = self.controller.update(LaneCenteringInput(
      model=model_v2,
      model_sample_id=int(sm.logMonoTime['modelV2']),
      model_valid=bool(sm.all_checks(['modelV2'])),
      base_curvature=float(base),
      speed_mps=float(CS.vEgo),
      mode=mode,
      lateral_active=bool(CC.latActive),
      settings=self.settings,
      elapsed_seconds=float(dt),
      turn_signal_active=bool(CS.leftBlinker or CS.rightBlinker),
      driver_override=bool(CS.steeringPressed),
      time_discontinuity=bool(discontinuity),
      measured_curvature=float(measured),
      lateral_delay_s=self.delay,
    ))
    self.result = result
    self.reason = result.reason
    if result.candidate_curvature is None or not math.isfinite(result.candidate_curvature):
      return base
    return result.candidate_curvature

  def post_limit(self, model_v2, baseline: float, limited: float, measured: float, v_ego: float) -> float:
    """After clip_curvature: keep only the part of the nudge that still tracks the lane."""
    if not self.correcting:
      return limited
    try:
      guarded = self.controller.guard_applied(model_v2, baseline, limited, measured, v_ego, self.delay)
      guarded = guarded if math.isfinite(guarded) else baseline
      if self._tick is not None:
        self._tick["applied"] = float(guarded - baseline)
      return guarded
    except Exception:
      self._fail()
      return baseline
