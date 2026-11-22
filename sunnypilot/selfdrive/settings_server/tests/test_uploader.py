"""Ioniq6 build: drive-data upload to the owner's private repo (tested against a local bare repo, no network)."""
import json
import os
import shutil
import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from openpilot.sunnypilot.selfdrive.settings_server import uploader as up

ON = {"enabled": True, "repo": "C4rohan/ioniq6-drive-data"}


def git(*args, cwd=None):
  return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                        cwd=cwd, capture_output=True, text=True, check=True).stdout


class Env:
  def __init__(self, tmp_path, cfg=ON):
    self.data = tmp_path / "data"
    self.data.mkdir()
    self.cfg = tmp_path / "upload.json"
    if cfg is not None:
      self.cfg.write_text(json.dumps(cfg))
    self.bare = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(self.bare))
    self.now = 1_700_000_000.0
    self.u = up.Uploader(data_dir=str(self.data), config_path=str(self.cfg), status_path=str(tmp_path / "status.json"),
                         key_path=str(tmp_path / "key"), known_hosts=str(tmp_path / "known_hosts"),
                         repo_dir=str(tmp_path / "work"), remote=str(self.bare), clock=lambda: self.now)

  def write(self, name, text):
    (self.data / name).write_text(text)

  def remote_log(self):
    r = subprocess.run(["git", "--git-dir", str(self.bare), "log", "main", "--format=%s"], capture_output=True, text=True)
    return r.stdout.split("\n")[:-1] if r.returncode == 0 else []

  def remote_file(self, name):
    return git("--git-dir", str(self.bare), "show", f"main:{name}")

  def remote_files(self):
    return git("--git-dir", str(self.bare), "ls-tree", "--name-only", "main").split()


@pytest.fixture
def env(tmp_path):
  return Env(tmp_path)


class TestConfig:
  @pytest.mark.parametrize("cfg,ok", [
    (ON, True), ({"enabled": False}, True), ({"enabled": True}, False), ({"enabled": "yes", "repo": "a/b"}, False),
    ({"enabled": True, "repo": "a/b; rm -rf /"}, False), ({"enabled": True, "repo": "git@github.com:a/b.git"}, False),
    ({"enabled": True, "repo": "a/b.git"}, False), ({"enabled": True, "repo": "../x"}, False), ([], False),
  ])
  def test_validate(self, cfg, ok):
    assert (up.validate(cfg) is not None) == ok


class TestKey:
  def test_ssh_keygen(self, tmp_path):
    if not shutil.which("ssh-keygen"):
      pytest.skip("no ssh-keygen")
    k = str(tmp_path / "k")
    pub = up.ensure_key(k)
    assert pub.startswith("ssh-ed25519 ") and pub.endswith(up.KEY_COMMENT)
    assert oct(os.stat(k).st_mode & 0o777) == "0o600" and up.ensure_key(k) == pub          # made once

  def test_fallback_without_ssh_keygen_makes_a_usable_key(self, tmp_path, monkeypatch):
    real = shutil.which("ssh-keygen")
    monkeypatch.setattr(up.shutil, "which", lambda name: None)
    k = str(tmp_path / "k")
    pub = up.ensure_key(k)
    assert pub.startswith("ssh-ed25519 ") and oct(os.stat(k).st_mode & 0o777) == "0o600"
    if real:                                    # OpenSSH can read the private key and derive the same public key
      derived = subprocess.run([real, "-y", "-f", k], capture_output=True, text=True, check=True).stdout.split()[:2]
      assert derived == pub.split()[:2]


  def test_lost_public_half_is_rebuilt_not_replaced(self, tmp_path):
    k = str(tmp_path / "k")
    pub = up.ensure_key(k)
    with open(k, "rb") as f:
      private = f.read()
    os.remove(k + ".pub")
    assert up.ensure_key(k) == pub
    with open(k, "rb") as f:
      assert f.read() == private                                     # same key: the deploy key still works


class TestUpload:
  def test_off_by_default_makes_no_key(self, tmp_path):
    e = Env(tmp_path, cfg=None)
    e.write("sp_curves.csv", "a\n")
    e.u.step()
    assert e.u.status["state"] == "off" and not os.path.exists(e.u.key_path) and e.remote_log() == []

  def test_uploads_only_location_free_files(self, env):
    env.write("sp_curves.csv", "dir\nright\n")
    env.write("sp_trips.csv", "km\n12\n")
    env.write("sp_overrides.csv", "lat,lon\n40.7,-74.0\n")        # has GPS: must stay on the device
    env.u.step()
    assert env.u.status["state"] == "ok" and env.u.status["pubkey"].startswith("ssh-ed25519")
    assert sorted(env.remote_files()) == ["sp_curves.csv", "sp_trips.csv"]
    assert env.remote_file("sp_curves.csv") == "dir\nright\n"
    with open(env.u.known_hosts) as f:
      assert f.read().strip() == up.GITHUB_HOST_KEY

  def test_edited_known_hosts_is_repinned(self, env):
    env.write("sp_curves.csv", "1\n")
    with open(env.u.known_hosts, "w") as f:
      f.write("github.com ssh-ed25519 AAAAattacker\n")
    env.u.step()
    with open(env.u.known_hosts) as f:
      assert f.read().strip() == up.GITHUB_HOST_KEY

  def test_status_file_not_rewritten_when_nothing_changed(self, env):
    env.write("sp_curves.csv", "1\n")
    env.u.step()
    before = os.stat(env.u.status_path).st_mtime_ns
    env.u.step()
    assert os.stat(env.u.status_path).st_mtime_ns == before

  def test_only_pushes_when_data_changed(self, env):
    env.write("sp_curves.csv", "1\n")
    env.u.step()
    env.u.step()
    assert len(env.remote_log()) == 1
    env.write("sp_curves.csv", "1\n2\n")
    env.u.step()
    assert len(env.remote_log()) == 2 and env.remote_file("sp_curves.csv") == "1\n2\n"

  def test_builds_on_top_of_someone_elses_commit(self, env, tmp_path):
    env.write("sp_curves.csv", "1\n")
    env.u.step()
    other = tmp_path / "other"
    git("clone", "-q", "-b", "main", str(env.bare), str(other))
    (other / "README.md").write_text("notes\n")
    git("add", "README.md", cwd=other)
    git("commit", "-q", "-m", "readme", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)
    env.write("sp_curves.csv", "1\n2\n")
    env.u.step()
    assert env.u.status["state"] == "ok" and "README.md" in env.remote_files() and env.remote_file("sp_curves.csv") == "1\n2\n"
    assert "readme" in env.remote_log()                               # their commit is kept, not overwritten

  def test_failure_backs_off_then_recovers(self, env, tmp_path):
    env.write("sp_curves.csv", "1\n")
    env.u.remote_override = str(tmp_path / "missing.git")
    env.u.step()
    first = env.u.status["last_try"]
    assert env.u.status["state"] in ("error", "needs_key") and env.u.next_try == env.now + 60
    env.now += 30
    env.u.step()
    assert env.u.status["last_try"] == first                          # waits for the backoff
    env.now += 31
    env.u.step()
    assert env.u.next_try == env.now + 120                            # backoff doubles
    env.u.remote_override = str(env.bare)
    env.now += 121
    env.u.step()
    assert env.u.status["state"] == "ok" and env.u.backoff == up.BACKOFF_MIN_S and len(env.remote_log()) == 1

  def test_status_survives_a_restart(self, env):
    env.write("sp_curves.csv", "1\n")
    env.u.step()
    again = up.Uploader(data_dir=env.u.data_dir, config_path=env.u.config_path, status_path=env.u.status_path,
                        key_path=env.u.key_path, known_hosts=env.u.known_hosts, repo_dir=env.u.repo_dir,
                        remote=str(env.bare), clock=lambda: env.now)
    again.step()
    assert len(env.remote_log()) == 1


class TestExplain:
  @pytest.mark.parametrize("err,state", [
    ("git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.", "needs_key"),
    ("ERROR: Repository not found.", "needs_key"),
    ("ssh: Could not resolve hostname github.com: Temporary failure in name resolution", "offline"),
    ("ssh: connect to host github.com port 22: Connection timed out", "offline"),
    ("Host key verification failed.", "error"),
    ("something odd", "error"),
  ])
  def test_explain(self, err, state):
    assert up.explain(err)[0] == state


class TestPhone:
  NOW = 1_700_000_000.0

  @pytest.mark.parametrize("status,cfg,problem,level,text", [
    (None, None, "", "mut", "off"),
    (None, ON, "", "mut", "starting…"),
    ({"enabled": True, "state": "needs_key", "repo": "a/b", "pubkey": "ssh-ed25519 X"}, ON, "", "warn",
     "waiting: add the key below to a/b as a deploy key with write access"),
    ({"enabled": True, "state": "ok", "repo": "a/b", "last_ok": NOW - 600}, ON, "", "on", "up to date with a/b; last upload 10 min ago"),
    ({"enabled": True, "state": "offline", "last_ok": NOW - 7200}, ON, "", "mut", "will upload when the device is online; last upload 2 h ago"),
    (None, None, "settings file is not valid JSON", "bad", "off: upload settings settings file is not valid JSON"),
  ])
  def test_describe(self, status, cfg, problem, level, text):
    d = up.describe(status, cfg, problem, self.NOW)
    assert (d["level"], d["text"]) == (level, text)

  def test_http(self, env, monkeypatch):
    from openpilot.sunnypilot.selfdrive.settings_server import settings_server as ss
    env.write("sp_curves.csv", "1\n")
    env.u.step()
    monkeypatch.setattr(up, "STATUS", env.u.status_path)
    monkeypatch.setitem(ss.CONFIGS, "upload", (env.u.config_path, True))
    monkeypatch.setattr(ss, "TELEMETRY", None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ss.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
      with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/upload") as r:
        d = json.loads(r.read())
      assert d["level"] == "on" and d["pubkey"].startswith("ssh-ed25519") and "PRIVATE" not in json.dumps(d)
    finally:
      httpd.shutdown()
      httpd.server_close()
