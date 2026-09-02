#!/usr/bin/env python3
"""
On-device settings server for sunnypilot custom features.

Adapted in spirit from FrogPilot's phone/browser settings management
(FrogAi/FrogPilot, MIT). Serves a tiny web page (stdlib http.server, no deps)
on the device's LAN so the JSON configs for the custom features can be edited
from a phone browser instead of SSH.

Scope is deliberately narrow and safe:
- only a fixed whitelist of config files under /data can be read or written
- writes must be valid JSON and are written atomically
- no code execution, no arbitrary file access, no path traversal
- read-only view of the weather status file

Security note: there is NO authentication (matching FrogPilot). Anyone on the
same network can change these settings. It binds to the device LAN only; use it
on a trusted network (your car hotspot / home WiFi). Disable by removing the
process_config.py entry.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from openpilot.common.swaglog import cloudlog

PORT = 8088

# name -> (path, writable)
CONFIGS = {
  "weather": ("/data/sunnypilot_weather.json", True),
  "accel":   ("/data/sunnypilot_accel.json", True),
  "weather_status": ("/data/sunnypilot_weather_status.json", False),
}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>sunnypilot settings</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#111;color:#eee}
header{padding:16px;background:#1c1c1e;font-size:20px;font-weight:600}
main{padding:16px;max-width:720px;margin:auto}
h2{margin:20px 0 6px;font-size:16px;color:#0a84ff}
textarea{width:100%;box-sizing:border-box;min-height:220px;background:#000;color:#0f0;
  border:1px solid #333;border-radius:8px;padding:10px;font-family:ui-monospace,monospace;font-size:13px}
button{background:#0a84ff;color:#fff;border:0;border-radius:8px;padding:12px 18px;font-size:15px;margin-top:8px}
.msg{margin-top:8px;font-size:14px;min-height:18px}
.ok{color:#30d158}.err{color:#ff453a}
pre{background:#000;border:1px solid #333;border-radius:8px;padding:10px;overflow:auto;font-size:13px}
small{color:#888}
</style></head><body>
<header>🌦️ sunnypilot settings</header><main>
<div id="editors"></div>
<h2>Weather status (read-only)</h2>
<pre id="status">loading…</pre>
<small>LAN only, no login. Changes apply within seconds; some need a reboot.</small>
</main>
<script>
const WRITABLE=["weather","accel"];
async function load(name){
  const r=await fetch("/api/config?name="+name); return r.ok?await r.text():"{}";
}
async function save(name,ta,msg){
  let body=ta.value;
  try{JSON.parse(body);}catch(e){msg.className="msg err";msg.textContent="Invalid JSON: "+e.message;return;}
  const r=await fetch("/api/config?name="+name,{method:"POST",body});
  const t=await r.text();
  msg.className="msg "+(r.ok?"ok":"err");msg.textContent=t;
}
(async()=>{
  const root=document.getElementById("editors");
  for(const name of WRITABLE){
    const h=document.createElement("h2");h.textContent=name;root.appendChild(h);
    const ta=document.createElement("textarea");ta.value=await load(name);root.appendChild(ta);
    const b=document.createElement("button");b.textContent="Save "+name;root.appendChild(b);
    const m=document.createElement("div");m.className="msg";root.appendChild(m);
    b.onclick=()=>save(name,ta,m);
  }
  async function status(){
    try{const r=await fetch("/api/config?name=weather_status");
      document.getElementById("status").textContent=r.ok?await r.text():"(none yet)";}catch(e){}
  }
  status();setInterval(status,5000);
})();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
  def log_message(self, *a):  # silence default stderr logging
    pass

  def _send(self, code, body, ctype="text/plain"):
    data = body.encode() if isinstance(body, str) else body
    self.send_response(code)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)

  def do_GET(self):
    u = urlparse(self.path)
    if u.path == "/":
      return self._send(200, PAGE, "text/html; charset=utf-8")
    if u.path == "/api/config":
      name = (parse_qs(u.query).get("name") or [""])[0]
      if name not in CONFIGS:
        return self._send(404, "unknown config")
      path, _ = CONFIGS[name]
      try:
        with open(path) as f:
          return self._send(200, f.read(), "application/json")
      except FileNotFoundError:
        return self._send(200, "{}", "application/json")
      except Exception as e:
        return self._send(500, f"read error: {e}")
    return self._send(404, "not found")

  def do_POST(self):
    u = urlparse(self.path)
    if u.path != "/api/config":
      return self._send(404, "not found")
    name = (parse_qs(u.query).get("name") or [""])[0]
    if name not in CONFIGS:
      return self._send(404, "unknown config")
    path, writable = CONFIGS[name]
    if not writable:
      return self._send(403, "read-only")
    try:
      length = int(self.headers.get("Content-Length", 0))
      raw = self.rfile.read(length).decode("utf-8")
      obj = json.loads(raw)  # reject anything that is not valid JSON
      if not isinstance(obj, dict):
        return self._send(400, "config must be a JSON object")
      tmp = path + ".tmp"
      with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
      os.replace(tmp, path)
      cloudlog.info(f"settings_server: wrote {name}")
      return self._send(200, "saved")
    except json.JSONDecodeError as e:
      return self._send(400, f"invalid JSON: {e}")
    except Exception as e:
      return self._send(500, f"write error: {e}")


def _serve():
  while True:
    try:
      httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
      cloudlog.info(f"settings_server: listening on :{PORT}")
      httpd.serve_forever()
    except Exception as e:
      cloudlog.warning(f"settings_server: {e}; retrying in 30s")
      time.sleep(30)


def main():
  # Never let this take down the manager.
  try:
    _serve()
  except Exception as e:
    cloudlog.exception(f"settings_server: fatal, exiting: {e}")


if __name__ == "__main__":
  main()
