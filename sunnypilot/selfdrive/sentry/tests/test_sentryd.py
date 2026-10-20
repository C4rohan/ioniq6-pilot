import io
import os

import numpy as np
import pytest

from openpilot.sunnypilot.selfdrive.sentry import sentryd as sd
from openpilot.sunnypilot.selfdrive.sentry import state

W, H, STRIDE = 1928, 1208, 2048
CFG = sd.merge_config({"enabled": True, "arm_delay_s": 120, "max_hours": 1, "min_voltage": 12.2})


def frame(blob=None, level=60):
  y = np.full((H, STRIDE), level, np.uint8)
  if blob:
    x0, y0 = blob
    y[y0:y0 + 200, x0:x0 + 200] = 230
  uv = np.full((H // 2, STRIDE), 128, np.uint8)
  uv[:, 0:W:2], uv[:, 1:W:2] = 90, 200        # reddish chroma
  return np.concatenate([y.ravel(), uv.ravel()]), STRIDE * H


def luma(**kw):
  return sd.luma_small(frame(**kw)[0], W, H, STRIDE)


def test_frame_conversion_and_png():
  data, uv_off = frame()
  assert 150 <= luma().shape[1] <= 165
  rgb = sd.nv12_to_rgb_small(data, W, H, STRIDE, uv_off)
  assert rgb.shape[2] == 3 and rgb[..., 0].mean() > rgb[..., 2].mean()
  Image = pytest.importorskip("PIL.Image")
  with Image.open(io.BytesIO(sd.png_bytes(rgb))) as im:
    assert np.array_equal(np.array(im.convert("RGB")), rgb)


def test_motion_detector():
  md = sd.MotionDetector(25, 0.015, warmup_frames=5)
  assert not any(md.update(luma())[0] for _ in range(16))          # warmup + static
  first, second = md.update(luma(blob=(800, 500))), md.update(luma(blob=(830, 520)))
  assert not first[0] and second[0]                                  # needs 2 frames
  md2 = sd.MotionDetector(25, 0.015, warmup_frames=2)
  for _ in range(5):
    md2.update(luma(level=60))
  assert not md2.update(luma(level=140))[0]                          # exposure shift ignored


def test_arming_lifecycle():
  a = sd.Arming()
  assert a.update(0, True, 12.6, "ok", CFG) == "driving"
  assert a.update(10, False, 12.6, "ok", CFG) == "arming"
  assert a.update(131, False, 12.6, "ok", CFG) == "armed"
  assert a.update(140, False, 12.6, "overheated", CFG) == "paused_hot"
  assert a.update(160, False, 12.6, "ok", CFG) == "armed"
  assert a.update(131 + 3601, False, 12.6, "ok", CFG) == "disarmed" and a.disarmed_reason == "max_time"
  assert a.update(9000, False, 12.6, "ok", CFG) == "disarmed"       # latched
  assert a.update(9100, True, 12.6, "ok", CFG) == "driving"


def test_low_voltage_is_low_passed():
  b = sd.Arming()
  b.update(0, False, 12.6, "ok", CFG)
  b.update(130, False, 12.6, "ok", CFG)
  assert b.update(132, False, 11.0, "ok", CFG) == "armed"           # brief dip ignored
  st = None
  for t in range(134, 600, 2):
    st = b.update(t, False, 11.9, "ok", CFG)
  assert st == "disarmed" and b.disarmed_reason == "low_voltage"


def test_config_sanitised():
  assert sd.merge_config({"cameras": ["rear"]})["cameras"] == ["road"]
  assert sd.merge_config({"fps": 99})["fps"] == 5.0
  assert sd.Arming().update(0, False, 12.6, "ok", sd.merge_config({})) == "off"


def test_state_file(tmp_path, monkeypatch):
  monkeypatch.setattr(state, "STATE_PATH", str(tmp_path / "state"))
  assert not state.sentry_armed()                                   # missing file -> disarmed
  state.write_state(True)
  assert state.sentry_armed()
  state.write_state(False)
  assert not state.sentry_armed()


def test_storage_cap_and_names(tmp_path):
  for i in range(10):
    p = tmp_path / f"f{i}.png"
    p.write_bytes(b"x" * 1000)
    os.utime(p, (i, i))
  sd.enforce_storage_cap(str(tmp_path), 4500)
  assert sorted(os.listdir(tmp_path)) == ["f6.png", "f7.png", "f8.png", "f9.png"]
  assert sd.SAFE_NAME.match("20260922-101010_road.png") and not sd.SAFE_NAME.match("../x.png")
