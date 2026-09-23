#!/usr/bin/env python3
"""
Sentry mode for the Ioniq 6 build: watch the cameras while parked and save
photos (plus an optional phone notification) when something moves.

Inspired by StarPilot's "parked motion capture". Never touches vehicle control.

How it works
  - Runs always (light 1 Hz loop). When the car has been parked for arm_delay_s
    and sentry is enabled, it writes "armed" to the shared state file; the
    process manager then starts camerad (and only camerad).
  - Pulls frames from the road and/or driver camera at a low rate, detects
    motion by comparing against an adaptive background, and on a hit saves a
    PNG snapshot from each camera to /data/sentry and notifies.
  - Disarms itself on low 12V (low-passed, default 12.2 V, above comma's own
    11.8 V cutoff), after max_hours, and pauses while the device is overheated.
    Disarm latches until the car is driven again.

Config: /data/sunnypilot_sentry.json   Output: /data/sentry/ (PNG, events.csv, status.json)
Note: the cameras have no night illumination while parked, so dark scenes see little.
"""
import csv
import json
import os
import re
import struct
import threading
import time
import urllib.request
import uuid
import zlib
from datetime import datetime

import numpy as np

from openpilot.sunnypilot.selfdrive.sentry.state import write_state

DATA = os.environ.get("SUNNYPILOT_DATA", "/data")
CONFIG_PATH = os.path.join(DATA, "sunnypilot_sentry.json")
NOTIFY_CONFIG = os.path.join(DATA, "sunnypilot_notify.json")
OUT_DIR = os.path.join(DATA, "sentry")
EVENTS_CSV = os.path.join(OUT_DIR, "events.csv")
STATUS_PATH = os.path.join(OUT_DIR, "status.json")

DEFAULT_CONFIG = {
  "enabled": False,
  "arm_delay_s": 120,          # wait after parking so you walking away isn't an event
  "cameras": ["road", "driver"],
  "fps": 2,
  "warmup_s": 10,              # let auto-exposure settle after the camera starts
  "pixel_threshold": 25,       # luma change (0-255) that counts as a changed pixel
  "sensitivity": 0.015,        # fraction of changed pixels that counts as motion
  "cooldown_s": 60,
  "max_hours": 12,
  "min_voltage": 12.2,
  "notify": True,
  "max_notify_per_hour": 10,
  "max_storage_mb": 500,
}
EVENT_FIELDS = ["time", "camera", "score", "files"]
VOLTAGE_TAU_S = 60.0
DETECT_WIDTH = 160
SNAP_WIDTH = 640
LIGHTING_FRAC = 0.5
CONFIRM_FRAMES = 2
BG_ALPHA = 0.05


# --------------------------------------------------------------------------- config

def merge_config(user) -> dict:
  cfg = dict(DEFAULT_CONFIG)
  if isinstance(user, dict):
    for k, v in user.items():
      if k in DEFAULT_CONFIG and not k.startswith("_"):
        cfg[k] = v
  cams = [c for c in (cfg.get("cameras") or []) if c in ("road", "driver")]
  cfg["cameras"] = cams or ["road"]
  cfg["fps"] = float(min(max(float(cfg["fps"]), 0.5), 5.0))
  return cfg


def load_json(path):
  try:
    with open(path) as f:
      return json.load(f)
  except Exception:
    return {}


# --------------------------------------------------------------------------- frames

def luma_small(data, width, height, stride, target_w=DETECT_WIDTH) -> np.ndarray:
  """Block-mean downscaled luma (float32) from an NV12 buffer."""
  step = max(1, int(width) // target_w)
  y = np.asarray(data)[: stride * height].reshape(height, stride)[:, :width]
  h, w = (height // step) * step, (width // step) * step
  return y[:h, :w].reshape(h // step, step, w // step, step).mean(axis=(1, 3)).astype(np.float32)


def nv12_to_rgb_small(data, width, height, stride, uv_offset, target_w=SNAP_WIDTH) -> np.ndarray:
  """Downscaled RGB (uint8) from an NV12 buffer, BT.601."""
  s = max(1, int(width) // target_w)
  buf = np.asarray(data)
  y = buf[: stride * height].reshape(height, stride)[:, :width][::s, ::s].astype(np.float32)
  uv = buf[uv_offset: uv_offset + stride * (height // 2)].reshape(height // 2, stride)[:, :width]
  ri = (np.arange(y.shape[0]) * s) // 2
  ci = ((np.arange(y.shape[1]) * s) // 2) * 2
  u = uv[np.ix_(ri, ci)].astype(np.float32) - 128.0
  v = uv[np.ix_(ri, ci + 1)].astype(np.float32) - 128.0
  r = y + 1.402 * v
  g = y - 0.344136 * u - 0.714136 * v
  b = y + 1.772 * u
  return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


def png_bytes(rgb: np.ndarray) -> bytes:
  """Minimal RGB PNG encoder (stdlib only)."""
  h, w, _ = rgb.shape
  raw = b"".join(b"\x00" + rgb[i].tobytes() for i in range(h))
  def chunk(tag, payload):
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
  return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
          + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- detection

class MotionDetector:
  def __init__(self, pixel_threshold=25, sensitivity=0.015, warmup_frames=20):
    self.pixel_threshold = float(pixel_threshold)
    self.sensitivity = float(sensitivity)
    self.warmup_frames = int(warmup_frames)
    self.bg = None
    self.n = 0
    self.hits = 0

  def update(self, luma: np.ndarray) -> tuple[bool, float, str]:
    x = luma - float(luma.mean())            # ignore uniform exposure shifts
    self.n += 1
    if self.bg is None or self.bg.shape != x.shape:
      self.bg = x.copy()
      return False, 0.0, "warmup"
    frac = float((np.abs(x - self.bg) > self.pixel_threshold).mean())
    if self.n <= self.warmup_frames:
      self.bg = 0.7 * self.bg + 0.3 * x
      return False, frac, "warmup"
    if frac > LIGHTING_FRAC:                 # whole scene changed: lights, not a person
      self.bg = x.copy()
      self.hits = 0
      return False, frac, "lighting"
    triggered = False
    if frac >= self.sensitivity:
      self.hits += 1
      if self.hits >= CONFIRM_FRAMES:
        triggered, self.hits = True, 0
    else:
      self.hits = 0
    self.bg = (1.0 - BG_ALPHA) * self.bg + BG_ALPHA * x
    return triggered, frac, ("motion" if triggered else "watch")


class Arming:
  """driving -> parked(arming) -> armed -> disarmed(latched) ; overheated pauses."""
  def __init__(self):
    self.parked_since = None
    self.armed_since = None
    self.disarmed_reason = None
    self.v_lpf = None
    self.hot = False
    self._last = None

  def update(self, now: float, started: bool, voltage_v, thermal: str, cfg: dict) -> str:
    dt = 1.0 if self._last is None else max(0.0, now - self._last)
    self._last = now
    if started:
      self.parked_since = self.armed_since = self.disarmed_reason = None
      self.v_lpf = None
      self.hot = False
      return "driving"
    if not cfg["enabled"]:
      self.armed_since = None
      return "off"
    if self.parked_since is None:
      self.parked_since = now
    if voltage_v is not None and voltage_v > 5.0:
      a = dt / (VOLTAGE_TAU_S + dt) if self.v_lpf is not None else 1.0
      self.v_lpf = voltage_v if self.v_lpf is None else (1 - a) * self.v_lpf + a * voltage_v
    if self.disarmed_reason:
      return "disarmed"
    if thermal in ("overheated", "critical"):
      self.hot = True
    elif thermal == "ok":
      self.hot = False
    if self.v_lpf is not None and self.v_lpf < float(cfg["min_voltage"]):
      self.disarmed_reason, self.armed_since = "low_voltage", None
      return "disarmed"
    if self.armed_since is None:
      if now - self.parked_since < float(cfg["arm_delay_s"]):
        return "arming"
      self.armed_since = now
    if now - self.armed_since >= float(cfg["max_hours"]) * 3600:
      self.disarmed_reason, self.armed_since = "max_time", None
      return "disarmed"
    return "paused_hot" if self.hot else "armed"


# --------------------------------------------------------------------------- output

def enforce_storage_cap(out_dir: str, max_bytes: int) -> None:
  try:
    files = sorted((os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".png")), key=os.path.getmtime)
    total = sum(os.path.getsize(f) for f in files)
    for f in files:
      if total <= max_bytes:
        break
      total -= os.path.getsize(f)
      os.remove(f)
  except OSError:
    pass


def _multipart(fields: dict, file_field: str, filename: str, data: bytes, ctype: str) -> tuple[bytes, str]:
  boundary = uuid.uuid4().hex
  parts = []
  for k, v in fields.items():
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
  parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
               f'Content-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n")
  parts.append(f"--{boundary}--\r\n".encode())
  return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def notify(caption: str, png: bytes | None) -> None:
  """Telegram photo, or text to a generic webhook. Fire-and-forget, never raises."""
  def _send():
    try:
      cfg = load_json(NOTIFY_CONFIG)
      if not cfg.get("enabled"):
        return
      kind = str(cfg.get("type", "")).lower()
      if kind == "telegram" and cfg.get("bot_token") and cfg.get("chat_id"):
        base = f"https://api.telegram.org/bot{cfg['bot_token']}"
        if png:
          body, ctype = _multipart({"chat_id": cfg["chat_id"], "caption": caption}, "photo", "sentry.png", png, "image/png")
          req = urllib.request.Request(base + "/sendPhoto", data=body, headers={"Content-Type": ctype})
        else:
          req = urllib.request.Request(base + "/sendMessage", data=json.dumps({"chat_id": cfg["chat_id"], "text": caption}).encode(),
                                       headers={"Content-Type": "application/json"})
      elif kind == "webhook" and cfg.get("webhook_url"):
        req = urllib.request.Request(cfg["webhook_url"], data=json.dumps({"text": caption}).encode(),
                                     headers={"Content-Type": "application/json"})
      else:
        return
      urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
      print(f"sentryd: notify failed: {e}", flush=True)
  threading.Thread(target=_send, daemon=True).start()


def write_status(obj: dict) -> None:
  try:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as f:
      json.dump(obj, f, indent=2)
    os.replace(tmp, STATUS_PATH)
  except OSError:
    pass


SAFE_NAME = re.compile(r"^[\w\-]+\.png$")


# --------------------------------------------------------------------------- main

def main():
  import cereal.messaging as messaging
  from msgq.visionipc import VisionIpcClient, VisionStreamType

  streams = {"road": VisionStreamType.VISION_STREAM_ROAD, "driver": VisionStreamType.VISION_STREAM_DRIVER}
  sm = messaging.SubMaster(["deviceState", "peripheralState"])
  arming = Arming()
  clients: dict = {}
  detectors: dict = {}
  cfg, cfg_mtime, cfg_checked = merge_config({}), None, 0.0
  last_event, notify_times, events_today, last_status = 0.0, [], 0, 0.0
  prev_armed = None
  write_state(False)
  os.makedirs(OUT_DIR, exist_ok=True)

  while True:
    now = time.monotonic()
    if now - cfg_checked > 5.0:
      cfg_checked = now
      try:
        m = os.path.getmtime(CONFIG_PATH)
      except OSError:
        m = None
      if m != cfg_mtime:
        cfg_mtime, cfg = m, merge_config(load_json(CONFIG_PATH))

    sm.update(0)
    started = bool(sm["deviceState"].started) if sm.seen["deviceState"] else True   # unknown -> never arm
    voltage = sm["peripheralState"].voltage / 1000.0 if sm.seen["peripheralState"] and sm["peripheralState"].voltage > 0 else None
    thermal = str(sm["deviceState"].thermalStatus) if sm.seen["deviceState"] else "ok"
    state = arming.update(now, started, voltage, thermal, cfg)
    armed = state == "armed"

    if armed != prev_armed:
      write_state(armed)     # process manager starts/stops camerad on this
      prev_armed = armed
      if not armed:
        clients.clear()
        detectors.clear()
      print(f"sentryd: {state}" + (f" ({arming.disarmed_reason})" if arming.disarmed_reason else ""), flush=True)

    if armed:
      for cam in cfg["cameras"]:
        if cam not in clients:
          c = VisionIpcClient("camerad", streams[cam], True)
          if c.connect(False):
            clients[cam] = c
            detectors[cam] = MotionDetector(cfg["pixel_threshold"], cfg["sensitivity"], int(cfg["warmup_s"] * cfg["fps"]))
      for cam, c in list(clients.items()):
        buf = c.recv(timeout_ms=50)
        if buf is None:
          continue
        hit, score, _ = detectors[cam].update(luma_small(buf.data, buf.width, buf.height, buf.stride))
        if hit and now - last_event >= float(cfg["cooldown_s"]):
          last_event = now
          stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
          files, first_png = [], None
          for scam, sc in clients.items():
            sbuf = buf if scam == cam else sc.recv(timeout_ms=100)
            if sbuf is None:
              continue
            png = png_bytes(nv12_to_rgb_small(sbuf.data, sbuf.width, sbuf.height, sbuf.stride, sbuf.uv_offset))
            name = f"{stamp}_{scam}.png"
            with open(os.path.join(OUT_DIR, name), "wb") as f:
              f.write(png)
            files.append(name)
            if scam == cam:
              first_png = png
          new = not os.path.exists(EVENTS_CSV)
          with open(EVENTS_CSV, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
            if new:
              w.writeheader()
            w.writerow({"time": stamp, "camera": cam, "score": round(score, 4), "files": " ".join(files)})
          events_today += 1
          enforce_storage_cap(OUT_DIR, int(cfg["max_storage_mb"]) * 1024 * 1024)
          notify_times = [t for t in notify_times if now - t < 3600]
          if cfg["notify"] and len(notify_times) < int(cfg["max_notify_per_hour"]):
            notify_times.append(now)
            notify(f"🚨 Sentry: motion on the {cam} camera at {datetime.now().strftime('%H:%M:%S')}", first_png)
          print(f"sentryd: event {stamp} cam={cam} score={score:.3f}", flush=True)
      time.sleep(1.0 / cfg["fps"])
    else:
      time.sleep(1.0)

    if now - last_status > 5.0:
      last_status = now
      write_status({"state": state, "reason": arming.disarmed_reason, "voltage": round(arming.v_lpf, 2) if arming.v_lpf else None,
                    "cameras": list(clients.keys()), "events_this_session": events_today,
                    "armed_for_min": round((now - arming.armed_since) / 60, 1) if arming.armed_since else 0,
                    "updated": round(time.time())})
    if state == "driving":
      events_today = 0


if __name__ == "__main__":
  main()
