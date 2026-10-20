import os

import pytest

from openpilot.sunnypilot.selfdrive.settings_server import clips

ROUTE = "0000002a--abcdef1234"


@pytest.fixture
def log_root(tmp_path, monkeypatch):
  root = tmp_path / "realdata"
  for i in range(6):
    seg = root / f"{ROUTE}--{i}"
    seg.mkdir(parents=True)
    (seg / "qcamera.ts").write_bytes(os.urandom(2000))
    (seg / "fcamera.hevc").write_bytes(b"F" * 5000)
    os.utime(seg, (1000 + i, 1000 + i))
  monkeypatch.setenv("LOG_ROOT", str(root))
  return str(root)


def no_xattr(*_):
  return None


def test_saves_current_and_two_prior(log_root, tmp_path):
  out = str(tmp_path / "out")
  res = clips.save_clip(log_root, out, setxattr=no_xattr)
  assert res["ok"] and res["segments"] == [f"{ROUTE}--{i}" for i in (3, 4, 5)]
  assert sorted(res["files"]) == sorted(f"{ROUTE.replace('--', '-')}-{i}_qcamera.ts" for i in (3, 4, 5))
  for i in (3, 4, 5):
    with open(os.path.join(out, res["clip"], f"{ROUTE.replace('--', '-')}-{i}_qcamera.ts"), "rb") as a, \
         open(os.path.join(log_root, f"{ROUTE}--{i}", "qcamera.ts"), "rb") as b:
      assert a.read() == b.read()


def test_real_deleter_keeps_flagged_minutes(log_root, tmp_path):
  res = clips.save_clip(log_root, str(tmp_path / "out"))
  if not res["native_preserve"]:
    pytest.skip("filesystem does not support user xattrs")
  from openpilot.system.loggerd import deleter
  dirs = sorted(os.listdir(log_root), key=lambda d: int(d.rsplit("--", 1)[1]))
  assert deleter.get_preserved_segments(dirs) == {f"{ROUTE}--{i}" for i in (3, 4, 5)}


def test_options_and_failures(log_root, tmp_path):
  out = str(tmp_path / "out")
  assert any(f.endswith("fcamera.hevc") for f in clips.save_clip(log_root, out, full_res=True, setxattr=no_xattr)["files"])

  def broken(*_):
    raise OSError("read-only")
  r = clips.save_clip(log_root, out, setxattr=broken)
  assert r["ok"] and r["native_preserve"] is False and len(r["files"]) == 3
  assert clips.save_clip(str(tmp_path / "missing"), out)["ok"] is False


def test_reason_names_and_cap(log_root, tmp_path):
  out = str(tmp_path / "out")
  a = clips.save_clip(log_root, out, setxattr=no_xattr, reason="hard_brake", detail="5.0 m/s²")
  b = clips.save_clip(log_root, out, setxattr=no_xattr)
  assert a["label"] == "Hard braking" and a["detail"] == "5.0 m/s²"
  assert a["clip"] != b["clip"] and clips.CLIP_RE.match(b["clip"])
  assert clips.save_clip(log_root, out, setxattr=no_xattr, reason="bogus")["reason"] == "manual"
  assert len(clips.list_clips(out)) == 3
  clips.enforce_cap(out, 1)
  assert len([c for c in os.listdir(out) if clips.CLIP_RE.match(c)]) == 1


def test_name_patterns_block_traversal():
  assert not clips.CLIP_RE.match("../x") and not clips.FILE_RE.match("../etc/passwd")
  assert clips.FILE_RE.match("0000002a-abcdef1234-5_qcamera.ts")
