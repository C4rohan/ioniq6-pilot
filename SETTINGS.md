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
| `/data/sunnypilot_upload.json` | Drive-data upload (off until enabled) | `enabled`, `repo` (`owner/name`) |
| `/data/sunnypilot_lane_centering.json` | Lane centering (off until enabled) | `enabled`, `offset_m`, `strength`, `pause_on_signal`, `e2e_authority` |
| `/data/sunnypilot_ioniq6_steering.json` | StarPilot Ioniq 6 steering controller (off until enabled; next drive) | `enabled`, `live_learning`, `lat_accel_factor`, `friction` |
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
(with NNLC on, it reflects the neural model; off, the torque tune). Takeovers logged before the November 2026
sign fix had too weak and too strong swapped; they're corrected automatically once. If you had set
`invert_sign: true` to work around that, remove it now.

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

**Status line:** the phone page's *Live status* card shows what lane centering is doing, updated
every 2 seconds. For example: *correcting ← left, 0.13 m/s², 85% of the last second*, *centred, no
correction needed*, *waiting: lane lines aren't clear enough*, *paused: turn signal on*, *paused:
you're steering*, or *on, starts when you drive* while parked. controlsd writes it once a second to
`/dev/shm/sp_lane_centering.json` (RAM, not flash). If that file can't be written, only the status
line stops; steering is unaffected. If the settings file is invalid, the line says so.

## StarPilot Ioniq 6 steering controller

StarPilot's steering controller built specifically for the Ioniq 6. When it's on, it **replaces
sunnypilot's torque controller and NNLC**. Instead of one setting for every situation, it shapes the
torque by what the car is doing:

- separate tuning for right and left curves (the plain controller treats both the same)
- a boost when turning into a curve, and less push when straightening out of one
- an extra correction when a 2023–24 car over-rotates coming out of a curve
- tapers around centre and on the highway, to stop small back-and-forth hunting
- a separate tune for 2025-firmware cars, chosen automatically from the car's firmware

**Off by default.** Turn it on from the `ioniq6_steering` tab on the phone page:

```json
{"enabled": true}
```

It takes effect **on the next drive**, never mid-drive. The phone page's *Live status* card shows a
*Steering controller* line, so you can see which one is running and whether a change is still waiting
for the next drive.

| Key | Default | Range | What it does |
|---|---|---|---|
| `enabled` | `false` | | use StarPilot's controller from the next drive |
| `live_learning` | `true` | | use the car's live-learned steering values when they're within StarPilot's bounds |
| `lat_accel_factor` | `3.0` | 2.0 to 4.5 | StarPilot's base factor, in StarPilot's units. **Lower = more steering torque** |
| `friction` | `0.09` | 0 to 0.2 | StarPilot's friction value, in StarPilot's units |

Some things to know:
- **It's rescaled to this car's torque limit.** StarPilot lets CAN-FD Hyundais use up to 409 units of
  steering torque; this build (like sunnypilot) allows 270, which the comma's safety layer enforces.
  StarPilot's tune assumes 409, so it's converted to ask for the same torque in units: factor
  3.0 → 1.98 (× 1.22 inside → 2.42), friction 0.09 → 0.136. The phone shows the converted numbers.
  The two limits only differ in the sharpest curves, where this build tops out sooner.
- **It uses live learning like StarPilot does.** The car's learned steering values are used once they're
  within StarPilot's bounds: factor within ±30% of the base tune, friction between half and 1.5 times
  it, and a small offset. Until then it uses the base tune. The status line says which.
  The on-device torque sliders and the NNLC toggle are ignored while it's on.
- In a simulated 90 km/h curve it asks for about 20–30% more torque than your current tune while
  holding the curve, and about the same (±16%) turning in and straightening out, with more in right
  curves than left. If it feels too strong, raise `lat_accel_factor` (e.g. 3.3); too weak, lower it.
- Lane centering works with it: lane centering adjusts where the car aims, and this controller
  decides how hard to steer to get there.
- If it ever errors or produces a bad value, sunnypilot's normal controller takes over for the
  rest of the drive, and the status line shows it. sunnypilot's controller keeps receiving the
  live-learned values in the background, so it's ready to take over.
- The code is pinned to StarPilot commit `008b0cb` (2026-11-21). StarPilot changes its Ioniq 6
  tune often; this build doesn't follow it automatically. A test checks that our copy matches
  StarPilot's recorded controller output number for number.

## Drive stats

Nothing to enable. The **Drive stats** card has five tabs: Overview, This week, Records, Models and
Recent drives. It's modelled on StarPilot's Galaxy stats, but uses this build's own drive log, so
it needs no extra processing. "Steering-only" is time openpilot steered while cruise wasn't engaged.
"Attention alerts" count openpilot's second distraction warning and its final "unresponsive" warning,
counted the same way Galaxy counts them. Drives logged before this update don't have the steering and
attention numbers; they're left out of those stats instead of counting as zero. "Most engaged day"
only counts days with at least 10 minutes of driving.

## Calibration & steering health

Nothing to enable. A bad or old camera calibration feels just like a bad steering tune, so check this
card first when steering feels off. It shows the calibration status, how far the mount is turned or tilted
compared with what openpilot allows, whether that moved since your recent drives, the wheel offset, and
how far live steering learning has got. *Steering mode* says what is steering on the current drive; with
StarPilot's Ioniq 6 controller on, it shows that controller's tune and whether it's using live learning. Each drive adds one row to `/data/sp_calibration.csv`.

## Curve log

Nothing to enable. For every curve openpilot steers through above 40 km/h (with no turn signal on), it
records which way the curve went and where the car sat in its lane at the entry, middle and exit,
measured from the driving model's own lane lines. It also records whether the steering turned as much
as it wanted, what lane centering did, and which steering controller was running. Each drive also gets
one "straights" row: if the car sits off-centre by the same amount on straights as in curves, the
device is probably mounted off-centre, and the steering isn't the cause.

The **Curve log** card on the phone page groups the last 10 drives by setup (which controller, and lane
centering on or off), so you can compare a drive with a change against one without it. Each side needs
3 curves with clear lane lines before it's summarised. To download the raw rows, use the *download CSV*
link or `http://<device-ip>:8088/api/curves.csv`. The last 30 drives are kept in `/data/sp_curves.csv`.

## Drive-data upload

Sends the curve log, calibration history and trip list to a **private** GitHub repo whenever the device
is online. Nothing with GPS is sent: takeovers, the drive track and sentry photos stay on the device.
To set it up once:

1. In the phone page's **upload** tab, save `{"enabled": true, "repo": "owner/name"}`.
2. The **Data upload** card shows the device's key after a few seconds. Add it to that repo as a deploy
   key with write access: *repo → Settings → Deploy keys → Add deploy key*, tick *Allow write access*.
3. The card changes to "up to date" after the next upload attempt. Failed attempts are retried, waiting
   longer each time, up to once an hour.

The device makes its own key (`/data/sp_upload_key`), and the private half never leaves it. The deploy key
can only write to that one repo; to cut it off, delete the deploy key on GitHub. GitHub's host key is
pinned, so the upload won't go to an impostor server.

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
