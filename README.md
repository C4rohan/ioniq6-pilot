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
- **Weather-adaptive driving** (adapted from FrogPilot, MIT): in rain, storms, snow, or low visibility it increases following time, increases the stopped gap behind a lead, and reduces maximum acceleration. It is only ever more conservative, and it is neutral (stock behaviour) when disabled, offline, or on any error. Longitudinal only for now.

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

Defaults, as *seconds added to following time / feet added to stopped gap / % max-acceleration reduction*: rain 0.3 / 3 / 15 · storm 0.5 / 5 / 30 · snow 0.7 / 8 / 40 · low visibility 0.4 / 4 / 20. These are conservative starting points, not field-tested values — adjust to taste.

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
