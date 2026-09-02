# openpilot — Ioniq 6 custom build

A personal build of openpilot for a **Hyundai Ioniq 6 (2023–24, non-HDA-II / Highway Driving Assist)** running on a **comma 3X**.

Tracks the latest sunnypilot `release-tizi` (auto-rebased weekly) with Ioniq 6–specific tuning.

## Install on the comma 3X

**Prerequisites:** comma 3X mounted, the Hyundai **"L" harness** (CAN-FD, non-HDA-II), and a WiFi connection.

### Already running this branch → just update

No URL needed; the device tracks the branch.

1. Car on, device booted and on **WiFi**.
2. **Settings → Software → Check for Update → Download**, then **Install / Reboot**.
3. Your on-device toggles carry over. (It also self-updates overnight on WiFi with the car off.)

### Fresh install → enter the Custom Software URL

1. If openpilot/sunnypilot is already installed: **Settings → Uninstall**, confirm, and let it reboot into the setup wizard. (A fresh 3X boots straight into the wizard.)
2. Wizard: pick language → connect to **WiFi**.
3. Choose **Custom Software** (not "openpilot").
4. Enter exactly:

   ```
   installer.comma.ai/C4rohan/ioniq6-tizi-custom
   ```

5. Let it download, install, and reboot; then pair via [comma connect](https://connect.comma.ai/).
6. Plug into the car with the **Hyundai "L" harness** and let it fingerprint the Ioniq 6.
7. Enable your toggles: **MADS / Always-on Lateral**, **NNLC**, **Auto Lane Change**.

> ⚠️ First drive after any install/update: treat it as a shakedown — empty road, hands ready — especially as this build carries a custom steering-torque tune.

## What's customized

- Dedicated NNLC (Neural Network Lateral Control) model for the Ioniq 6, shared with the E-GMP platform Ioniq 5.
- Lateral torque tuning aligned to the Ioniq 5 measured fit.
- **Weather-adaptive driving** (adapted from FrogPilot, MIT): in rain, storms, snow, or low visibility it increases following time, increases the stopped gap behind a lead, reduces maximum acceleration, and slows more for curves. Only ever more conservative; neutral (stock) when disabled, offline, or on any error.
- **Acceleration profiles** (Eco / Normal / Sport, adapted from FrogPilot, MIT): scales how briskly it accelerates to the set speed. Never touches braking, following distance, or any safety limit; hard-capped at 2.5 m/s². Off by default (Normal).
- **On-device settings server**: a small web page to edit the config files below from a phone browser on the car's network, instead of SSH. Off by default (not registered as a process) — see below.

See **[SETTINGS.md](SETTINGS.md)** for the full settings guideline (fields, safe ranges, and how to enable/test safely).

### Enabling weather-adaptive driving

It is **off by default** and needs a free [OpenWeatherMap](https://openweathermap.org/api) API key, which stays on the device and never goes in this repo.

1. SSH into the comma 3X.
2. Copy the template and add your key:

   ```
   cp /data/openpilot/sunnypilot/selfdrive/controls/lib/weather_adaptive.example.json /data/sunnypilot_weather.json
   nano /data/sunnypilot_weather.json    # set "owm_api_key"; keep "enabled": true
   ```

3. Reboot (or restart openpilot). You can tune the per-condition offsets in that file at any time; changes are picked up within seconds.

To see what it is doing: `cat /data/sunnypilot_weather_status.json` (current condition, last fetch, applied offsets, last error).

Defaults, as *sec added to following / feet added to stopped gap / % max-accel reduction / % curve-speed reduction*: rain 0.3 / 3 / 15 / 10 · storm 0.5 / 5 / 30 / 20 · snow 0.7 / 8 / 40 / 30 · low visibility 0.4 / 4 / 20 / 15. These are conservative starting points, not field-tested values — adjust to taste.

### Enabling acceleration profiles

```
cp /data/openpilot/sunnypilot/selfdrive/controls/lib/accel_profiles.example.json /data/sunnypilot_accel.json
nano /data/sunnypilot_accel.json    # "enabled": true, "profile": "eco" | "normal" | "sport"
```

Applied within seconds; no reboot needed. Sport is capped so it stays comfortable.

### Enabling the on-device settings server

The server **code** ships on the branch but is **not** auto-started — running an always-on, network-listening process on the car is a change you should make deliberately. To turn it on, add one line to the sunnypilot process block in `system/manager/process_config.py`:

```python
# sunnypilot
procs += [
  PythonProcess("sunnypilot_settings_server", "sunnypilot.selfdrive.settings_server.settings_server", always_run),
  # Models
  ...
```

Reboot, then browse to `http://<device-ip>:8088` from a phone on the same network. It edits the weather and accel configs and shows the live weather status.

> ⚠️ **No login.** Anyone on the same network can change these settings. Only enable it on a network you trust (your car hotspot / home WiFi), and remove the line to disable it. Because this edits `process_config.py`, it will need re-applying (or resolving in the weekly rebase) after updates.

## User data

By default openpilot uploads driving data to comma's servers; you can view it via [comma connect](https://connect.comma.ai/) and disable collection in settings. Logged data includes the road-facing camera, CAN, GPS, IMU, magnetometer, thermal sensors, crashes, and OS logs. The driver-facing camera and microphone are only logged if you opt in.

## Licensing

Released under the [MIT License](LICENSE). This repository contains significant portions of code derived from [openpilot by comma.ai](https://github.com/commaai/openpilot), released under the MIT license with additional disclaimers, reproduced below as required:

> openpilot is released under the MIT license. Some parts of the software are released under other licenses as specified.
>
> Any user of this software shall indemnify and hold harmless Comma.ai, Inc. and its directors, officers, employees, agents, stockholders, affiliates, subcontractors and customers from and against all allegations, claims, actions, suits, demands, damages, liabilities, obligations, losses, settlements, judgments, costs and expenses (including without limitation attorneys' fees and costs) which arise out of, relate to or result from any use of this software by user.
>
> **THIS IS ALPHA QUALITY SOFTWARE FOR RESEARCH PURPOSES ONLY. THIS IS NOT A PRODUCT.
> YOU ARE RESPONSIBLE FOR COMPLYING WITH LOCAL LAWS AND REGULATIONS.
> NO WARRANTY EXPRESSED OR IMPLIED.**

For full license terms, see the [`LICENSE`](LICENSE) file.
