"""
Stand-in for StarPilot's flm/torque_stages.py (its automatic steering-learning hooks).

StarPilot's hooks return their baseline unchanged when no learning profile is attached to the
controller (no manual_flm_effects / route_flm_effects / automatic_flm_stages), which is always the
case in this build. These keep the same call signatures and return the baseline, so
ioniq6_policy.py runs unmodified.
"""


def apply_selected_deadzone(parent, cs, vm, baseline_accel, requested_curvature, *, active, limited, curvature_limited=False):
  return baseline_accel


def apply_stages(parent, cs, setpoint, jerk, ff, threshold, *, active, limited, curvature_limited=False):
  return ff, threshold


def apply_selected_friction(parent, cs, setpoint, baseline, *, active, limited, curvature_limited=False):
  return baseline
