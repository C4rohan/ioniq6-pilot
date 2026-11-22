"""
Ioniq6 build: StarPilot's highway smoothing and lane-change smoothing, hosted in controlsd.

Both are off unless /data/sunnypilot_smoothing.json turns them on; the file is re-read once a
second, so they can be changed from the phone page mid-drive (a lane-change setting waits until
no lane change is in progress).

  highway_smoothing  0-9 (0 = off). On roads above ~64 km/h, damps the small left-right weave in
                     the model's steering request around its recent average; it backs off in
                     tight curves, when the request moves a lot, and fades out on a turn signal,
                     a lane change, your hands on the wheel, or a sunnypilot steering maneuver.
                     StarPilot's "Highway Smoothing" level = 10 - this (StarPilot 10 = off).
  lane_change_smoothing  true/false. Limits how fast the steering request may change during a
                     lane change (and eases back afterwards), so lane changes are gentler. It
                     only ever slows changes down, never speeds them up, and lets the steering
                     unwind at the normal rate.
  lane_change_seconds  3-8 (StarPilot's default 5.78): how long a lane change should take; longer
                     = gentler.

Wiring follows StarPilot's controlsd: highway smoothing runs before lane centering (so lane
centering's guard measures only its own nudge), and the lane-change rate limit runs just before
openpilot's normal curvature limiter, on both the request and lane centering's baseline.

Any error here switches that feature off for the rest of the drive and passes the request through
unchanged, so it can never stop openpilot from steering.
"""
import json
import math
import os
import time

from cereal import log
from openpilot.common.constants import CV
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.selfdrive.controls.lib.smoothing.highway_correction_gain import (
  MAX_SETTING, HighwayCorrectionGain, predicted_lateral_accels,
)
from openpilot.sunnypilot.selfdrive.controls.lib.smoothing.lane_change_smoothing import LaneChangeSmoother, limit_rate

DATA = os.environ.get("SUNNYPILOT_DATA", "/data")
CONFIG = os.path.join(DATA, "sunnypilot_smoothing.json")
SHM = os.environ.get("SUNNYPILOT_SHM", "/dev/shm")
STATUS = os.path.join(SHM, "sp_smoothing.json")

DEFAULT_LANE_CHANGE_SECONDS = 3.0 + 25.0 / 9.0          # StarPilot LaneChangePolicy.duration_s
LANE_CHANGE_MIN_SPEED = 5 * CV.MPH_TO_MS                 # StarPilot lane_change_preferences.MINIMUM_SPEED_MPS
LIMITS = {
  "highway_smoothing": (0, 0, 9),
  "lane_change_smoothing": (False, None, None),
  "lane_change_seconds": (DEFAULT_LANE_CHANGE_SECONDS, 3.0, 8.0),
}
REFRESH_S = 1.0
STATUS_PERIOD_S = 1.0
STALE_S = 3.0
OFF = log.LaneChangeState.off


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
    out[key] = v if lo is None else float(v)
  if out["highway_smoothing"] != int(out["highway_smoothing"]):
    return None
  return out


def read_config(path: str | None = None) -> tuple[dict, str]:
  """(settings, problem). Missing -> defaults (off); unreadable or out of range -> off with a reason."""
  off = validate({})
  try:
    with open(path or CONFIG) as f:
      text = f.read()
  except FileNotFoundError:
    return off, ""
  except Exception:
    return off, "unreadable settings file"
  if not text.strip():
    return off, ""
  try:
    cfg = validate(json.loads(text))
  except Exception:
    return off, "settings file is not valid JSON"
  return (cfg, "") if cfg else (off, "a setting is the wrong type or out of range")


def highway_gain(strength: float) -> float:
  """Our 0-9 strength -> StarPilot's gain (level / 10): 0 -> 1.0 (off), 9 -> 0.1 (strongest)."""
  return (MAX_SETTING - strength) / MAX_SETTING


class SmoothingRuntime:
  def __init__(self, path: str | None = None, status_path: str | None = None, clock=time.monotonic):
    self.path, self.status_path, self.clock = path, status_path or STATUS, clock
    self.cfg, self.problem = read_config(path)
    self.last_read = None
    self.highway = HighwayCorrectionGain()
    self.lane_change = LaneChangeSmoother()
    self.highway_failed = self.lane_change_failed = False
    self.status_broken = False
    self.last_status = None
    self._reset_tally()

  # --- settings ------------------------------------------------------------------------------
  def _refresh(self, lane_change_active: bool) -> None:
    now = self.clock()
    if self.last_read is not None and 0.0 <= now - self.last_read < REFRESH_S:
      return
    self.last_read = now
    cfg, problem = read_config(self.path)
    if lane_change_active:                   # don't change lane-change pacing in the middle of one
      cfg = {**cfg, "lane_change_smoothing": self.cfg["lane_change_smoothing"],
             "lane_change_seconds": self.cfg["lane_change_seconds"]}
    if cfg != self.cfg:
      cloudlog.info(f"smoothing settings: {cfg}")
    self.cfg, self.problem = cfg, problem

  # --- controlsd hooks -----------------------------------------------------------------------
  def smooth_highway(self, sm, CS, CC, model_v2, curvature: float) -> float:
    """Highway weave damping on the model's request (before lane centering)."""
    try:
      self._refresh(model_v2.meta.laneChangeState != OFF)
    except Exception:
      cloudlog.exception("smoothing: settings refresh failed")
    if self.highway_failed or self.cfg["highway_smoothing"] <= 0:
      self.highway.reset(curvature)
      self.hw_ticks += 1
      return curvature
    try:
      predicted = None
      if sm.all_checks(['modelV2']):
        predicted = predicted_lateral_accels(model_v2.position.t, model_v2.velocity.x, model_v2.orientationRate.z)
      bypass = bool(CS.leftBlinker or CS.rightBlinker or CS.steeringPressed or
                    model_v2.meta.laneChangeState != OFF or sm.valid['lateralManeuverPlan'])
      out = self.highway.update(curvature, CS.vEgo, CC.latActive, highway_gain(self.cfg["highway_smoothing"]),
                                bypass=bypass, predicted=predicted)
      if not math.isfinite(out):
        raise ValueError(f"non-finite highway smoothing output {out!r}")
      self.hw_ticks += 1
      self.hw_active += int(CC.latActive and self.highway.weight > 0.05)
      return out
    except Exception:
      cloudlog.exception("smoothing: highway smoothing failed; off for the rest of this drive")
      self.highway_failed = True
      return curvature

  def lane_change_factor(self, sm, CS, CC, model_v2, previous: float, desired: float) -> float:
    """1.0 = openpilot's normal rate; below 1 slows how fast the request may change."""
    if self.lane_change_failed or not self.cfg["lane_change_smoothing"]:
      self.lane_change.reset()
      return 1.0
    try:
      factor = self.lane_change.factor(
        active=bool(CC.latActive and CS.canValid and not CS.canTimeout and sm.all_checks(['modelV2'])),
        lane_change_state=model_v2.meta.laneChangeState, speed=CS.vEgo, minimum_speed=LANE_CHANGE_MIN_SPEED,
        duration=self.cfg["lane_change_seconds"], previous=previous, desired=desired)
      if not math.isfinite(factor):
        raise ValueError(f"non-finite lane-change factor {factor!r}")
      self.lc_min = min(self.lc_min, factor)
      return factor
    except Exception:
      cloudlog.exception("smoothing: lane-change smoothing failed; off for the rest of this drive")
      self.lane_change_failed = True
      return 1.0

  @staticmethod
  def limit(speed: float, previous: float, desired: float, factor: float) -> float:
    out = limit_rate(speed, previous, desired, factor)
    return out if math.isfinite(out) else desired

  # --- phone status --------------------------------------------------------------------------
  def _reset_tally(self):
    self.hw_ticks = self.hw_active = 0
    self.lc_min = 1.0

  def report(self) -> None:
    """Once a second: what smoothing did. Errors only stop the status file."""
    try:
      self._report()
    except Exception:
      self.status_broken = True

  def _report(self) -> None:
    now = self.clock()
    if self.last_status is not None and 0.0 <= now - self.last_status < STATUS_PERIOD_S:
      return
    self.last_status = now
    snap = {"mono": now, "highway_smoothing": self.cfg["highway_smoothing"],
            "lane_change_smoothing": self.cfg["lane_change_smoothing"],
            "highway_active_pct": round(100 * self.hw_active / self.hw_ticks) if self.hw_ticks else 0,
            "lane_change_min_factor": round(self.lc_min, 3),
            "highway_failed": self.highway_failed, "lane_change_failed": self.lane_change_failed}
    self._reset_tally()
    if self.status_broken:
      return
    try:
      tmp = self.status_path + ".tmp"
      with open(tmp, "w") as f:
        f.write(json.dumps(snap))
      os.replace(tmp, self.status_path)
    except Exception:
      cloudlog.exception("smoothing: can't write status file")
      self.status_broken = True


def describe(snap: dict | None, cfg: dict, problem: str, now: float) -> dict:
  if problem:
    return {"level": "bad", "text": f"off (smoothing settings: {problem})"}
  hw, lc = int(cfg["highway_smoothing"]), bool(cfg["lane_change_smoothing"])
  if not hw and not lc:
    return {"level": "mut", "text": "off"}
  parts = []
  live = isinstance(snap, dict) and isinstance(snap.get("mono"), (int, float)) and 0.0 <= now - snap["mono"] <= STALE_S
  if hw:
    if live and snap.get("highway_failed"):
      parts.append("highway: error, off for this drive")
    elif live and snap.get("highway_active_pct"):
      parts.append(f"highway {hw}/9, smoothing now")
    else:
      parts.append(f"highway {hw}/9")
  if lc:
    if live and snap.get("lane_change_failed"):
      parts.append("lane changes: error, off for this drive")
    elif live and (snap.get("lane_change_min_factor") or 1.0) < 1.0:
      parts.append("lane changes on, easing one now")
    else:
      parts.append(f"lane changes on ({cfg['lane_change_seconds']:.1f} s)")
  bad = live and (snap.get("highway_failed") or snap.get("lane_change_failed"))
  return {"level": "bad" if bad else "on", "text": " · ".join(parts)}


def read_status(now: float, config_path: str | None = None, status_path: str | None = None) -> dict:
  cfg, problem = read_config(config_path)
  try:
    with open(status_path or STATUS) as f:
      snap = json.load(f)
  except Exception:
    snap = None
  return describe(snap, cfg, problem, now)
