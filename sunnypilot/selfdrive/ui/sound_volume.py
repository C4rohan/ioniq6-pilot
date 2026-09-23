"""
User alert volume for the Ioniq 6 build.

A volume scale applied on top of openpilot's automatic ambient-noise volume, for
NON-critical sounds only (engage/disengage chimes, prompts). Critical warnings --
the same list sunnypilot's Quiet Mode always plays -- are never made quieter.

Config: /data/sunnypilot_sound.json  {"enabled": true, "volume": 0.5}
  volume 0.1-1.0 (1.0 = stock). Missing/invalid config = stock volume.
Reads are cached; factor() is called from the audio callback and does no I/O.
"""
import json
import os
import time

from openpilot.sunnypilot.selfdrive.ui.quiet_mode import ALERTS_ALWAYS_PLAY

CONFIG_PATH = os.path.join(os.environ.get("SUNNYPILOT_DATA", "/data"), "sunnypilot_sound.json")
MIN_SCALE = 0.1
CHECK_PERIOD = 5.0


def scale_from_config(cfg) -> float:
  if not isinstance(cfg, dict) or not cfg.get("enabled", True):
    return 1.0
  try:
    return min(max(float(cfg.get("volume", 1.0)), MIN_SCALE), 1.0)
  except (TypeError, ValueError):
    return 1.0


class UserVolume:
  def __init__(self):
    self.scale = 1.0
    self._mtime = None
    self._checked = -1e9

  def reload(self) -> None:
    now = time.monotonic()
    if now - self._checked < CHECK_PERIOD:
      return
    self._checked = now
    try:
      mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
      mtime = None
    if mtime == self._mtime:
      return
    self._mtime = mtime
    cfg = {}
    if mtime is not None:
      try:
        with open(CONFIG_PATH) as f:
          cfg = json.load(f)
      except Exception:
        cfg = {}
    self.scale = scale_from_config(cfg)

  def factor(self, alert) -> float:
    """Multiplier for the current alert. Critical warnings always 1.0."""
    return 1.0 if alert in ALERTS_ALWAYS_PLAY else self.scale
