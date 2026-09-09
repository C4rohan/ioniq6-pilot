"""
Reverse camera view for the Ioniq 6 build.

When the car is shifted into reverse, show a clean full-screen camera view: the
base camera stays, but the driving overlays (model path, HUD, driver-monitoring)
are hidden and a small REVERSE badge is drawn, so the screen acts as a parking
aid instead of the driving HUD.

NOTE: the comma 3X has no rear camera. This shows the forward road camera
(usually the wide one at low speed) — useful for watching the front of the car
while backing out, not a true backup camera.

Config: /data/sunnypilot_reverse_cam.json  (optional)
  {"enabled": true, "hide_overlays": true}
Enabled by default; delete the file or set enabled=false to keep the normal HUD
in reverse. Reads are cached, so it costs the render loop nothing per frame.
"""
import json
import os
import time

import pyray as rl

from openpilot.common.swaglog import cloudlog
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached

CONFIG_PATH = "/data/sunnypilot_reverse_cam.json"
CONFIG_CHECK_PERIOD = 2.0  # s

BADGE_FONT = 62
PILL_H = 108
PAD_X = 44
GAP = 22
TOP_MARGIN = 40
BG = rl.Color(0x00, 0x00, 0x00, 0xB0)
BORDER = rl.Color(0xFF, 0xFF, 0xFF, 0x33)
R_COLOR = rl.Color(0xFF, 0x5A, 0x4F, 0xFF)  # reverse red
TXT = rl.Color(0xFF, 0xFF, 0xFF, 0xFF)


class ReverseCam:
  def __init__(self):
    self.enabled = True
    self.hide_overlays = True
    self._mtime = None
    self._checked = 0.0
    self._font = None

  def _reload(self) -> None:
    now = time.monotonic()
    if now - self._checked < CONFIG_CHECK_PERIOD:
      return
    self._checked = now
    try:
      mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
      mtime = None
    if mtime == self._mtime:
      return
    self._mtime = mtime
    if mtime is None:
      self.enabled, self.hide_overlays = True, True   # default on when unconfigured
      return
    try:
      with open(CONFIG_PATH) as f:
        cfg = json.load(f)
      self.enabled = bool(cfg.get("enabled", True))
      self.hide_overlays = bool(cfg.get("hide_overlays", True))
    except Exception as e:
      cloudlog.warning(f"reverse_cam: bad config: {e}")
      self.enabled, self.hide_overlays = True, True

  def is_reverse(self, sm) -> bool:
    """True when the gear is reverse. Fails safe to False on any error."""
    try:
      return str(sm["carState"].gearShifter) == "reverse"
    except Exception:
      return False

  def active(self, sm) -> bool:
    """Show the clean reverse view (hide overlays) this frame?"""
    self._reload()
    return self.enabled and self.hide_overlays and self.is_reverse(sm)

  def render(self, rect: rl.Rectangle) -> None:
    """Draw the REVERSE badge, centered near the top of the content area."""
    try:
      if self._font is None:
        self._font = gui_app.font(FontWeight.BOLD)
      r_w = measure_text_cached(self._font, "R", BADGE_FONT).x
      t_w = measure_text_cached(self._font, "REVERSE", BADGE_FONT).x
      pill_w = PAD_X * 2 + r_w + GAP + t_w
      x = rect.x + (rect.width - pill_w) / 2
      y = rect.y + TOP_MARGIN
      pill = rl.Rectangle(x, y, pill_w, PILL_H)
      rl.draw_rectangle_rounded(pill, 0.5, 12, BG)
      rl.draw_rectangle_rounded_lines_ex(pill, 0.5, 12, 4, BORDER)
      ty = y + (PILL_H - BADGE_FONT) / 2
      rl.draw_text_ex(self._font, "R", rl.Vector2(x + PAD_X, ty), BADGE_FONT, 0, R_COLOR)
      rl.draw_text_ex(self._font, "REVERSE", rl.Vector2(x + PAD_X + r_w + GAP, ty), BADGE_FONT, 0, TXT)
    except Exception as e:
      cloudlog.warning(f"reverse_cam: render error: {e}")
