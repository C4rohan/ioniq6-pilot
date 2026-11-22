"""
Drive stats for the phone page, modelled on StarPilot's Galaxy stats (MIT,
firestar5683/StarPilot galaxy/drive_stats.py at the commit in ioniq6_torque.STARPILOT_COMMIT).

Galaxy re-reads the device's route logs; this build already logs one row per drive
(telemetry.py -> sp_trips.csv), so the same numbers are computed from that instead. Definitions
follow Galaxy's:
  engaged     selfdriveState.enabled
  steering    carControl.latActive (openpilot steering, including steering-only/MADS)
  AOL         steering while not engaged ("always-on lateral")
  distracted  rising edges of the driverDistracted2 event (the second, louder distraction alert)
  unresponsive rising edges of driverUnresponsive3 (the final one, which disengages)
Personal records, the Monday-to-Sunday week and per-model totals match Galaxy's. Drives logged before
a field existed leave it blank and are left out of that stat, never counted as zero.

Pure functions over CSV rows; never touches vehicle control.
"""
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

DISTRACTED_EVENT = "driverDistracted2"
UNRESPONSIVE_EVENT = "driverUnresponsive3"
RECENT = 5
MIN_DAY_MINUTES = 10.0          # "most engaged day" ignores days with only a few minutes of driving


def _num(row, key):
  try:
    v = float(row.get(key, ""))
  except (TypeError, ValueError):
    return None
  return v if math.isfinite(v) else None


def _parse(row) -> dict | None:
  try:
    start = datetime.strptime(str(row.get("start", "")), "%Y-%m-%d %H:%M")
  except ValueError:
    return None
  km, minutes = _num(row, "km"), _num(row, "minutes")
  if km is None or minutes is None or km < 0 or minutes < 0:
    return None
  engaged = _num(row, "engaged_pct")
  distracted, unresponsive = _num(row, "distracted"), _num(row, "unresponsive")
  return {"start": start, "day": start.date(), "km": km, "minutes": minutes,
          "engaged_min": minutes * (engaged or 0.0) / 100.0, "engaged_pct": engaged,
          "steer_pct": _num(row, "steer_pct"), "aol_min": _num(row, "aol_min"),
          "distracted": None if distracted is None else int(distracted),
          "unresponsive": None if unresponsive is None else int(unresponsive),
          "disengagements": int(_num(row, "disengagements") or 0), "max_kph": _num(row, "max_kph"),
          "model": str(row.get("model") or "?")}


def _pct(part, whole):
  return round(100.0 * part / whole, 1) if whole > 0 else None


def _sums(drives):
  minutes = sum(d["minutes"] for d in drives)
  steer = [d for d in drives if d["steer_pct"] is not None]
  aol = [d for d in drives if d["aol_min"] is not None]
  steer_minutes = sum(d["minutes"] for d in steer)
  return {
    "drives": len(drives), "km": round(sum(d["km"] for d in drives), 1), "hours": round(minutes / 60.0, 1),
    "engaged_pct": _pct(sum(d["engaged_min"] for d in drives), minutes),
    "steer_pct": _pct(sum(d["minutes"] * d["steer_pct"] / 100.0 for d in steer), steer_minutes) if steer else None,
    "aol_min": round(sum(d["aol_min"] for d in aol), 1) if aol else None,
  }


def _records(drives):
  if not drives:
    return []
  ordered = sorted(drives, key=lambda d: d["start"])
  longest = max(drives, key=lambda d: d["km"])
  daily = defaultdict(lambda: [0.0, 0.0])                   # day -> [minutes, engaged minutes]
  weekly = defaultdict(float)
  for d in drives:
    daily[d["day"]][0] += d["minutes"]
    daily[d["day"]][1] += d["engaged_min"]
    weekly[d["day"] - timedelta(days=d["day"].weekday())] += d["km"]
  days = [(100.0 * e / m, day) for day, (m, e) in daily.items() if m >= MIN_DAY_MINUTES]
  best_day = max(days, default=None)
  best_week = max(((km, monday) for monday, km in weekly.items()), default=None)
  streak = best_streak = 0
  previous = None
  for day in sorted(daily):
    streak = streak + 1 if previous is not None and (day - previous).days == 1 else 1
    best_streak, previous = max(best_streak, streak), day
  undistracted = [d for d in drives if d["distracted"] == 0]
  best_quiet = max(undistracted, key=lambda d: d["minutes"]) if undistracted else None
  clean = best_clean = 0
  for d in ordered:
    if d["unresponsive"] is None:
      continue                                              # logged before this was recorded
    clean = clean + 1 if d["unresponsive"] == 0 else 0
    best_clean = max(best_clean, clean)
  any_attention = any(d["unresponsive"] is not None for d in drives)
  return [
    {"id": "longest_drive", "label": "Longest drive", "value": round(longest["km"], 1), "unit": "km",
     "when": longest["day"].isoformat()},
    {"id": "most_engaged_day", "label": "Most engaged day", "value": round(best_day[0], 1) if best_day else None, "unit": "%",
     "when": best_day[1].isoformat() if best_day else None},
    {"id": "best_week", "label": "Best week", "value": round(best_week[0], 1) if best_week else None, "unit": "km",
     "when": f"week of {best_week[1].isoformat()}" if best_week else None},
    {"id": "highest_streak", "label": "Highest streak", "value": best_streak, "unit": "days in a row", "when": None},
    {"id": "longest_undistracted", "label": "Longest drive with no distraction alert",
     "value": round(best_quiet["minutes"]) if best_quiet else None, "unit": "min",
     "when": best_quiet["day"].isoformat() if best_quiet else None},
    {"id": "clean_streak", "label": "Clean drive streak (no unresponsive alert)",
     "value": best_clean if any_attention else None, "unit": "drives", "when": None},
  ]


def _public(d):
  return {"start": d["start"].strftime("%Y-%m-%d %H:%M"), "km": round(d["km"], 1), "minutes": round(d["minutes"], 1),
          "engaged_pct": d["engaged_pct"], "steer_pct": d["steer_pct"], "aol_min": d["aol_min"],
          "distracted": d["distracted"], "unresponsive": d["unresponsive"], "disengagements": d["disengagements"],
          "max_kph": d["max_kph"], "model": d["model"]}


def summarize(trips: list[dict], overrides: list[dict] | None = None, today: date | None = None) -> dict:
  today = today or date.today()
  drives = [d for d in (_parse(r) for r in trips) if d]
  drives.sort(key=lambda d: d["start"], reverse=True)
  monday = today - timedelta(days=today.weekday())
  week_days = [monday + timedelta(days=n) for n in range(7)]
  week = [d for d in drives if monday <= d["day"] <= week_days[-1]]
  per_day = defaultdict(float)
  for d in week:
    per_day[d["day"]] += d["km"]
  totals = _sums(drives)
  km = sum(d["km"] for d in drives)
  dis = sum(d["disengagements"] for d in drives)
  totals["disengagements_per_100km"] = round(100.0 * dis / km, 1) if km >= 1 else None
  totals["takeovers_per_100km"] = round(100.0 * len(overrides) / km, 1) if overrides is not None and km >= 1 else None
  models = defaultdict(lambda: {"km": 0.0, "minutes": 0.0, "drives": 0})
  for d in drives:
    m = models[d["model"]]
    m["km"] += d["km"]
    m["minutes"] += d["minutes"]
    m["drives"] += 1
  return {
    "totals": totals,
    "past_week": _sums([d for d in drives if today - timedelta(days=6) <= d["day"] <= today]),
    "week": {**_sums(week), "days": [{"date": day.isoformat(), "label": day.strftime("%a"), "km": round(per_day[day], 1)}
                                     for day in week_days]},
    "last_drive": _public(drives[0]) if drives else None,
    "recent": [_public(d) for d in drives[:RECENT]],
    "records": _records(drives),
    "models": sorted(({"name": name, "km": round(v["km"], 1), "hours": round(v["minutes"] / 60.0, 1), "drives": v["drives"]}
                      for name, v in models.items()), key=lambda m: (-m["km"], m["name"])),
  }
