"""
StarPilot's Ioniq 6 steering controller, for the Ioniq 6 custom build.

ioniq6_policy.py and torque_shaping.py are copied from StarPilot (github.com/firestar5683/StarPilot,
branch stable, openpilot/starpilot/lateral) with only their import lines changed; STARPILOT_COMMIT
records the source commit. stages.py stands in for StarPilot's learning hooks (unused here).
controller.py is ours: it hosts the policy inside sunnypilot's controlsd and falls back to
sunnypilot's own controller on any error. MIT notice in LICENSE.
"""
STARPILOT_COMMIT = "008b0cbcf1c7f9a71a6eee1c2e6e11419aaaebb2"
