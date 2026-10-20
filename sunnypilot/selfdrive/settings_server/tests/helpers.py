class Msg:
  def __init__(self, **kw):
    self.__dict__.update(kw)


def fake_sm(started=True, v=25.0, a=0.0, pressed=False, torque=0.0, dcurv=0.0, lat=True, enabled=True,
            gas=False, brake=False, buttons=(), fix=True, glat=40.7, glon=-74.0):
  return {
    "deviceState": Msg(started=started),
    "carState": Msg(vEgo=v, aEgo=a, steeringPressed=pressed, steeringTorque=torque, gasPressed=gas, brakePressed=brake,
                  standstill=False, buttonEvents=list(buttons)),
    "selfdriveState": Msg(enabled=enabled),
    "carControl": Msg(latActive=lat),
    "controlsState": Msg(desiredCurvature=dcurv, curvature=dcurv),
    "gpsLocation": Msg(hasFix=fix, latitude=glat, longitude=glon),
  }
