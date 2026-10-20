import pytest

PATHS = {
  "TRIPS_CSV": "sp_trips.csv", "DISENGAGE_CSV": "sp_disengagements.csv", "OVERRIDES_CSV": "sp_overrides.csv",
  "STEER_STATS": "sp_steer_stats.json", "NOTIFY_CONFIG": "sunnypilot_notify.json",
  "STEER_CONFIG": "sunnypilot_steer_feedback.json", "TRACK_CSV": "sp_track.csv",
  "CLIPS_CONFIG": "sunnypilot_clips.json", "CLIPS_DIR": "saved_clips",
}


@pytest.fixture
def tm(tmp_path, monkeypatch):
  """telemetry module with every file redirected into this test's tmp dir."""
  from openpilot.sunnypilot.selfdrive.settings_server import telemetry
  monkeypatch.setattr(telemetry, "DATA", str(tmp_path))
  for name, fname in PATHS.items():
    monkeypatch.setattr(telemetry, name, str(tmp_path / fname))
  monkeypatch.setattr(telemetry, "active_model_name", lambda: "TEST")
  return telemetry
