#!/usr/bin/env python3
"""
On-device settings server + dashboard for the Ioniq 6 custom build.

Serves a phone-friendly page (stdlib http.server, no deps) on the device LAN:
  - live status (onroad, engaged, speed, model, trip)
  - editors for the custom-feature JSON configs
  - trip and disengagement logs
  - drive stats (Galaxy-style: totals, this week, personal records, per-model, recent drives)
  - calibration & steering-learning health
  - curve log (where the car sits in its lane through left and right curves)
  - drive-data upload to the owner's private GitHub repo (uploader.py)
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
from openpilot.sunnypilot.selfdrive.settings_server import telemetry, clips, curves, stats, uploader

PORT = 8088
MASK = "••••••••"
SECRET_KEYS = {"bot_token", "chat_id", "webhook_url"}
DATA = telemetry.DATA
SENTRY_DIR = os.path.join(DATA, "sentry")
CLIPS_DIR = telemetry.CLIPS_DIR
SENTRY_IMG = re.compile(r"^[\w\-]+\.png$")
# The page is LAN-only with no login, so requests are checked instead:
#  - Host must be an IP, a single-label name, or a local name (.local/.lan/.home.arpa/.localhost).
#    A DNS-rebinding page arrives with its own public domain as Host and is refused.
#  - Writes must come from this page: no foreign Origin, no cross-site Sec-Fetch-Site, and a JSON
#    content type (which a cross-site form or no-cors fetch can't send without a preflight).
LOCAL_HOST = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9-]{1,63}|"
                        r"[A-Za-z0-9.-]{1,200}\.(?:local|lan|home\.arpa|localhost))(?::\d{1,5})?$")

# name -> (path, writable)
CONFIGS = {
  "notify":         (os.path.join(DATA, "sunnypilot_notify.json"), True),
  "reverse_cam":    (os.path.join(DATA, "sunnypilot_reverse_cam.json"), True),
  "sentry":         (os.path.join(DATA, "sunnypilot_sentry.json"), True),
  "sound":          (os.path.join(DATA, "sunnypilot_sound.json"), True),
  "steer_feedback": (os.path.join(DATA, "sunnypilot_steer_feedback.json"), True),
  "clips":          (os.path.join(DATA, "sunnypilot_clips.json"), True),
  "models":         (os.path.join(DATA, "sunnypilot_models.json"), True),
  "lane_centering": (os.path.join(DATA, "sunnypilot_lane_centering.json"), True),
  "ioniq6_steering": (os.path.join(DATA, "sunnypilot_ioniq6_steering.json"), True),
  "upload":         (os.path.join(DATA, "sunnypilot_upload.json"), True),
}

TELEMETRY: telemetry.Telemetry | None = None


def lane_centering_status() -> dict:
  """One line for the Live status card, from the file controlsd writes to /dev/shm (lane_centering/status.py)."""
  try:
    from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering.status import read_status
    return read_status(time.monotonic())
  except Exception:
    cloudlog.exception("lane centering status")
    return {"state": "unknown", "level": "mut", "text": "unknown"}


def steering_controller_status() -> dict:
  """Which steering controller controlsd picked (ioniq6_torque/controller.py)."""
  try:
    from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque.controller import read_status
    return read_status()
  except Exception:
    cloudlog.exception("steering controller status")
    return {"level": "mut", "text": "unknown"}


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
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
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
</div>
<div style="margin-top:10px;font-size:14px"><span style="color:var(--mut)">Lane centering:</span> <span id="lc">—</span></div>
<div style="margin-top:4px;font-size:14px"><span style="color:var(--mut)">Steering controller:</span> <span id="stc">—</span></div></div>

<div class="card"><h2>Drive stats</h2><div class="tabs" id="dstabs"></div><div id="dsbody">loading…</div>
<div style="font-size:12px;color:var(--mut);margin-top:6px">From your drive log on this device. Steering-only = openpilot steering while cruise isn't engaged. Attention alerts count openpilot's second distraction warning and its final "unresponsive" warning. Drives from before an update may not have every number.</div></div>
<div class="card"><h2>Settings</h2>
<div class="tabs" id="tabs"></div><div id="editors"></div></div>

<div class="card"><h2>Calibration &amp; steering health</h2><div id="calv" style="margin-bottom:8px">loading…</div>
<div class="grid" id="calstats"></div><div id="caliss" style="margin-top:8px"></div><div id="calhist" style="margin-top:8px"></div>
<div style="font-size:12px;color:var(--mut);margin-top:6px">A bad or stale calibration feels like a bad steering tune. Rule this out first. Updated every second while driving; offroad it shows the last drive.</div></div>
<div class="card"><h2>Curve log</h2><div id="cvv" style="margin-bottom:8px">loading…</div><div id="cvt"></div>
<div style="font-size:12px;color:var(--mut);margin-top:6px">Where the car sits in its lane through curves, from the model's lane lines (above 40 km/h, openpilot steering, no turn signal). Middle = the middle third of the curve. "Steered vs asked": negative means it turned less than it wanted, so it drifts to the outside. Straights are the baseline: the same offset everywhere points at the mount, not the steering. Last 10 drives · <a href="/api/curves.csv" style="color:var(--acc)">download CSV</a></div></div>
<div class="card"><h2>Data upload</h2><div id="upv">loading…</div><div id="upk"></div>
<div style="font-size:12px;color:var(--mut);margin-top:6px">Sends the curve log, calibration history and trip list (no GPS) to your private GitHub repo whenever the device is online. Set it up in the <b>upload</b> tab. The device makes its own key, and the private half never leaves the device.</div></div>
<div class="card"><h2>Steering feedback</h2><div id="sfv" style="margin-bottom:6px">loading…</div><div id="sfchart" style="overflow-x:auto"></div><div id="sfmeta" style="font-size:12px;color:var(--mut);margin-top:6px"></div></div>
<div class="card"><h2>Model scorecard</h2><div id="scv" style="margin-bottom:8px">loading…</div><div id="sct"></div><div style="font-size:12px;color:var(--mut);margin-top:6px">Ranked by your takeovers per 100 km (fewer is better), then disengagements. A model needs 20 km of your driving before it is ranked.</div></div>
<div class="card"><h2>Takeover map</h2><div id="map" style="height:320px;border-radius:10px;border:1px solid var(--line);background:#0b0d11"></div><div style="font-size:12px;color:var(--mut);margin-top:6px"><span style="color:#f5b841">●</span> too weak <span style="color:#f87171">●</span> too strong <span style="color:#8b93a7">●</span> straight <span style="color:#60a5fa">◆</span> disengagement · lines are your recent drives</div></div>
<div class="card"><h2>Dashcam clips</h2><button id="savebtn">Save last 3 minutes</button><div id="savemsg" class="msg"></div><div id="clips" style="margin-top:8px"></div><div style="font-size:12px;color:var(--mut);margin-top:6px">Keeps the current minute and the 2 before it. Low-res road video (.ts) opens in VLC; full-quality footage stays on the device and in comma connect.</div></div>
<div class="card"><h2>Sentry</h2><div id="sen" style="margin-bottom:8px;color:var(--mut)">loading…</div><div id="senimgs" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px"></div></div>
<div class="card"><h2>Recent trips</h2><div id="trips">loading…</div></div>
<div class="card"><h2>Recent disengagements</h2><div id="dis">loading…</div></div>
</main>
<footer>LAN only · no login · secrets are masked (leave the dots to keep a saved key)</footer>
<script>
const CFGS=["sentry","sound","steer_feedback","clips","notify","reverse_cam","models","lane_centering","ioniq6_steering","upload"];
const $=id=>document.getElementById(id);
const esc=x=>String(x??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function get(u){const r=await fetch(u);return r.ok?await r.text():"";}
async function status(){try{const s=JSON.parse(await get("/api/status"));
 $("onroad").className="pill "+(s.onroad?"on":"off");$("onroad").textContent=s.onroad?"onroad":"offroad";
 $("engaged").className="pill "+(s.engaged?"on":"off");$("engaged").textContent=s.engaged?"engaged":"not engaged";
 $("kph").textContent=s.kph;$("tripkm").textContent=s.trip_km;
 $("tripeng").textContent=s.trip_engaged_pct;$("model").textContent=s.model;
 const LV={on:"var(--ok)",warn:"var(--warn)",bad:"var(--bad)",mut:"var(--mut)"};
 const lc=s.lane_centering||{};$("lc").textContent=lc.text||"—";$("lc").style.color=LV[lc.level]||"";
 const sc=s.steering_controller||{};$("stc").textContent=sc.text||"—";$("stc").style.color=LV[sc.level]||"";
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
function spark(vals,color,label,unit,lim){const W=300,H=56,P=4;const v=vals.filter(x=>!isNaN(x));if(v.length<2)return "";
 let lo=Math.min(...v),hi=Math.max(...v);if(lim!=null){lo=Math.min(lo,-lim);hi=Math.max(hi,lim);}if(hi-lo<1e-6){hi+=1;lo-=1;}
 const x=i=>P+i*(W-2*P)/(vals.length-1),y=t=>H-P-(t-lo)/(hi-lo)*(H-2*P);
 const pts=vals.map((t,i)=>isNaN(t)?null:x(i)+","+y(t)).filter(Boolean).join(" ");
 const z=lim!=null?`<line x1="${P}" x2="${W-P}" y1="${y(lim)}" y2="${y(lim)}" stroke="#f87171" stroke-dasharray="3 3" stroke-opacity=".6"/><line x1="${P}" x2="${W-P}" y1="${y(-lim)}" y2="${y(-lim)}" stroke="#f87171" stroke-dasharray="3 3" stroke-opacity=".6"/>`:"";
 return `<div style="font-size:12px;color:var(--mut);margin-top:6px">${label}: <b style="color:var(--txt)">${v[v.length-1]}${unit}</b> (last ${vals.length} drives)</div><svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:520px">${z}<polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}"/></svg>`;}
async function calib(){try{const d=JSON.parse(await get("/api/calibration"));const col={bad:"#f87171",warn:"#fbbf24",wait:"#8b93a7",ok:"#34d399"}[d.level];
 $("calv").innerHTML=`<b style="color:${col}">${d.verdict}</b>`;const s=d.snap;
 if(!s){$("calstats").innerHTML="";$("caliss").innerHTML="";$("calhist").innerHTML="";return;}
 const st={calibrated:"on",recalibrating:"wx",uncalibrated:"wx",invalid:"off"}[s.status]||"wx";
 const stat=(k,v,sub)=>`<div class="stat"><span>${k}</span><b style="font-size:17px">${v}</b><span>${sub||""}</span></div>`;
 const lf=d.offline?`baked ${d.offline.laf}${s.laf_delta_pct!=null?" · "+(s.laf_delta_pct>0?"+":"")+s.laf_delta_pct+"%":""}`:"";
 $("calstats").innerHTML=stat("Calibration",`<span class="pill ${st}">${s.status}</span>`,s.status==="calibrated"?"":s.cal_pct+"%")+
  stat("Mount yaw",(s.yaw_deg>0?"+":"")+s.yaw_deg+"°","limit ±"+d.limits.yaw+"°")+
  stat("Mount pitch",(s.pitch_deg>0?"+":"")+s.pitch_deg+"°",`limit ${d.limits.pitch[0]}° to +${d.limits.pitch[1]}°`)+
  stat("Wheel offset",(s.angle_offset_deg>0?"+":"")+s.angle_offset_deg+"°",s.params_valid?"steer ratio "+s.steer_ratio:"estimating")+
  stat("Steering strength",s.torque_valid?s.laf:"learning",s.torque_valid?lf:d.torque_progress+"% of data")+
  stat("Steering mode",`<span style="font-size:14px">${d.mode_label}</span>`,"");
 const ic={bad:"⛔",warn:"⚠️",wait:"⏳",info:"ℹ️"};
 $("caliss").innerHTML=d.issues.map(i=>`<div style="padding:4px 0;font-size:13px">${ic[i.level]||""} ${i.text}</div>`).join("")+
  (d.events.length?"<div style='font-size:12px;color:var(--mut);margin-top:4px'>Mid-drive resets: "+d.events.map(e=>e.time+" "+e.event).join(" · ")+"</div>":"");
 const h=d.history,n=k=>h.map(r=>parseFloat(r[k]));
 $("calhist").innerHTML=h.length>=2?spark(n("yaw_deg"),"#4f8cff","Mount yaw","°",d.limits.yaw)+spark(n("pitch_deg"),"#a78bfa","Mount pitch","°")+
  spark(n("angle_offset_deg"),"#f5b841","Wheel offset","°")+spark(n("laf"),"#34d399","Learned steering strength",""):"";}catch(e){console.error("calibration",e)}}
function cvside(x){if(x==null)return "–";if(Math.abs(x)<0.02)return "centred";return Math.abs(x).toFixed(2)+" m "+(x>0?"right →":"← left");}
async function curvelog(){try{const d=JSON.parse(await get("/api/curves"));$("cvv").innerHTML="<b>"+d.verdict+"</b>";
 $("cvt").innerHTML=d.setups.map(s=>`<div style="margin-top:8px;font-size:13px"><b>${s.setup}</b> <span style="color:var(--mut)">· ${s.trips} drive${s.trips===1?"":"s"}</span></div>`+
  "<div style='overflow:auto'><table><tr><th></th><th>curves</th><th>entry</th><th>middle</th><th>exit</th><th>steered vs asked</th><th>takeovers</th></tr>"+
  ["right","left"].map(k=>{const r=s[k];return `<tr style="${r.enough?"":"color:var(--mut)"}"><td>${k} curves</td><td>${r.curves}</td><td>${cvside(r.entry)}</td><td><b>${cvside(r.sits)}</b></td><td>${cvside(r.exit)}</td><td>${r.steer_gap==null?"–":(r.steer_gap>0?"+":"")+r.steer_gap.toFixed(2)+" m/s²"}</td><td>${r.takeovers}</td></tr>`;}).join("")+
  `<tr><td>straights</td><td>${s.straight.drives} drive${s.straight.drives===1?"":"s"}</td><td></td><td><b>${cvside(s.straight.sits)}</b></td><td></td><td></td><td></td></tr></table></div>`).join("");}catch(e){console.error("curves",e)}}
async function upload(){try{const d=JSON.parse(await get("/api/upload"));const col={on:"#34d399",warn:"#fbbf24",bad:"#f87171",mut:"#8b93a7"}[d.level]||"#8b93a7";
 $("upv").innerHTML=`<b style="color:${col}">${esc(d.text)}</b>`;
 $("upk").innerHTML=d.pubkey?`<div style="font-size:12px;color:var(--mut);margin-top:8px">Device key (tap to select, then copy):</div><textarea id="upkey" readonly rows="3" style="width:100%;font-family:monospace;font-size:11px;background:#0b0d11;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px" onclick="this.select();try{document.execCommand('copy')}catch(e){}"></textarea>`:"";
 if(d.pubkey)$("upkey").value=d.pubkey;}catch(e){console.error("upload",e)}}
let DS=null,DSTAB="overview";
const dsn=(x,u)=>x==null?"–":x+(u?" "+u:"");
const dsh=m=>m==null?"–":(m>=60?Math.floor(m/60)+" h "+Math.round(m%60)+" min":Math.round(m)+" min");
function dsdrives(rows){if(!rows.length)return "<div style='color:var(--mut)'>no drives yet</div>";
 return "<div style='overflow:auto'><table><tr><th>start</th><th>km</th><th>time</th><th>engaged</th><th>steering</th><th>steering-only</th><th>alerts</th><th>model</th></tr>"+
  rows.map(r=>`<tr><td>${r.start}</td><td>${r.km}</td><td>${dsh(r.minutes)}</td><td>${dsn(r.engaged_pct,"%")}</td><td>${dsn(r.steer_pct,"%")}</td><td>${dsh(r.aol_min)}</td><td>${r.distracted==null?"–":r.distracted+" / "+r.unresponsive}</td><td>${esc(r.model)}</td></tr>`).join("")+"</table></div>";}
function dsweek(w){const W=320,H=120,P=18,mx=Math.max(1,...w.days.map(d=>d.km)),bw=(W-2*P)/7;
 const bars=w.days.map((d,i)=>{const h=(H-2*P)*d.km/mx,x=P+i*bw+4,y=H-P-h;return `<rect x="${x}" y="${y}" width="${bw-8}" height="${Math.max(h,1)}" rx="3" fill="#4f8cff"${d.km?"":" fill-opacity='.25'"}/><text x="${x+(bw-8)/2}" y="${H-4}" fill="#8b93a7" font-size="10" text-anchor="middle">${d.label}</text>`+(d.km?`<text x="${x+(bw-8)/2}" y="${y-3}" fill="#e8eaf0" font-size="9" text-anchor="middle">${Math.round(d.km)}</text>`:"");}).join("");
 return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:520px">${bars}</svg>`;}
function dsrender(){const d=DS;if(!d)return;const st=(k,v,sub)=>`<div class="stat"><span>${k}</span><b style="font-size:17px">${v}</b><span>${sub||""}</span></div>`;
 const t=d.totals,w=d.week,p=d.past_week;let h="";
 if(DSTAB==="overview")h=`<div class="grid">${st("Distance",dsn(t.km,"km"),t.drives+" drives")+st("Driving time",dsn(t.hours,"h"),"")+st("Engaged",dsn(t.engaged_pct,"%"),"of driving time")+
  st("openpilot steering",dsn(t.steer_pct,"%"),"of driving time")+st("Steering-only",dsh(t.aol_min),"steering without cruise")+
  st("Disengagements",dsn(t.disengagements_per_100km),"per 100 km")+st("Takeovers",dsn(t.takeovers_per_100km),"per 100 km")+st("Last 7 days",dsn(p.km,"km"),p.drives+" drives")}</div>`+
  (d.last_drive?`<div style="font-size:13px;margin-top:10px"><b>Last drive</b> ${d.last_drive.start}: ${d.last_drive.km} km in ${dsh(d.last_drive.minutes)}, ${dsn(d.last_drive.engaged_pct,"%")} engaged</div>`:"");
 if(DSTAB==="week")h=dsweek(w)+`<div class="grid" style="margin-top:6px">${st("This week",dsn(w.km,"km"),w.drives+" drives")+st("Time",dsn(w.hours,"h"),"")+st("Engaged",dsn(w.engaged_pct,"%"),"")+st("Steering-only",dsh(w.aol_min),"")}</div>`;
 if(DSTAB==="records")h=`<div class="grid">${d.records.map(r=>st(r.label,r.value==null?"–":r.value+" "+r.unit,r.when||"")).join("")||"<div style='color:var(--mut)'>no drives yet</div>"}</div>`;
 if(DSTAB==="models")h=d.models.length?"<div style='overflow:auto'><table><tr><th>model</th><th>km</th><th>hours</th><th>drives</th></tr>"+d.models.map(m=>`<tr><td>${esc(m.name)}</td><td>${m.km}</td><td>${m.hours}</td><td>${m.drives}</td></tr>`).join("")+"</table></div>":"<div style='color:var(--mut)'>no drives yet</div>";
 if(DSTAB==="drives")h=dsdrives(d.recent);
 $("dsbody").innerHTML=h;
 $("dstabs").innerHTML=[["overview","Overview"],["week","This week"],["records","Records"],["models","Models"],["drives","Recent drives"]].map(([k,l])=>`<span class="tab${k===DSTAB?" act":""}" onclick="DSTAB='${k}';dsrender()">${l}</span>`).join("");}
async function drivestats(){try{DS=JSON.parse(await get("/api/stats"));dsrender();}catch(e){console.error("stats",e)}}
async function scorecard(){try{const d=JSON.parse(await get("/api/scorecard"));$("scv").innerHTML="<b>"+d.verdict+"</b>";
 $("sct").innerHTML=d.rows.length?"<div style='overflow:auto'><table><tr><th>model</th><th>km</th><th>takeovers /100 km</th><th>disengage /100 km</th><th>engaged</th><th>weak / strong</th></tr>"+
  d.rows.map(r=>`<tr style="${r.model===d.best?"color:#34d399;font-weight:600":r.enough?"":"color:var(--mut)"}"><td>${r.model}${r.model===d.best?" ★":""}</td><td>${r.km}</td>`+
  `<td>${r.ovr_per_100km??"–"}</td><td>${r.dis_per_100km??"–"}</td><td>${r.engaged_pct!=null?r.engaged_pct+"%":"–"}</td><td>${r.too_weak} / ${r.too_strong}</td></tr>`).join("")+"</table></div>":"";}catch(e){}}
let MAP=null,LAYER=null,LASTN=-1;
async function takeoverMap(){try{const d=JSON.parse(await get("/api/map"));const el=$("map");const col={under:"#f5b841",over:"#f87171",straight:"#8b93a7"};
 const all=[...d.overrides.map(p=>[p.lat,p.lon]),...d.disengagements.map(p=>[p.lat,p.lon]),...d.tracks.flat()];
 if(!all.length){el.innerHTML="<div style='padding:14px;color:var(--mut)'>No GPS points yet — drive with openpilot steering.</div>";return;}
 if(window.L){const first=!MAP;if(first){MAP=L.map(el);L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:"© OpenStreetMap"}).addTo(MAP);}
  if(LAYER)LAYER.remove();LAYER=L.layerGroup().addTo(MAP);
  d.tracks.forEach(t=>L.polyline(t,{color:"#4f8cff",weight:3,opacity:.5}).addTo(LAYER));
  d.disengagements.forEach(p=>L.circleMarker([p.lat,p.lon],{radius:5,color:"#60a5fa",fillOpacity:.9}).bindPopup(`Disengaged · ${p.kph} km/h<br>${p.time}`).addTo(LAYER));
  d.overrides.forEach(p=>L.circleMarker([p.lat,p.lon],{radius:7,color:col[p.kind]||"#fff",fillColor:col[p.kind]||"#fff",fillOpacity:.85,weight:2})
   .bindPopup(`${p.kind==="under"?"Too weak":p.kind==="over"?"Too strong":"Straight"} · ${p.kph} km/h · ${p.lat_accel} m/s²<br>${p.time}`).addTo(LAYER));
  if(first||all.length!==LASTN){LASTN=all.length;const fit=()=>{MAP.invalidateSize();MAP.fitBounds(L.latLngBounds(all).pad(0.15));};
   fit();setTimeout(fit,300);}}   // fit on first load / new data only, so a refresh never undoes your zoom
 else{const la=all.map(p=>p[0]),lo=all.map(p=>p[1]);const a=Math.min(...la),b=Math.max(...la),c=Math.min(...lo),e=Math.max(...lo);
  const W=320,H=300,x=v=>12+(v-c)/((e-c)||1e-9)*(W-24),y=v=>H-12-(v-a)/((b-a)||1e-9)*(H-24);let g="";
  d.tracks.forEach(t=>g+=`<polyline fill="none" stroke="#4f8cff" stroke-opacity=".5" stroke-width="2" points="${t.map(p=>x(p[1])+","+y(p[0])).join(" ")}"/>`);
  d.overrides.forEach(p=>g+=`<circle cx="${x(p.lon)}" cy="${y(p.lat)}" r="5" fill="${col[p.kind]||"#fff"}"/>`);
  d.disengagements.forEach(p=>g+=`<rect x="${x(p.lon)-4}" y="${y(p.lat)-4}" width="8" height="8" fill="#60a5fa" transform="rotate(45 ${x(p.lon)} ${y(p.lat)})"/>`);
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="100%">${g}</svg><div style="font-size:11px;color:var(--mut);padding:4px">Offline: no basemap</div>`;}}catch(e){console.error("takeover map",e)}}
async function clipsList(){try{const c=JSON.parse(await get("/api/clips"));$("clips").innerHTML=c.length?c.map(x=>`<div style="padding:6px 0;border-bottom:1px solid var(--line)"><b>${x.time}</b> · ${x.label||"Saved"}${x.detail?" ("+x.detail+")":""} · ${x.segments.length} min${x.native_preserve?"":" (copy only)"}<br>`+x.files.map(f=>`<a style="color:var(--acc);font-size:13px" href="/clips/file?c=${x.clip}&f=${f}">${f}</a>`).join(" · ")+"</div>").join(""):"<div style='color:var(--mut)'>no saved clips yet</div>";}catch(e){}}
async function logs(){try{$("trips").innerHTML=table(JSON.parse(await get("/api/trips")));
 $("dis").innerHTML=table(JSON.parse(await get("/api/disengagements")));
}catch(e){}}
async function editor(name){const box=$("editors");box.innerHTML="";
 document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("act",t.dataset.n===name));
 const ta=document.createElement("textarea");ta.value=(await get("/api/config?name="+name))||"{}";box.appendChild(ta);
 const b=document.createElement("button");b.textContent="Save "+name;box.appendChild(b);
 const m=document.createElement("div");m.className="msg";box.appendChild(m);
 b.onclick=async()=>{try{JSON.parse(ta.value)}catch(e){m.className="msg err";m.textContent="Invalid JSON: "+e.message;return}
  const r=await fetch("/api/config?name="+name,{method:"POST",headers:{"Content-Type":"application/json"},body:ta.value});m.className="msg "+(r.ok?"ok":"err");m.textContent=await r.text();};}
CFGS.forEach(n=>{const t=document.createElement("span");t.className="tab";t.dataset.n=n;t.textContent=n;t.onclick=()=>editor(n);$("tabs").appendChild(t)});
$("savebtn").onclick=async()=>{$("savemsg").className="msg";$("savemsg").textContent="Saving…";try{const r=await fetch("/api/clip",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});const d=await r.json();
 $("savemsg").className="msg "+(d.ok?"ok":"err");$("savemsg").textContent=d.ok?`Saved ${d.segments.length} min (${d.files.length} files)`:d.error;clipsList();}catch(e){$("savemsg").className="msg err";$("savemsg").textContent="Save failed";}};
editor("sentry");status();logs();sentry();calib();setInterval(calib,5000);curvelog();setInterval(curvelog,30000);drivestats();setInterval(drivestats,60000);upload();setInterval(upload,15000);steer();clipsList();scorecard();takeoverMap();setInterval(steer,30000);setInterval(scorecard,60000);setInterval(takeoverMap,60000);setInterval(clipsList,30000);setInterval(status,2000);setInterval(logs,15000);setInterval(sentry,10000);
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

  def _host_ok(self) -> bool:
    return bool(LOCAL_HOST.match(self.headers.get("Host", "")))

  def _write_ok(self) -> str:
    """'' if this POST may change something, else why not."""
    origin = self.headers.get("Origin")
    if origin is not None and urlparse(origin).netloc != self.headers.get("Host", ""):
      return "cross-site request refused"
    if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
      return "cross-site request refused"
    if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
      return "expected application/json"
    return ""

  def do_GET(self):
    if not self._host_ok():
      return self._send(403, "unexpected Host")
    u = urlparse(self.path)
    if u.path == "/":
      return self._send(200, PAGE, "text/html; charset=utf-8")
    if u.path == "/api/status":
      return self._json({**(TELEMETRY.snapshot() if TELEMETRY else {}), "lane_centering": lane_centering_status(),
                         "steering_controller": steering_controller_status()})
    if u.path == "/api/trips":
      return self._json(telemetry.read_csv_tail(telemetry.TRIPS_CSV))
    if u.path == "/api/disengagements":
      return self._json(telemetry.read_csv_tail(telemetry.DISENGAGE_CSV))
    if u.path == "/api/overrides":
      if TELEMETRY:
        return self._json(TELEMETRY.overrides_summary())
      lat_km = float(telemetry.load_json(telemetry.STEER_STATS).get("lateral_km", 0.0) or 0.0)
      return self._json(telemetry.summarize_overrides(telemetry.read_csv_all(telemetry.OVERRIDES_CSV), lat_km))
    if u.path == "/api/calibration":
      return self._json(TELEMETRY.calibration() if TELEMETRY else telemetry.calibration_report(from_file=True))
    if u.path == "/api/stats":
      return self._json(TELEMETRY.drive_stats() if TELEMETRY else stats.summarize(
        telemetry.read_csv_all(telemetry.TRIPS_CSV), telemetry.read_csv_all(telemetry.OVERRIDES_CSV)))
    if u.path == "/api/upload":
      return self._json(uploader.read_status(config_path=CONFIGS["upload"][0]))
    if u.path == "/api/curves":
      return self._json(TELEMETRY.curves() if TELEMETRY else curves.summarize(telemetry.read_csv_all(telemetry.CURVES_CSV)))
    if u.path == "/api/curves.csv":
      try:
        with open(telemetry.CURVES_CSV, "rb") as fh:
          data = fh.read()
      except FileNotFoundError:
        data = (",".join(curves.FIELDS) + "\n").encode()
      self.send_response(200)
      self.send_header("Content-Type", "text/csv")
      self.send_header("Content-Disposition", 'attachment; filename="sp_curves.csv"')
      self.send_header("Content-Length", str(len(data)))
      self.end_headers()
      self.wfile.write(data)
      return
    if u.path == "/api/scorecard":
      return self._json(TELEMETRY.scorecard() if TELEMETRY else telemetry.model_scorecard(
        telemetry.read_csv_all(telemetry.TRIPS_CSV), telemetry.read_csv_all(telemetry.OVERRIDES_CSV)))
    if u.path == "/api/map":
      return self._json(TELEMETRY.map() if TELEMETRY else telemetry.map_data(telemetry.read_csv_all(telemetry.OVERRIDES_CSV),
        telemetry.read_csv_all(telemetry.DISENGAGE_CSV), telemetry.read_csv_all(telemetry.TRACK_CSV)))
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
    if not self._host_ok():
      return self._send(403, "unexpected Host")
    refused = self._write_ok()
    if refused:
      return self._send(403 if "cross-site" in refused else 415, refused)
    u = urlparse(self.path)
    if u.path == "/api/clip":
      try:
        from openpilot.system.hardware.hw import Paths
        cfg = telemetry.load_json(CONFIGS["clips"][0])
        res = clips.save_clip(Paths.log_root(), CLIPS_DIR, bool(cfg.get("full_res", False)), int(cfg.get("max_storage_mb", 1000)), reason="manual")
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


ENABLE_FILE = os.path.join(DATA, "sunnypilot_settings_enabled")
ENABLE_CHECK_S = 10.0


def enabled() -> bool:
  return os.path.exists(ENABLE_FILE)


def wait_until_enabled(check_s: float = ENABLE_CHECK_S, sleep=time.sleep) -> None:
  """The process ships registered in the branch, so it must stay idle (no port, no telemetry)
  until the owner opts in by creating the enable file on the device."""
  while not enabled():
    sleep(check_s)


def main():
  global TELEMETRY
  wait_until_enabled()
  cloudlog.info("settings_server: enabled, starting")
  try:
    TELEMETRY = telemetry.start()
  except Exception as e:
    cloudlog.exception(f"settings_server: telemetry failed to start (continuing without it): {e}")
  try:
    uploader.start()
  except Exception as e:
    cloudlog.exception(f"settings_server: uploader failed to start (continuing without it): {e}")
  try:
    _serve()
  except Exception as e:
    cloudlog.exception(f"settings_server: fatal, exiting: {e}")


if __name__ == "__main__":
  main()
