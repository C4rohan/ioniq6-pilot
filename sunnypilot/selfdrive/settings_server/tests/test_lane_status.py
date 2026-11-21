"""Ioniq6 build: lane-centering line on the phone page, end to end (runtime -> status file -> /api/status)."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering import runtime as rt, status as st
from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering.tests.test_runtime import BASE, Clock, SM, cs, model
from openpilot.sunnypilot.selfdrive.settings_server import settings_server as ss


@pytest.fixture
def server(tmp_path, monkeypatch):
  monkeypatch.setattr(st, "STATUS", str(tmp_path / "sp_lane_centering.json"))
  monkeypatch.setattr(rt, "CONFIG", str(tmp_path / "sunnypilot_lane_centering.json"))
  monkeypatch.setattr(ss, "TELEMETRY", None)
  httpd = ThreadingHTTPServer(("127.0.0.1", 0), ss.Handler)
  threading.Thread(target=httpd.serve_forever, daemon=True).start()
  yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
  httpd.shutdown()
  httpd.server_close()


def get(url):
  with urllib.request.urlopen(url) as r:
    return r.read()


def test_page_has_status_line(server):
  base, _ = server
  assert b'id="lc"' in get(base + "/") and b"Lane centering:" in get(base + "/")


def test_off_when_not_configured(server):
  base, _ = server
  assert json.loads(get(base + "/api/status"))["lane_centering"]["text"] == "off"


def test_live_correction_reaches_the_phone(server, monkeypatch):
  base, tmp = server
  (tmp / "sunnypilot_lane_centering.json").write_text('{"enabled": true}')
  clock = Clock()
  monkeypatch.setattr(ss.time, "monotonic", clock)          # the server reads the same clock controlsd writes with
  r = rt.LaneCenteringRuntime(clock=clock)
  m, sm, CS, CC = model(left=-2.1, right=1.5), SM(), cs(), SimpleNamespace(latActive=True)
  for i in range(400):
    sm.logMonoTime["modelV2"] = 1 + i // 5
    clock.t += 0.01
    out = r.pre_limit(sm, CS, CC, m, BASE, BASE)
    if r.correcting:
      r.post_limit(m, BASE, out, BASE, CS.vEgo)
  lc = json.loads(get(base + "/api/status"))["lane_centering"]
  assert lc["state"] == "correcting" and lc["side"] == "left" and lc["text"].startswith("correcting ← left")
  clock.t += 5                                               # parked: controlsd stops writing
  assert json.loads(get(base + "/api/status"))["lane_centering"]["text"] == "on, starts when you drive"
