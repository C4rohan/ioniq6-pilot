import json

import pytest

from openpilot.sunnypilot.models import model_filter as mf
from openpilot.sunnypilot.models.fetcher import ModelFetcher, ModelParser


def bundle(name, folder=None, ref=None, index=0):
  b = {"short_name": name, "display_name": name, "index": index, "generation": "12", "environment": "development",
       "runner": "tinygrad", "is_20hz": True, "minimum_selector_version": "15", "ref": ref or f"ref-{name}", "models": []}
  if folder:
    b["overrides"] = {"folder": folder}
  return b


CATALOG = {"bundles": [bundle("OLD", "Legacy Models", index=1), bundle("PRE", "2025 Pre World Models", index=2),
                       bundle("W25", "2025 World Models", index=3), bundle("MERGE", "Custom Merge Models", index=4),
                       bundle("W26", "2026 World Models", index=5), bundle("MASTER", "Master Models", index=6),
                       bundle("REL", "Release Models", index=7), bundle("NOFOLDER", index=8)]}


def parsed():
  return ModelParser.parse_models(CATALOG)


def names(bs):
  return [b.internalName for b in bs]


@pytest.fixture(autouse=True)
def no_device_config(tmp_path, monkeypatch):
  monkeypatch.setattr(mf, "CONFIG", str(tmp_path / "sunnypilot_models.json"))
  return tmp_path / "sunnypilot_models.json"


def test_default_hides_old_folders():
  assert names(mf.visible_bundles(parsed())) == ["W26", "MASTER", "REL", "NOFOLDER"]


@pytest.mark.parametrize("active", [lambda bs: bs[0], lambda bs: ModelParser._parse_bundle({**bundle("OLD", "Legacy Models", index=1), "ref": ""})])
def test_active_model_in_hidden_folder_is_kept(active):
  bs = parsed()
  assert "OLD" in names(mf.visible_bundles(bs, active(bs)))       # matched by ref, then by name when there is no ref


@pytest.mark.parametrize("content,expect", [
  ({"hidden_folders": []}, 8),
  ({"hidden_folders": ["Legacy Models"]}, 7),
  ({"other": 1}, 4),
  ("not json", 4),
])
def test_device_config(no_device_config, content, expect):
  no_device_config.write_text(content if isinstance(content, str) else json.dumps(content))
  assert len(mf.visible_bundles(parsed())) == expect


class FakeParams:
  def __init__(self, active=None):
    self.store = {"ModelManager_ModelsCache": CATALOG, "ModelManager_LastSyncTime": 1}
    if active:
      self.store["ModelManager_ActiveBundle"] = active

  def get(self, key, **_):
    return self.store.get(key)

  def put(self, key, value, **_):
    self.store[key] = value


def test_fetcher_filters_but_keeps_active(monkeypatch):
  monkeypatch.setattr("openpilot.sunnypilot.models.fetcher.ModelCache._is_expired", lambda self: False)
  assert names(ModelFetcher(FakeParams()).get_available_bundles()) == ["W26", "MASTER", "REL", "NOFOLDER"]
  active = ModelParser._parse_bundle(CATALOG["bundles"][2]).to_dict()
  assert "W25" in names(ModelFetcher(FakeParams(active)).get_available_bundles())


def test_active_survives_manager_validation(monkeypatch):
  """The real reason the active model is kept: validate_active_bundle resets it if it's missing from the list."""
  from openpilot.sunnypilot.models import helpers
  monkeypatch.setattr("openpilot.sunnypilot.models.fetcher.ModelCache._is_expired", lambda self: False)
  monkeypatch.setattr(helpers, "_bundle_is_valid_locally", lambda b: True)
  monkeypatch.setattr(helpers, "_LAST_VALIDATED_RAW", None)
  active = ModelParser._parse_bundle(CATALOG["bundles"][0]).to_dict()
  p = FakeParams(active)
  p.remove = lambda key: p.store.pop(key, None)
  helpers.validate_active_bundle(p, ModelFetcher(p).get_available_bundles())
  assert p.store.get("ModelManager_ActiveBundle") == active
