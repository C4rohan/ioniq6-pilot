# Features — Ioniq 6 custom build

A reference for everything on this branch (`ioniq6-tizi-custom`). It tracks the
latest sunnypilot `release-tizi` with the custom work below layered on top.

Car: **Hyundai Ioniq 6 (2023–24, non-HDA-II / HDA)** · Device: **comma 3X** · Harness: **Hyundai "L"** (CAN-FD)

---

## Custom additions (built on top of sunnypilot)

| Feature | What it does | Default | Config / how to enable |
|---|---|---|---|
| **Ioniq 6 NNLC model** | Neural-net steering model copied from the Ioniq 5 (same E-GMP platform) so NNLC resolves for the Ioniq 6 as an exact match | Active when NNLC toggle is on | on-device toggle |
| **Ioniq 6 torque tune** | Lateral steering torque aligned to the Ioniq 5 measured fit `[3.172929, 2.713050, 0.096019]` (replaced the `[2.5, 2.5, 0.005]` placeholder) | Always active | baked into the branch |
| **Weather-adaptive longitudinal** | In rain / storm / snow / low visibility: more following time, larger stopped gap, reduced max acceleration | **Off** | `/data/sunnypilot_weather.json` (needs OpenWeatherMap key) |
| **Weather-adaptive lateral** | Same conditions: slows more for curves (shrinks the lateral-accel budget the vision turn controller uses) | **Off** | same file, `lat_pct` per condition |
| **Accel profiles (Eco/Normal/Sport)** | Scales how briskly it accelerates to the set speed; never touches braking, gap, or safety limits; hard-capped at 2.5 m/s² | **Off (Normal)** | `/data/sunnypilot_accel.json` |
| **On-device settings server** | Edit the config files above from a phone browser on the car's network (`:8088`), instead of SSH | **Off (code only, not registered)** | manual one-line `process_config.py` edit |

All custom features are **off by default**, **fail safe to stock** (any error / offline / stale / disabled → identical to unmodified sunnypilot), and configured via on-device JSON files (new settings can't be registered on a prebuilt branch). API keys stay on the device and never enter this repo.

### Safety design
- Every hook is a `getattr(..., neutral_default)` — the code path is inert unless configured.
- Hard clamps: extra following ≤ +1.5 s · extra stopped gap ≤ 5 m · min accel factor 0.30 · min curve-speed factor 0.60 · absolute max accel 2.5 m/s².
- The weather features only ever make driving **more** conservative (except Sport accel, which is capped).

### Weather-adaptive defaults
Per condition, as *sec added to following / feet added to stopped gap / % max-accel reduction / % curve-speed reduction*:

| Condition | follow | stop gap | max accel | curve speed |
|---|---|---|---|---|
| Rain | +0.3 s | +3 ft | −15% | −10% |
| Rain storm | +0.5 s | +5 ft | −30% | −20% |
| Snow | +0.7 s | +8 ft | −40% | −30% |
| Low visibility | +0.4 s | +4 ft | −20% | −15% |

These are conservative starting points, not field-tested values — tune them in the config file.

### Accel profile factors
Speed-dependent multiplier on the max-accel ceiling: Eco 0.70–0.80× · Normal 1.0× · Sport 1.45× (low speed) tapering to 1.10× (highway). Result hard-capped at 2.5 m/s².

---

## sunnypilot base features (inherited — toggle on-device)

- **MADS** / Always-on Lateral — steering stays active independent of cruise
- **NNLC** — Neural Network Lateral Control, plus enforced torque control
- **Auto Lane Change** — nudgeless / auto on signal
- **Blinker-pause lateral**, **Lane-turn desire**
- **Smart Cruise Control** — Vision Turn Speed + Map Turn Speed (curve slowing)
- **Speed Limit** — resolver + assist (OpenStreetMap / mapd / dashboard, with offsets)
- **Dynamic Experimental Control** — auto-switch between ACC and end-to-end
- **E2E alerts** — green-light alert, lead-departure alert
- **Custom driving-model selector** — sunnypilot's model library
- Standard openpilot: ACC, lane centering, driver monitoring, comma connect logging

---

## Not included (and why)

- **Adjacent-lead tracking** — not safely portable on this setup. The device only exposes in-path leads (`radarState.leadOne/leadTwo`); adjacent-lane data needs raw radar tracks (compiled `radard` changes), and the Ioniq 6 runs camera-SCC.
- **Themes / UI customization, custom-model runtime swaps** — these are compiled Qt/model changes that need a full (non-prebuilt) build environment.

---

## Maintenance

- The branch auto-rebases onto the latest sunnypilot `release-tizi` weekly, replaying every custom patch. It fails safe on conflict (aborts, leaves the branch unchanged, notifies).
- The patch set touches several core longitudinal/lateral files, so occasional manual conflict resolution is expected as sunnypilot evolves.
- After any update, **reinstall on the device** to pull it, and treat the first drive as a shakedown.

See [README](README.md) for install and per-feature setup steps.
