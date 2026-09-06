#!/usr/bin/env bash
# One-time setup for the Ioniq 6 custom build. Run ON the comma 3X over SSH:
#   bash /data/openpilot/install_settings.sh
# Drops the config files into /data and enables the phone settings server (:8088).
# Contains no secrets: add your OpenWeatherMap key (and Telegram/geofence details)
# afterwards from the phone page.
set -e
PC=/data/openpilot/system/manager/process_config.py

echo ">> /data/sunnypilot_weather.json (weather-adaptive + night mode, key added from phone)"
[ -f /data/sunnypilot_weather.json ] && echo "   exists, keeping" || cat > /data/sunnypilot_weather.json <<'JSON'
{
  "enabled": true,
  "owm_api_key": "",
  "refresh_s": 300,
  "night_enabled": true,
  "offsets": {
    "rain":           {"follow_s": 0.3, "stop_ft": 3, "accel_pct": 15, "lat_pct": 10},
    "rain_storm":     {"follow_s": 0.5, "stop_ft": 5, "accel_pct": 30, "lat_pct": 20},
    "snow":           {"follow_s": 0.7, "stop_ft": 8, "accel_pct": 40, "lat_pct": 30},
    "low_visibility": {"follow_s": 0.4, "stop_ft": 4, "accel_pct": 20, "lat_pct": 15},
    "night":          {"follow_s": 0.2, "stop_ft": 2, "accel_pct": 10, "lat_pct": 5}
  }
}
JSON

echo ">> /data/sunnypilot_accel.json (Normal; switch from phone)"
[ -f /data/sunnypilot_accel.json ] && echo "   exists, keeping" || echo '{ "enabled": true, "profile": "normal" }' > /data/sunnypilot_accel.json

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
    assert anchor in s, "could not find the sunnypilot procs block; add the line manually (see README)"
    open(pc, "w").write(s.replace(anchor, anchor + line, 1)); print("   added")
PY

echo ">> done. Rebooting in 5s (Ctrl-C to cancel)..."
sleep 5
sudo reboot
