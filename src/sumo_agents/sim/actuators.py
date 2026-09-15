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
    SetCycleLength,
    SetOffset,
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

    `request_vms` is the one action type still not implemented here --
    it needs real rerouting/VMS device infrastructure that doesn't exist
    yet, and is explicitly its own later step (STEPS.md Step 19: "VMS /
    rerouting + compliance rate"). Every other action type (STEPS.md Step
    14 -- JunctionAgent's full action space is now live) is implemented.
    """
    if isinstance(action, NoAction):
        return
    if isinstance(action, AdjustPhaseSplit):
        _apply_adjust_phase_split(conn, action)
        return
    if isinstance(action, SetCycleLength):
        _apply_set_cycle_length(conn, action)
        return
    if isinstance(action, SetOffset):
        _apply_set_offset(conn, action)
        return
    raise NotImplementedError(f"apply_action: {type(action).__name__} not implemented yet (STEPS.md Step 19)")


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


def _apply_set_cycle_length(conn: SumoConnection, action: SetCycleLength) -> None:
    """Rescale every GREEN phase's duration so the whole cycle hits
    `action.cycle_s`, keeping yellow/all-red phases fixed (same constraint
    `adjust_phase_split` respects) and the current 2-green-phase SPLIT
    proportionally unchanged -- this changes total capacity without
    silently re-deciding which direction gets more of it."""
    Phase = conn.trafficlight.Phase
    Logic = conn.trafficlight.Logic
    logic = conn.trafficlight.getAllProgramLogics(action.junction_id)[0]
    phases = list(logic.phases)

    green_indices = [i for i, p in enumerate(phases) if phase_kind(p.state) == "green"]
    fixed_total = sum(p.duration for i, p in enumerate(phases) if i not in green_indices)
    green_total_current = sum(phases[i].duration for i in green_indices)
    green_total_target = action.cycle_s - fixed_total
    if not green_indices or green_total_current <= 0 or green_total_target <= 0:
        return  # nothing sane to redistribute onto -- leave the program untouched

    scale = green_total_target / green_total_current
    for i in green_indices:
        old = phases[i]
        phases[i] = Phase(old.duration * scale, old.state, old.minDur, old.maxDur, old.next, old.name)

    conn.trafficlight.setProgramLogic(
        action.junction_id,
        Logic(logic.programID, logic.type, logic.currentPhaseIndex, phases, logic.subParameter),
    )


def _apply_set_offset(conn: SumoConnection, action: SetOffset) -> None:
    """Nudge this junction's cycle to help resynchronize with a neighbor.

    Simplification, documented honestly (plan section 7's "báo cáo trung
    thực cả chỗ thua"): SUMO's static TLS programs don't expose a
    persistent "offset" parameter over TraCI, only the remaining duration
    of whichever phase is CURRENTLY active (`setPhaseDuration`). This
    applies `offset_s` as a ONE-TIME nudge to that remaining duration
    rather than a lasting structural offset -- a real coordinated-signal
    system would rebuild the whole program with a phase shift, which is
    out of scope for this POC.
    """
    conn.trafficlight.setPhaseDuration(action.junction_id, action.offset_s)
