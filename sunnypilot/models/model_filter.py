"""
Ioniq6 build: hide old model folders from the driving-model selector.

sunnypilot's catalog keeps every model ever published (68 in v17). This trims the list to
the newer folders. The active model is ALWAYS kept: the model manager resets you to the
default model if the active bundle is missing from the list (helpers.validate_active_bundle).

Override on the device with /data/sunnypilot_models.json:
  {"hidden_folders": ["Legacy Models", ...]}     # [] shows everything
"""
import json
import os

DATA = os.environ.get("SUNNYPILOT_DATA", "/data")
CONFIG = os.path.join(DATA, "sunnypilot_models.json")
DEFAULT_HIDDEN = ["Legacy Models", "2025 Pre World Models", "2025 World Models", "Custom Merge Models"]


def hidden_folders(path: str | None = None) -> set[str]:
  try:
    with open(path or CONFIG) as f:
      cfg = json.load(f)
    if isinstance(cfg, dict) and isinstance(cfg.get("hidden_folders"), list):
      return {str(x) for x in cfg["hidden_folders"]}
  except FileNotFoundError:
    pass
  except Exception:
    pass  # a broken file must never empty the selector; fall back to the defaults
  return set(DEFAULT_HIDDEN)


def _folder(bundle) -> str:
  for o in getattr(bundle, "overrides", []) or []:
    if o.key == "folder":
      return o.value
  return ""


def _same(a, b) -> bool:
  ref_a, ref_b = getattr(a, "ref", None), getattr(b, "ref", None)
  if ref_a and ref_b:
    return ref_a == ref_b
  return getattr(a, "internalName", None) == getattr(b, "internalName", None)


def visible_bundles(bundles, active=None, hidden: set[str] | None = None):
  hidden = hidden_folders() if hidden is None else hidden
  return [b for b in bundles if _folder(b) not in hidden or (active is not None and _same(b, active))]
