"""Stand-in for StarPilot's SignalTurnEvidence (starpilot/lateral/signal_turn.py).

LaneChangeSmoother builds one, but only StarPilot's turn assist calls it (controlsd's observe());
this build has no turn assist and always passes turn_assist=False, so it's never used.
"""


class SignalTurnEvidence:
  def reset(self):
    pass

  def observe(self, *args, **kwargs) -> bool:
    return False
