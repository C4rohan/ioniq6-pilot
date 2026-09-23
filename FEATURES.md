# Features — Ioniq 6 custom build

Everything on this branch (`ioniq6-tizi-custom`). It tracks the latest sunnypilot
`release-tizi` with the custom work below layered on top.

Car: **Hyundai Ioniq 6 (2023–24, non-HDA-II / HDA)** · Device: **comma 3X** · Harness: **Hyundai "L"** (CAN-FD)

> **Platform limit that shapes this build:** the Ioniq 6 carries the `CANFD_NO_RADAR_DISABLE`
> flag — these CAN-FD cars refuse the request to disable the factory ADAS, so **openpilot
> longitudinal is not available**. The **factory ACC owns gas, braking, and following gap**;
> openpilot controls **steering**. Anything that would change speed / following distance /
> acceleration has no effect here, so this build is steering + conveniences only.

## Custom additions

| Feature | What it does | Config |
|---|---|---|
| **Ioniq 6 NNLC model** | Neural-net steering model copied from the Ioniq 5 (same E-GMP platform) so NNLC resolves as an exact match | on-device NNLC toggle |
| **Ioniq 6 torque tune** | Lateral steering torque aligned to the Ioniq 5 measured fit (replaced the `[2.5, 2.5, 0.005]` placeholder) | baked into the branch |
| **Reverse camera view** | Shifting to reverse hides the driving overlays and shows a clean forward-camera view + a REVERSE badge (parking aid; the 3X has no rear camera) | `/data/sunnypilot_reverse_cam.json` (`enabled`, `hide_overlays`) |
| **Sentry mode** | While parked: pulls road + driver camera frames at 2 fps, detects motion against an adaptive background (ignores exposure/lighting changes), saves PNG snapshots to `/data/sentry`, logs `events.csv`, and sends a Telegram photo (or webhook text). Arms 2 min after parking; disarms on low 12V (12.2 V, low-passed), after `max_hours`, pauses when overheated; latched until the next drive. Only `camerad` is woken — not the driver-monitoring model | `/data/sunnypilot_sentry.json` (off until enabled) |
| **Steering-tune feedback** | On each takeover (rising edge of `steeringPressed`) while openpilot is steering above 30 km/h: logs speed, desired/actual curvature, lateral accel, driver torque. Classifies *under* (driver steers into the curve → tune too weak), *over* (against it → too strong), or *straight* (< 0.3 m/s², lane-position preference). Phone chart by speed band + verdict after 10 curve events; rate per 100 km steered. A heuristic trend, not a measurement | `/data/sp_overrides.csv`, optional `sunnypilot_steer_feedback.json` |
| **Alert volume** | Scales non-critical sounds (0.1–1.0) on top of the ambient-noise volume; warnings in Quiet Mode's must-play list (`warningSoft`, `warningImmediate`, `promptDistracted`, `promptRepeat`) are never scaled. 4-line hook in `soundd.py` | `/data/sunnypilot_sound.json` |
| **Save clip** | Phone button: sets openpilot's native `user.preserve` flag on the current segment (the deleter keeps it + the 2 prior, same as the device's own bookmark button; last 5 honoured) **and** copies those segments' `qcamera.ts` to `/data/saved_clips` (never auto-deleted; capped). `full_res` also copies `fcamera.hevc` | optional `sunnypilot_clips.json` |
| **Live dashboard** | Onroad/engaged, speed, model, trip km & engaged % in real time on the phone page | with settings server |
| **Trip logger** | One row per drive: duration, km, max speed, engaged %, disengagements, model → `/data/sp_trips.csv` | with settings server |
| **Disengagement logger** | Time, speed, gas/brake/steer pressed, standstill, trip km per disengagement → `/data/sp_disengagements.csv` | with settings server |
| **Notifications** | Drive summary to Telegram or a generic webhook when you park | `/data/sunnypilot_notify.json` |
| **Phone settings page** | Edit the notify + reverse-cam configs and view the dashboard/logs from a browser on the car LAN (`:8088`) | manual `process_config.py` edit |

The dashboard, logs, and notifications run inside the settings-server process and **never touch
vehicle control** — they read state and write files/webhooks. All are off/idle until configured.

## sunnypilot base features (inherited)

- **MADS** / Always-on Lateral, **NNLC**, **Auto Lane Change** — steering, all work
- **Driving-model selector** — sunnypilot's model library
- Standard openpilot steering, driver monitoring, comma connect logging
- Speed features (Speed Limit Control, curve slowing, DEC) exist but **rely on longitudinal
  control this car doesn't expose, so they don't actuate** on the Ioniq 6

## Removed / not included (and why)

- **Weather-adaptive driving, night mode, Eco/Sport accel profiles, geofenced accel** — all
  removed. They changed openpilot's **longitudinal** planner, which does not drive this car
  (factory ACC does). They were inert on the Ioniq 6, so they were taken out rather than left
  as dead features.
- **Adjacent-lead tracking** — not portable on a camera-SCC prebuilt setup.
- **Themes / UI icon-sound packs, custom-model runtime swaps** — compiled Qt/model changes that
  need a full (non-prebuilt) build environment.

## Maintenance

- The branch auto-rebases onto the latest sunnypilot `release-tizi` weekly, replaying every
  custom patch; it fails safe on conflict.
- After an update, **reinstall on the device**; your `/data` settings are preserved.

See [README](README.md) for install and [SETTINGS.md](SETTINGS.md) for the phone page.
