"""
Curve log: where the car sits in its lane through each curve, for the Ioniq 6 custom build.

"Sits right in curves" is hard to judge by feel, and it can come from the steering tune, from
lane centering, or from the mount (camera not centred). So for every curve openpilot steers
through, this records one row: left or right, speed, how sharp, how far the car was from the lane
centre at entry / middle / exit (from the model's own lane lines), whether steering delivered
the curvature it asked for, what lane centering did, and which steering controller was running.
Straight driving gets one row per drive too, as the baseline: an offset that is the same on
straights and in curves points at the mount, not the steering.

Sign conventions (checked against openpilot's code):
  curvature   positive = RIGHT curve (controlsd: curvature = -VM.calc_curvature(steer angle), and
              a positive steering angle is a left turn)
  lane lines  modelV2 y is metres to the RIGHT of the device; laneLines[1] = left line, [2] = right
  offset      written here as metres the car sits RIGHT of the lane centre (negative = left)

Pure (fed plain values) so it tests without msgq; telemetry.py does the I/O. Never touches control.
"""
import json
import math
import os

SHM = os.environ.get("SUNNYPILOT_SHM", "/dev/shm")
LANE_STATUS = os.path.join(SHM, "sp_lane_centering.json")       # lane_centering/status.py STATUS

FIELDS = ["time", "trip", "dir", "kph", "peak_lat", "seconds", "off_entry", "off_mid", "off_exit", "off_mean",
          "lane_pct", "steer_gap", "lc", "lc_pct", "lc_push", "controller", "takeover", "model"]

MIN_KPH = 40.0
START_LAT = 0.5          # desired lateral accel (m/s²) that opens a curve
KEEP_LAT = 0.3           # ...and keeps it open
END_HOLD_S = 1.0         # below KEEP_LAT this long closes it
MIN_PEAK_LAT = 0.7
MIN_CURVE_S = 3.0
STRAIGHT_LAT = 0.15
MIN_STRAIGHT_S = 30.0
MIN_LANE_PCT = 50        # offsets need good lane lines for at least this share of the curve
NEAR_X_M = 3.0           # where on the lane lines the car's position is read
LANE_PROB, LANE_STD = 0.6, 0.3
LANE_WIDTH = (2.6, 4.8)
STALE_S = 3.0
MIN_SUMMARY_CURVES = 3


def _interp(xs, ys, x):
  if len(xs) < 2 or len(xs) != len(ys) or not xs[0] <= x <= xs[-1]:
    return None
  for i in range(1, len(xs)):
    if x <= xs[i]:
      x0, x1 = xs[i - 1], xs[i]
      if x1 <= x0:
        return None
      return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0)
  return None


def lane_position(model) -> tuple[float, float] | None:
  """(metres right of lane centre, lane width) from modelV2, or None if the lines aren't clear."""
  try:
    probs, stds, lines = model.laneLineProbs, model.laneLineStds, model.laneLines
    if len(probs) < 3 or len(stds) < 3 or len(lines) < 3:
      return None
    if not all(LANE_PROB <= float(probs[i]) <= 1.0 and 0.0 <= float(stds[i]) <= LANE_STD for i in (1, 2)):
      return None
    left = _interp(list(lines[1].x), list(lines[1].y), NEAR_X_M)
    right = _interp(list(lines[2].x), list(lines[2].y), NEAR_X_M)
  except Exception:
    return None
  if left is None or right is None or not (math.isfinite(left) and math.isfinite(right)):
    return None
  width = right - left
  if not LANE_WIDTH[0] <= width <= LANE_WIDTH[1]:
    return None
  return -(left + right) / 2.0, width


def lane_centering_state(snap: dict | None, now: float) -> tuple[bool, float, float]:
  """(enabled, % of the last second correcting, signed push m/s², + = right) from its status file."""
  if not isinstance(snap, dict) or not isinstance(snap.get("mono"), (int, float)) or not 0.0 <= now - snap["mono"] <= STALE_S:
    return False, 0.0, 0.0
  pct = float(snap.get("correcting_pct") or 0.0)
  push = abs(float(snap.get("lat_accel") or 0.0)) * (-1.0 if float(snap.get("curvature") or 0.0) < 0 else 1.0)
  return bool(snap.get("enabled")) and not snap.get("failed"), pct, push if pct > 0 else 0.0


def read_json(path: str) -> dict | None:
  try:
    with open(path) as f:
      d = json.load(f)
    return d if isinstance(d, dict) else None
  except Exception:
    return None


def _mean(xs):
  return sum(xs) / len(xs) if xs else None


def _r(x, nd=3):
  return "" if x is None else round(x, nd)


class CurveTracker:
  """Feed one sample per telemetry tick; returns a finished row (dict) when a curve closes.

  A sample is (dt, v, |desired lat|, signed tracking gap, offset or None, lc pct, lc push, lc on, pressed).
  Samples that dip below KEEP_LAT wait in `pending`: if the curve tightens again they belong to
  it, if it stays straight for END_HOLD_S the curve closes without them.
  """

  def __init__(self):
    self.curve: dict | None = None
    self.pending: list = []
    self.straight = {"s": 0.0, "v": [], "off": [], "lc": False}

  def _open(self, sign):
    self.curve = {"sign": sign, "samples": []}
    self.pending = []

  def _close(self, ctx) -> dict | None:
    c, self.curve, self.pending = self.curve, None, []
    if c is None or not c["samples"]:
      return None
    sm = c["samples"]
    seconds = sum(x[0] for x in sm)
    peak = max(x[2] for x in sm)
    if seconds < MIN_CURVE_S or peak < MIN_PEAK_LAT:
      return None
    offs = [x[4] for x in sm]
    good_offs = [o for o in offs if o is not None]
    lane_pct = round(100 * len(good_offs) / len(offs))
    good = lane_pct >= MIN_LANE_PCT
    third = max(1, len(offs) // 3)
    def part(xs):
      return _r(_mean([o for o in xs if o is not None])) if good else ""
    pushes = [x[6] for x in sm if x[5] > 0]
    return {
      "dir": "right" if c["sign"] > 0 else "left", "kph": round(3.6 * _mean([x[1] for x in sm])), "peak_lat": _r(peak, 2),
      "seconds": round(seconds, 1), "off_entry": part(offs[:third]), "off_mid": part(offs[third:len(offs) - third] or offs),
      "off_exit": part(offs[-third:]), "off_mean": part(offs), "lane_pct": lane_pct,
      "steer_gap": _r(_mean([x[3] for x in sm])), "lc": int(any(x[7] for x in sm)),
      "lc_pct": round(_mean([x[5] for x in sm])), "lc_push": _r(_mean(pushes)) if pushes else 0.0,
      "takeover": int(any(x[8] for x in sm)), **ctx,
    }

  def update(self, *, dt: float, v: float, lat_active: bool, blinker: bool, pressed: bool, desired_curv: float,
             actual_curv: float, lane: tuple[float, float] | None, lc: tuple[bool, float, float], ctx: dict) -> dict | None:
    eligible = lat_active and not blinker and v * 3.6 >= MIN_KPH and math.isfinite(desired_curv) and math.isfinite(actual_curv)
    if not eligible:
      if blinker:                      # lane change or turn: not a curve the steering took on its own
        self.curve, self.pending = None, []
        return None
      return self._close(ctx)
    lat = desired_curv * v * v
    sign = 1 if lat > 0 else -1
    offset = lane[0] if lane else None
    def sample(s):
      return (dt, v, abs(lat), (actual_curv * v * v - lat) * s, offset, lc[1], lc[2], lc[0], pressed)

    done = None
    if self.curve is not None and abs(lat) >= KEEP_LAT and sign != self.curve["sign"]:
      done = self._close(ctx)          # S-bend: close this one, maybe open the next
    if self.curve is None:
      if abs(lat) >= START_LAT:
        self._open(sign)
        self.curve["samples"].append(sample(sign))
      elif abs(lat) < STRAIGHT_LAT:
        st = self.straight
        st["s"] += dt
        st["v"].append(v)
        st["lc"] = st["lc"] or lc[0]
        if offset is not None:
          st["off"].append(offset)
      return done
    if abs(lat) < KEEP_LAT:
      self.pending.append(sample(self.curve["sign"]))
      if sum(x[0] for x in self.pending) >= END_HOLD_S:
        return self._close(ctx)
      return None
    self.curve["samples"] += self.pending
    self.pending = []
    self.curve["samples"].append(sample(sign))
    return None

  def trip_end(self, ctx: dict) -> list[dict]:
    """Rows to write when the drive ends: any open curve, plus the straight-road baseline."""
    rows = [r for r in (self._close(ctx),) if r]
    st, self.straight = self.straight, {"s": 0.0, "v": [], "off": [], "lc": False}
    if st["s"] >= MIN_STRAIGHT_S and st["v"]:
      lane_pct = round(100 * len(st["off"]) / len(st["v"]))
      rows.append({"dir": "straight", "kph": round(3.6 * _mean(st["v"])), "peak_lat": "", "seconds": round(st["s"]),
                   "off_entry": "", "off_mid": "", "off_exit": "",
                   "off_mean": _r(_mean(st["off"])) if lane_pct >= MIN_LANE_PCT else "", "lane_pct": lane_pct,
                   "steer_gap": "", "lc": int(st["lc"]), "lc_pct": "", "lc_push": "", "takeover": "", **ctx})
    return rows


# --------------------------------------------------------------------------- phone page
def setup_label(row: dict) -> str:
  base = "StarPilot Ioniq 6 controller" if row.get("controller") == "starpilot_ioniq6" else "sunnypilot controller"
  return base + (" + lane centering" if str(row.get("lc")) == "1" else "")


def side_text(x: float | None) -> str:
  if x is None:
    return "no lane data"
  if abs(x) < 0.02:
    return "centred"
  return f"{abs(x):.2f} m {'right' if x > 0 else 'left'}"


def _num(r, k):
  try:
    v = float(r.get(k, ""))
    return v if math.isfinite(v) else None
  except (TypeError, ValueError):
    return None


def summarize(rows: list[dict], n_trips: int = 10) -> dict:
  """Per setup (controller + lane centering), per direction: where the car sits, over the last drives."""
  trips = list(dict.fromkeys(r.get("trip") for r in rows))[-n_trips:]
  recent = [r for r in rows if r.get("trip") in set(trips)]
  setups = {}
  for r in recent:
    setups.setdefault(setup_label(r), []).append(r)
  out = []
  for label, rs in setups.items():
    sides = {}
    for d in ("left", "right", "straight"):
      group = [r for r in rs if r.get("dir") == d]
      key = "off_mean" if d == "straight" else "off_mid"
      offs = [x for x in (_num(r, key) for r in group) if x is not None]
      entry = {"curves" if d != "straight" else "drives": len(group), "sits": _mean(offs), "text": side_text(_mean(offs))}
      if d != "straight":
        entry.update({
          "entry": _mean([x for x in (_num(r, "off_entry") for r in group) if x is not None]),
          "exit": _mean([x for x in (_num(r, "off_exit") for r in group) if x is not None]),
          "steer_gap": _mean([x for x in (_num(r, "steer_gap") for r in group) if x is not None]),
          "takeovers": sum(1 for r in group if str(r.get("takeover")) == "1"),
          "enough": len(offs) >= MIN_SUMMARY_CURVES,
        })
      sides[d] = entry
    out.append({"setup": label, "trips": len(dict.fromkeys(r.get("trip") for r in rs)), **sides})
  parts = []
  for s in out:
    bits = [f"{d} curves sit {s[d]['text']} ({s[d]['curves']})" for d in ("right", "left") if s[d]["enough"]]
    if s["straight"]["sits"] is not None:
      bits.append(f"straights {s['straight']['text']}")
    if bits:
      parts.append(f"{s['setup']}: " + "; ".join(bits))
  verdict = ". ".join(parts) + "." if parts else \
    f"Not enough curves yet: each side needs {MIN_SUMMARY_CURVES} curves above {MIN_KPH:.0f} km/h with clear lane lines."
  return {"verdict": verdict, "setups": out, "rows": len(recent), "trips": len(trips)}
