import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.lane_centering import status


@pytest.fixture(autouse=True)
def status_file(tmp_path, monkeypatch):
  """Keep the phone-status file out of /dev/shm during tests."""
  path = tmp_path / "sp_lane_centering.json"
  monkeypatch.setattr(status, "STATUS", str(path))
  return path
