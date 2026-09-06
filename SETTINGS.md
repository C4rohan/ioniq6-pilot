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
| `/data/sunnypilot_notify.json` | Notifications | `enabled`, `type` (`telegram`/`webhook`), `bot_token`, `chat_id`, `webhook_url`, `notify_trips`, `notify_zones` |
| `/data/sunnypilot_geofences.json` | Geofenced accel profile | `enabled`, `default_accel_profile`, `zones[]` (`name`, `lat`, `lon`, `radius_m`, `accel_profile`) |

Read-only, auto-written (don't edit): `/data/sunnypilot_weather_status.json`, `/data/sp_trips.csv`, `/data/sp_disengagements.csv`.

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

**Night mode:** set `night_enabled: true`; the `offsets.night` entry is applied after sunset / before sunrise (times come from the same OWM lookup) whenever the weather is otherwise clear. A real weather condition always wins over night.
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

## Notifications & geofences

- **Notifications:** `type: telegram` needs a bot token + chat id (message @BotFather, then @userinfobot for your id). `type: webhook` POSTs `{"text": ...}` to `webhook_url` — point it at any bridge (e.g. your own WhatsApp gateway). Secrets stay on the device and are masked on the phone page.
- **Geofences:** each zone is a circle (`lat`, `lon`, `radius_m`). Entering a zone writes its `accel_profile` into `sunnypilot_accel.json`; leaving restores `default_accel_profile`. It only writes on a zone *change*, so manual edits aren't fought. Get coordinates from Google Maps (long-press a spot).

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
