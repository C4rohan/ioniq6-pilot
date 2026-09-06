#!/usr/bin/env bash
# Build what's needed to run this (prebuilt) branch's Python raylib UI on macOS.
# The branch ships aarch64-Linux .so files and no root SConstruct, so we rebuild
# just three Cython modules for the Mac. No Homebrew needed: every native dep is
# pip-vendored into the venv by `uv sync`.
#
# Usage (from a FULL clone at a path with NO spaces):
#   bash tools/op.sh setup          # uv + .venv (Python 3.12); ignore its git-lfs complaint
#   bash tools/mac_dev/setup_mac_dev.sh
#   PYTHONPATH=$PWD .venv/bin/python selfdrive/ui/ui.py
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
case "$ROOT" in *" "*) echo "ERROR: repo path contains a space; comma's op.sh breaks on that. Move the clone."; exit 1;; esac
cd "$ROOT"
[ -x .venv/bin/scons ] || { echo "ERROR: .venv missing. Run: bash tools/op.sh setup"; exit 1; }
export PATH="$ROOT/.venv/bin:$PATH"

echo ">> [1/3] msgq (ipc_pyx, visionipc_pyx) via msgq_repo's own SConstruct"
( cd msgq_repo && scons -j"$(sysctl -n hw.ncpu)" )

echo ">> [2/3] params_pyx via trimmed root SConstruct + upstream common/SConscript"
cp tools/mac_dev/SConstruct.mac SConstruct
cp tools/mac_dev/common_SConscript.mac common/SConscript
scons -j"$(sysctl -n hw.ncpu)" common/params_pyx.so

echo ">> [3/3] verify"
for f in msgq_repo/msgq/ipc_pyx.so msgq_repo/msgq/visionipc/visionipc_pyx.so common/params_pyx.so; do
  file -b "$f" | grep -q "Mach-O" || { echo "ERROR: $f is not a Mach-O build"; exit 1; }
done
PYTHONPATH="$ROOT" .venv/bin/python -c "import msgq; from cereal import messaging; from openpilot.common.params import Params; from openpilot.system.ui.lib.application import gui_app; print('   msgq / cereal.messaging / Params / raylib app: OK')"
echo ">> done. Run the UI:  PYTHONPATH=$ROOT .venv/bin/python selfdrive/ui/ui.py"
