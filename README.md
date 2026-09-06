<div align="center">

# 🚗 openpilot — Ioniq 6 custom build

**A personal, weather-aware, phone-managed build of openpilot for the Hyundai Ioniq 6 on a comma 3X.**

![Car](https://img.shields.io/badge/car-Hyundai%20Ioniq%206%20%282023%E2%80%9324%2C%20non--HDA--II%29-2f6feb?style=flat-square)
![Device](https://img.shields.io/badge/device-comma%203X-111827?style=flat-square)
![Base](https://img.shields.io/badge/base-sunnypilot%20release--tizi-f59e0b?style=flat-square)
![Updates](https://img.shields.io/badge/updates-auto--rebased%20weekly-10b981?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-8b5cf6?style=flat-square)

`installer.comma.ai/C4rohan/ioniq6-tizi-custom`

</div>

---

Tracks the latest sunnypilot `release-tizi` (auto-rebased weekly) and layers Ioniq 6–specific tuning plus a set of features you won't find in stock sunnypilot. Everything custom is **off by default**, **configured from your phone**, and **fails safe to stock**.

## ✨ Unique features

| | Feature | What it does |
|---|---|---|
| 🌧️ | **Weather-adaptive driving** | Rain, storm, snow, or fog → more following distance, a bigger stopped gap, softer acceleration, and slower curves. Only ever more conservative. |
| 🌙 | **Night mode** | After sunset (from the same weather lookup) it drives a touch more carefully when the weather is otherwise clear. |
| ⚡ | **Eco / Normal / Sport** | Choose how briskly it accelerates to the set speed. Braking and gaps untouched; hard-capped for safety. |
| 📍 | **Geofences** | Auto-switch the accel profile by location — Eco near home, Sport on your highway — no taps needed. |
| 📊 | **Live dashboard** | Speed, engagement, weather mode, model, and trip stats on your phone in real time. |
| 🧾 | **Trip & disengagement logs** | Every drive and every disengagement, with context (speed, pedals, weather), so tuning is data-driven. |
| 💬 | **Notifications** | Drive summaries to Telegram or any webhook the moment you park. |
| 🧠 | **Ioniq 6 NNLC + torque tune** | A dedicated neural steering model and a measured lateral tune instead of the stock placeholder. |
| 📱 | **Phone settings page** | Edit every setting from a browser on the car's network. No SSH after setup. |

Plus everything sunnypilot gives you: MADS / Always-on Lateral, NNLC, Auto Lane Change, Smart Cruise Control (vision + map curve speed), Speed Limit Assist, Dynamic Experimental Control, and the driving-model selector.

## 🚀 Quick start

**Prerequisites:** comma 3X, the Hyundai **"L" harness** (CAN-FD, non-HDA-II), WiFi.

1. **Install** — on the device choose *Custom Software* and enter `installer.comma.ai/C4rohan/ioniq6-tizi-custom`. (Already on the branch? *Settings → Software → Check for Update*.)
2. **Enable the phone page** — one-time SSH:
   ```
   bash install_settings.sh
   ```
   It drops the config files into `/data`, enables the settings server on **:8088**, and reboots.
3. **Open the dashboard** — put the 3X on your phone's hotspot, then browse to `http://<device-ip>:8088`. Paste your free [OpenWeatherMap](https://openweathermap.org/api) key under *weather* and you're live.
4. **Toggle the sunnypilot basics** on the device: **MADS**, **NNLC**, **Auto Lane Change**.

> ⚠️ Treat the first drive after any install, update, or setting change as a **shakedown** — empty road, hands ready. Enable one feature at a time.

## 🛡️ How it stays safe

- Every custom hook is a `getattr(..., neutral)` — the code path is **inert unless configured**.
- Weather/night features can only make driving **more** conservative; Sport is the single assertive setting and it's capped.
- Hard clamps: extra following ≤ +1.5 s · extra stopped gap ≤ 5 m · min accel 30% · min curve speed 60% · abs max accel 2.5 m/s².
- Offline, stale (> 3 h), disabled, or any error → **identical to stock**.
- Telemetry, geofences, and notifications **never touch control** — they only read state and write files/webhooks.
- Secrets (API keys, tokens) live only on the device and are masked on the phone page.

## 📚 Docs

- **[FEATURES.md](FEATURES.md)** — full inventory, defaults, and what's deliberately not included
- **[SETTINGS.md](SETTINGS.md)** — every config file, field, safe range, and the enable/test guideline

## 🔄 Updates

A weekly job rebases this branch onto the newest sunnypilot `release-tizi` and replays every patch; it fails safe (aborts and notifies) on conflict. After a bump, **reinstall on the device**; your `/data` settings are preserved.

## User data

By default openpilot uploads driving data to comma's servers; you can view it via [comma connect](https://connect.comma.ai/) and disable collection in settings. Logged data includes the road-facing camera, CAN, GPS, IMU, magnetometer, thermal sensors, crashes, and OS logs. The driver-facing camera and microphone are only logged if you opt in.

## Licensing

Released under the [MIT License](LICENSE). This repository contains significant portions of code derived from [openpilot by comma.ai](https://github.com/commaai/openpilot), released under the MIT license with additional disclaimers, reproduced below as required. Weather presets, acceleration profiles, and remote settings were adapted from [FrogPilot](https://github.com/FrogAi/FrogPilot) (MIT).

> openpilot is released under the MIT license. Some parts of the software are released under other licenses as specified.
>
> Any user of this software shall indemnify and hold harmless Comma.ai, Inc. and its directors, officers, employees, agents, stockholders, affiliates, subcontractors and customers from and against all allegations, claims, actions, suits, demands, damages, liabilities, obligations, losses, settlements, judgments, costs and expenses (including without limitation attorneys' fees and costs) which arise out of, relate to or result from any use of this software by user.
>
> **THIS IS ALPHA QUALITY SOFTWARE FOR RESEARCH PURPOSES ONLY. THIS IS NOT A PRODUCT.
> YOU ARE RESPONSIBLE FOR COMPLYING WITH LOCAL LAWS AND REGULATIONS.
> NO WARRANTY EXPRESSED OR IMPLIED.**

For full license terms, see the [`LICENSE`](LICENSE) file.
