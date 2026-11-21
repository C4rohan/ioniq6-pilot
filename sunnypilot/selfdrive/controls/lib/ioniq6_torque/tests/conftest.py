import pytest

from openpilot.sunnypilot.selfdrive.controls.lib.ioniq6_torque import controller


@pytest.fixture(autouse=True)
def status_file(tmp_path, monkeypatch):
  """Keep the controller-status file out of /dev/shm during tests."""
  monkeypatch.setattr(controller, "STATUS", str(tmp_path / "sp_steering_controller.json"))
