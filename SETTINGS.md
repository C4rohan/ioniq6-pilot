# Settings guideline

The custom features that have settings store them in on-device JSON files under `/data/`
(they persist across updates and are never in this repo). Everything here **reads state or
writes files** — nothing here controls the car (openpilot only steers on this platform).

## Config files

| File | Feature | Fields |
|---|---|---|
| `/data/sunnypilot_sentry.json` | Sentry mode | `enabled`, `arm_delay_s`, `cameras` (`road`/`driver`), `sensitivity`, `cooldown_s`, `max_hours`, `min_voltage`, `notify`, `max_notify_per_hour`, `max_storage_mb` |
| `/data/sunnypilot_sound.json` | Alert volume | `enabled`, `volume` (0.1–1.0) |
| `/data/sunnypilot_steer_feedback.json` | Steering-tune feedback (optional) | `min_speed_kph`, `straight_lat_accel`, `invert_sign` |
| `/data/sunnypilot_clips.json` | Save clip + auto-save (optional) | `full_res`, `max_storage_mb`, `auto_save`, `auto_brake_mps2`, `auto_takeover_lat`, `auto_takeover_kph`, `auto_disengage_kph`, `auto_delay_s`, `auto_min_gap_s`, `auto_max_per_day`, `notify_clips` |
| `/data/sunnypilot_models.json` | Model selector cleanup (optional) | `hidden_folders` (list of folder names; `[]` shows every model) |
| `/data/sunnypilot_lane_centering.json` | Lane centering (off until enabled) | `enabled`, `offset_m`, `strength`, `pause_on_signal`, `e2e_authority` |
| `/data/sunnypilot_reverse_cam.json` | Reverse camera view | `enabled`, `hide_overlays` |
| `/data/sunnypilot_notify.json` | Notifications | `enabled`, `type` (`telegram`/`webhook`), `bot_token`, `chat_id`, `webhook_url`, `notify_trips` |

Read-only, auto-written (don't edit): `/data/sp_trips.csv`, `/data/sp_disengagements.csv`.

Templates ship in the repo next to each feature's code (`*.example.json`).

## Sentry mode

Off until you set `enabled: true` (phone page → *sentry* tab). It also needs `install_settings.sh`, which lets the
process manager start **only the cameras** while sentry is armed. Updates replace the device's files, so **rerun the
script after each update** if you use sentry (everything else survives updates).

- **Arming:** 2 minutes after you park (`arm_delay_s`), so walking away isn't an event.
- **Detection:** `sensitivity` is the fraction of the scene that must change (default 1.5%); lower = more sensitive.
  Whole-scene changes (lights coming on, auto-exposure) are ignored. Events are at least `cooldown_s` apart.
- **Output:** photos in `/data/sentry` (capped at `max_storage_mb`, oldest deleted first), shown on the phone page;
  a Telegram photo per event if notifications are set up (max `max_notify_per_hour`).
- **Battery:** running the cameras draws extra power. Sentry disarms when the low-passed 12V drops below
  `min_voltage` (12.2 V — above comma's own 11.8 V shutdown), after `max_hours` (12), and pauses while the device
  is overheated. Keep `max_hours` at or below the device's *Max Time Offroad*.
- **Limits:** no night illumination while parked, so dark scenes show little. Recording people near your car is
  regulated in some places — check your local rules.

## Steering-tune feedback

Nothing to enable — it logs whenever the phone page is running. Each takeover while openpilot is steering is
classified *too weak* (you steered into the curve), *too strong* (against it), or *straight*. After 10 curve
takeovers the phone page gives a verdict. Read it as a trend over several drives, and compare with NNLC on vs off
(with NNLC on, it reflects the neural model; off, the torque tune). If the verdict feels backwards on your car,
set `invert_sign: true`.

## Alert volume

`volume: 0.5` halves chimes and prompts. It stacks with the automatic ambient-noise volume, and it cannot make
the critical warnings quieter. For silence instead, use sunnypilot's own **Quiet Mode** toggle.

## Save clip

Tap **Save last 3 minutes** on the phone page (best for a passenger, or right after you've parked). It keeps the
current minute and the 2 before it: full-quality footage stays on the device, and the low-res road video is copied
to `/data/saved_clips` for download (opens in VLC). The device's own on-screen bookmark button does the native half
of this too. Only the 5 most recent bookmarks are protected by the device; the saved copies are kept until
`max_storage_mb`.

## Auto-save clips

On by default. After hard braking (≤ −4 m/s²), a hard steering takeover (≥ 1.5 m/s² curve above 50 km/h), or a
disengagement you didn't cause (above 40 km/h, no pedal, wheel, or cruise button), it waits 20 s and then saves
the clip, so the footage includes what happened next. At most one every 60 s and 20 a day. Set `auto_save: false`
to turn it off; `notify_clips` sends a message for each one.

## Model scorecard and takeover map

Nothing to enable. The scorecard ranks a model once you've driven 20 km on it. To compare fairly, drive the same
roads on each model. The map needs your phone's internet for the street basemap (your phone's hotspot works); without
it you still get a plain plot of the points. Your drive track and takeover locations are location history stored only
on the device (`sp_track.csv` keeps the last 30 drives).

## Model selector cleanup

sunnypilot's model list keeps every model it ever published. This build hides the older folders
(Legacy, 2025 Pre World, 2025 World, Custom Merge), which takes the selector from 68 models to 29: the
2026 World, Master and Release models. **The model you're using always stays in the list**, even if its
folder is hidden. sunnypilot would otherwise reset you to the default model. To change what's hidden,
edit the `models` tab on the phone page (or `/data/sunnypilot_models.json`), e.g. `{"hidden_folders": []}`
to show everything. The change shows up within a second while the car is off.
To free storage from models you downloaded before, use *Settings → Models → Clear Model Cache*.

## Lane centering

Ported from StarPilot. When both lane lines are clear, it nudges the car toward the middle of the
lane. It only adds a small correction to the curvature the driving model asks for: at most
0.0012 1/m, which is about 0.75 m/s² sideways at 90 km/h, and it builds up gently. The result
still goes through openpilot's normal steering limits. It lets go when you signal, change lanes,
or turn the wheel yourself, below 18 km/h, and whenever the lane lines are faint or the lane is
an odd width. If one line is missing, it holds your spacing from the line it can see. **Off by
default.** Turn it on from the `lane_centering` tab on the phone page:

```json
{"enabled": true}
```

| Key | Default | Range | What it does |
|---|---|---|---|
| `enabled` | `false` | | on/off; re-read once a second, so you can switch it during a drive |
| `offset_m` | `0.0` | −0.3 to 0.3 | sit off-centre: **negative = further left**, positive = further right |
| `strength` | `1.5` | 0.5 to 3.0 | how firmly it pulls back to the target |
| `pause_on_signal` | `true` | | let go while a turn signal is on |
| `e2e_authority` | `0.5` | 0 to 1 | how much a confident model path overrules the lane lines on big corrections |

A missing, broken or out-of-range file means **off**. If lane centering ever hits an error, it
switches itself off until the next drive, and the car keeps steering on the model as normal.

## Reverse camera view

`enabled` (default true) shows the clean forward-camera view in reverse; `hide_overlays: false`
keeps the normal HUD but stays in reverse. Delete the file for stock behaviour.

## Notifications

`type: telegram` needs a bot token + chat id (message @BotFather, then @userinfobot for your id).
`type: webhook` POSTs `{"text": ...}` to `webhook_url` — point it at any bridge (e.g. your own
WhatsApp gateway). Secrets stay on the device and are masked on the phone page.

## The phone page

The phone page ships with the branch, so it survives updates, but it stays **idle** (no network port,
no logging) until you switch it on — running a network service on the car is a deliberate step. Switch it
on once, over SSH:

```
touch /data/sunnypilot_settings_enabled
```

(`install_settings.sh` does this for you.) Within about 10 seconds, browse to `http://<device-ip>:8088` from a phone on the same network (your phone's
hotspot works well). It shows the live dashboard, recent trips and disengagements, and the
notify + reverse-cam settings.

> ⚠️ **No login** — LAN only. Use it on a trusted network. To switch it off, delete
> `/data/sunnypilot_settings_enabled` and reboot.

## Enabling / testing safely

Treat the first drive after any install or update as a shakedown — empty road, hands ready.
This build carries a custom steering-torque tune. See [README](README.md) and [FEATURES.md](FEATURES.md).
