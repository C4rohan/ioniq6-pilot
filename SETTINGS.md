# Settings guideline

The custom features that have settings store them in on-device JSON files under `/data/`
(they persist across updates and are never in this repo). Everything here **reads state or
writes files** — nothing here controls the car (openpilot only steers on this platform).

## Config files

| File | Feature | Fields |
|---|---|---|
| `/data/sunnypilot_sentry.json` | Sentry mode | `enabled`, `arm_delay_s`, `cameras` (`road`/`driver`), `sensitivity`, `cooldown_s`, `max_hours`, `min_voltage`, `notify`, `max_notify_per_hour`, `max_storage_mb` |
| `/data/sunnypilot_reverse_cam.json` | Reverse camera view | `enabled`, `hide_overlays` |
| `/data/sunnypilot_notify.json` | Notifications | `enabled`, `type` (`telegram`/`webhook`), `bot_token`, `chat_id`, `webhook_url`, `notify_trips` |

Read-only, auto-written (don't edit): `/data/sp_trips.csv`, `/data/sp_disengagements.csv`.

Templates ship in the repo next to each feature's code (`*.example.json`).

## Sentry mode

Off until you set `enabled: true` (phone page → *sentry* tab). Needs the one-time `install_settings.sh`, which lets
the process manager start **only the cameras** while sentry is armed.

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

## Reverse camera view

`enabled` (default true) shows the clean forward-camera view in reverse; `hide_overlays: false`
keeps the normal HUD but stays in reverse. Delete the file for stock behaviour.

## Notifications

`type: telegram` needs a bot token + chat id (message @BotFather, then @userinfobot for your id).
`type: webhook` POSTs `{"text": ...}` to `webhook_url` — point it at any bridge (e.g. your own
WhatsApp gateway). Secrets stay on the device and are masked on the phone page.

## The phone page

Off by default (running a network service on the car is a deliberate step). Enable it with one
line in the sunnypilot process block of `system/manager/process_config.py`:

```python
# sunnypilot
procs += [
  PythonProcess("sunnypilot_settings_server", "sunnypilot.selfdrive.settings_server.settings_server", always_run),
  # Models
  ...
```

Reboot, then browse to `http://<device-ip>:8088` from a phone on the same network (your phone's
hotspot works well). It shows the live dashboard, recent trips and disengagements, and the
notify + reverse-cam settings.

> ⚠️ **No login** — LAN only. Use it on a trusted network. Because this edits `process_config.py`,
> it may need re-applying (or resolving in the weekly rebase) after an update.

## Enabling / testing safely

Treat the first drive after any install or update as a shakedown — empty road, hands ready.
This build carries a custom steering-torque tune. See [README](README.md) and [FEATURES.md](FEATURES.md).
