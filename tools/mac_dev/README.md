# Run the real driving UI on a Mac

This branch is a **prebuilt** device branch: its `.so` files are aarch64-Linux and the
root build system is stripped. This folder rebuilds only the three Cython modules the
Python (raylib) UI needs, then feeds the UI a fake drive so the **onroad HUD** renders on
your Mac with no car, no device, and no replay binary. Use it to design and test UI
changes before they go on the 3X.

Requirements: Apple Silicon Mac, Xcode Command Line Tools. **No Homebrew packages needed** —
every native dependency is pip-vendored into the venv by `uv sync`.

## One-time setup

```bash
# 1) full clone at a path with NO spaces (comma's op.sh breaks on spaces)
git clone --depth 1 --branch ioniq6-tizi-custom https://github.com/C4rohan/ioniq6-pilot.git ~/openpilot-dev
cd ~/openpilot-dev

# 2) uv + .venv (Python 3.12). It ends with a "git lfs files not found" complaint — ignore it,
#    this branch tracks no LFS files.
bash tools/op.sh setup

# 3) build msgq (ipc_pyx, visionipc_pyx) and common/params_pyx for macOS, then verify imports
bash tools/mac_dev/setup_mac_dev.sh
```

## Run the simulator (two terminals)

```bash
# terminal 1 — the real UI (a raylib window opens; offroad screen until data arrives)
cd ~/openpilot-dev && PYTHONPATH=$PWD .venv/bin/python selfdrive/ui/ui.py

# terminal 2 — fake drive: flips the UI onroad, engaged, ramping to 100 km/h with gentle curves,
# plus a synthetic road camera over VisionIPC
cd ~/openpilot-dev && PYTHONPATH=$PWD .venv/bin/python tools/mac_dev/fake_drive.py --kph 100
#   --kph N        target speed
#   --no-engage    stay disengaged (test the not-engaged HUD state)
```

Stop with Ctrl-C in each terminal (or `pkill -f selfdrive/ui/ui.py; pkill -f fake_drive.py`).

## Iterating on UI code

Edit files under `selfdrive/ui/` (e.g. `onroad/hud_renderer.py`) and restart terminal 1.
Use `tools/ui_mock/hud_mock.html` first to design placements; it prints device-pixel specs
for the 2160 × 1080 frame that map directly onto `rl.Rectangle` values.

## Expected noise on macOS (harmless)

- `Failed to connect to system D-Bus` / `send_and_get_reply` / `FileNotFoundError` tracebacks:
  the offroad WiFi manager probing Linux D-Bus. The UI keeps running.
- `FPS dropped below 60: NN`: the render loop reporting frame time — it means it's drawing.

## Why the publisher uses "safe setters"

cereal schemas move between versions, so `fake_drive.py` sets optional fields and enum
values through helpers that skip anything this branch's schema doesn't have, instead of
crashing on a renamed field.
