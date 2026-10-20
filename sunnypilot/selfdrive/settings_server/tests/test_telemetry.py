import csv
import os

import pytest

from openpilot.sunnypilot.selfdrive.settings_server.tests.helpers import fake_sm

W = 1_700_000_000.0


def new_telemetry(tm, **clip_cfg):
  t = tm.Telemetry()
  t.cfg_read = float("inf")                      # keep the test's config
  t.clips_cfg.update({"auto_delay_s": 5, "auto_min_gap_s": 60, "auto_max_per_day": 20, "notify_clips": False})
  t.clips_cfg.update(clip_cfg)
  calls = []
  t.saver = lambda reason, detail: calls.append((reason, detail)) or {"ok": True, "label": reason}
  orig = t._fire_pending
  t._fire_pending = lambda wall: orig(wall, background=False)
  return t, calls


class TestClassify:
  @pytest.mark.parametrize("torque,curv,expected", [
    (+150, +0.002, "under"), (-150, +0.002, "over"), (-150, -0.002, "under"), (+150, -0.002, "over"), (150, 0.0001, "straight"),
  ])
  def test_kinds(self, tm, torque, curv, expected):
    assert tm.classify_override(25.0, torque, curv)[0] == expected

  def test_invert_sign(self, tm):
    assert tm.classify_override(25.0, +150, +0.002, invert_sign=True)[0] == "over"


class TestSummary:
  def test_verdicts(self, tm):
    under = [{"kph": "80", "kind": "under"}]
    over = [{"kph": "80", "kind": "over"}]
    assert tm.summarize_overrides(under * 12 + over * 3, 200)["level"] == "under"
    assert tm.summarize_overrides(over * 12 + under * 2, 200)["level"] == "over"
    assert tm.summarize_overrides(under * 6 + over * 6, 200)["level"] == "ok"
    assert tm.summarize_overrides(under * 5, 50)["level"] == "wait"

  def test_buckets_and_rate(self, tm):
    rows = [{"kph": "80", "kind": "under"}] * 12 + [{"kph": "50", "kind": "over"}] * 3 + [{"kph": "110", "kind": "straight"}] * 4
    s = tm.summarize_overrides(rows, 200.0)
    by = {b["speed"]: b for b in s["buckets"]}
    assert by["70–100"]["under"] == 12 and by["40–70"]["over"] == 3 and by["100+"]["straight"] == 4
    assert s["per_100km"] == round(19 / 200 * 100, 1)


class TestStep:
  def test_overrides_logged_once_per_press_with_debounce(self, tm):
    t, _ = new_telemetry(tm)
    for i in range(50):
      t.step(fake_sm(), 0.2, i * 0.2, W + i * 0.2)
    assert abs(t.lat_km - 25.0 * 10 / 1000) < 1e-6
    t.step(fake_sm(pressed=True, torque=+120, dcurv=0.002), 0.2, 20, W + 20)
    t.step(fake_sm(pressed=True, torque=+120, dcurv=0.002), 0.2, 20.2, W + 20.2)   # held
    t.step(fake_sm(), 0.2, 20.4, W + 20.4)
    t.step(fake_sm(pressed=True, torque=-120, dcurv=0.002), 0.2, 21, W + 21)       # inside 2 s debounce
    t.step(fake_sm(), 0.2, 21.2, W + 21.2)
    t.step(fake_sm(pressed=True, torque=-120, dcurv=0.002), 0.2, 24, W + 24)
    rows = tm.read_csv_all(tm.OVERRIDES_CSV)
    assert [r["kind"] for r in rows] == ["under", "over"]
    assert rows[0]["kph"] == "90" and rows[0]["model"] == "TEST" and rows[0]["lat"] == "40.7"

  def test_ignored_when_not_steering_or_slow(self, tm):
    t, _ = new_telemetry(tm)
    t.step(fake_sm(), 0.2, 0, W)
    t.step(fake_sm(pressed=True, torque=100, lat=False), 0.2, 5, W + 5)
    t.step(fake_sm(lat=False), 0.2, 5.2, W + 5.2)
    t.step(fake_sm(pressed=True, torque=100, v=5.0), 0.2, 10, W + 10)
    assert tm.read_csv_all(tm.OVERRIDES_CSV) == []

  def test_trip_uses_measured_time(self, tm):
    """Trip time and distance come from the elapsed time passed in (the device loop measures it), not the loop count."""
    t, _ = new_telemetry(tm)
    t.step(fake_sm(started=False), 0.0, 0, W)
    for i in range(1, 61):
      t.step(fake_sm(v=20.0), 1.0, i, W + i)
    t.step(fake_sm(started=False, v=0.0), 0.0, 62, W + 62)
    row = tm.read_csv_all(tm.TRIPS_CSV)[-1]
    assert row["minutes"] == "1.0" and abs(float(row["km"]) - 1.2) < 0.03

  def test_lat_km_persisted(self, tm):
    t, _ = new_telemetry(tm)
    for i in range(20):
      t.step(fake_sm(), 1.0, i, W + i)
    t._save_stats()
    assert tm.Telemetry().lat_km == round(t.lat_km, 3)

  def test_no_gps_logs_blank_position(self, tm):
    t, _ = new_telemetry(tm)
    t.step(fake_sm(fix=False), 0.2, 0, W)
    t.step(fake_sm(fix=False, pressed=True, torque=150, dcurv=0.002), 0.2, 5, W + 5)
    assert tm.read_csv_all(tm.OVERRIDES_CSV)[-1]["lat"] == ""


class TestAutoSave:
  def test_hard_brake_latched_delayed_and_gapped(self, tm):
    t, calls = new_telemetry(tm)
    t.step(fake_sm(), 0.2, 0, W)
    for i in range(5):
      t.step(fake_sm(v=20, a=-5.0), 0.2, 1 + i * 0.2, W + 1 + i * 0.2)
    assert t.pending_save is not None and t.pending_save[1] == "hard_brake"
    t.step(fake_sm(v=15), 0.2, 3, W + 3)
    assert calls == []
    t.step(fake_sm(v=15), 0.2, 7, W + 7)
    assert len(calls) == 1 and calls[0][0] == "hard_brake"
    for i in range(5):
      t.step(fake_sm(v=20, a=-5.0), 0.2, 20 + i * 0.2, W + 20 + i * 0.2)
    assert t.pending_save is None                  # inside the 60 s gap

  @pytest.mark.parametrize("kw,expect", [
    ({"v": 20, "a": -2.5}, None),                                      # normal braking
    ({"v": 25, "pressed": True, "torque": 150, "dcurv": 0.003}, "hard_takeover"),
    ({"v": 25, "pressed": True, "torque": 150, "dcurv": 0.001}, None),  # gentle takeover
  ])
  def test_trigger_thresholds(self, tm, kw, expect):
    t, _ = new_telemetry(tm)
    t.step(fake_sm(), 0.2, 0, W)
    t.step(fake_sm(**kw), 0.2, 1, W + 1)
    assert (t.pending_save[1] if t.pending_save else None) == expect

  @pytest.mark.parametrize("cause,expect", [({}, "unexpected_disengage"), ({"brake": True}, None), ({"gas": True}, None)])
  def test_disengage(self, tm, cause, expect):
    t, _ = new_telemetry(tm)
    t.step(fake_sm(v=25), 0.2, 0, W)
    t.step(fake_sm(v=25, enabled=False, **cause), 0.2, 1, W + 1)
    assert (t.pending_save[1] if t.pending_save else None) == expect

  def test_cruise_button_cancel_is_not_unexpected(self, tm):
    t, _ = new_telemetry(tm)
    t.step(fake_sm(v=25, buttons=["cancel"]), 0.2, 0, W)
    t.step(fake_sm(v=25, enabled=False), 0.2, 1, W + 1)
    assert t.pending_save is None

  def test_disabled_and_daily_cap(self, tm):
    t, _ = new_telemetry(tm, auto_save=False)
    t.step(fake_sm(v=25), 0.2, 0, W)
    t.step(fake_sm(v=25, enabled=False), 0.2, 1, W + 1)
    assert t.pending_save is None
    t, calls = new_telemetry(tm, auto_max_per_day=2, auto_min_gap_s=0)
    for k in range(4):
      b = W + k * 100
      t.step(fake_sm(v=25), 0.2, k * 100, b)
      t.step(fake_sm(v=25, enabled=False), 0.2, k * 100 + 1, b + 1)
      t.step(fake_sm(v=25), 0.2, k * 100 + 10, b + 10)
    assert len(calls) == 2


class TestTrackAndMap:
  def test_track_every_15s_and_trim(self, tm):
    t, _ = new_telemetry(tm)
    t.step(fake_sm(started=False), 0.2, 0, W)
    for i in range(61):
      t.step(fake_sm(glat=40.7 + i * 1e-4), 1.0, i, W + i)
    assert len(tm.read_csv_all(tm.TRACK_CSV)) == 5
    with open(tm.TRACK_CSV, "w", newline="") as f:
      w = csv.DictWriter(f, fieldnames=tm.TRACK_FIELDS)
      w.writeheader()
      for k in range(40):
        w.writerow({"trip": f"T{k:02d}", "time": "x", "lat": "40", "lon": "-74", "kph": "50"})
    tm.Telemetry()._trim_track()
    assert len({r["trip"] for r in tm.read_csv_all(tm.TRACK_CSV)}) == 30

  def test_map_filters_bad_points(self, tm):
    md = tm.map_data([{"lat": "40.1", "lon": "-74.1", "kind": "under"}, {"lat": "", "lon": "", "kind": "over"}, {"lat": "0", "lon": "0", "kind": "over"}],
                     [{"lat": "40.2", "lon": "-74.2"}],
                     [{"trip": "A", "lat": "40.0", "lon": "-74.0"}, {"trip": "A", "lat": "40.01", "lon": "-74.01"}, {"trip": "B", "lat": "41", "lon": "-75"}])
    assert len(md["overrides"]) == 1 and len(md["disengagements"]) == 1
    assert [len(t) for t in md["tracks"]] == [2, 1]


class TestScorecard:
  TRIPS = [{"model": "PM", "km": "30", "minutes": "30", "engaged_pct": "90", "disengagements": "3"},
           {"model": "PM", "km": "20", "minutes": "20", "engaged_pct": "80", "disengagements": "1"},
           {"model": "NDMV2", "km": "40", "minutes": "40", "engaged_pct": "95", "disengagements": "2"},
           {"model": "GWMV9", "km": "8", "minutes": "8", "engaged_pct": "70", "disengagements": "1"}]
  OVR = [{"model": "PM", "kind": "under"}] * 10 + [{"model": "PM", "kind": "over"}] * 5 + [{"model": "NDMV2", "kind": "under"}] * 4

  def test_ranking_and_math(self, tm):
    sc = tm.model_scorecard(self.TRIPS, self.OVR)
    by = {r["model"]: r for r in sc["rows"]}
    assert by["PM"]["ovr_per_100km"] == 30.0 and by["PM"]["engaged_pct"] == 86
    assert by["NDMV2"]["dis_per_100km"] == 5.0
    assert sc["best"] == "NDMV2" and sc["rows"][0]["model"] == "NDMV2"
    assert by["GWMV9"]["enough"] is False and sc["rows"][-1]["model"] == "GWMV9"

  def test_not_enough_data(self, tm):
    assert "Drive another model" in tm.model_scorecard(self.TRIPS[:2], self.OVR)["verdict"]
    assert "at least 20 km" in tm.model_scorecard([], [])["verdict"]


def test_csv_header_migration(tm, tmp_path):
  p = tmp_path / "old.csv"
  with open(p, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["time", "kph", "kind", "lat_accel", "desired_curv", "actual_curv", "torque", "model"])
    w.writerow(["t0", "80", "under", "1.1", "0.001", "0.001", "150", "PM"])
  tm.append_csv(str(p), tm.OVR_FIELDS, {"time": "t1", "kph": "90", "kind": "over", "model": "PM", "lat": "40.5", "lon": "-74.5"})
  rows = tm.read_csv_all(str(p))
  assert list(rows[0].keys()) == tm.OVR_FIELDS
  assert rows[0]["kind"] == "under" and rows[0]["lat"] == "" and rows[1]["lat"] == "40.5"
  assert not os.path.exists(str(p) + ".tmp")
