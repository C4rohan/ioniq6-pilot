"""
Acceleration profiles (Eco / Normal / Sport) for sunnypilot.

Adapted from FrogPilot's acceleration profiles (FrogAi/FrogPilot, MIT). This
port scales the maximum-acceleration ceiling by a speed-dependent factor read
from an on-device JSON file (new Params keys cannot be registered on a prebuilt
branch). It only changes how briskly openpilot accelerates toward the set
speed; it does not touch braking, following distance, or any safety limit.

Fail-safe: disabled/unconfigured/"normal"/any error -> factor 1.0 (stock).
The result is also clamped so Sport can never exceed a safe absolute ceiling.

Config: /data/sunnypilot_accel.json  (template: accel_profiles.example.json)
"""
import json
import os
import time

import numpy as np

from openpilot.common.swaglog import cloudlog

CONFIG_PATH = "/data/sunnypilot_accel.json"
CONFIG_CHECK_PERIOD = 5.0        # s
ABS_MAX_ACCEL = 2.5              # m/s^2, hard ceiling regardless of profile/factor
MIN_FACTOR, MAX_FACTOR = 0.4, 1.6

# Speed-dependent multipliers on get_max_accel(v_ego). Breakpoints in m/s.
# Eco is gentler everywhere; Sport is punchier, most at low speed, and tapers
# so it never fights the stock high-speed ceiling.
PROFILE_BP = [0.0, 10.0, 25.0, 40.0]  # m/s
PROFILES = {
  "eco":    [0.70, 0.70, 0.75, 0.80],
  "normal": [1.00, 1.00, 1.00, 1.00],
  "sport":  [1.45, 1.35, 1.20, 1.10],
}
DEFAULT_CONFIG = {"enabled": False, "profile": "normal"}


def merge_config(user) -> dict:
  cfg = dict(DEFAULT_CONFIG)
  if isinstance(user, dict):
    if "enabled" in user:
      cfg["enabled"] = bool(user["enabled"])
    if str(user.get("profile", "")).lower() in PROFILES:
      cfg["profile"] = str(user["profile"]).lower()
  return cfg


def factor_for(cfg: dict, v_ego: float) -> float:
  if not cfg.get("enabled"):
    return 1.0
  vals = PROFILES.get(cfg.get("profile", "normal"), PROFILES["normal"])
  f = float(np.interp(max(v_ego, 0.0), PROFILE_BP, vals))
  return float(np.clip(f, MIN_FACTOR, MAX_FACTOR))


class AccelProfiles:
  def __init__(self):
    self.cfg = merge_config({})
    self.cfg_mtime = None
    self.cfg_checked = 0.0
    self.factor = 1.0

  def _reload(self, now: float) -> None:
    if now - self.cfg_checked < CONFIG_CHECK_PERIOD:
      return
    self.cfg_checked = now
    try:
      mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
      mtime = None
    if mtime == self.cfg_mtime:
      return
    self.cfg_mtime = mtime
    user = {}
    if mtime is not None:
      try:
        with open(CONFIG_PATH) as f:
          user = json.load(f)
      except Exception as e:
        cloudlog.warning(f"accel_profiles: bad config: {e}")
    self.cfg = merge_config(user)
    cloudlog.info(f"accel_profiles: enabled={self.cfg['enabled']} profile={self.cfg['profile']}")

  def update(self, v_ego: float) -> float:
    """Returns a multiplier for the max-accel ceiling; 1.0 == stock. Never raises."""
    try:
      self._reload(time.monotonic())
      self.factor = factor_for(self.cfg, v_ego)
    except Exception:
      self.factor = 1.0
    return self.factor

  @staticmethod
  def clamp_accel(a_max: float) -> float:
    return min(a_max, ABS_MAX_ACCEL)
