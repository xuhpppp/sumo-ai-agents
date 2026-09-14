"""Turn a validated Action into real TraCI calls, and read the real TLS
state needed to build a validator.TlsState (STEPS.md Step 8).

Split out from sim/state.py (read-only metric aggregation) because this
module WRITES to the simulation -- a deliberately separate, narrower
surface. Used today by baselines/maxpressure.py and, from Phase 2 on, by
the LLM-driven orchestrator (same Action type, same validator gate).
"""

from __future__ import annotations

from sumo_agents.safety.validator import (
    Action,
    AdjustPhaseSplit,
    NoAction,
    PhaseKind,
    PhaseState,
    TlsState,
)
from sumo_agents.sim.conn import SumoConnection


def phase_kind(state: str) -> PhaseKind:
    """Classify a SUMO tlLogic phase's link-state string.

    Convention (see grid_4x4's net.xml <tlLogic>, cross-checked in STEPS.md
    Step 6/8): any 'y'/'Y' means a yellow transition phase, even if it also
    contains a 'G' for a permissive movement that stays green throughout
    (e.g. "yyyyrrrrGyyy") -- transitions are never adjustable (validator.py
    rejects touching them). No green or yellow signal anywhere means
    all-red.
    """
    lowered = state.lower()
    if "y" in lowered:
        return "yellow"
    if "g" in lowered:
        return "green"
    return "all_red"


def read_tls_state(
    conn: SumoConnection,
    junction_id: str,
    *,
    time_since_last_green_s: dict[str, float] | None = None,
) -> TlsState:
    """Build a validator.TlsState from the junction's currently active
    program (assumes a single program per junction, programID "0" -- true
    for every scenario this project generates, STEPS.md Step 3/8).

    `time_since_last_green_s` defaults to empty (validator then treats
    every phase as "recently green") -- Step 8's baselines don't track
    starvation history across cycles; that arrives with the agent
    decision-cycle loop (Phase 2).
    """
    logic = conn.trafficlight.getAllProgramLogics(junction_id)[0]
    phases = tuple(
        PhaseState(phase_id=str(index), duration_s=phase.duration, kind=phase_kind(phase.state))
        for index, phase in enumerate(logic.phases)
    )
    return TlsState(
        junction_id=junction_id,
        phases=phases,
        time_since_last_green_s=dict(time_since_last_green_s or {}),
    )


def apply_action(conn: SumoConnection, action: Action) -> None:
    """Apply an already-validated action via TraCI.

    Only the action types Step 8's baselines actually emit are implemented;
    the rest raise NotImplementedError until Phase 2 needs them (JunctionAgent
    gets the full action space, STEPS.md Step 12+).
    """
    if isinstance(action, NoAction):
        return
    if isinstance(action, AdjustPhaseSplit):
        _apply_adjust_phase_split(conn, action)
        return
    raise NotImplementedError(f"apply_action: {type(action).__name__} not implemented yet (Phase 2)")


def _apply_adjust_phase_split(conn: SumoConnection, action: AdjustPhaseSplit) -> None:
    if action.delta_s == 0:
        return  # no-op clamp result -- avoid a pointless setProgramLogic round trip

    Phase = conn.trafficlight.Phase
    Logic = conn.trafficlight.Logic
    logic = conn.trafficlight.getAllProgramLogics(action.junction_id)[0]
    phase_index = int(action.phase_id)

    phases = list(logic.phases)
    old = phases[phase_index]
    # minDur/maxDur are ignored by static-type programs (Phase's own
    # docstring) -- carried over unchanged, only `duration` matters here.
    phases[phase_index] = Phase(old.duration + action.delta_s, old.state, old.minDur, old.maxDur, old.next, old.name)

    conn.trafficlight.setProgramLogic(
        action.junction_id,
        Logic(logic.programID, logic.type, logic.currentPhaseIndex, phases, logic.subParameter),
    )
