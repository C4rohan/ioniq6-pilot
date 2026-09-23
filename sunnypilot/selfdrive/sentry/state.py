"""
Sentry arm state, shared with the process manager.

Kept tiny and dependency-free on purpose: system/manager/process_config.py imports
sentry_armed() to decide whether camerad should run while the car is parked.
The state lives in /tmp so a reboot always starts disarmed.
"""
import os

STATE_PATH = os.environ.get("SUNNYPILOT_SENTRY_STATE", "/tmp/sunnypilot_sentry_state")


def sentry_armed() -> bool:
  try:
    with open(STATE_PATH) as f:
      return f.read().strip() == "armed"
  except OSError:
    return False


def write_state(armed: bool) -> None:
  tmp = STATE_PATH + ".tmp"
  with open(tmp, "w") as f:
    f.write("armed" if armed else "idle")
  os.replace(tmp, STATE_PATH)
