"""
Telemetry for the Ioniq 6 custom build: live status, trip & disengagement
logging, steering-tune feedback, and notifications.

Runs as a background thread inside the settings-server process. It NEVER touches
vehicle control: it only READS openpilot state and WRITES log files and webhooks.

Steering-tune feedback: every time you take over the wheel while openpilot is
steering, it records speed and curve context and classifies the override:
  under    you steered INTO the curve   -> openpilot wasn't turning enough (tune too weak)
  over     you steered AGAINST the curve -> openpilot was turning too much (tune too strong)
  straight on a straight / gentle road  -> lane position preference, not tune strength
It's a heuristic: a trend over many overrides is meaningful, a single one isn't.

Files (under /data, never in the repo):
  sp_trips.csv, sp_disengagements.csv, sp_overrides.csv, sp_steer_stats.json
  sunnypilot_notify.json, sunnypilot_steer_feedback.json (optional)
"""
import csv
import json
import os
import threading
import time
import urllib.request
from datetime import datetime

DATA = os.environ.get("SUNNYPILOT_DATA", "/data")
TRIPS_CSV = os.path.join(DATA, "sp_trips.csv")
DISENGAGE_CSV = os.path.join(DATA, "sp_disengagements.csv")
OVERRIDES_CSV = os.path.join(DATA, "sp_overrides.csv")
STEER_STATS = os.path.join(DATA, "sp_steer_stats.json")
NOTIFY_CONFIG = os.path.join(DATA, "sunnypilot_notify.json")
STEER_CONFIG = os.path.join(DATA, "sunnypilot_steer_feedback.json")

LOOP_HZ = 5
CONFIG_PERIOD = 5.0
STATS_PERIOD = 60.0
MIN_TRIP_M = 200.0
MPS_TO_KPH = 3.6
OVERRIDE_DEBOUNCE_S = 2.0

TRIP_FIELDS = ["start", "end", "minutes", "km", "max_kph", "engaged_pct", "disengagements", "model"]
DIS_FIELDS = ["time", "kph", "gas", "brake", "steer", "standstill", "trip_km"]
OVR_FIELDS = ["time", "kph", "kind", "lat_accel", "desired_curv", "actual_curv", "torque", "model"]

STEER_DEFAULTS = {"min_speed_kph": 30, "straight_lat_accel": 0.3, "invert_sign": False}
SPEED_BUCKETS = [(0, 40, "<40"), (40, 70, "40–70"), (70, 100, "70–100"), (100, 1000, "100+")]
MIN_CURVE_EVENTS = 10


# --------------------------------------------------------------------------- helpers

def load_json(path, default=None):
  try:
    with open(path) as f:
      return json.load(f)
  except Exception:
    return default if default is not None else {}


def write_json_atomic(path, obj) -> None:
  tmp = path + ".tmp"
  with open(tmp, "w") as f:
    json.dump(obj, f, indent=2)
  os.replace(tmp, path)


def append_csv(path, fields, row) -> None:
  new = not os.path.exists(path)
  with open(path, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    if new:
      w.writeheader()
    w.writerow(row)


def read_csv_all(path) -> list[dict]:
  try:
    with open(path, newline="") as f:
      return list(csv.DictReader(f))
  except Exception:
    return []


def read_csv_tail(path, n=50) -> list[dict]:
  return read_csv_all(path)[-n:][::-1]


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


def steer_config() -> dict:
  cfg = dict(STEER_DEFAULTS)
  user = load_json(STEER_CONFIG)
  if isinstance(user, dict):
    for k in STEER_DEFAULTS:
      if k in user:
        cfg[k] = user[k]
  return cfg


# --------------------------------------------------------------------------- steering feedback (pure)

def classify_override(v_mps: float, torque: float, desired_curv: float,
                      straight_lat_accel: float = 0.3, invert_sign: bool = False) -> tuple[str, float]:
  """Returns (kind, desired lateral accel). Convention: left-positive torque and curvature."""
  lat = float(v_mps) ** 2 * float(desired_curv)
  if abs(lat) < float(straight_lat_accel) or torque == 0:
    return "straight", lat
  t = -float(torque) if invert_sign else float(torque)
  return ("under" if (t > 0) == (desired_curv > 0) else "over"), lat


def bucket_for(kph: float) -> str:
  for lo, hi, label in SPEED_BUCKETS:
    if lo <= kph < hi:
      return label
  return SPEED_BUCKETS[-1][2]


def summarize_overrides(rows: list[dict], lat_km: float) -> dict:
  buckets = {label: {"under": 0, "over": 0, "straight": 0} for _, _, label in SPEED_BUCKETS}
  totals = {"under": 0, "over": 0, "straight": 0}
  for r in rows:
    kind = r.get("kind")
    if kind not in totals:
      continue
    try:
      kph = float(r.get("kph", 0))
    except ValueError:
      continue
    buckets[bucket_for(kph)][kind] += 1
    totals[kind] += 1
  n_curve = totals["under"] + totals["over"]
  if n_curve < MIN_CURVE_EVENTS:
    verdict, level = f"Not enough curve overrides yet ({n_curve}/{MIN_CURVE_EVENTS}) — keep driving.", "wait"
  elif totals["under"] >= 2 * totals["over"]:
    verdict, level = "Leans too WEAK in curves — you often add steering into bends.", "under"
  elif totals["over"] >= 2 * totals["under"]:
    verdict, level = "Leans too STRONG in curves — you often push back against the car in bends.", "over"
  else:
    verdict, level = "Balanced — no clear too-weak or too-strong bias in curves.", "ok"
  total = sum(totals.values())
  per100 = round(total / lat_km * 100, 1) if lat_km >= 1.0 else None
  return {"buckets": [{"speed": k, **v} for k, v in buckets.items()], "totals": totals,
          "curve_events": n_curve, "verdict": verdict, "level": level,
          "lateral_km": round(lat_km, 1), "per_100km": per100}


# --------------------------------------------------------------------------- trips

class Trip:
  def __init__(self, now_wall: float):
    self.start = self.end = now_wall
    self.dist_m = self.max_mps = self.engaged_s = self.total_s = 0.0
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
    return {"start": datetime.fromtimestamp(self.start).strftime("%Y-%m-%d %H:%M"),
            "end": datetime.fromtimestamp(self.end).strftime("%H:%M"),
            "minutes": round(self.total_s / 60.0, 1), "km": round(self.dist_m / 1000.0, 2),
            "max_kph": round(self.max_mps * MPS_TO_KPH), "engaged_pct": pct,
            "disengagements": self.disengagements, "model": self.model}

  def summary(self) -> str:
    r = self.row()
    return (f"🚗 Drive done: {r['km']} km in {r['minutes']} min, max {r['max_kph']} km/h. "
            f"openpilot engaged {r['engaged_pct']}% with {r['disengagements']} disengagement(s). Model: {r['model']}.")


# --------------------------------------------------------------------------- notifications

def send_notification(text: str) -> None:
  def _send():
    try:
      cfg = load_json(NOTIFY_CONFIG)
      if not cfg.get("enabled"):
        return
      kind = str(cfg.get("type", "")).lower()
      if kind == "telegram" and cfg.get("bot_token") and cfg.get("chat_id"):
        url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
        body = json.dumps({"chat_id": cfg["chat_id"], "text": text}).encode()
      elif kind == "webhook" and cfg.get("webhook_url"):
        url, body = cfg["webhook_url"], json.dumps({"text": text}).encode()
      else:
        return
      urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=10).read()
    except Exception as e:
      print(f"telemetry: notify failed: {e}", flush=True)
  threading.Thread(target=_send, daemon=True).start()


# --------------------------------------------------------------------------- the thread

class Telemetry:
  def __init__(self):
    self.status = {"onroad": False, "engaged": False, "steering": False, "kph": 0, "model": "?",
                   "trip_km": 0.0, "trip_engaged_pct": 0, "updated": 0}
    self.lock = threading.Lock()
    self.trip: Trip | None = None
    self.prev_started = self.prev_engaged = self.prev_pressed = False
    self.last_override = 0.0
    self.notify_cfg, self.steer_cfg = {}, dict(STEER_DEFAULTS)
    self.cfg_read = self.stats_saved = 0.0
    self.lat_km = float(load_json(STEER_STATS).get("lateral_km", 0.0) or 0.0)

  def _reload_configs(self, now):
    if now - self.cfg_read < CONFIG_PERIOD:
      return
    self.cfg_read = now
    self.notify_cfg = load_json(NOTIFY_CONFIG)
    self.steer_cfg = steer_config()

  def _save_stats(self):
    try:
      write_json_atomic(STEER_STATS, {"lateral_km": round(self.lat_km, 3), "updated": round(time.time())})
    except OSError:
      pass

  def step(self, sm, dt: float, now: float, wall: float) -> None:
    """One loop iteration; separated from run() so it can be tested with a fake SubMaster."""
    self._reload_configs(now)
    started = bool(sm["deviceState"].started)
    cs, ss, cc, ctl = sm["carState"], sm["selfdriveState"], sm["carControl"], sm["controlsState"]
    engaged, lat_active = bool(ss.enabled), bool(cc.latActive)
    v = float(cs.vEgo)

    if started and not self.prev_started:
      self.trip = Trip(wall)
    if not started and self.prev_started and self.trip:
      if self.trip.dist_m >= MIN_TRIP_M:
        append_csv(TRIPS_CSV, TRIP_FIELDS, self.trip.row())
        if self.notify_cfg.get("notify_trips", True):
          send_notification(self.trip.summary())
      self.trip = None
      self._save_stats()
    self.prev_started = started
    if self.trip:
      self.trip.update(v, engaged, dt, wall)

    if self.prev_engaged and not engaged and started:
      if self.trip:
        self.trip.disengagements += 1
      append_csv(DISENGAGE_CSV, DIS_FIELDS, {
        "time": datetime.fromtimestamp(wall).strftime("%Y-%m-%d %H:%M:%S"), "kph": round(v * MPS_TO_KPH),
        "gas": int(bool(cs.gasPressed)), "brake": int(bool(cs.brakePressed)), "steer": int(bool(cs.steeringPressed)),
        "standstill": int(bool(cs.standstill)), "trip_km": round(self.trip.dist_m / 1000.0, 2) if self.trip else 0})
    self.prev_engaged = engaged

    # steering-tune feedback
    min_mps = float(self.steer_cfg["min_speed_kph"]) / MPS_TO_KPH
    if started and lat_active and v >= min_mps:
      self.lat_km += v * dt / 1000.0
    pressed = bool(cs.steeringPressed)
    if pressed and not self.prev_pressed and started and lat_active and v >= min_mps and wall - self.last_override >= OVERRIDE_DEBOUNCE_S:
      self.last_override = wall
      kind, lat = classify_override(v, float(cs.steeringTorque), float(ctl.desiredCurvature),
                                    self.steer_cfg["straight_lat_accel"], bool(self.steer_cfg["invert_sign"]))
      append_csv(OVERRIDES_CSV, OVR_FIELDS, {
        "time": datetime.fromtimestamp(wall).strftime("%Y-%m-%d %H:%M:%S"), "kph": round(v * MPS_TO_KPH),
        "kind": kind, "lat_accel": round(lat, 3), "desired_curv": round(float(ctl.desiredCurvature), 6),
        "actual_curv": round(float(ctl.curvature), 6), "torque": round(float(cs.steeringTorque), 1),
        "model": self.trip.model if self.trip else active_model_name()})
    self.prev_pressed = pressed
    if now - self.stats_saved >= STATS_PERIOD:
      self.stats_saved = now
      self._save_stats()

    with self.lock:
      self.status.update({"onroad": started, "engaged": engaged, "steering": lat_active, "kph": round(v * MPS_TO_KPH),
                          "model": self.trip.model if self.trip else active_model_name(),
                          "trip_km": round(self.trip.dist_m / 1000.0, 2) if self.trip else 0.0,
                          "trip_engaged_pct": self.trip.row()["engaged_pct"] if self.trip else 0,
                          "updated": round(wall)})

  def run(self):
    import cereal.messaging as messaging
    sm = messaging.SubMaster(["deviceState", "carState", "selfdriveState", "carControl", "controlsState"])
    dt = 1.0 / LOOP_HZ
    while True:
      try:
        sm.update(int(dt * 1000))
        self.step(sm, dt, time.monotonic(), time.time())
      except Exception as e:
        print(f"telemetry: loop error: {e}", flush=True)
        time.sleep(1.0)

  def snapshot(self) -> dict:
    with self.lock:
      return dict(self.status)

  def overrides_summary(self) -> dict:
    return summarize_overrides(read_csv_all(OVERRIDES_CSV), self.lat_km)


def start() -> Telemetry:
  t = Telemetry()
  threading.Thread(target=t.run, daemon=True, name="sp-telemetry").start()
  return t
