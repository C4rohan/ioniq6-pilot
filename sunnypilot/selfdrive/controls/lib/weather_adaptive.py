"""
Weather-adaptive longitudinal driving for sunnypilot.

Adapted from FrogPilot's "Weather Presets" (FrogAi/FrogPilot, MIT License):
frogpilot/controls/lib/weather_checker.py and the code that consumes it. This
port calls OpenWeatherMap directly (free-tier Current Weather API 2.5) with the
user's own key instead of FrogPilot's API proxy, uses only the standard library
for HTTP, and reads its settings from an on-device JSON file because new Params
keys cannot be registered on a prebuilt branch.

In poor weather it increases following time, increases the stopped gap behind a
lead, and reduces the maximum acceleration. It only ever makes driving more
conservative, and it fails safe: if disabled, unconfigured, offline, or on any
error the offsets are neutral and behaviour is identical to stock.

Config: /data/sunnypilot_weather.json  (template: weather_adaptive.example.json)
Status: /data/sunnypilot_weather_status.json  (what it currently sees/applies)
"""
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from openpilot.common.gps import get_gps_location_service
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

CONFIG_PATH = "/data/sunnypilot_weather.json"
STATUS_PATH = "/data/sunnypilot_weather_status.json"
OWM_URL = "https://api.openweathermap.org/data/2.5/weather"

CONFIG_CHECK_PERIOD = 5.0        # s, how often the config file is stat'ed for changes
STATUS_WRITE_PERIOD = 5.0        # s
DEFAULT_REFRESH = 300.0          # s, how often to re-query weather at the same location
CACHE_DISTANCE_KM = 25.0         # re-query sooner once we have moved this far
MAX_GPS_FIX_AGE = 30.0           # s, ignore stale fixes
REQUEST_TIMEOUT = 10.0           # s
MAX_RETRIES = 3
RETRY_DELAY = 60.0               # s, between retries inside the background worker
STALE_WEATHER_AGE = 3 * 3600.0   # s, drop offsets if a refresh has not succeeded for this long
ERROR_LOG_PERIOD = 60.0          # s, rate limit for repeated error logging

MIN_ACCEL_FACTOR = 0.3           # never cut max acceleration below 30% of normal
MIN_LAT_ACCEL_FACTOR = 0.6       # never cut curve-speed lateral accel below 60% (avoid crawling curves)
MAX_T_FOLLOW_OFFSET = 1.5        # s, hard cap on extra following time
MAX_STOP_OFFSET_M = 5.0          # m, hard cap on extra stopped gap
FOOT_TO_METER = 0.3048

# The extra stopped gap applies fully at standstill and fades out with speed so
# it does not double count the following-time increase at road speeds.
STOP_OFFSET_FADE_BP = [0.0, 10.0]  # m/s
STOP_OFFSET_FADE_V = [1.0, 0.0]

# OpenWeatherMap condition ids: https://openweathermap.org/weather-conditions
WEATHER_CATEGORIES = {
  "rain": [(300, 321), (500, 504)],
  "rain_storm": [(200, 232), (511, 511), (520, 531)],
  "snow": [(600, 622)],
  "low_visibility": [(701, 762)],
}

# Conservative starting points. FrogPilot ships all of these at 0 and leaves
# them to the user, so these are NOT field-tested values: tune them in the
# config file. Units: follow_s = seconds added to the following time,
# stop_ft = feet added to the stopped gap, accel_pct = percent reduction of
# the maximum acceleration.
DEFAULT_CONFIG = {
  "enabled": False,
  "owm_api_key": "",
  "refresh_s": DEFAULT_REFRESH,
  "night_enabled": False,
  "offsets": {
    "rain": {"follow_s": 0.3, "stop_ft": 3, "accel_pct": 15, "lat_pct": 10},
    "rain_storm": {"follow_s": 0.5, "stop_ft": 5, "accel_pct": 30, "lat_pct": 20},
    "snow": {"follow_s": 0.7, "stop_ft": 8, "accel_pct": 40, "lat_pct": 30},
    "low_visibility": {"follow_s": 0.4, "stop_ft": 4, "accel_pct": 20, "lat_pct": 15},
    # applied after sunset / before sunrise when the weather is otherwise clear
    "night": {"follow_s": 0.2, "stop_ft": 2, "accel_pct": 10, "lat_pct": 5},
  },
}


def category_for_id(weather_id: int) -> str | None:
  for name, ranges in WEATHER_CATEGORIES.items():
    if any(lo <= weather_id <= hi for lo, hi in ranges):
      return name
  return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
  r = 6373.0
  p1, p2 = math.radians(lat1), math.radians(lat2)
  dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
  a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
  return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def merge_config(user) -> dict:
  cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy of the defaults
  if not isinstance(user, dict):
    return cfg
  for k in ("enabled", "owm_api_key", "refresh_s", "night_enabled"):
    if k in user:
      cfg[k] = user[k]
  for name, vals in (user.get("offsets") or {}).items():
    if name in cfg["offsets"] and isinstance(vals, dict):
      cfg["offsets"][name].update({k: v for k, v in vals.items() if k in ("follow_s", "stop_ft", "accel_pct", "lat_pct")})
  return cfg


def offsets_for(cfg: dict, category: str | None, v_ego: float) -> tuple[float, float, float, float]:
  """Returns (t_follow_offset_s, stop_offset_m, accel_factor, lat_accel_factor); neutral is (0, 0, 1, 1)."""
  if category is None:
    return 0.0, 0.0, 1.0, 1.0
  o = cfg["offsets"].get(category, {})
  follow = float(np.clip(float(o.get("follow_s", 0.0)), 0.0, MAX_T_FOLLOW_OFFSET))
  stop_m = float(np.clip(float(o.get("stop_ft", 0.0)) * FOOT_TO_METER, 0.0, MAX_STOP_OFFSET_M))
  stop_m *= float(np.interp(max(v_ego, 0.0), STOP_OFFSET_FADE_BP, STOP_OFFSET_FADE_V))
  accel = float(np.clip(1.0 - float(o.get("accel_pct", 0.0)) / 100.0, MIN_ACCEL_FACTOR, 1.0))
  lat = float(np.clip(1.0 - float(o.get("lat_pct", 0.0)) / 100.0, MIN_LAT_ACCEL_FACTOR, 1.0))
  return follow, stop_m, accel, lat


class WeatherAdaptive:
  def __init__(self):
    self.params = Params()
    self.gps_service = get_gps_location_service(self.params)
    self.executor = ThreadPoolExecutor(max_workers=1)
    self.lock = threading.Lock()

    self.cfg = merge_config({})
    self.cfg_mtime = None
    self.cfg_checked = 0.0
    self.status_written = 0.0
    self.last_err_log = 0.0

    self.weather_id = 0
    self.category = None
    self.location_name = ""
    self.last_fetch_mono = 0.0
    self.last_fetch_wall = 0.0
    self.last_position = None
    self.last_error = ""
    self.requesting = False
    self.sunrise = 0.0   # unix, from OWM for the fetched location
    self.sunset = 0.0

    self.t_follow_offset = 0.0
    self.stop_distance_offset_m = 0.0
    self.accel_factor = 1.0
    self.lat_accel_factor = 1.0

  # -- config ---------------------------------------------------------------

  def _reload_config(self, now: float) -> None:
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
        cloudlog.warning(f"weather_adaptive: bad config {CONFIG_PATH}: {e}")
    self.cfg = merge_config(user)
    cloudlog.info(f"weather_adaptive: config loaded enabled={self.cfg['enabled']} "
                  f"key={'set' if self.cfg['owm_api_key'] else 'missing'}")

  @property
  def active(self) -> bool:
    return bool(self.cfg.get("enabled")) and bool(self.cfg.get("owm_api_key"))

  # -- fetching (background thread) ----------------------------------------

  def _fetch(self, lat: float, lon: float, key: str):
    q = urllib.parse.urlencode({"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "appid": key})
    req = urllib.request.Request(f"{OWM_URL}?{q}", headers={"User-Agent": "sunnypilot-weather-adaptive/1.0"})
    err = ""
    for attempt in range(1, MAX_RETRIES + 1):
      try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
          return json.loads(resp.read().decode("utf-8")), ""
      except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if attempt < MAX_RETRIES:
          time.sleep(RETRY_DELAY)
    return None, err

  def _on_fetch_done(self, future, position, started_mono: float) -> None:
    try:
      data, err = future.result()
    except Exception as e:
      data, err = None, f"{type(e).__name__}: {e}"
    with self.lock:
      self.requesting = False
      self.last_error = err
      if data is not None:
        prev = self.category
        self.weather_id = int((data.get("weather") or [{}])[0].get("id", 0) or 0)
        self.category = category_for_id(self.weather_id)
        self.location_name = str(data.get("name", ""))
        sysd = data.get("sys") or {}
        self.sunrise = float(sysd.get("sunrise") or 0.0)
        self.sunset = float(sysd.get("sunset") or 0.0)
        self.last_fetch_mono = started_mono
        self.last_fetch_wall = time.time()
        self.last_position = position
        if self.category != prev:
          cloudlog.info(f"weather_adaptive: {self.location_name} id={self.weather_id} -> {self.category or 'clear'}")
      else:
        cloudlog.warning(f"weather_adaptive: fetch failed: {err}")
    self._write_status()

  def is_night(self, wall: float) -> bool:
    return self.sunrise > 0 and self.sunset > 0 and not (self.sunrise <= wall <= self.sunset)

  def _maybe_fetch(self, sm, now: float) -> None:
    if self.requesting:
      return
    gps = sm[self.gps_service]
    if not gps.hasFix:
      return
    if time.time() - gps.unixTimestampMillis * 1e-3 > MAX_GPS_FIX_AGE:
      return
    lat, lon = float(gps.latitude), float(gps.longitude)
    if lat == 0.0 and lon == 0.0:
      return
    position = (lat, lon)

    due = (now - self.last_fetch_mono) >= float(self.cfg.get("refresh_s", DEFAULT_REFRESH))
    if self.last_position is not None and haversine_km(*self.last_position, lat, lon) > CACHE_DISTANCE_KM:
      due = True
    if not due:
      return

    self.requesting = True
    fut = self.executor.submit(self._fetch, lat, lon, self.cfg["owm_api_key"])
    fut.add_done_callback(lambda f: self._on_fetch_done(f, position, now))

  # -- status file ----------------------------------------------------------

  def _write_status(self) -> None:
    try:
      st = {
        "active": self.active,
        "condition": self.category or "clear",
        "is_night": self.is_night(time.time()),
        "night_mode": bool(self.cfg.get("night_enabled")),
        "weather_id": self.weather_id,
        "location": self.location_name,
        "last_fetch_unix": round(self.last_fetch_wall) if self.last_fetch_wall else None,
        "last_fetch_age_s": round(time.time() - self.last_fetch_wall) if self.last_fetch_wall else None,
        "last_error": self.last_error,
        "applied": {
          "t_follow_offset_s": round(self.t_follow_offset, 3),
          "stop_distance_offset_m": round(self.stop_distance_offset_m, 3),
          "accel_factor": round(self.accel_factor, 3),
          "lat_accel_factor": round(self.lat_accel_factor, 3),
        },
      }
      tmp = STATUS_PATH + ".tmp"
      with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
      os.replace(tmp, STATUS_PATH)
    except Exception:
      pass

  # -- main entry point -----------------------------------------------------

  def update(self, sm, v_ego: float) -> None:
    """Called every planner cycle. Cheap, and never raises into the planner."""
    try:
      now = time.monotonic()
      self._reload_config(now)
      if not self.active:
        self._neutral()
        return

      self._maybe_fetch(sm, now)

      with self.lock:
        category = self.category
        stale = bool(self.last_fetch_mono) and (now - self.last_fetch_mono) > STALE_WEATHER_AGE
      if stale:
        category = None
      elif category is None and self.cfg.get("night_enabled") and self.is_night(time.time()):
        category = "night"
      self.t_follow_offset, self.stop_distance_offset_m, self.accel_factor, self.lat_accel_factor = \
        offsets_for(self.cfg, category, v_ego)

      if now - self.status_written >= STATUS_WRITE_PERIOD:
        self.status_written = now
        self._write_status()
    except Exception as e:
      self._neutral()
      now = time.monotonic()
      if now - self.last_err_log >= ERROR_LOG_PERIOD:
        self.last_err_log = now
        cloudlog.exception(f"weather_adaptive: update error, offsets neutral: {e}")

  def _neutral(self) -> None:
    self.t_follow_offset = 0.0
    self.stop_distance_offset_m = 0.0
    self.accel_factor = 1.0
    self.lat_accel_factor = 1.0
    self.lat_accel_factor = 1.0
