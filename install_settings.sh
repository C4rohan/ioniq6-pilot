#!/usr/bin/env bash
# One-time setup for the Ioniq 6 custom build. Run ON the comma 3X over SSH:
#   bash /data/openpilot/install_settings.sh
# 1) enables the phone settings server + dashboard (:8088)
# 2) installs the sentry hook (cameras may run while parked ONLY when sentry is
#    enabled and armed) and the sentry daemon; sentry itself stays OFF until you
#    enable it on the phone page.
# Re-run after a software update (updates replace process_config.py). Idempotent.
set -e
PC=/data/openpilot/system/manager/process_config.py

python3 - "$PC" <<'PY'
import sys
pc = sys.argv[1]
s = open(pc).read()
orig = s

# (a) settings server
if "sunnypilot_settings_server" not in s:
    a = "# sunnypilot\nprocs += [\n"
    assert a in s, "sunnypilot procs block not found; see SETTINGS.md to add lines manually"
    s = s.replace(a, a + '  PythonProcess("sunnypilot_settings_server", "sunnypilot.selfdrive.settings_server.settings_server", always_run),\n', 1)

# (b) sentry condition (import guarded: a broken/missing module must never stop openpilot)
if "def sentry_camera(" not in s:
    a = "\nprocs = [\n"
    assert a in s, "procs list not found"
    hook = ('\ntry:\n'
            '  from openpilot.sunnypilot.selfdrive.sentry.state import sentry_armed as _sentry_armed\n'
            'except Exception:\n'
            '  def _sentry_armed() -> bool:\n'
            '    return False\n\n'
            'def sentry_camera(started: bool, params: Params, CP: car.CarParams) -> bool:\n'
            '  try:\n'
            '    return (not started) and _sentry_armed()\n'
            '  except Exception:\n'
            '    return False\n')
    s = s.replace(a, hook + a, 1)

# (c) camerad also runs while sentry is armed
cam_old = 'NativeProcess("camerad", "system/camerad", ["./camerad"], driverview, enabled=not WEBCAM),'
cam_new = 'NativeProcess("camerad", "system/camerad", ["./camerad"], or_(driverview, sentry_camera), enabled=not WEBCAM),'
if cam_new not in s:
    assert cam_old in s, "camerad line not found; see SETTINGS.md"
    s = s.replace(cam_old, cam_new, 1)

# (d) sentry daemon
if '"sentryd"' not in s:
    a = "# sunnypilot\nprocs += [\n"
    s = s.replace(a, a + '  PythonProcess("sentryd", "sunnypilot.selfdrive.sentry.sentryd", always_run),\n', 1)

if s != orig:
    compile(s, pc, "exec")          # refuse to write a file that doesn't parse
    open(pc + ".bak", "w").write(orig)
    open(pc, "w").write(s)
    print("   process_config.py updated (backup: process_config.py.bak)")
else:
    print("   already installed")
PY

if [ ! -f /data/sunnypilot_sentry.json ]; then
  cat > /data/sunnypilot_sentry.json <<'JSON'
{ "enabled": false, "arm_delay_s": 120, "cameras": ["road", "driver"], "sensitivity": 0.015,
  "cooldown_s": 60, "max_hours": 12, "min_voltage": 12.2, "notify": true, "max_notify_per_hour": 10, "max_storage_mb": 500 }
JSON
  echo "   wrote /data/sunnypilot_sentry.json (sentry OFF — enable it on the phone page)"
fi

echo ">> done. Rebooting in 5s (Ctrl-C to cancel)..."
sleep 5
sudo reboot
