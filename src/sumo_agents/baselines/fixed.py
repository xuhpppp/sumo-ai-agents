"""Floor baseline (STEPS.md Step 8): never touches the traffic lights --
the network's already-loaded static <tlLogic> program runs unmodified.
Every other mode is expected to beat this; if it doesn't, that mode is
broken, not the network.
"""

from __future__ import annotations

from sumo_agents.safety.validator import Action
from sumo_agents.sim.state import JunctionSnapshot


class FixedController:
    name = "fixed"

    def decide(self, snapshot: dict[str, JunctionSnapshot], sim_time: float) -> list[Action]:
        return []
