"""
Save-clip for the Ioniq 6 build: keep the last few minutes of dashcam footage.

Two layers, because either alone can fail:
  1. Native: set the same `user.preserve` xattr that loggerd sets for the device's
     own bookmark button; the deleter then keeps that segment + the 2 before it
     (full quality, all cameras). Caveat: the deleter caches xattrs per process,
     and only honours the 5 most recent preserved segments.
  2. Guaranteed: copy each segment's low-res road video (qcamera.ts) into
     /data/saved_clips/<clip>/ -- a folder the deleter never touches -- capped by
     max_storage_mb (oldest clips removed first).
Never touches vehicle control.
"""
import json
import os
import re
import shutil
from datetime import datetime

SEG_RE = re.compile(r"^[0-9A-Za-z]+--[0-9A-Za-z]+--\d+$")
CLIP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}_[0-9A-Za-z\-]+$")
FILE_RE = re.compile(r"^[0-9A-Za-z\-]+_(qcamera\.ts|fcamera\.hevc)$")
PRESERVE_ATTR_NAME = "user.preserve"
PRESERVE_ATTR_VALUE = b"1"
PRIOR_SEGMENTS = 2          # matches the deleter: keep the segment and the 2 before it


def list_segments(root: str) -> list[str]:
  try:
    names = [d for d in os.listdir(root) if SEG_RE.match(d) and os.path.isdir(os.path.join(root, d))]
  except OSError:
    return []
  return sorted(names, key=lambda d: (os.stat(os.path.join(root, d)).st_mtime, d))


def _default_setxattr(path, name, value):
  from openpilot.system.loggerd.xattr_cache import setxattr
  setxattr(path, name, value)


def _dir_size(p: str) -> int:
  total = 0
  for base, _, files in os.walk(p):
    for f in files:
      try:
        total += os.path.getsize(os.path.join(base, f))
      except OSError:
        pass
  return total


def enforce_cap(out_dir: str, max_bytes: int) -> None:
  try:
    clips = sorted((os.path.join(out_dir, c) for c in os.listdir(out_dir) if CLIP_RE.match(c)), key=os.path.getmtime)
  except OSError:
    return
  sizes = {c: _dir_size(c) for c in clips}
  total = sum(sizes.values())
  for c in clips[:-1]:                 # never delete the clip we just made
    if total <= max_bytes:
      break
    total -= sizes[c]
    shutil.rmtree(c, ignore_errors=True)


REASONS = {"manual": "Saved from phone", "hard_brake": "Hard braking", "hard_takeover": "Hard steering takeover",
           "unexpected_disengage": "Unexpected disengagement"}


def save_clip(root: str, out_dir: str, full_res: bool = False, max_storage_mb: int = 1000, setxattr=None,
              reason: str = "manual", detail: str = "") -> dict:
  segs = list_segments(root)
  if not segs:
    return {"ok": False, "error": "No dashcam segments found yet — drive first."}
  latest = segs[-1]
  route, _, seg = latest.rpartition("--")
  seg_num = int(seg)
  names = [f"{route}--{i}" for i in range(max(0, seg_num - PRIOR_SEGMENTS), seg_num + 1)
           if os.path.isdir(os.path.join(root, f"{route}--{i}"))]

  native = True
  try:
    (setxattr or _default_setxattr)(os.path.join(root, latest), PRESERVE_ATTR_NAME, PRESERVE_ATTR_VALUE)
  except Exception:
    native = False

  clip = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{route.replace('--', '-')}"
  base, n = clip, 1
  dest = os.path.join(out_dir, clip)
  while os.path.exists(dest):
    n += 1
    clip = f"{base}-{n}"
    dest = os.path.join(out_dir, clip)
  os.makedirs(dest, exist_ok=True)
  files = []
  wanted = ["qcamera.ts"] + (["fcamera.hevc"] if full_res else [])
  for n in names:
    for w in wanted:
      src = os.path.join(root, n, w)
      if os.path.isfile(src):
        dst_name = f"{n.replace('--', '-')}_{w}"
        shutil.copyfile(src, os.path.join(dest, dst_name))
        files.append(dst_name)
  meta = {"ok": True, "clip": clip, "segments": names, "files": files, "native_preserve": native,
          "reason": reason if reason in REASONS else "manual", "label": REASONS.get(reason, REASONS["manual"]), "detail": detail,
          "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
  with open(os.path.join(dest, "meta.json"), "w") as f:
    json.dump(meta, f, indent=2)
  enforce_cap(out_dir, int(max_storage_mb) * 1024 * 1024)
  return meta


def list_clips(out_dir: str, n: int = 20) -> list[dict]:
  out = []
  try:
    names = sorted((c for c in os.listdir(out_dir) if CLIP_RE.match(c)), reverse=True)
  except OSError:
    return out
  for c in names[:n]:
    try:
      with open(os.path.join(out_dir, c, "meta.json")) as f:
        out.append(json.load(f))
    except Exception:
      continue
  return out
