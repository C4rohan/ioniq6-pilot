import json

from openpilot.selfdrive.ui.sunnypilot.onroad import reverse_cam as rc


class Gear:
  def __init__(self, name):
    self.name = name

  def __str__(self):
    return self.name


def sm(gear):
  return {"carState": type("CS", (), {"gearShifter": Gear(gear)})()}


def test_reverse_view(tmp_path, monkeypatch):
  monkeypatch.setattr(rc, "CONFIG_PATH", str(tmp_path / "rc.json"))
  r = rc.ReverseCam()
  assert r.active(sm("reverse"))                                    # on by default
  assert not r.active(sm("drive")) and not r.active(sm("park"))
  assert not r.is_reverse({})                                       # missing carState -> normal HUD
  for cfg in ({"enabled": False}, {"hide_overlays": False}):
    (tmp_path / "rc.json").write_text(json.dumps(cfg))
    r._checked = -1e9
    assert not r.active(sm("reverse"))
