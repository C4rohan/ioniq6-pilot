"""
Telemetry for the Ioniq 6 custom build: live status, trip & disengagement
logging, and notifications.

Runs as a background thread inside the settings-server process. It NEVER touches
vehicle control: it only READS openpilot state and WRITES log files and webhooks.
Every capability is independently config-gated and fails safe -- errors are
logged and skipped.

Files (all under /data, never in the repo):
  sp_trips.csv               one row per drive
  sp_disengagements.csv      one row per disengagement, with context
  sunnypilot_notify.json     notifications (telegram / generic webhook)
"""
import csv
import os
import threading
import time
import urllib.request
from datetime import datetime

import cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

TRIPS_CSV = "/data/sp_trips.csv"
DISENGAGE_CSV = "/data/sp_disengagements.csv"
NOTIFY_CONFIG = "/data/sunnypilot_notify.json"

LOOP_HZ = 5
CONFIG_PERIOD = 5.0       # s
MIN_TRIP_M = 200.0        # ignore drives shorter than this
MPS_TO_KPH = 3.6

TRIP_FIELDS = ["start", "end", "minutes", "km", "max_kph", "engaged_pct", "disengagements", "model"]
DIS_FIELDS = ["time", "kph", "gas", "brake", "steer", "standstill", "trip_km"]


# --------------------------------------------------------------------------- helpers

def load_json(path, default=None):
  import json
  try:
    with open(path) as f:
      return json.load(f)
  except Exception:
    return default if default is not None else {}


def append_csv(path, fields, row) -> None:
  new = not os.path.exists(path)
  with open(path, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    if new:
      w.writeheader()
    w.writerow(row)


def read_csv_tail(path, n=50) -> list[dict]:
  try:
    with open(path, newline="") as f:
      rows = list(csv.DictReader(f))
    return rows[-n:][::-1]
  except Exception:
    return []


def active_model_name() -> str:
  try:
    from openpilot.sunnypilot.models.helpers import get_active_bundle
    b = get_active_bundle()
    if not b:
      return "default"
    for attr in ("displayName", "display_name", "shortName", "short_name", "name"):
      v = getattr(b, attr, None)
      if v:
        return str(v)
    return "custom"
  except Exception:
    return "?"


class Trip:
  def __init__(self, now_wall: float):
    self.start = now_wall
    self.end = now_wall
    self.dist_m = 0.0
    self.max_mps = 0.0
    self.engaged_s = 0.0
    self.total_s = 0.0
    self.disengagements = 0
    self.model = active_model_name()

  def update(self, v_mps: float, engaged: bool, dt: float, now_wall: float) -> None:
    v = max(0.0, float(v_mps))
    self.dist_m += v * dt
    self.max_mps = max(self.max_mps, v)
    self.total_s += dt
    if engaged:
      self.engaged_s += dt
    self.end = now_wall

  def row(self) -> dict:
    pct = round(100.0 * self.engaged_s / self.total_s) if self.total_s > 0 else 0
    return {
      "start": datetime.fromtimestamp(self.start).strftime("%Y-%m-%d %H:%M"),
      "end": datetime.fromtimestamp(self.end).strftime("%H:%M"),
      "minutes": round(self.total_s / 60.0, 1),
      "km": round(self.dist_m / 1000.0, 2),
      "max_kph": round(self.max_mps * MPS_TO_KPH),
      "engaged_pct": pct,
      "disengagements": self.disengagements,
      "model": self.model,
    }

  def summary(self) -> str:
    r = self.row()
    return (f"🚗 Drive done: {r['km']} km in {r['minutes']} min, max {r['max_kph']} km/h. "
            f"openpilot engaged {r['engaged_pct']}% with {r['disengagements']} disengagement(s). "
            f"Model: {r['model']}.")


# --------------------------------------------------------------------------- notifications

def send_notification(text: str) -> None:
  """Fire-and-forget; config-gated; never raises."""
  def _send():
    import json
    try:
      cfg = load_json(NOTIFY_CONFIG)
      if not cfg.get("enabled"):
        return
      kind = str(cfg.get("type", "")).lower()
      if kind == "telegram":
        token, chat = cfg.get("bot_token", ""), cfg.get("chat_id", "")
        if not token or not chat:
          return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat, "text": text}).encode()
      elif kind == "webhook":
        url = cfg.get("webhook_url", "")
        if not url:
          return
        body = json.dumps({"text": text}).encode()
      else:
        return
      req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
      urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
      cloudlog.warning(f"telemetry: notify failed: {e}")
  threading.Thread(target=_send, daemon=True).start()


# --------------------------------------------------------------------------- the thread

class Telemetry:
  def __init__(self):
    self.status = {"onroad": False, "engaged": False, "kph": 0, "model": "?",
                   "trip_km": 0.0, "trip_engaged_pct": 0, "updated": 0}
    self.lock = threading.Lock()
    self.trip: Trip | None = None
    self.prev_started = False
    self.prev_engaged = False
    self.notify_cfg = {}
    self.cfg_read = 0.0

  def _reload_configs(self, now):
    if now - self.cfg_read < CONFIG_PERIOD:
      return
    self.cfg_read = now
    self.notify_cfg = load_json(NOTIFY_CONFIG)

  def run(self):
    params = Params()
    sm = messaging.SubMaster(["deviceState", "carState", "selfdriveState"])
    dt = 1.0 / LOOP_HZ
    while True:
      try:
        sm.update(int(dt * 1000))
        now, wall = time.monotonic(), time.time()
        self._reload_configs(now)

        started = bool(sm["deviceState"].started)
        cs, ss = sm["carState"], sm["selfdriveState"]
        engaged = bool(ss.enabled)
        v = float(cs.vEgo)

        if started and not self.prev_started:
          self.trip = Trip(wall)
        if not started and self.prev_started and self.trip:
          if self.trip.dist_m >= MIN_TRIP_M:
            append_csv(TRIPS_CSV, TRIP_FIELDS, self.trip.row())
            if self.notify_cfg.get("notify_trips", True):
              send_notification(self.trip.summary())
          self.trip = None
        self.prev_started = started

        if self.trip:
          self.trip.update(v, engaged, dt, wall)

        if self.prev_engaged and not engaged and started:
          if self.trip:
            self.trip.disengagements += 1
          append_csv(DISENGAGE_CSV, DIS_FIELDS, {
            "time": datetime.fromtimestamp(wall).strftime("%Y-%m-%d %H:%M:%S"),
            "kph": round(v * MPS_TO_KPH),
            "gas": int(bool(cs.gasPressed)), "brake": int(bool(cs.brakePressed)),
            "steer": int(bool(cs.steeringPressed)), "standstill": int(bool(cs.standstill)),
            "trip_km": round(self.trip.dist_m / 1000.0, 2) if self.trip else 0,
          })
        self.prev_engaged = engaged

        with self.lock:
          self.status.update({
            "onroad": started, "engaged": engaged, "kph": round(v * MPS_TO_KPH),
            "model": self.trip.model if self.trip else active_model_name(),
            "trip_km": round(self.trip.dist_m / 1000.0, 2) if self.trip else 0.0,
            "trip_engaged_pct": self.trip.row()["engaged_pct"] if self.trip else 0,
            "updated": round(wall),
          })
      except Exception as e:
        cloudlog.warning(f"telemetry: loop error: {e}")
        time.sleep(1.0)

  def snapshot(self) -> dict:
    with self.lock:
      return dict(self.status)


def write_json_atomic(path, obj) -> None:
  import json
  tmp = path + ".tmp"
  with open(tmp, "w") as f:
    json.dump(obj, f, indent=2)
  os.replace(tmp, path)


def start() -> Telemetry:
  t = Telemetry()
  threading.Thread(target=t.run, daemon=True, name="sp-telemetry").start()
  return t
