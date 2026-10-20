import json

from cereal import car

from openpilot.sunnypilot.selfdrive.ui import sound_volume as sv

A = car.CarControl.HUDControl.AudibleAlert


def test_scale_and_critical_floor(tmp_path, monkeypatch):
  monkeypatch.setattr(sv, "CONFIG_PATH", str(tmp_path / "sound.json"))
  u = sv.UserVolume()
  u.reload()
  assert u.factor(A.engage) == 1.0                                  # no config = stock
  (tmp_path / "sound.json").write_text(json.dumps({"volume": 0.4}))
  u._checked = -1e9
  u.reload()
  assert abs(u.factor(A.engage) - 0.4) < 1e-9
  for alert in (A.warningSoft, A.warningImmediate, A.promptDistracted, A.promptRepeat):
    assert u.factor(alert) == 1.0                                   # never quieter


def test_bad_config_is_stock():
  assert sv.scale_from_config({"volume": 0}) == sv.MIN_SCALE
  assert sv.scale_from_config({"volume": 5}) == 1.0
  assert sv.scale_from_config({"enabled": False, "volume": 0.2}) == 1.0
  assert sv.scale_from_config({"volume": "x"}) == 1.0
  assert sv.scale_from_config(None) == 1.0
