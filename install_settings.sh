#!/usr/bin/env bash
# One-time setup for the Ioniq 6 custom build. Run ON the comma 3X over SSH:
#   bash /data/openpilot/install_settings.sh
# Enables the phone settings server + dashboard (:8088). No secrets here; add
# your notification details (and toggle reverse-cam) from the phone page after.
set -e
PC=/data/openpilot/system/manager/process_config.py

echo ">> enabling settings server + dashboard (:8088) in process_config.py (idempotent)"
python3 - "$PC" <<'PY'
import sys
pc = sys.argv[1]
s = open(pc).read()
line = '  PythonProcess("sunnypilot_settings_server", "sunnypilot.selfdrive.settings_server.settings_server", always_run),\n'
if "sunnypilot_settings_server" in s:
    print("   already enabled")
else:
    anchor = "# sunnypilot\nprocs += [\n"
    assert anchor in s, "could not find the sunnypilot procs block; add the line manually (see SETTINGS.md)"
    open(pc, "w").write(s.replace(anchor, anchor + line, 1)); print("   added")
PY

echo ">> done. Rebooting in 5s (Ctrl-C to cancel)..."
sleep 5
sudo reboot
