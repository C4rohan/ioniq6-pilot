"""Ioniq6 build: 'Steering controller' line on the phone page, end to end (controlsd selection -> status file -> /api/status)."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque import controller as ctl
from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque.tests.test_controller import parts
from openpilot.common.realtime import DT_CTRL
from openpilot.sunnypilot.selfdrive.settings_server import settings_server as ss


@pytest.fixture
def server(tmp_path, monkeypatch):
  monkeypatch.setattr(ctl, "STATUS", str(tmp_path / "sp_steering_controller.json"))
  monkeypatch.setattr(ctl, "CONFIG", str(tmp_path / "sunnypilot_ioniq6_steering.json"))
  monkeypatch.setattr(ss, "TELEMETRY", None)
  monkeypatch.setattr(ss, "CONFIGS", {**ss.CONFIGS, "ioniq6_steering": (ctl.CONFIG, True)})
  httpd = ThreadingHTTPServer(("127.0.0.1", 0), ss.Handler)
  threading.Thread(target=httpd.serve_forever, daemon=True).start()
  yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
  httpd.shutdown()
  httpd.server_close()


def status(base):
  with urllib.request.urlopen(base + "/api/status") as r:
    return json.loads(r.read())["steering_controller"]


def test_page_has_line_and_tab(server):
  base, _ = server
  with urllib.request.urlopen(base + "/") as r:
    page = r.read()
  assert b'id="stc"' in page and b'"ioniq6_steering"' in page


def test_switching_on_shows_next_drive_then_running(server):
  base, tmp = server
  assert status(base)["text"] == "sunnypilot standard"
  urllib.request.urlopen(urllib.request.Request(base + "/api/config?name=ioniq6_steering", data=b'{"enabled": true}', method="POST")).read()
  assert status(base)["text"] == "StarPilot Ioniq 6 (2023–24 tune), from your next drive"
  cp, cp_sp, ci, current = parts()
  assert isinstance(ctl.select_lateral_control(cp, cp_sp, ci, DT_CTRL, current), ctl.LatControlIoniq6)   # next drive starts
  s = status(base)
  assert s["level"] == "on" and s["text"].startswith("StarPilot Ioniq 6 (2023–24 tune), factor 3.66")
