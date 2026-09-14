"""SUMO's own built-in actuated (gap-based) traffic-light controller
(STEPS.md Step 8) -- the "real" competitor, not just a floor.

All the actual control logic lives in the network file, not here: mode
"actuated" makes sim/runner.py load networks/<scenario>/sim_actuated.sumocfg
(net_actuated.xml -- every junction's <tlLogic type="static"> rebuilt to
type="actuated" via netconvert, see that file's header comment for the exact
command) instead of the usual sim.sumocfg. SUMO auto-generates induction-loop
detectors at each stop line for that program at network-LOAD time (verified
empirically: green durations vary per cycle, e.g. 7-10s, instead of the
static network's constant 42s) -- there is nothing left for this controller
to decide every cycle.
"""

from __future__ import annotations

from sumo_agents.safety.validator import Action
from sumo_agents.sim.state import JunctionSnapshot


class ActuatedController:
    name = "actuated"

    def decide(self, snapshot: dict[str, JunctionSnapshot], sim_time: float) -> list[Action]:
        return []
