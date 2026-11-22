"""
Calibration & steering-learning health for the Ioniq 6 custom build.

A bad or stale camera calibration makes openpilot misjudge where the lane and the curve
are, which feels exactly like a bad steering tune (it once looked like the custom torque
tune was failing on curve exits; recalibrating fixed it). This module turns what openpilot
already estimates into a plain health check, so calibration is ruled out first:

  liveCalibration       status, mount pitch/yaw vs calibrationd's limits
  liveParameters        steering-angle offset, steer ratio (paramsd)
  liveTorqueParameters  how far live torque learning has got, learned vs baked strength

Pure functions only (fed plain dicts) so they test without msgq; telemetry.py does the I/O.
It NEVER touches vehicle control.
"""
import json
import math
import os
import statistics

STATUS_NAMES = {0: "uncalibrated", 1: "calibrated", 2: "invalid", 3: "recalibrating"}

# calibrationd.py limits for the comma 3X (radians); outside them calibration goes invalid
PITCH_LIMITS = (-0.09074112085129739, 0.17)
YAW_LIMITS = (-0.06912048084718224, 0.06912048084718235)
NEAR_LIMIT = 0.75               # warn when the mount uses this much of the allowed range
ANGLE_OFFSET_WARN_DEG = 1.5     # paramsd's average steering-angle offset
DRIFT_YAW_DEG, DRIFT_PITCH_DEG = 0.5, 1.0
DRIFT_MIN_TRIPS = 3
LAF_DELTA_WARN = 0.25           # learned strength this far from the baked tune (torqued allows +-30%)
TORQUE_POINTS_NEEDED = 4000     # torqued MIN_POINTS_TOTAL

CAL_FIELDS = ["time", "event", "status", "cal_pct", "pitch_deg", "yaw_deg", "angle_offset_deg", "steer_ratio",
              "laf", "friction", "torque_pts", "torque_valid", "mode"]

# Written by controlsd at the start of each drive (ioniq6_torque/controller.py STATUS): which lateral
# controller it picked. Read as a plain file so this module stays free of controls imports.
STEERING_STATUS = os.path.join(os.environ.get("SUNNYPILOT_SHM", "/dev/shm"), "sp_steering_controller.json")
STARPILOT = "starpilot_ioniq6"

ORDER = {"ok": 0, "info": 0, "wait": 1, "warn": 2, "bad": 3}


def status_name(v) -> str:
  """pycapnp gives enums as strings; fakes and raw logs may give ints."""
  if isinstance(v, int):
    return STATUS_NAMES.get(v, "unknown")
  s = str(v)
  return s if s in STATUS_NAMES.values() else "unknown"


def _g(msg, name, default=0.0):
  try:
    return getattr(msg, name)
  except Exception:
    return default


def extract(cal, lp, ltp) -> dict:
  """Flatten the three messages into a JSON-able snapshot (angles in degrees)."""
  rpy = list(_g(cal, "rpyCalib", []) or [0.0, 0.0, 0.0])
  spread = list(_g(cal, "rpyCalibSpread", []) or [0.0, 0.0, 0.0])
  rpy += [0.0] * (3 - len(rpy))
  spread += [0.0] * (3 - len(spread))
  return {
    "status": status_name(_g(cal, "calStatus", 0)), "cal_pct": int(_g(cal, "calPerc", 0)),
    "pitch_deg": round(math.degrees(rpy[1]), 2), "yaw_deg": round(math.degrees(rpy[2]), 2),
    "roll_deg": round(math.degrees(rpy[0]), 2),
    "pitch_spread_deg": round(math.degrees(spread[1]), 2), "yaw_spread_deg": round(math.degrees(spread[2]), 2),
    "params_valid": bool(_g(lp, "valid", False)),
    "angle_offset_deg": round(float(_g(lp, "angleOffsetAverageDeg", 0.0)), 2),
    "steer_ratio": round(float(_g(lp, "steerRatio", 0.0)), 2),
    "stiffness": round(float(_g(lp, "stiffnessFactor", 0.0)), 3),
    "torque_valid": bool(_g(ltp, "liveValid", False)),
    "laf": round(float(_g(ltp, "latAccelFactorFiltered", 0.0)), 3),
    "friction": round(float(_g(ltp, "frictionCoefficientFiltered", 0.0)), 3),
    "torque_pts": int(_g(ltp, "totalBucketPoints", 0)),
  }


def read_steering_status(path: str | None = None) -> dict | None:
  try:
    with open(path or STEERING_STATUS) as f:
      d = json.load(f)
    return d if isinstance(d, dict) else None
  except Exception:
    return None


def tuning_mode(get_bool, get_float, steering: dict | None = None) -> dict:
  """Which steering path is in charge, from sunnypilot's toggles (mirrors torqued_ext / latcontrol_torque_ext).

  steering = what controlsd chose at drive start. While StarPilot's Ioniq 6 controller runs, it
  replaces NNLC, the sliders and live learning, with its own fixed tune.
  """
  if steering and steering.get("controller") == STARPILOT and not steering.get("failed"):
    return {"nnlc": False, "sliders": False, "live_learning": False, "controller": STARPILOT,
            "variant": str(steering.get("variant") or "2023-24"),
            "controller_laf": steering.get("lat_accel_factor"), "controller_friction": steering.get("friction")}
  nnlc = bool(get_bool("NeuralNetworkLateralControl"))
  enforce = bool(get_bool("EnforceTorqueControl"))
  sliders = enforce and bool(get_bool("CustomTorqueParams")) and bool(get_bool("TorqueParamsOverrideEnabled"))
  live = (not sliders) and (bool(get_bool("LiveTorqueParamsToggle")) if enforce else True)
  out = {"nnlc": nnlc, "sliders": sliders, "live_learning": live}
  if sliders:
    out["slider_laf"] = round(float(get_float("TorqueParamsOverrideLatAccelFactor")), 3)
    out["slider_friction"] = round(float(get_float("TorqueParamsOverrideFriction")), 3)
  if steering and steering.get("controller") == STARPILOT and steering.get("failed"):
    out["controller_failed"] = True
  return out


def mode_label(mode: dict | None) -> str:
  if not mode:
    return "?"
  if mode.get("controller") == STARPILOT:
    return f"StarPilot Ioniq 6 ({mode['variant']}) · fixed {mode['controller_laf']}/{mode['controller_friction']}"
  parts = ["NNLC" if mode["nnlc"] else "torque"]
  if mode.get("sliders"):
    parts.append(f"sliders {mode['slider_laf']}/{mode['slider_friction']}")
  else:
    parts.append("live learning" if mode.get("live_learning") else "fixed tune")
  return " · ".join(parts)


def offline_torque(cp) -> dict | None:
  """Baked tune from a CarParams reader (latAccelFactor, friction)."""
  try:
    t = cp.lateralTuning.torque
    return {"laf": round(float(t.latAccelFactor), 3), "friction": round(float(t.friction), 3)}
  except Exception:
    return None


def limit_use(x: float, lo: float, hi: float) -> float:
  """0 = centred, 1 = at the limit calibrationd allows on that side."""
  return x / hi if x >= 0 else x / lo


def drift(snap: dict, trips: list[dict]) -> dict | None:
  """Mount angle now vs the median of your last few drives. None until there's enough history."""
  ok = [r for r in trips if r.get("status") == "calibrated"][-5:]
  if len(ok) < DRIFT_MIN_TRIPS:
    return None
  try:
    yaw0 = statistics.median(float(r["yaw_deg"]) for r in ok)
    pitch0 = statistics.median(float(r["pitch_deg"]) for r in ok)
  except (KeyError, ValueError):
    return None
  return {"yaw": round(snap["yaw_deg"] - yaw0, 2), "pitch": round(snap["pitch_deg"] - pitch0, 2)}


def evaluate(snap: dict | None, offline: dict | None = None, mode: dict | None = None, history: list[dict] | None = None) -> dict:
  if not snap:
    return {"level": "wait", "verdict": "No calibration data yet. Drive once with the phone page enabled.", "issues": [], "snap": None}
  issues = []
  add = lambda level, text: issues.append({"level": level, "text": text})
  st = snap["status"]
  pitch_lim = f"{math.degrees(PITCH_LIMITS[0]):.1f}° to +{math.degrees(PITCH_LIMITS[1]):.1f}°"
  yaw_lim = f"±{math.degrees(YAW_LIMITS[1]):.1f}°"

  if st == "invalid":
    add("bad", f"Calibration INVALID: the device is mounted at an angle openpilot can't use (pitch {pitch_lim}, yaw {yaw_lim}). "
               "Remount it straighter, then reset calibration on the device.")
  elif st == "recalibrating":
    add("warn", "Recalibrating: the mount angle changed (bumped or remounted?). Steering stays off until it finishes. "
                "Drive straight on a highway.")
  elif st == "uncalibrated":
    add("wait", f"Calibrating: {snap['cal_pct']}%. Drive straight on a highway or main road to finish.")

  if st in ("calibrated", "recalibrating"):
    yaw_use = abs(snap["yaw_deg"]) / math.degrees(YAW_LIMITS[1])
    pitch_use = limit_use(math.radians(snap["pitch_deg"]), *PITCH_LIMITS)
    if yaw_use >= NEAR_LIMIT:
      add("warn", f"Mount yaw is {snap['yaw_deg']:+.1f}°, close to the {yaw_lim} limit. The device is turned left or right; "
                  "a small bump could make calibration invalid.")
    if pitch_use >= NEAR_LIMIT:
      add("warn", f"Mount pitch is {snap['pitch_deg']:+.1f}°, close to the limit ({pitch_lim}). The device is tilted up or down.")
    d = drift(snap, [r for r in (history or []) if r.get("event") == "trip"])
    if d and (abs(d["yaw"]) >= DRIFT_YAW_DEG or abs(d["pitch"]) >= DRIFT_PITCH_DEG):
      add("warn", f"Mount angle moved since your recent drives (yaw {d['yaw']:+.1f}°, pitch {d['pitch']:+.1f}°). "
                  "If the device was bumped or the mount is loose, fix it and reset calibration.")

  if snap["params_valid"] and abs(snap["angle_offset_deg"]) >= ANGLE_OFFSET_WARN_DEG:
    add("warn", f"Steering-angle offset is {snap['angle_offset_deg']:+.1f}°: the wheel sits off-centre when driving straight. "
                "Can be alignment, tyre pressure, or a road crown; check alignment if it stays this high.")

  if mode and mode.get("sliders"):
    add("info", f"Custom Tuning sliders are ON (factor {mode['slider_laf']}, friction {mode['slider_friction']}). "
                "They replace the branch tune and turn live learning off.")
  if mode and mode.get("nnlc"):
    add("info", "NNLC is on, so the neural model steers; the torque numbers below matter less.")
  starpilot = bool(mode and mode.get("controller") == STARPILOT)
  if starpilot:
    add("info", f"StarPilot's Ioniq 6 controller is steering with its own fixed tune (factor {mode['controller_laf']}, "
                f"friction {mode['controller_friction']}). The learned numbers below don't change its steering.")
  if mode and mode.get("controller_failed"):
    add("warn", "StarPilot's Ioniq 6 controller hit an error at the last drive; sunnypilot's controller took over.")

  progress = min(100, round(100 * snap["torque_pts"] / TORQUE_POINTS_NEEDED))
  if snap["torque_valid"] and offline and offline.get("laf"):
    delta = snap["laf"] / offline["laf"] - 1.0
    snap = {**snap, "laf_delta_pct": round(100 * delta)}
    if abs(delta) >= LAF_DELTA_WARN and not (mode and mode.get("sliders")) and not starpilot:
      add("warn", f"Learned steering strength {snap['laf']} is {100 * delta:+.0f}% from the baked tune {offline['laf']}. "
                  "The car is compensating a lot; if calibration is fine, the baked tune may be off.")
  elif not snap["torque_valid"] and (not mode or mode.get("live_learning")):
    add("wait", f"Steering learning: {progress}% of the data it needs. It uses the baked tune until then.")

  level = max((i["level"] for i in issues), key=lambda lv: ORDER[lv], default="ok")
  verdict = {"bad": "Calibration problem: fix this before judging the steering.",
             "warn": "Check the items below before blaming the steering tune.",
             "wait": "Still learning: steering feel may change over the next drives.",
             "ok": "Calibration and steering learning look healthy.",
             "info": "Calibration and steering learning look healthy."}[level]
  return {"level": "ok" if level == "info" else level, "verdict": verdict, "issues": issues, "snap": snap,
          "offline": offline, "mode": mode, "mode_label": mode_label(mode), "torque_progress": progress,
          "limits": {"pitch": [round(math.degrees(x), 2) for x in PITCH_LIMITS], "yaw": round(math.degrees(YAW_LIMITS[1]), 2)}}


def row(snap: dict, event: str, time_str: str, mode: dict | None) -> dict:
  return {"time": time_str, "event": event, "mode": mode_label(mode), **{k: snap.get(k, "") for k in CAL_FIELDS[2:-1]}}
