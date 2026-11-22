"""Ioniq6 build: the phone page refuses writes from other sites and requests for foreign host names."""
import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from openpilot.sunnypilot.selfdrive.settings_server import settings_server as ss


@pytest.fixture
def server(tmp_path, monkeypatch):
  monkeypatch.setitem(ss.CONFIGS, "lane_centering", (str(tmp_path / "lc.json"), True))
  monkeypatch.setattr(ss, "TELEMETRY", None)
  httpd = ThreadingHTTPServer(("127.0.0.1", 0), ss.Handler)
  threading.Thread(target=httpd.serve_forever, daemon=True).start()
  yield httpd.server_address[1], tmp_path / "lc.json"
  httpd.shutdown()
  httpd.server_close()


def request(port, method, path, headers=None, body=None):
  c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
  c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
  for k, v in (headers or {}).items():
    c.putheader(k, v)
  data = body.encode() if body is not None else b""
  if method == "POST":
    c.putheader("Content-Length", str(len(data)))
  c.endheaders(data)
  r = c.getresponse()
  return r.status, r.read().decode()


BODY = '{"enabled": true, "offset_m": 0.3}'
JSON = {"Content-Type": "application/json"}


class TestWrites:
  def test_page_itself_can_save(self, server):
    port, path = server
    host = f"192.168.1.20:{port}"
    assert request(port, "POST", "/api/config?name=lane_centering", {"Host": host, "Origin": f"http://{host}", **JSON}, BODY)[0] == 200
    assert path.exists()

  def test_cli_without_origin_can_save(self, server):
    port, path = server
    assert request(port, "POST", "/api/config?name=lane_centering", {"Host": f"192.168.1.20:{port}", **JSON}, BODY)[0] == 200

  @pytest.mark.parametrize("headers,code", [
    ({"Origin": "https://evil.example", **JSON}, 403),                       # another site's fetch
    ({"Origin": "null", **JSON}, 403),                                       # sandboxed iframe / file page
    ({"Sec-Fetch-Site": "cross-site", **JSON}, 403),
    ({"Content-Type": "text/plain"}, 415),                                   # what a no-cors fetch or form can send
    ({}, 415),
  ])
  def test_cross_site_writes_refused(self, server, headers, code):
    port, path = server
    assert request(port, "POST", "/api/config?name=lane_centering", {"Host": f"192.168.1.20:{port}", **headers}, BODY)[0] == code
    assert not path.exists()

  def test_clip_button_needs_same_checks(self, server):
    port, _ = server
    assert request(port, "POST", "/api/clip", {"Host": f"192.168.1.20:{port}", "Origin": "https://evil.example", **JSON}, "{}")[0] == 403


class TestHost:
  @pytest.mark.parametrize("host", ["192.168.43.1:8088", "10.0.0.5", "[fe80::1]:8088", "localhost:8088", "comma-1a2b3c:8088",
                                    "comma.local:8088", "tici.lan"])
  def test_local_names_allowed(self, server, host):
    port, _ = server
    assert request(port, "GET", "/api/curves", {"Host": host})[0] == 200

  @pytest.mark.parametrize("host", ["evil.example:8088", "rebind.attacker.com", "", "192.168.1.20.evil.com"])
  def test_dns_rebinding_names_refused(self, server, host):
    port, path = server
    assert request(port, "GET", "/api/curves", {"Host": host} if host else {})[0] == 403
    assert request(port, "POST", "/api/config?name=lane_centering", {"Host": host, **JSON} if host else JSON, BODY)[0] == 403
    assert not path.exists()
