#!/usr/bin/env python3
"""
On-device settings server + dashboard for the Ioniq 6 custom build.

Serves a phone-friendly page (stdlib http.server, no deps) on the device LAN:
  - live status (onroad, engaged, speed, model, trip)
  - editors for the custom-feature JSON configs
  - trip and disengagement logs
Runs the telemetry thread (see telemetry.py).

Safety scope is deliberately narrow:
  - only a fixed whitelist of /data config files can be read or written
  - writes must be valid JSON objects and are written atomically
  - secrets (API keys, tokens) are masked on read and preserved on write
  - no code execution, no arbitrary file access, no path traversal
Security: NO authentication (LAN only). Use on a trusted network. Not
auto-registered as a process; enabling it is a deliberate manual edit.
"""
import csv
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.selfdrive.settings_server import telemetry, clips

PORT = 8088
MASK = "••••••••"
SECRET_KEYS = {"bot_token", "chat_id", "webhook_url"}
DATA = telemetry.DATA
SENTRY_DIR = os.path.join(DATA, "sentry")
CLIPS_DIR = os.path.join(DATA, "saved_clips")
SENTRY_IMG = re.compile(r"^[\w\-]+\.png$")

# name -> (path, writable)
CONFIGS = {
  "notify":         (os.path.join(DATA, "sunnypilot_notify.json"), True),
  "reverse_cam":    (os.path.join(DATA, "sunnypilot_reverse_cam.json"), True),
  "sentry":         (os.path.join(DATA, "sunnypilot_sentry.json"), True),
  "sound":          (os.path.join(DATA, "sunnypilot_sound.json"), True),
  "steer_feedback": (os.path.join(DATA, "sunnypilot_steer_feedback.json"), True),
  "clips":          (os.path.join(DATA, "sunnypilot_clips.json"), True),
}

TELEMETRY: telemetry.Telemetry | None = None


def mask_secrets(obj):
  if isinstance(obj, dict):
    return {k: (MASK if k in SECRET_KEYS and obj[k] else mask_secrets(v)) for k, v in obj.items()}
  if isinstance(obj, list):
    return [mask_secrets(x) for x in obj]
  return obj


def unmask_secrets(new, existing):
  """Where the client sent back the mask, keep the value already on disk."""
  if isinstance(new, dict):
    out = {}
    for k, v in new.items():
      if k in SECRET_KEYS and v == MASK:
        out[k] = (existing or {}).get(k, "") if isinstance(existing, dict) else ""
      else:
        out[k] = unmask_secrets(v, (existing or {}).get(k) if isinstance(existing, dict) else None)
    return out
  return new


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ioniq 6 · sunnypilot</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--txt:#e8eaf0;--mut:#8b93a7;--acc:#4f8cff;--ok:#34d399;--warn:#fbbf24;--bad:#f87171}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif}
header{padding:14px 18px;background:linear-gradient(90deg,#1b2340,#0f1115);font-weight:700;font-size:18px;display:flex;justify-content:space-between;align-items:center}
header small{color:var(--mut);font-weight:400}
main{padding:14px;max-width:820px;margin:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:12px 0}
h2{margin:0 0 10px;font-size:14px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.stat{background:#0f1115;border:1px solid var(--line);border-radius:10px;padding:10px}
.stat b{display:block;font-size:22px;margin-top:2px}.stat span{color:var(--mut);font-size:12px}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.on{background:rgba(52,211,153,.15);color:var(--ok)}.off{background:rgba(248,113,113,.15);color:var(--bad)}.wx{background:rgba(251,191,36,.15);color:var(--warn)}
textarea{width:100%;min-height:170px;background:#0b0d11;color:#9fe3b8;border:1px solid var(--line);border-radius:10px;padding:10px;font:13px ui-monospace,SFMono-Regular,Menlo,monospace}
button{background:var(--acc);color:#fff;border:0;border-radius:10px;padding:10px 16px;font-size:14px;font-weight:600;margin-top:8px}
.msg{font-size:13px;min-height:16px;margin-top:6px}.ok{color:var(--ok)}.err{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--mut);font-weight:600}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.tab{padding:6px 12px;border-radius:999px;background:#0f1115;border:1px solid var(--line);color:var(--mut);font-size:13px}
.tab.act{color:var(--txt);border-color:var(--acc)}
pre{background:#0b0d11;border:1px solid var(--line);border-radius:10px;padding:10px;overflow:auto;font-size:12px;color:#c8cfe0}
footer{color:var(--mut);font-size:12px;text-align:center;padding:14px}
</style></head><body>
<header>🚗 Ioniq 6 · sunnypilot <small id="upd">—</small></header>
<main>
<div class="card"><h2>Live status</h2>
<div style="margin-bottom:10px"><span id="onroad" class="pill off">offroad</span> <span id="engaged" class="pill off">not engaged</span> </div>
<div class="grid">
<div class="stat"><span>Speed</span><b id="kph">0</b>km/h</div>
<div class="stat"><span>Trip</span><b id="tripkm">0.0</b>km</div>
<div class="stat"><span>Engaged (trip)</span><b id="tripeng">0</b>%</div>
<div class="stat"><span>Model</span><b id="model" style="font-size:15px">?</b></div>
</div></div>

<div class="card"><h2>Settings</h2>
<div class="tabs" id="tabs"></div><div id="editors"></div></div>

<div class="card"><h2>Steering feedback</h2><div id="sfv" style="margin-bottom:6px">loading…</div><div id="sfchart" style="overflow-x:auto"></div><div id="sfmeta" style="font-size:12px;color:var(--mut);margin-top:6px"></div></div>
<div class="card"><h2>Dashcam clips</h2><button id="savebtn">Save last 3 minutes</button><div id="savemsg" class="msg"></div><div id="clips" style="margin-top:8px"></div><div style="font-size:12px;color:var(--mut);margin-top:6px">Keeps the current minute and the 2 before it. Low-res road video (.ts) opens in VLC; full-quality footage stays on the device and in comma connect.</div></div>
<div class="card"><h2>Sentry</h2><div id="sen" style="margin-bottom:8px;color:var(--mut)">loading…</div><div id="senimgs" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px"></div></div>
<div class="card"><h2>Recent trips</h2><div id="trips">loading…</div></div>
<div class="card"><h2>Recent disengagements</h2><div id="dis">loading…</div></div>
</main>
<footer>LAN only · no login · secrets are masked (leave the dots to keep a saved key)</footer>
<script>
const CFGS=["sentry","sound","steer_feedback","clips","notify","reverse_cam"];
const $=id=>document.getElementById(id);
async function get(u){const r=await fetch(u);return r.ok?await r.text():"";}
async function status(){try{const s=JSON.parse(await get("/api/status"));
 $("onroad").className="pill "+(s.onroad?"on":"off");$("onroad").textContent=s.onroad?"onroad":"offroad";
 $("engaged").className="pill "+(s.engaged?"on":"off");$("engaged").textContent=s.engaged?"engaged":"not engaged";
 $("kph").textContent=s.kph;$("tripkm").textContent=s.trip_km;
 $("tripeng").textContent=s.trip_engaged_pct;$("model").textContent=s.model;
 $("upd").textContent=s.updated?new Date(s.updated*1000).toLocaleTimeString():"—";}catch(e){}}
function table(rows){if(!rows.length)return "<div style='color:var(--mut)'>none yet</div>";
 const h=Object.keys(rows[0]);return "<div style='overflow:auto'><table><tr>"+h.map(x=>"<th>"+x+"</th>").join("")+"</tr>"+
 rows.map(r=>"<tr>"+h.map(x=>"<td>"+(r[x]??"")+"</td>").join("")+"</tr>").join("")+"</table></div>";}
async function sentry(){try{const d=JSON.parse(await get("/api/sentry"));const s=d.status||{};
 $("sen").textContent=s.state?("State: "+s.state+(s.reason?" ("+s.reason+")":"")+(s.voltage?" · 12V "+s.voltage+" V":"")+" · events this park: "+(s.events_this_session||0)):"Sentry not running";
 const ims=[];(d.events||[]).forEach(e=>(e.files||"").split(" ").filter(Boolean).forEach(f=>ims.push([e.time,f])));
 $("senimgs").innerHTML=ims.slice(0,8).map(([t,f])=>"<a href='/sentry/img?f="+f+"' target=_blank><img src='/sentry/img?f="+f+"' style='width:100%;border-radius:8px;border:1px solid var(--line)'><div style='font-size:11px;color:var(--mut)'>"+t+"</div></a>").join("")||"<div style='color:var(--mut)'>no events yet</div>";}catch(e){}}
function sfchart(b){const W=320,H=150,P=24,cols=["under","over","straight"],colr={under:"#f5b841",over:"#f87171",straight:"#6b7489"};
 const mx=Math.max(1,...b.map(x=>x.under+x.over+x.straight));const bw=(W-P*2)/b.length;let g="";
 b.forEach((x,i)=>{let y=H-P;const xx=P+i*bw+bw*0.18,w=bw*0.64;cols.forEach(c=>{const h=(x[c]/mx)*(H-P*2);if(h>0){y-=h;g+=`<rect x="${xx}" y="${y}" width="${w}" height="${h}" fill="${colr[c]}" rx="2"/>`;}});
  const tot=x.under+x.over+x.straight;g+=`<text x="${xx+w/2}" y="${y-4}" fill="#c8cfe0" font-size="10" text-anchor="middle">${tot||""}</text>`;
  g+=`<text x="${xx+w/2}" y="${H-8}" fill="#8b93a7" font-size="10" text-anchor="middle">${x.speed}</text>`;});
 const leg=cols.map((c,i)=>`<rect x="${P+i*96}" y="4" width="10" height="10" fill="${colr[c]}" rx="2"/><text x="${P+i*96+14}" y="13" fill="#c8cfe0" font-size="11">${c==="under"?"too weak":c==="over"?"too strong":"straight"}</text>`).join("");
 return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:520px" role="img" aria-label="overrides by speed">${leg}<line x1="${P}" y1="${H-P}" x2="${W-P}" y2="${H-P}" stroke="#262b36"/>${g}</svg>`;}
async function steer(){try{const d=JSON.parse(await get("/api/overrides"));const col={under:"#f5b841",over:"#f87171",ok:"#34d399",wait:"#8b93a7"}[d.level];
 $("sfv").innerHTML=`<b style="color:${col}">${d.verdict}</b>`;$("sfchart").innerHTML=sfchart(d.buckets);
 $("sfmeta").textContent=`${d.totals.under} too-weak · ${d.totals.over} too-strong · ${d.totals.straight} straight · ${d.lateral_km} km steered`+(d.per_100km!=null?` · ${d.per_100km} overrides/100 km`:"");}catch(e){}}
async function clipsList(){try{const c=JSON.parse(await get("/api/clips"));$("clips").innerHTML=c.length?c.map(x=>`<div style="padding:6px 0;border-bottom:1px solid var(--line)"><b>${x.time}</b> · ${x.segments.length} min${x.native_preserve?"":" (copy only)"}<br>`+x.files.map(f=>`<a style="color:var(--acc);font-size:13px" href="/clips/file?c=${x.clip}&f=${f}">${f}</a>`).join(" · ")+"</div>").join(""):"<div style='color:var(--mut)'>no saved clips yet</div>";}catch(e){}}
async function logs(){try{$("trips").innerHTML=table(JSON.parse(await get("/api/trips")));
 $("dis").innerHTML=table(JSON.parse(await get("/api/disengagements")));
}catch(e){}}
async function editor(name){const box=$("editors");box.innerHTML="";
 document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("act",t.dataset.n===name));
 const ta=document.createElement("textarea");ta.value=(await get("/api/config?name="+name))||"{}";box.appendChild(ta);
 const b=document.createElement("button");b.textContent="Save "+name;box.appendChild(b);
 const m=document.createElement("div");m.className="msg";box.appendChild(m);
 b.onclick=async()=>{try{JSON.parse(ta.value)}catch(e){m.className="msg err";m.textContent="Invalid JSON: "+e.message;return}
  const r=await fetch("/api/config?name="+name,{method:"POST",body:ta.value});m.className="msg "+(r.ok?"ok":"err");m.textContent=await r.text();};}
CFGS.forEach(n=>{const t=document.createElement("span");t.className="tab";t.dataset.n=n;t.textContent=n;t.onclick=()=>editor(n);$("tabs").appendChild(t)});
$("savebtn").onclick=async()=>{$("savemsg").className="msg";$("savemsg").textContent="Saving…";try{const r=await fetch("/api/clip",{method:"POST"});const d=await r.json();
 $("savemsg").className="msg "+(d.ok?"ok":"err");$("savemsg").textContent=d.ok?`Saved ${d.segments.length} min (${d.files.length} files)`:d.error;clipsList();}catch(e){$("savemsg").className="msg err";$("savemsg").textContent="Save failed";}};
editor("sentry");status();logs();sentry();steer();clipsList();setInterval(steer,30000);setInterval(status,2000);setInterval(logs,15000);setInterval(sentry,10000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
  def log_message(self, *a):
    pass

  def _send(self, code, body, ctype="text/plain"):
    data = body.encode() if isinstance(body, str) else body
    self.send_response(code)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)

  def _json(self, obj, code=200):
    self._send(code, json.dumps(obj), "application/json")

  def do_GET(self):
    u = urlparse(self.path)
    if u.path == "/":
      return self._send(200, PAGE, "text/html; charset=utf-8")
    if u.path == "/api/status":
      return self._json(TELEMETRY.snapshot() if TELEMETRY else {})
    if u.path == "/api/trips":
      return self._json(telemetry.read_csv_tail(telemetry.TRIPS_CSV))
    if u.path == "/api/disengagements":
      return self._json(telemetry.read_csv_tail(telemetry.DISENGAGE_CSV))
    if u.path == "/api/overrides":
      if TELEMETRY:
        return self._json(TELEMETRY.overrides_summary())
      lat_km = float(telemetry.load_json(telemetry.STEER_STATS).get("lateral_km", 0.0) or 0.0)
      return self._json(telemetry.summarize_overrides(telemetry.read_csv_all(telemetry.OVERRIDES_CSV), lat_km))
    if u.path == "/api/clips":
      return self._json(clips.list_clips(CLIPS_DIR))
    if u.path == "/clips/file":
      q = parse_qs(u.query)
      c, fn = (q.get("c") or [""])[0], (q.get("f") or [""])[0]
      path = os.path.join(CLIPS_DIR, c, fn)
      if not clips.CLIP_RE.match(c) or not clips.FILE_RE.match(fn) or not os.path.isfile(path):
        return self._send(404, "not found")
      with open(path, "rb") as fh:
        data = fh.read()
      self.send_response(200)
      self.send_header("Content-Type", "video/mp2t" if fn.endswith(".ts") else "application/octet-stream")
      self.send_header("Content-Disposition", f'attachment; filename="{c}_{fn}"')
      self.send_header("Content-Length", str(len(data)))
      self.end_headers()
      self.wfile.write(data)
      return
    if u.path == "/api/sentry":
      status = telemetry.load_json(os.path.join(SENTRY_DIR, "status.json"))
      return self._json({"status": status, "events": telemetry.read_csv_tail(os.path.join(SENTRY_DIR, "events.csv"), 12)})
    if u.path == "/sentry/img":
      name = (parse_qs(u.query).get("f") or [""])[0]
      path = os.path.join(SENTRY_DIR, name)
      if not SENTRY_IMG.match(name) or not os.path.isfile(path):
        return self._send(404, "not found")
      with open(path, "rb") as fh:
        return self._send(200, fh.read(), "image/png")
    if u.path == "/api/config":
      name = (parse_qs(u.query).get("name") or [""])[0]
      if name not in CONFIGS:
        return self._send(404, "unknown config")
      path, writable = CONFIGS[name]
      try:
        with open(path) as f:
          raw = f.read()
      except FileNotFoundError:
        return self._send(200, "{}", "application/json")
      except Exception as e:
        return self._send(500, f"read error: {e}")
      if not writable:
        return self._send(200, raw, "application/json")
      try:
        return self._send(200, json.dumps(mask_secrets(json.loads(raw)), indent=2), "application/json")
      except Exception:
        return self._send(200, raw, "application/json")
    return self._send(404, "not found")

  def do_POST(self):
    u = urlparse(self.path)
    if u.path == "/api/clip":
      try:
        from openpilot.system.hardware.hw import Paths
        cfg = telemetry.load_json(CONFIGS["clips"][0])
        res = clips.save_clip(Paths.log_root(), CLIPS_DIR, bool(cfg.get("full_res", False)), int(cfg.get("max_storage_mb", 1000)))
        return self._json(res, 200 if res.get("ok") else 409)
      except Exception as e:
        return self._json({"ok": False, "error": f"save failed: {e}"}, 500)
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
      obj = json.loads(self.rfile.read(length).decode("utf-8"))
      if not isinstance(obj, dict):
        return self._send(400, "config must be a JSON object")
      obj = unmask_secrets(obj, telemetry.load_json(path))
      telemetry.write_json_atomic(path, obj)
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
  global TELEMETRY
  try:
    TELEMETRY = telemetry.start()
  except Exception as e:
    cloudlog.exception(f"settings_server: telemetry failed to start (continuing without it): {e}")
  try:
    _serve()
  except Exception as e:
    cloudlog.exception(f"settings_server: fatal, exiting: {e}")


if __name__ == "__main__":
  main()
