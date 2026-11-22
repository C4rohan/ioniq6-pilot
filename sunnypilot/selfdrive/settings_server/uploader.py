"""
Drive-data upload for the Ioniq 6 custom build: pushes the curve log (and other location-free logs)
to the owner's PRIVATE GitHub repo whenever the device is online, so it can be analysed off the car.

Off unless /data/sunnypilot_upload.json says {"enabled": true, "repo": "owner/name"}.

How it stays contained:
  - The device makes its own SSH key (/data/sp_upload_key). The private key never leaves the device;
    the phone page shows the public key, which the owner adds to that one repo as a deploy key, so
    it can write to nothing else.
  - GitHub's host key is pinned (no trust-on-first-use).
  - Only files without GPS are uploaded (FILES). Takeovers, the drive track and sentry photos stay.
  - It runs in its own thread in the settings-server process: never touches vehicle control, and a
    slow or failed push can't hold up the drive logs. Failed pushes retry with backoff (1 min -> 1 h).
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

DATA = os.environ.get("SUNNYPILOT_DATA", "/data")
CONFIG = os.path.join(DATA, "sunnypilot_upload.json")
STATUS = os.path.join(DATA, "sp_upload_status.json")
KEY = os.path.join(DATA, "sp_upload_key")
KNOWN_HOSTS = os.path.join(DATA, "sp_upload_known_hosts")
REPO_DIR = os.path.join(DATA, "sp_upload_repo")

FILES = ("sp_curves.csv", "sp_calibration.csv", "sp_trips.csv")          # none of these hold GPS
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
KEY_COMMENT = "ioniq6-comma-upload"
CHECK_S = 30.0
BACKOFF_MIN_S, BACKOFF_MAX_S = 60.0, 3600.0
GIT_TIMEOUT_S = 90


def validate(cfg) -> dict | None:
  if not isinstance(cfg, dict) or type(cfg.get("enabled", False)) is not bool:
    return None
  repo = cfg.get("repo", "")
  if not isinstance(repo, str) or (repo and not REPO_RE.match(repo)) or repo.endswith(".git"):
    return None
  if cfg.get("enabled") and not repo:
    return None
  return {"enabled": cfg.get("enabled", False), "repo": repo}


def read_config(path: str | None = None) -> tuple[dict | None, str]:
  try:
    with open(path or CONFIG) as f:
      text = f.read()
  except FileNotFoundError:
    return None, ""
  except Exception:
    return None, "unreadable settings file"
  if not text.strip():
    return None, ""
  try:
    cfg = validate(json.loads(text))
  except Exception:
    return None, "settings file is not valid JSON"
  return (cfg, "") if cfg else (None, 'needs {"enabled": true, "repo": "owner/name"}')


def ensure_key(path: str) -> str:
  """The device's own upload key: create it once, return the public half."""
  pub = path + ".pub"
  if not (os.path.exists(path) and os.path.exists(pub)):
    for p in (path, pub):
      if os.path.exists(p):
        os.remove(p)
    if shutil.which("ssh-keygen"):
      subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", KEY_COMMENT, "-f", path],
                     check=True, capture_output=True, timeout=30)
    else:
      from cryptography.hazmat.primitives import serialization
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      k = Ed25519PrivateKey.generate()
      priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
      pubtxt = k.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
      fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
      with os.fdopen(fd, "wb") as f:
        f.write(priv)
      with open(pub, "w") as f:
        f.write(pubtxt.decode() + f" {KEY_COMMENT}\n")
  os.chmod(path, 0o600)
  with open(pub) as f:
    return f.read().strip()


def files_hash(data_dir: str) -> str:
  h = hashlib.sha256()
  for name in FILES:
    p = os.path.join(data_dir, name)
    if os.path.isfile(p):
      h.update(name.encode() + b"\0")
      with open(p, "rb") as f:
        h.update(f.read())
      h.update(b"\0")
  return h.hexdigest()


def explain(err: str) -> tuple[str, str]:
  """(state, plain text) for a failed push."""
  e = err.lower()
  if "permission denied" in e or "publickey" in e or "repository not found" in e or "access denied" in e:
    return "needs_key", "waiting for the key to be added to the repo"
  if "could not resolve" in e or "timed out" in e or "network is unreachable" in e or "connection refused" in e \
     or "connection reset" in e or "timeout" in e:
    return "offline", "will upload when the device is online"
  if "host key verification failed" in e:
    return "error", "GitHub's host key didn't match the pinned one; not uploading"
  return "error", "upload failed: " + err.strip().splitlines()[-1][:160] if err.strip() else "upload failed"


class Uploader:
  def __init__(self, data_dir: str = DATA, config_path: str = CONFIG, status_path: str = STATUS, key_path: str = KEY,
               known_hosts: str = KNOWN_HOSTS, repo_dir: str = REPO_DIR, remote: str | None = None,
               clock=time.time):  # noqa: TID251  wall clock: upload times are stored and shown across reboots
    self.data_dir, self.config_path, self.status_path = data_dir, config_path, status_path
    self.key_path, self.known_hosts, self.repo_dir = key_path, known_hosts, repo_dir
    self.remote_override, self.clock = remote, clock
    self.status = self._load_status()
    self.backoff = BACKOFF_MIN_S
    self.next_try = 0.0

  def _load_status(self) -> dict:
    try:
      with open(self.status_path) as f:
        d = json.load(f)
      return d if isinstance(d, dict) else {}
    except Exception:
      return {}

  def _save_status(self, **kw) -> None:
    self.status.update(kw)
    tmp = self.status_path + ".tmp"
    with open(tmp, "w") as f:
      json.dump(self.status, f)
    os.replace(tmp, self.status_path)

  def _git(self, *args, check=True):
    ssh = shlex.join(["ssh", "-i", self.key_path, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                    "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}"])
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": ssh}
    r = subprocess.run(["git", "-C", self.repo_dir, "-c", "user.name=ioniq6 comma", "-c", "user.email=comma@ioniq6.invalid",
                        "-c", "commit.gpgsign=false", *args], env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    if check and r.returncode != 0:
      raise RuntimeError((r.stderr or r.stdout or f"git {args[0]} failed").strip())
    return r

  def _commit_snapshot(self) -> None:
    for name in FILES:
      src = os.path.join(self.data_dir, name)
      if os.path.isfile(src):
        shutil.copyfile(src, os.path.join(self.repo_dir, name))
    self._git("add", "-A")
    if self._git("diff", "--cached", "--quiet", check=False).returncode != 0:
      self._git("commit", "-q", "-m", "drive data " + time.strftime("%Y-%m-%d %H:%M", time.localtime(self.clock())))

  def push(self, repo: str) -> None:
    remote = self.remote_override or f"git@github.com:{repo}.git"
    if not os.path.isdir(os.path.join(self.repo_dir, ".git")):
      os.makedirs(self.repo_dir, exist_ok=True)
      self._git("init", "-q")
      self._git("checkout", "-q", "-b", "main")
    self._commit_snapshot()
    if self._git("rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
      return                                           # nothing to send yet
    r = self._git("push", "-q", remote, "HEAD:refs/heads/main", check=False)
    if r.returncode == 0:
      return
    err = r.stderr or ""
    if "rejected" not in err and "fetch first" not in err and "non-fast-forward" not in err:
      raise RuntimeError(err.strip() or "push failed")
    # someone else wrote to the repo: start from what's there (this work dir is only a cache of
    # /data), lay the current files on top, and push again. Keeps their files and history.
    self._git("fetch", "-q", remote, "main")
    self._git("reset", "-q", "--hard", "FETCH_HEAD")
    self._commit_snapshot()
    self._git("push", "-q", remote, "HEAD:refs/heads/main")

  def step(self) -> None:
    now = self.clock()
    cfg, problem = read_config(self.config_path)
    if not cfg or not cfg["enabled"]:
      self._save_status(enabled=False, state="off" if not problem else "error", error=problem)
      return
    pub = ensure_key(self.key_path)
    if not os.path.exists(self.known_hosts):
      with open(self.known_hosts, "w") as f:
        f.write(GITHUB_HOST_KEY + "\n")
    digest = files_hash(self.data_dir)
    if digest == self.status.get("last_hash") and self.status.get("repo") == cfg["repo"]:
      self._save_status(enabled=True, repo=cfg["repo"], pubkey=pub, state="ok", error="")
      return
    if now < self.next_try:
      return
    self._save_status(enabled=True, repo=cfg["repo"], pubkey=pub, last_try=now)
    try:
      self.push(cfg["repo"])
    except Exception as e:
      state, text = explain(str(e))
      self.next_try = now + self.backoff
      self.backoff = min(BACKOFF_MAX_S, self.backoff * 2)
      self._save_status(state=state, error=text, next_try=self.next_try)
      return
    self.backoff, self.next_try = BACKOFF_MIN_S, 0.0
    self._save_status(state="ok", error="", last_ok=now, last_hash=digest, next_try=0.0)

  def run(self) -> None:
    while True:
      try:
        self.step()
      except Exception as e:
        try:
          self._save_status(state="error", error=f"upload error: {e}"[:200])
        except Exception:
          pass
      time.sleep(CHECK_S)


def start() -> Uploader:
  u = Uploader()
  threading.Thread(target=u.run, name="uploader", daemon=True).start()
  return u


# --------------------------------------------------------------------------- phone page
def _ago(seconds: float) -> str:
  if seconds < 90:
    return "just now"
  if seconds < 5400:
    return f"{round(seconds / 60)} min ago"
  if seconds < 129600:
    return f"{round(seconds / 3600)} h ago"
  return f"{round(seconds / 86400)} days ago"


def describe(status: dict | None, cfg: dict | None, problem: str, now: float) -> dict:
  s = status if isinstance(status, dict) else {}
  if problem:
    return {"level": "bad", "text": f"off: upload settings {problem}", "pubkey": ""}
  if not cfg or not cfg.get("enabled"):
    return {"level": "mut", "text": "off", "pubkey": ""}
  pub = s.get("pubkey", "") if s.get("enabled") else ""
  last = f"; last upload {_ago(now - s['last_ok'])}" if isinstance(s.get("last_ok"), (int, float)) else ""
  state = s.get("state")
  if not s.get("enabled"):
    return {"level": "mut", "text": "starting…", "pubkey": ""}
  if state == "ok":
    return {"level": "on", "text": f"up to date with {s.get('repo')}{last}", "pubkey": pub}
  if state == "needs_key":
    return {"level": "warn", "text": f"waiting: add the key below to {s.get('repo')} as a deploy key with write access", "pubkey": pub}
  if state == "offline":
    return {"level": "mut", "text": f"will upload when the device is online{last}", "pubkey": pub}
  if state == "error":
    return {"level": "bad", "text": (s.get("error") or "upload failed") + last, "pubkey": pub}
  return {"level": "mut", "text": "new data waiting to upload" + last, "pubkey": pub}


def read_status(now: float | None = None, config_path: str | None = None, status_path: str | None = None) -> dict:
  cfg, problem = read_config(config_path)
  try:
    with open(status_path or STATUS) as f:
      st = json.load(f)
  except Exception:
    st = None
  return describe(st, cfg, problem, time.time() if now is None else now)  # noqa: TID251  compared with stored wall times
