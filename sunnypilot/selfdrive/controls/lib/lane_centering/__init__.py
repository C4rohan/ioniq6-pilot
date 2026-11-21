"""
Lane centering for the Ioniq 6 custom build, ported from StarPilot.

lane_centering.py, lane_tracking.py, lane_clearance.py and scalar_clip.py are copied from
StarPilot (github.com/firestar5683/StarPilot, branch stable, openpilot/starpilot/lateral) with
only their import lines changed, so they can be diffed against upstream. They derive from
jc01rho's lane-centering work; the MIT notice is in LICENSE. runtime.py is ours: it reads the
/data config and connects the controller to controlsd.
"""
