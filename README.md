<div align="center">

# 🚗 openpilot — Ioniq 6 custom build

**A personal, phone-managed build of openpilot for the Hyundai Ioniq 6 on a comma 3X.**

![Car](https://img.shields.io/badge/car-Hyundai%20Ioniq%206%20%282023%E2%80%9324%2C%20non--HDA--II%29-2f6feb?style=flat-square)
![Device](https://img.shields.io/badge/device-comma%203X-111827?style=flat-square)
![Base](https://img.shields.io/badge/base-sunnypilot%20release--tizi-f59e0b?style=flat-square)
![Updates](https://img.shields.io/badge/updates-auto--rebased%20weekly-10b981?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-8b5cf6?style=flat-square)

`installer.comma.ai/C4rohan/ioniq6-tizi-custom`

</div>

---

Tracks the latest sunnypilot `release-tizi` (auto-rebased weekly) with Ioniq 6–specific steering tuning and a few phone-managed conveniences.

> **What openpilot controls on this car:** the Ioniq 6 (CAN-FD, `CANFD_NO_RADAR_DISABLE`) does **not** support openpilot longitudinal — your **factory ACC does gas, braking, and following distance**. openpilot here controls **steering only**. Features that would change speed, following distance, or acceleration don't work on this platform, so this build focuses on **steering quality and driver conveniences**.

## ✨ Features

| | Feature | What it does |
|---|---|---|
| 🧠 | **Ioniq 6 NNLC + torque tune** | A dedicated neural steering model and a measured lateral tune instead of the stock placeholder — smoother, more accurate lane centering. |
| 🅿️ | **Reverse camera view** | Shift to reverse and the screen shows a clean full-screen camera (driving overlays hidden) with a REVERSE badge — a parking aid using the forward camera. |
| 📊 | **Live dashboard** | Speed, engagement, model, and trip stats on your phone in real time. |
| 🧾 | **Trip & disengagement logs** | Every drive and every disengagement, with context (speed, pedals), so you can review how it's doing. |
| 💬 | **Notifications** | Drive summary to Telegram or any webhook the moment you park. |
| 📱 | **Phone settings page** | Edit the notification and reverse-camera settings from a browser on the car's network. |

Plus everything sunnypilot gives you for **steering**: MADS / Always-on Lateral, NNLC, Auto Lane Change, and the driving-model selector. (Sunnypilot's speed features — Speed Limit Control, curve slowing — rely on longitudinal control this car doesn't expose, so they don't actuate here.)

## 🚀 Quick start

**Prerequisites:** comma 3X, the Hyundai **"L" harness** (CAN-FD, non-HDA-II), WiFi.

1. **Install** — on the device choose *Custom Software* and enter `installer.comma.ai/C4rohan/ioniq6-tizi-custom`. (Already on the branch? *Settings → Software → Check for Update*.)
2. **Toggle the sunnypilot basics** on the device: **MADS**, **NNLC**, **Auto Lane Change**.
3. *(Optional)* **Phone page** — one-time SSH: `bash install_settings.sh` enables the settings server on **:8088** and reboots. Then, on your phone's hotspot, browse to `http://<device-ip>:8088` for the dashboard, logs, and notification/reverse-cam settings.

> ⚠️ Treat the first drive after any install or update as a **shakedown** — empty road, hands ready. This build carries a custom steering-torque tune.

## 🛡️ How it stays safe

- The custom steering tune and NNLC model are for the Ioniq 6's own platform (shared with the Ioniq 5).
- Reverse view, dashboard, logs, and notifications **never touch control** — they only read state and write files/webhooks.
- Secrets (bot tokens, webhook URLs) live only on the device and are masked on the phone page.

## 📚 Docs

- **[FEATURES.md](FEATURES.md)** — full inventory and what's deliberately not included (and why)
- **[SETTINGS.md](SETTINGS.md)** — the config files and how to enable the phone page
- **[tools/mac_dev/](tools/mac_dev/setup_mac_dev.sh)** — run the real raylib driving UI on a Mac to test UI changes
- **[tools/ui_mock/hud_mock.html](tools/ui_mock/hud_mock.html)** — the HUD Workbench for designing UI additions

## 🔄 Updates

A weekly job rebases this branch onto the newest sunnypilot `release-tizi` and replays every patch; it fails safe (aborts and notifies) on conflict. After a bump, **reinstall on the device**; your `/data` settings are preserved.

## User data

By default openpilot uploads driving data to comma's servers; you can view it via [comma connect](https://connect.comma.ai/) and disable collection in settings. Logged data includes the road-facing camera, CAN, GPS, IMU, magnetometer, thermal sensors, crashes, and OS logs. The driver-facing camera and microphone are only logged if you opt in.

## Licensing

Released under the [MIT License](LICENSE). This repository contains significant portions of code derived from [openpilot by comma.ai](https://github.com/commaai/openpilot), released under the MIT license with additional disclaimers, reproduced below as required.

> openpilot is released under the MIT license. Some parts of the software are released under other licenses as specified.
>
> Any user of this software shall indemnify and hold harmless Comma.ai, Inc. and its directors, officers, employees, agents, stockholders, affiliates, subcontractors and customers from and against all allegations, claims, actions, suits, demands, damages, liabilities, obligations, losses, settlements, judgments, costs and expenses (including without limitation attorneys' fees and costs) which arise out of, relate to or result from any use of this software by user.
>
> **THIS IS ALPHA QUALITY SOFTWARE FOR RESEARCH PURPOSES ONLY. THIS IS NOT A PRODUCT.
> YOU ARE RESPONSIBLE FOR COMPLYING WITH LOCAL LAWS AND REGULATIONS.
> NO WARRANTY EXPRESSED OR IMPLIED.**

For full license terms, see the [`LICENSE`](LICENSE) file.
