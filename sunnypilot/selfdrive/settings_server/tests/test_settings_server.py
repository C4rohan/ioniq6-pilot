import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from openpilot.sunnypilot.selfdrive.settings_server import settings_server as ss


@pytest.fixture
def server(tm, tmp_path, monkeypatch):
  monkeypatch.setattr(ss, "CONFIGS", {
    "notify": (str(tmp_path / "notify.json"), True),
    "sentry": (str(tmp_path / "sentry.json"), True),
    "clips": (str(tmp_path / "clips.json"), True),
  })
  monkeypatch.setattr(ss, "SENTRY_DIR", str(tmp_path / "sentry"))
  monkeypatch.setattr(ss, "CLIPS_DIR", str(tmp_path / "saved_clips"))
  monkeypatch.setattr(ss, "TELEMETRY", None)
  os.makedirs(tmp_path / "sentry")
  (tmp_path / "sentry" / "20260101-000000_road.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
  (tmp_path / "secret.png").write_bytes(b"SECRET")
  httpd = ThreadingHTTPServer(("127.0.0.1", 0), ss.Handler)
  th = threading.Thread(target=httpd.serve_forever, daemon=True)
  th.start()
  yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
  httpd.shutdown()
  httpd.server_close()


def req(url, data=None, method=None):
  r = urllib.request.Request(url, data=data.encode() if data else None, method=method)
  try:
    with urllib.request.urlopen(r) as resp:
      return resp.status, resp.read()
  except urllib.error.HTTPError as e:
    with e:
      return e.code, e.read()


def test_page_and_json_endpoints(server):
  base, _ = server
  code, body = req(base + "/")
  assert code == 200
  for marker in (b"Steering feedback", b"Model scorecard", b"Takeover map", b"Dashcam clips", b"<h2>Sentry</h2>"):
    assert marker in body
  for ep in ("/api/overrides", "/api/scorecard", "/api/map", "/api/clips", "/api/trips", "/api/sentry"):
    code, body = req(base + ep)
    assert code == 200 and json.loads(body) is not None, ep


def test_secrets_masked_and_preserved(server):
  base, tmp = server
  assert req(base + "/api/config?name=notify", json.dumps({"enabled": True, "bot_token": "REAL"}), "POST")[0] == 200
  code, body = req(base + "/api/config?name=notify")
  assert json.loads(body)["bot_token"] == ss.MASK and b"REAL" not in body
  req(base + "/api/config?name=notify", json.dumps({"enabled": False, "bot_token": ss.MASK}), "POST")
  with open(tmp / "notify.json") as f:
    saved = json.load(f)
  assert saved == {"enabled": False, "bot_token": "REAL"}


@pytest.mark.parametrize("path,data,code", [
  ("/api/config?name=notify", "[1]", 400),
  ("/api/config?name=notify", "{bad", 400),
  ("/api/config?name=../../etc/passwd", "{}", 404),
])
def test_config_validation(server, path, data, code):
  base, _ = server
  assert req(base + path, data, "POST")[0] == code


@pytest.mark.parametrize("path", [
  "/sentry/img?f=../secret.png", "/sentry/img?f=%2E%2E%2Fsecret.png", "/sentry/img?f=x.txt",
  "/clips/file?c=..&f=..%2F..%2Fnotify.json", "/api/config?name=../../etc/passwd",
])
def test_traversal_blocked(server, path):
  base, _ = server
  assert req(base + path)[0] == 404


def test_sentry_image_served(server):
  base, _ = server
  code, body = req(base + "/sentry/img?f=20260101-000000_road.png")
  assert code == 200 and body.startswith(b"\x89PNG")


def test_save_clip_endpoint(server, monkeypatch):
  base, tmp = server
  seg = tmp / "realdata" / "00000001--aaaa--0"
  seg.mkdir(parents=True)
  (seg / "qcamera.ts").write_bytes(b"x" * 10)
  monkeypatch.setenv("LOG_ROOT", str(tmp / "realdata"))
  code, body = req(base + "/api/clip", "", "POST")
  res = json.loads(body)
  assert code == 200 and res["ok"] and res["reason"] == "manual"


def test_idle_until_enabled(tmp_path, monkeypatch):
  """The process ships in the branch; it must not open a port until the owner opts in."""
  monkeypatch.setattr(ss, "ENABLE_FILE", str(tmp_path / "sunnypilot_settings_enabled"))
  sleeps = []

  def fake_sleep(s):
    sleeps.append(s)
    if len(sleeps) == 3:
      (tmp_path / "sunnypilot_settings_enabled").touch()
  ss.wait_until_enabled(sleep=fake_sleep)
  assert len(sleeps) == 3 and ss.enabled()
