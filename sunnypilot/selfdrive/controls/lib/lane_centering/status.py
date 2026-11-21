"""
Ioniq6 build: lane-centering status for the phone page.

controlsd sums up each second of lane centering (what it was doing, how much it corrected) and
writes one small JSON file to /dev/shm (RAM, not flash). The phone page reads it and turns it into
one plain-English line. Writing can never affect steering: any error just stops the status file.
"""
import json
import math
import os
from collections import Counter

from openpilot.common.swaglog import cloudlog

SHM = os.environ.get("SUNNYPILOT_SHM", "/dev/shm")
STATUS = os.path.join(SHM, "sp_lane_centering.json")
PERIOD_S = 1.0
STALE_S = 3.0          # controlsd only runs while driving; an old file means "not driving"
CORRECTING_EPS = 1e-6  # curvature (1/m) below this counts as no correction

# controller reason -> (state, text shown on the phone)
REASONS = {
  "qualified": ("centred", "centred, no correction needed"),
  "clearance_limited": ("correcting", "correcting (held back near the road edge)"),
  "disabled": ("off", "off"),
  "inactive": ("standby", "standby: openpilot isn't steering, or below 18 km/h"),
  "driver_override": ("paused", "paused: you're steering"),
  "lane_change": ("paused", "paused: lane change"),
  "signal_release": ("paused", "paused: turn signal on"),
  "unqualified_model": ("waiting", "waiting: lane lines aren't clear enough"),
  "can_invalid": ("paused", "paused: car data not valid"),
  "carstate_unhealthy": ("paused", "paused: car data not valid"),
  "time_discontinuity": ("paused", "paused: timing hiccup"),
  "invalid_input": ("paused", "paused: bad input"),
  "invalid_model": ("waiting", "waiting: model output not usable"),
  "error": ("error", "error: switched off until the next drive"),
}
LEVEL = {"correcting": "on", "centred": "on", "standby": "mut", "off": "mut", "not_driving": "mut",
         "paused": "warn", "waiting": "warn", "error": "bad"}


class StatusWriter:
  def __init__(self, path: str | None = None):
    self.path = path or STATUS
    self.broken = False
    self.last_write: float | None = None
    self._reset()

  def _reset(self):
    self.ticks = 0
    self.lat_ticks = 0
    self.correcting_ticks = 0
    self.applied_sum = 0.0
    self.lat_accel_sum = 0.0
    self.reasons: Counter = Counter()

  def add(self, *, lat_active: bool, reason: str, applied: float, v_ego: float) -> None:
    self.ticks += 1
    self.reasons[reason] += 1
    if lat_active:
      self.lat_ticks += 1
    if math.isfinite(applied) and abs(applied) > CORRECTING_EPS:
      self.correcting_ticks += 1
      self.applied_sum += applied
      self.lat_accel_sum += applied * v_ego * v_ego

  def summary(self, now: float, *, enabled: bool, failed: bool) -> dict:
    n = self.correcting_ticks
    reason = "error" if failed else self.reasons.most_common(1)[0][0] if self.reasons else ("qualified" if enabled else "disabled")
    return {
      "mono": now, "enabled": enabled, "failed": failed, "reason": reason,
      "correcting_pct": round(100 * n / self.lat_ticks) if self.lat_ticks else 0,
      "curvature": self.applied_sum / n if n else 0.0,            # mean while correcting; negative = left
      "lat_accel": round(self.lat_accel_sum / n, 3) if n else 0.0,
    }

  def flush(self, now: float, *, enabled: bool, failed: bool) -> None:
    if self.broken or (self.last_write is not None and 0.0 <= now - self.last_write < PERIOD_S):
      return
    snap = self.summary(now, enabled=enabled, failed=failed)
    self.last_write = now
    self._reset()
    try:
      tmp = self.path + ".tmp"
      with open(tmp, "w") as f:
        f.write(json.dumps(snap))
      os.replace(tmp, self.path)
    except Exception:
      cloudlog.exception("lane centering: can't write status file, phone status disabled")
      self.broken = True


def describe(snap: dict | None, now: float, *, config_enabled: bool, config_invalid: bool = False) -> dict:
  """One status line for the phone page."""
  if config_invalid:
    return {"state": "off", "level": "bad", "text": "off: the lane_centering settings are invalid (check the tab)"}
  if not snap or not isinstance(snap.get("mono"), (int, float)) or not 0.0 <= now - snap["mono"] <= STALE_S:
    on = "on, starts when you drive" if config_enabled else "off"
    return {"state": "not_driving" if config_enabled else "off", "level": "mut", "text": on}
  if not snap.get("enabled"):
    return {"state": "off", "level": "mut", "text": "off"}
  pct = int(snap.get("correcting_pct") or 0)
  lat = float(snap.get("lat_accel") or 0.0)
  if pct > 0 and not snap.get("failed"):
    side = "left" if snap.get("curvature", 0.0) < 0 else "right"
    held = " (held back near the road edge)" if snap.get("reason") == "clearance_limited" else ""
    text = f"correcting {'←' if side == 'left' else '→'} {side}, {abs(lat):.2f} m/s², {pct}% of the last second{held}"
    return {"state": "correcting", "level": "on", "text": text, "side": side, "lat_accel": lat, "pct": pct}
  state, text = REASONS.get(snap.get("reason", ""), ("paused", f"paused: {snap.get('reason')}"))
  return {"state": state, "level": LEVEL[state], "text": text}


def read_status(now: float, *, path: str | None = None, config_path: str | None = None) -> dict:
  """For the settings server: last status file + the saved config, as one line."""
  from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering.runtime import CONFIG, validate
  enabled, invalid = False, False
  try:
    with open(config_path or CONFIG) as f:
      text = f.read()
    if text.strip():                                     # an empty tab is just "off"
      settings = validate(json.loads(text))
      invalid = settings is None
      enabled = bool(settings and settings.enabled)
  except FileNotFoundError:
    pass
  except Exception:
    invalid = True
  try:
    with open(path or STATUS) as f:
      snap = json.load(f)
  except Exception:
    snap = None
  return describe(snap, now, config_enabled=enabled, config_invalid=invalid)
