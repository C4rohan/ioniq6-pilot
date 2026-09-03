# Settings guideline

How to configure the custom features on this build. All settings live in
on-device JSON files under `/data/` (they persist across updates and are never
stored in this repo). Changes are picked up within ~5 seconds — no reboot needed
to re-tune. Every custom feature is **off by default** and **fails safe to stock**
(any error / offline / disabled → identical to unmodified sunnypilot).

## The two config files

| File | Feature | Fields |
|---|---|---|
| `/data/sunnypilot_weather.json` | Weather-adaptive (longitudinal + lateral) | `enabled`, `owm_api_key`, `refresh_s`, `offsets` |
| `/data/sunnypilot_accel.json` | Accel profile | `enabled`, `profile` (`eco`/`normal`/`sport`) |

Read-only status (auto-written, don't edit): `/data/sunnypilot_weather_status.json`.

Templates ship in the repo: `sunnypilot/selfdrive/controls/lib/weather_adaptive.example.json`
and `accel_profiles.example.json`.

## Weather-adaptive fields

Per condition (`rain`, `rain_storm`, `snow`, `low_visibility`):

| Field | Meaning | Safe range (clamped in code) |
|---|---|---|
| `follow_s` | seconds added to following time | 0 … 1.5 |
| `stop_ft` | feet added to the stopped gap (fades out with speed) | 0 … ~16 (capped at 5 m) |
| `accel_pct` | % reduction of max acceleration | 0 … 70 (never below 30% of normal) |
| `lat_pct` | % reduction of curve speed | 0 … 40 (never below 60% of normal) |

`enabled: true` **and** a non-empty `owm_api_key` are both required for it to act.
Get a free key at <https://openweathermap.org/api>. The key stays on the device.

## Accel profile

`profile`: `eco` (gentler), `normal` (stock), `sport` (punchier). Only affects how
briskly it accelerates to the set speed — never braking, following distance, or
any safety limit. Hard-capped at 2.5 m/s².

## Connectivity — reaching the phone page and getting weather

You configure settings **parked**, never while driving. The phone and the comma
3X just need to be on the same network:

- **Phone hotspot (recommended):** turn on your phone's personal hotspot and, in
  the device's Settings -> Network, connect the 3X to it (one-time). The device
  then has internet (weather fetches while you drive) and your phone can open
  `http://<device-ip>:8088` (the device shows its IP in Settings -> Network).
- **Device hotspot:** the 3X can broadcast its own WiFi (Settings -> Network ->
  Tethering). Your phone joins it and browses to the device IP — but there is no
  internet in this mode unless the device has a comma LTE SIM, so weather stays
  neutral.

Weather needs internet only to fetch the forecast, and it caches for ~3 hours, so
brief dead zones don't drop the offsets. The accel profile needs no internet at
all. Without a comma LTE SIM, run the 3X on your phone's hotspot.

## Editing settings

- **From your phone** (if the settings server is enabled): browse to
  `http://<device-ip>:8088` on the car's network. No login — use only on a trusted
  network (car hotspot / home WiFi).
- **Over SSH**: edit the JSON files directly with `nano`.

## Guideline: enabling and testing safely

1. Enable **one feature at a time**, drive it, confirm it behaves, then move on.
2. Treat the first drive after any change as a **shakedown** — empty road, hands
   ready. This build also carries a custom steering-torque tune.
3. The weather features only ever make driving **more** conservative; Sport accel
   is the only setting that increases assertiveness, and it is capped.
4. Start from the shipped defaults (conservative) and adjust in small steps.
5. After an over-the-air update, **reinstall on the device** to pick it up; your
   `/data` settings are preserved.

See [README](README.md) for install steps and [FEATURES](FEATURES.md) for the full
feature list.
