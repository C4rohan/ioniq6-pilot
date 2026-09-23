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

Also: GPS position on each takeover/disengagement and a sparse drive track (for the takeover map),
a per-model scorecard, and auto-saved dashcam clips after hard braking, hard steering takeovers, or
unexpected disengagements.

Files (under /data, never in the repo):
  sp_trips.csv, sp_disengagements.csv, sp_overrides.csv, sp_track.csv, sp_steer_stats.json
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
TRACK_CSV = os.path.join(DATA, "sp_track.csv")
CLIPS_CONFIG = os.path.join(DATA, "sunnypilot_clips.json")
CLIPS_DIR = os.path.join(DATA, "saved_clips")

LOOP_HZ = 10
CONFIG_PERIOD = 5.0
STATS_PERIOD = 60.0
MIN_TRIP_M = 200.0
MPS_TO_KPH = 3.6
OVERRIDE_DEBOUNCE_S = 2.0

TRIP_FIELDS = ["start", "end", "minutes", "km", "max_kph", "engaged_pct", "disengagements", "model"]
DIS_FIELDS = ["time", "kph", "gas", "brake", "steer", "standstill", "trip_km", "lat", "lon", "model"]
OVR_FIELDS = ["time", "kph", "kind", "lat_accel", "desired_curv", "actual_curv", "torque", "model", "lat", "lon"]
TRACK_FIELDS = ["trip", "time", "lat", "lon", "kph"]
TRACK_PERIOD_S = 15.0
TRACK_KEEP_TRIPS = 30

CLIPS_DEFAULTS = {"auto_save": True, "auto_brake_mps2": -4.0, "auto_takeover_lat": 1.5, "auto_takeover_kph": 50,
                  "auto_disengage_kph": 40, "auto_delay_s": 20, "auto_min_gap_s": 60, "auto_max_per_day": 20,
                  "notify_clips": True, "full_res": False, "max_storage_mb": 1000}
SCORECARD_MIN_KM = 20.0

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
  """Append a row; if the file was written with older columns, migrate it in place first."""
  if os.path.exists(path):
    with open(path, newline="") as f:
      header = next(csv.reader(f), [])
    if header != fields:
      old = read_csv_all(path)
      with open(path + ".tmp", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in old:
          w.writerow({k: r.get(k, "") for k in fields})
      os.replace(path + ".tmp", path)
  new = not os.path.exists(path)
  with open(path, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
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


def clips_config() -> dict:
  cfg = dict(CLIPS_DEFAULTS)
  user = load_json(CLIPS_CONFIG)
  if isinstance(user, dict):
    for k in CLIPS_DEFAULTS:
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


def _f(x, default=0.0):
  try:
    return float(x)
  except (TypeError, ValueError):
    return default


def model_scorecard(trips: list[dict], overrides: list[dict], min_km: float = SCORECARD_MIN_KM) -> dict:
  """Per driving model: km, engaged %, disengagements and takeovers per 100 km. Lower takeovers = better."""
  models: dict = {}
  def slot(m):
    return models.setdefault(m or "?", {"trips": 0, "km": 0.0, "minutes": 0.0, "eng_min": 0.0, "dis": 0,
                                        "under": 0, "over": 0, "straight": 0})
  for t in trips:
    d = slot(t.get("model"))
    mins = _f(t.get("minutes"))
    d["trips"] += 1
    d["km"] += _f(t.get("km"))
    d["minutes"] += mins
    d["eng_min"] += mins * _f(t.get("engaged_pct")) / 100.0
    d["dis"] += int(_f(t.get("disengagements")))
  for o in overrides:
    if o.get("kind") in ("under", "over", "straight"):
      slot(o.get("model"))[o["kind"]] += 1
  rows = []
  for m, d in models.items():
    km, ovr = d["km"], d["under"] + d["over"] + d["straight"]
    rows.append({"model": m, "trips": d["trips"], "km": round(km, 1),
                 "engaged_pct": round(100 * d["eng_min"] / d["minutes"]) if d["minutes"] > 0 else None,
                 "dis_per_100km": round(d["dis"] / km * 100, 1) if km >= 1 else None,
                 "ovr_per_100km": round(ovr / km * 100, 1) if km >= 1 else None,
                 "too_weak": d["under"], "too_strong": d["over"], "enough": km >= min_km})
  ranked = sorted((r for r in rows if r["enough"]), key=lambda r: (r["ovr_per_100km"], r["dis_per_100km"]))
  rest = sorted((r for r in rows if not r["enough"]), key=lambda r: -r["km"])
  if len(ranked) >= 2:
    verdict = f"{ranked[0]['model']} needs the fewest takeovers on your drives."
  elif len(ranked) == 1:
    verdict = f"Only {ranked[0]['model']} has enough data. Drive another model {min_km:.0f} km to compare."
  else:
    verdict = f"Drive at least {min_km:.0f} km on a model to score it."
  return {"rows": ranked + rest, "best": ranked[0]["model"] if len(ranked) >= 2 else None,
          "verdict": verdict, "min_km": min_km}


def _ll(r):
  lat, lon = _f(r.get("lat"), None), _f(r.get("lon"), None)
  if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
    return None
  return round(lat, 6), round(lon, 6)


def map_data(overrides: list[dict], disengagements: list[dict], track: list[dict], n_trips: int = 10) -> dict:
  pts = [{"lat": ll[0], "lon": ll[1], "kind": o.get("kind"), "kph": o.get("kph"), "time": o.get("time"),
          "lat_accel": o.get("lat_accel")} for o in overrides if (ll := _ll(o))]
  dis = [{"lat": ll[0], "lon": ll[1], "kph": d.get("kph"), "time": d.get("time")} for d in disengagements if (ll := _ll(d))]
  trips: dict = {}
  for r in track:
    if (ll := _ll(r)):
      trips.setdefault(r.get("trip", "?"), []).append([ll[0], ll[1]])
  keep = list(trips)[-n_trips:]
  return {"overrides": pts[-500:], "disengagements": dis[-300:], "tracks": [trips[k] for k in keep]}


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
    self.gps_service = "gpsLocation"
    self.pos = None                  # (lat, lon) of the latest valid fix
    self.last_track = 0.0
    self.clips_cfg = dict(CLIPS_DEFAULTS)
    self.pending_save = None         # (fire_at_wall, reason, detail)
    self.last_auto_save = -1e9
    self.auto_saves_day = (None, 0)
    self.last_button_wall = -1e9
    self.hard_brake_latched = False
    self.saver = self._default_saver

  def _reload_configs(self, now):
    if now - self.cfg_read < CONFIG_PERIOD:
      return
    self.cfg_read = now
    self.notify_cfg = load_json(NOTIFY_CONFIG)
    self.steer_cfg = steer_config()
    self.clips_cfg = clips_config()

  def _save_stats(self):
    try:
      write_json_atomic(STEER_STATS, {"lateral_km": round(self.lat_km, 3), "updated": round(time.time())})
    except OSError:
      pass

  def _default_saver(self, reason: str, detail: str) -> dict:
    from openpilot.system.hardware.hw import Paths
    from openpilot.sunnypilot.selfdrive.settings_server import clips
    c = self.clips_cfg
    return clips.save_clip(Paths.log_root(), CLIPS_DIR, bool(c["full_res"]), int(c["max_storage_mb"]), reason=reason, detail=detail)

  def _request_save(self, reason: str, detail: str, wall: float) -> None:
    c = self.clips_cfg
    if not c["auto_save"] or self.pending_save is not None or wall - self.last_auto_save < float(c["auto_min_gap_s"]):
      return
    day = datetime.fromtimestamp(wall).strftime("%Y-%m-%d")
    d, n = self.auto_saves_day
    if d == day and n >= int(c["auto_max_per_day"]):
      return
    self.auto_saves_day = (day, (n if d == day else 0) + 1)
    self.last_auto_save = wall
    self.pending_save = (wall + float(c["auto_delay_s"]), reason, detail)   # wait so the clip includes the aftermath

  def _fire_pending(self, wall: float, background: bool = True) -> None:
    if self.pending_save is None or wall < self.pending_save[0]:
      return
    _, reason, detail = self.pending_save
    self.pending_save = None
    def run():
      try:
        res = self.saver(reason, detail)
        if res.get("ok") and self.clips_cfg["notify_clips"]:
          send_notification(f"🎞️ Saved a dashcam clip: {res.get('label', reason)} ({detail}).")
      except Exception as e:
        print(f"telemetry: auto-save failed: {e}", flush=True)
    threading.Thread(target=run, daemon=True).start() if background else run()

  def step(self, sm, dt: float, now: float, wall: float) -> None:
    """One loop iteration; separated from run() so it can be tested with a fake SubMaster."""
    self._reload_configs(now)
    started = bool(sm["deviceState"].started)
    cs, ss, cc, ctl = sm["carState"], sm["selfdriveState"], sm["carControl"], sm["controlsState"]
    engaged, lat_active = bool(ss.enabled), bool(cc.latActive)
    v = float(cs.vEgo)
    try:                      # real SubMaster has no __contains__; `in sm` would probe sm[0]
      g = sm[self.gps_service]
    except (KeyError, IndexError, TypeError):
      g = None
    if g is not None and bool(getattr(g, "hasFix", False)) and (abs(float(g.latitude)) > 0 or abs(float(g.longitude)) > 0):
      self.pos = (float(g.latitude), float(g.longitude))
    if len(getattr(cs, "buttonEvents", []) or []) > 0:
      self.last_button_wall = wall
    lat_s, lon_s = (round(self.pos[0], 6), round(self.pos[1], 6)) if self.pos else ("", "")

    if started and not self.prev_started:
      self.trip = Trip(wall)
    if not started and self.prev_started and self.trip:
      if self.trip.dist_m >= MIN_TRIP_M:
        append_csv(TRIPS_CSV, TRIP_FIELDS, self.trip.row())
        if self.notify_cfg.get("notify_trips", True):
          send_notification(self.trip.summary())
      self.trip = None
      self._save_stats()
      self._trim_track()
    self.prev_started = started
    if self.trip:
      self.trip.update(v, engaged, dt, wall)

    if self.prev_engaged and not engaged and started:
      if self.trip:
        self.trip.disengagements += 1
      driver_input = bool(cs.gasPressed) or bool(cs.brakePressed) or bool(cs.steeringPressed) or (wall - self.last_button_wall) < 2.0
      if not driver_input and v * MPS_TO_KPH >= float(self.clips_cfg["auto_disengage_kph"]):
        self._request_save("unexpected_disengage", f"{round(v * MPS_TO_KPH)} km/h", wall)
      append_csv(DISENGAGE_CSV, DIS_FIELDS, {
        "time": datetime.fromtimestamp(wall).strftime("%Y-%m-%d %H:%M:%S"), "kph": round(v * MPS_TO_KPH),
        "gas": int(bool(cs.gasPressed)), "brake": int(bool(cs.brakePressed)), "steer": int(bool(cs.steeringPressed)),
        "standstill": int(bool(cs.standstill)), "trip_km": round(self.trip.dist_m / 1000.0, 2) if self.trip else 0,
        "lat": lat_s, "lon": lon_s, "model": self.trip.model if self.trip else active_model_name()})
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
        "model": self.trip.model if self.trip else active_model_name(), "lat": lat_s, "lon": lon_s})
      if abs(lat) >= float(self.clips_cfg["auto_takeover_lat"]) and v * MPS_TO_KPH >= float(self.clips_cfg["auto_takeover_kph"]):
        self._request_save("hard_takeover", f"{round(v * MPS_TO_KPH)} km/h, {abs(lat):.1f} m/s² curve", wall)
    self.prev_pressed = pressed

    # hard braking (driver or car), latched until it eases off
    a_ego = float(getattr(cs, "aEgo", 0.0))
    if started and a_ego <= float(self.clips_cfg["auto_brake_mps2"]) and v * MPS_TO_KPH >= 20:
      if not self.hard_brake_latched:
        self.hard_brake_latched = True
        self._request_save("hard_brake", f"{abs(a_ego):.1f} m/s² at {round(v * MPS_TO_KPH)} km/h", wall)
    elif a_ego > float(self.clips_cfg["auto_brake_mps2"]) / 2:
      self.hard_brake_latched = False

    # sparse drive track for the map
    if started and self.trip and self.pos and wall - self.last_track >= TRACK_PERIOD_S:
      self.last_track = wall
      append_csv(TRACK_CSV, TRACK_FIELDS, {"trip": datetime.fromtimestamp(self.trip.start).strftime("%Y%m%d-%H%M%S"),
                                           "time": datetime.fromtimestamp(wall).strftime("%H:%M:%S"),
                                           "lat": lat_s, "lon": lon_s, "kph": round(v * MPS_TO_KPH)})

    self._fire_pending(wall)
    if now - self.stats_saved >= STATS_PERIOD:
      self.stats_saved = now
      self._save_stats()

    with self.lock:
      self.status.update({"onroad": started, "engaged": engaged, "steering": lat_active, "kph": round(v * MPS_TO_KPH),
                          "model": self.trip.model if self.trip else active_model_name(),
                          "trip_km": round(self.trip.dist_m / 1000.0, 2) if self.trip else 0.0,
                          "trip_engaged_pct": self.trip.row()["engaged_pct"] if self.trip else 0,
                          "updated": round(wall)})

  def _trim_track(self) -> None:
    rows = read_csv_all(TRACK_CSV)
    trips = list(dict.fromkeys(r.get("trip") for r in rows))
    if len(trips) <= TRACK_KEEP_TRIPS:
      return
    keep = set(trips[-TRACK_KEEP_TRIPS:])
    with open(TRACK_CSV + ".tmp", "w", newline="") as f:
      w = csv.DictWriter(f, fieldnames=TRACK_FIELDS, extrasaction="ignore")
      w.writeheader()
      for r in rows:
        if r.get("trip") in keep:
          w.writerow(r)
    os.replace(TRACK_CSV + ".tmp", TRACK_CSV)

  def run(self):
    import cereal.messaging as messaging
    try:
      from openpilot.common.gps import get_gps_location_service
      from openpilot.common.params import Params
      self.gps_service = get_gps_location_service(Params())
    except Exception:
      self.gps_service = "gpsLocation"
    sm = messaging.SubMaster(["deviceState", "carState", "selfdriveState", "carControl", "controlsState", self.gps_service])
    # Measure real elapsed time: SubMaster.update() returns as soon as ANY message arrives
    # (carState is 100 Hz), so a fixed dt would inflate trip time/distance ~20x.
    period = 1.0 / LOOP_HZ
    last = time.monotonic()
    while True:
      t0 = time.monotonic()
      try:
        sm.update(0)
        now = time.monotonic()
        dt = min(max(now - last, 0.0), 1.0)
        last = now
        self.step(sm, dt, now, time.time())
      except Exception as e:
        print(f"telemetry: loop error: {e}", flush=True)
        time.sleep(1.0)
      time.sleep(max(0.0, period - (time.monotonic() - t0)))

  def snapshot(self) -> dict:
    with self.lock:
      return dict(self.status)

  def overrides_summary(self) -> dict:
    return summarize_overrides(read_csv_all(OVERRIDES_CSV), self.lat_km)

  def scorecard(self) -> dict:
    return model_scorecard(read_csv_all(TRIPS_CSV), read_csv_all(OVERRIDES_CSV))

  def map(self) -> dict:
    return map_data(read_csv_all(OVERRIDES_CSV), read_csv_all(DISENGAGE_CSV), read_csv_all(TRACK_CSV))


def start() -> Telemetry:
  t = Telemetry()
  threading.Thread(target=t.run, daemon=True, name="sp-telemetry").start()
  return t
