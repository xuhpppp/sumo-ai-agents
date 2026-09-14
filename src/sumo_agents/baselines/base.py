"""Shared Controller interface for baselines (STEPS.md Step 8) -- the SAME
shape Phase 2's JunctionAgent/orchestrator will implement (IMPLEMENTATION_PLAN.md
section 8), so switching SimRunner to mode='llm' later is a drop-in
controller swap, not a rewrite of the control loop.
"""

from __future__ import annotations

from typing import Protocol

from sumo_agents.safety.validator import Action
from sumo_agents.sim.state import JunctionSnapshot


class Controller(Protocol):
    name: str  # matches Run.mode: 'fixed' | 'actuated' | 'maxpressure' | 'llm'

    def decide(self, snapshot: dict[str, JunctionSnapshot], sim_time: float) -> list[Action]:
        """Called once per control interval (sim/runner.py's CONTROL_INTERVAL_S).

        An empty list means "don't touch anything this cycle" -- exactly as
        valid (and, per plan section 5, encouraged for `no_action`) as
        returning explicit NoAction entries.
        """
        ...

    # `observe(snapshot, sim_time) -> None` is an OPTIONAL extension, not
    # part of this protocol: sim/runner.py calls it (if defined, via
    # getattr/duck-typing) every metric-sample tick, more often than
    # decide(). baselines/maxpressure.py uses it to average a noisy signal
    # over the whole control interval instead of sampling it once at
    # decide() time -- see its docstring for why that mattered empirically.
    # fixed/actuated don't define it.
