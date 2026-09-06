#!/usr/bin/env python3
"""
Fake drive for the Mac dev UI. Publishes the messages the raylib UI needs to
show the ONROAD HUD (engaged, ~100 km/h, gentle curves) plus a synthetic road
camera frame over VisionIPC. Dev-only; never runs on the device.

  PYTHONPATH=$PWD .venv/bin/python tools/mac_dev/fake_drive.py [--kph 100] [--no-engage]
"""
import argparse, math, time
import numpy as np
import cereal.messaging as messaging
from cereal import log, car
from msgq.visionipc import VisionIpcServer, VisionStreamType
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper

W, H = 1928, 1208           # comma 3X road camera (tici/tizi AR config)
N = 33                      # model points
X = np.linspace(0.0, 150.0, N)
T = np.linspace(0.0, 10.0, N)


def nv12_frame() -> bytes:
  y = np.full((H, W), 38, np.uint8)                       # asphalt
  y[: int(H * 0.52)] = 70                                 # sky
  hz = int(H * 0.52); cx = W // 2
  for r in range(hz, H):                                  # road trapezoid
    t = (r - hz) / (H - hz); half = int(60 + t * (W * 0.42))
    y[r, max(0, cx - half):min(W, cx + half)] = 58
    if (r // 28) % 2 == 0:                                # dashed centre line
      y[r, cx - 4:cx + 4] = 200
    y[r, max(0, cx - half):max(0, cx - half + 6)] = 170   # edges
    y[r, min(W - 1, cx + half - 6):min(W, cx + half)] = 170
  uv = np.full((H // 2, W), 128, np.uint8)                # neutral chroma
  return np.concatenate([y.ravel(), uv.ravel()]).tobytes()


def xyzt(obj, x, y, z):
  obj.x, obj.y, obj.z, obj.t = [float(v) for v in x], [float(v) for v in y], [float(v) for v in z], [float(v) for v in T]


def main():
  ap = argparse.ArgumentParser(); ap.add_argument("--kph", type=float, default=100.0); ap.add_argument("--no-engage", action="store_true")
  a = ap.parse_args()
  Params().put_bool("IsMetric", True)

  services = ["deviceState", "pandaStates", "carParams", "carState", "controlsState", "selfdriveState", "modelV2",
              "liveCalibration", "radarState", "longitudinalPlan", "driverMonitoringState", "roadCameraState",
              "wideRoadCameraState", "onroadEvents", "liveParameters", "carControl", "carOutput", "gpsLocationExternal"]
  pm = messaging.PubMaster(services)

  vipc = VisionIpcServer("camerad")
  size = W * H * 3 // 2
  for st in (VisionStreamType.VISION_STREAM_ROAD, VisionStreamType.VISION_STREAM_WIDE_ROAD):
    vipc.create_buffers_with_sizes(st, 5, W, H, size, W, W * H)
  vipc.start_listener()
  frame = nv12_frame()

  rk = Ratekeeper(20, print_delay_threshold=None)
  t0 = time.monotonic(); fid = 0; v_set = a.kph / 3.6
  print(f"fake_drive: publishing {len(services)} services + camera frames at 20 Hz (target {a.kph:.0f} km/h)", flush=True)
  while True:
    t = time.monotonic() - t0; fid += 1
    v = min(v_set, t * 1.5) + (1.5 * math.sin(t / 6.0) if t > 10 else 0.0)
    engaged = (not a.no_engage) and t > 3.0
    curv = 0.0012 * math.sin(t / 4.0)
    ts = int(time.monotonic() * 1e9)

    for st in (VisionStreamType.VISION_STREAM_ROAD, VisionStreamType.VISION_STREAM_WIDE_ROAD):
      vipc.send(st, frame, fid, ts, ts)
    for name in ("roadCameraState", "wideRoadCameraState"):
      m = messaging.new_message(name); c = getattr(m, name); c.frameId = fid; c.timestampSof = ts; c.timestampEof = ts; pm.send(name, m)

    cs = messaging.new_message("carState"); s = cs.carState
    s.vEgo = v; s.vEgoCluster = v; s.aEgo = 0.0; s.standstill = v < 0.1
    s.steeringAngleDeg = math.degrees(curv * 2.97 * 14.26); s.vCruise = a.kph; s.vCruiseCluster = a.kph
    s.cruiseState.enabled = True; s.cruiseState.available = True; s.cruiseState.speed = v_set
    s.gearShifter = car.CarState.GearShifter.drive; pm.send("carState", cs)

    ss = messaging.new_message("selfdriveState"); d = ss.selfdriveState
    d.enabled = engaged; d.active = engaged
    d.state = log.SelfdriveState.OpenpilotState.enabled if engaged else log.SelfdriveState.OpenpilotState.disabled
    d.personality = log.LongitudinalPersonality.standard; pm.send("selfdriveState", ss)

    cc = messaging.new_message("controlsState"); cc.controlsState.curvature = curv; cc.controlsState.desiredCurvature = curv; pm.send("controlsState", cc)

    md = messaging.new_message("modelV2"); m2 = md.modelV2
    yv = 0.5 * curv * X ** 2
    xyzt(m2.position, X, yv, np.zeros(N)); xyzt(m2.velocity, np.full(N, v), np.zeros(N), np.zeros(N)); xyzt(m2.orientation, np.zeros(N), np.zeros(N), np.zeros(N))
    ll = m2.init("laneLines", 4)
    for i, off in enumerate((-3.6, -1.8, 1.8, 3.6)): xyzt(ll[i], X, yv + off, np.zeros(N))
    m2.laneLineProbs = [1.0, 1.0, 1.0, 1.0]
    re = m2.init("roadEdges", 2)
    for i, off in enumerate((-5.4, 5.4)): xyzt(re[i], X, yv + off, np.zeros(N))
    m2.frameId = fid; pm.send("modelV2", md)

    lp = messaging.new_message("longitudinalPlan"); lp.longitudinalPlan.speeds = [float(v)] * N; lp.longitudinalPlan.accels = [0.0] * N; lp.longitudinalPlan.jerks = [0.0] * N; pm.send("longitudinalPlan", lp)
    rs = messaging.new_message("radarState"); rs.radarState.leadOne.status = False; pm.send("radarState", rs)
    dm = messaging.new_message("driverMonitoringState"); dm.driverMonitoringState.faceDetected = True; dm.driverMonitoringState.isActiveMode = True; dm.driverMonitoringState.awarenessStatus = 1.0; pm.send("driverMonitoringState", dm)
    oe = messaging.new_message("onroadEvents", 0); pm.send("onroadEvents", oe)
    ccm = messaging.new_message("carControl"); ccm.carControl.enabled = engaged; ccm.carControl.latActive = engaged; ccm.carControl.longActive = engaged; pm.send("carControl", ccm)
    co = messaging.new_message("carOutput"); pm.send("carOutput", co)

    if fid % 5 == 0:   # 4 Hz group
      ds = messaging.new_message("deviceState"); ds.deviceState.started = True; ds.deviceState.freeSpacePercent = 60; ds.deviceState.memoryUsagePercent = 30
      ds.deviceState.screenBrightnessPercent = 100; ds.deviceState.thermalStatus = log.DeviceState.ThermalStatus.green; ds.deviceState.networkType = log.DeviceState.NetworkType.wifi; pm.send("deviceState", ds)
      ps = messaging.new_message("pandaStates", 1); p = ps.pandaStates[0]; p.ignitionLine = True; p.controlsAllowed = engaged
      p.pandaType = getattr(log.PandaState.PandaType, "tres", log.PandaState.PandaType.uno); pm.send("pandaStates", ps)
      cp = messaging.new_message("carParams"); c = cp.carParams; c.carName = "HYUNDAI_IONIQ_6"; c.carFingerprint = "HYUNDAI_IONIQ_6"; c.brand = "hyundai"
      c.openpilotLongitudinalControl = True; c.steerControlType = car.CarParams.SteerControlType.torque; c.steerRatio = 14.26; c.wheelbase = 2.97; pm.send("carParams", cp)
      lc = messaging.new_message("liveCalibration"); l = lc.liveCalibration; l.calStatus = log.LiveCalibrationData.Status.calibrated; l.calPerc = 100
      l.rpyCalib = [0.0, 0.0, 0.0]; l.height = [1.22]; l.validBlocks = 20; pm.send("liveCalibration", lc)
      lpar = messaging.new_message("liveParameters"); lpar.liveParameters.angleOffsetDeg = 0.0; lpar.liveParameters.valid = True; pm.send("liveParameters", lpar)
      g = messaging.new_message("gpsLocationExternal"); g.gpsLocationExternal.hasFix = True; g.gpsLocationExternal.latitude = 40.7128; g.gpsLocationExternal.longitude = -74.0060
      g.gpsLocationExternal.unixTimestampMillis = int(time.time() * 1000); pm.send("gpsLocationExternal", g)
    rk.keep_time()


if __name__ == "__main__":
  main()
