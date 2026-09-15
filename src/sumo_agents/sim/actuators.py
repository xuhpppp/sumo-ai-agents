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
    SetGreenBounds,
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
        PhaseState(
            phase_id=str(index),
            duration_s=phase.duration,
            kind=phase_kind(phase.state),
            min_dur_s=phase.minDur,
            max_dur_s=phase.maxDur,
        )
        for index, phase in enumerate(logic.phases)
    )
    return TlsState(
        junction_id=junction_id,
        phases=phases,
        time_since_last_green_s=dict(time_since_last_green_s or {}),
    )


def phase_lane_groups(conn: SumoConnection, junction_id: str) -> dict[str, tuple[str, ...]]:
    """Map each GREEN phase_id to the (de-duped) incoming lanes it serves.

    Same link/lane grouping `baselines/maxpressure.py`'s `_build_movements`
    already uses to compute pressure (STEPS.md Step 8) -- duplicated here
    (not imported from there) rather than refactoring that module, to keep
    this addition's blast radius to LLM-only code (STEPS.md Step 14
    follow-up: `collect_state`'s junction-wide `queue_len` turned out to be
    too coarse for `adjust_phase_split(phase_id, ...)` -- the model has no
    way to tell which phase is actually congested from one aggregated
    number). Only in-lanes are needed here (unlike max-pressure's
    incoming-minus-outgoing), since this feeds a human/LLM-readable queue
    breakdown, not a pressure score.
    """
    logic = conn.trafficlight.getAllProgramLogics(junction_id)[0]
    links = conn.trafficlight.getControlledLinks(junction_id)  # one entry per signal index
    groups: dict[str, tuple[str, ...]] = {}
    for phase_index, phase in enumerate(logic.phases):
        if phase_kind(phase.state) != "green":
            continue
        lanes: set[str] = set()
        for signal_index, char in enumerate(phase.state):
            if char not in "Gg":
                continue
            for in_lane, _out_lane, _via in links[signal_index]:
                lanes.add(in_lane)
        groups[str(phase_index)] = tuple(lanes)
    return groups


def per_phase_traffic(
    conn: SumoConnection, lane_groups: dict[str, tuple[str, ...]]
) -> dict[str, dict[str, float | int]]:
    """Queue length and mean waiting time per GREEN phase_id, from the lane
    grouping `phase_lane_groups` builds once per junction (link/lane
    topology never changes over a run -- callers should cache that part;
    this function does the cheap per-cycle TraCI reads)."""
    result: dict[str, dict[str, float | int]] = {}
    for phase_id, lanes in lane_groups.items():
        queue_len = sum(conn.lane.getLastStepHaltingNumber(lane) for lane in lanes)
        n_vehicles = sum(conn.lane.getLastStepVehicleNumber(lane) for lane in lanes)
        total_waiting_s = sum(conn.lane.getWaitingTime(lane) for lane in lanes)
        result[phase_id] = {
            "queue_len": queue_len,
            "mean_waiting_s": total_waiting_s / n_vehicles if n_vehicles else 0.0,
        }
    return result


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
    if isinstance(action, SetGreenBounds):
        _apply_set_green_bounds(conn, action)
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


def _apply_set_green_bounds(conn: SumoConnection, action: SetGreenBounds) -> None:
    """Set one GREEN phase's actuated `minDur`/`maxDur` live (STEPS.md Step
    14 actuated-hybrid follow-up). Verified empirically (scratch probe, not
    committed) that SUMO's actuated engine picks up a `setProgramLogic`
    change to these bounds on the very next phase decision -- e.g. lowering
    `maxDur` to 15s mid-run visibly capped an already-running green at 15s.
    `duration` (the type="static" fallback/initial value) is left
    unchanged -- only `minDur`/`maxDur` govern actuated behavior."""
    Phase = conn.trafficlight.Phase
    Logic = conn.trafficlight.Logic
    logic = conn.trafficlight.getAllProgramLogics(action.junction_id)[0]
    phases = list(logic.phases)
    phase_index = int(action.phase_id)
    old = phases[phase_index]
    phases[phase_index] = Phase(old.duration, old.state, action.min_green_s, action.max_green_s, old.next, old.name)

    conn.trafficlight.setProgramLogic(
        action.junction_id,
        Logic(logic.programID, logic.type, logic.currentPhaseIndex, phases, logic.subParameter),
    )
