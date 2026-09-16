"""Turn a validated Action into real TraCI calls, and read the real TLS
state needed to build a validator.TlsState (STEPS.md Step 8).

Split out from sim/state.py (read-only metric aggregation) because this
module WRITES to the simulation -- a deliberately separate, narrower
surface. Used today by baselines/maxpressure.py and, from Phase 2 on, by
the LLM-driven orchestrator (same Action type, same validator gate).
"""

from __future__ import annotations

import random

from sumo_agents.safety.validator import (
    Action,
    AdjustPhaseSplit,
    NoAction,
    PhaseKind,
    PhaseState,
    RequestVms,
    SetCycleLength,
    SetGreenBounds,
    SetOffset,
    TlsState,
)
from sumo_agents.sim.conn import SumoConnection

# STEPS.md Step 19: default fraction of vehicles that actually heed a VMS
# detour, mirroring real-world driver compliance with variable message
# signs (plan section 5's "Mô hình compliance rate (mặc định 0.4)") -- not
# every vehicle capable of rerouting does so just because a sign asked.
DEFAULT_VMS_COMPLIANCE_RATE = 0.4

# How much slower a VMS-flagged edge is made to look to a complying
# vehicle's own internal router, relative to that edge's OWN current
# travel time -- large enough that any genuine alternate path wins the
# comparison outright, without being literal infinity (a real dead end
# still routes through it rather than finding no path at all).
_VMS_TRAVELTIME_PENALTY_FACTOR = 100.0


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
    """Apply an already-validated action via TraCI. Every action type
    `JunctionAgent`/`SupervisorAgent` can actually propose (STEPS.md Step
    14's `ActionUnion` -- `SetGreenBounds`, `RequestVms`, `NoAction`) is
    implemented here, plus the three retired-from-the-LLM types kept for
    any other caller (none currently)."""
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
    if isinstance(action, RequestVms):
        _apply_request_vms(conn, action)
        return
    raise TypeError(f"apply_action: unhandled action type {type(action).__name__!r}")


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


def incoming_edges(conn: SumoConnection, lane_groups: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Real edge IDs feeding this junction, derived from its own per-phase
    lane grouping (`phase_lane_groups`) -- STEPS.md Step 19: gives
    `JunctionAgent` real edge IDs it can name in `request_vms(edge=...)`
    instead of having no network vocabulary to draw on at all (it cannot
    see edge IDs anywhere else in its prompt)."""
    edges = {conn.lane.getEdgeID(lane) for lanes in lane_groups.values() for lane in lanes}
    return tuple(sorted(edges))


def edge_speed_limits(conn: SumoConnection, edges: tuple[str, ...]) -> dict[str, float]:
    """Each edge's OWN nominal (incident-free) speed limit -- STEPS.md Step
    19 follow-up. Callers must compute this ONCE, right after `conn.start()`
    (same "static, compute once" convention as `incoming_edges`/
    `phase_lane_groups` -- see sim/runner.py), NOT live inside the decision
    loop.

    Verified by real TraCI on all 3 networks: reading `lane.getMaxSpeed()`
    live instead would be WRONG once `sim/incidents.py`'s `IncidentInjector`
    has temporarily lowered that same value as part of simulating an
    incident on that very edge (`_start()` calls `lane.setMaxSpeed(lane,
    original_speed * speed_factor)`) -- during the incident window, the
    exact edge that most needs flagging as blocked instead reads as
    "running near its own (already-crippled) limit", while an adjacent,
    merely backed-up edge (whose real limit was never touched) gets
    flagged correctly. Reading the limit once before any incident can
    possibly have started sidesteps this entirely."""
    return {edge_id: conn.lane.getMaxSpeed(f"{edge_id}_0") for edge_id in edges}


def incoming_edge_conditions(
    conn: SumoConnection, edges: tuple[str, ...], speed_limits: dict[str, float]
) -> dict[str, dict[str, float]]:
    """Current mean speed vs. each edge's nominal speed limit
    (`speed_limits`, from `edge_speed_limits` -- STEPS.md Step 19
    follow-up), for each of a junction's `incoming_edges`.

    Real full-hour `--mode llm` runs on all 3 networks (seed=42, post-Step-
    19) found `request_vms` proposed ZERO times across 814 total decisions,
    even during the scheduled `incidents.yaml` incident window -- reading
    the runs' own messages, the model explicitly reasoned "chưa có bằng
    chứng sự cố trên một cạnh đường" (no evidence of an edge-specific
    incident) every time, and at the junction directly adjacent to the
    real incident it recorded `mean_speed_mps=0.0` for several consecutive
    cycles WITHOUT ever considering VMS -- because `queue_len`/
    `mean_waiting_s`/`mean_speed_mps` are all aggregated at the PHASE
    level (`sim/state.py`'s `JunctionSnapshot`, `per_phase_traffic`'s
    per-phase breakdown), which cannot distinguish "this specific edge is
    physically blocked" from "the signal just needs a longer green" --
    both look identical as elevated wait/reduced speed at the phase level.
    This gives the model the one independent signal that tells them apart:
    an edge whose current speed is near its OWN nominal limit is just
    normally used; one far below it despite that limit being high is a
    real road-level anomaly, not a timing problem."""
    result: dict[str, dict[str, float]] = {}
    for edge_id in edges:
        speed_limit = speed_limits[edge_id]
        mean_speed = conn.edge.getLastStepMeanSpeed(edge_id)
        result[edge_id] = {
            "mean_speed_mps": mean_speed,
            "speed_limit_mps": speed_limit,
            "speed_ratio": mean_speed / speed_limit if speed_limit else 0.0,
        }
    return result


def _apply_request_vms(
    conn: SumoConnection, action: RequestVms, *, compliance_rate: float = DEFAULT_VMS_COMPLIANCE_RATE
) -> None:
    """Apply a VMS detour request (STEPS.md Step 19): a subset of the
    vehicles currently headed down `action.edge` are made to treat it as
    much slower for `action.duration_s` seconds, via
    `vehicle.setAdaptedTraveltime` + `vehicle.rerouteTraveltime` (exactly
    the mechanism plan section 5 names) -- each affected vehicle's own
    router then picks whatever path it now judges fastest, which is not
    necessarily `action.alt_route` verbatim (that field only exists for
    `safety.validator`'s structural loop check; a real VMS tells drivers
    "avoid this", not "take this exact turn-by-turn path").

    Two independent gates decide which vehicles are even candidates,
    mirroring how a real VMS can only possibly affect a driver who would
    pass it and whose vehicle can act on it at all:
      1. `action.edge` must still be ahead of the vehicle in its current
         route (`getRouteIndex` onward) -- a vehicle already past it, or
         never headed that way, would never see this sign.
      2. The vehicle must carry SUMO's own rerouting device
         (`has.rerouting.device`, attached probabilistically at insertion
         via each scenario's `sim.sumocfg` `device.rerouting.probability`)
         -- plan section 5: "chỉ xe có device.rerouting phản ứng".

    Among those candidates, compliance is a per-(junction, edge, duration,
    vehicle) DETERMINISTIC draw, not a shared `random.Random` stream --
    STEPS.md Step 17's replay-fidelity requirement: re-applying the exact
    same recorded `RequestVms` action (`--replay`, no live simulation
    randomness available) must divert the exact same vehicles every time,
    regardless of call order or how many other actions were applied this
    run. A network edge with no real path around it simply routes every
    "diverted" vehicle straight back through it -- no special-casing
    needed, this falls out of the Dijkstra recompute itself.

    If `action.edge` doesn't exist in the loaded network (a hallucinated
    or stale edge ID), no vehicle's real `getRoute()` will ever contain
    it, so this is a silent no-op -- deliberately, matching how a `no_op`
    junction phase or an edge nobody uses is already a legitimate,
    harmless outcome elsewhere in this module.
    """
    sim_time = conn.simulation.getTime()
    for vehicle_id in conn.vehicle.getIDList():
        route = conn.vehicle.getRoute(vehicle_id)
        route_index = conn.vehicle.getRouteIndex(vehicle_id)
        if route_index < 0 or action.edge not in route[route_index:]:
            continue
        if conn.vehicle.getParameter(vehicle_id, "has.rerouting.device") != "true":
            continue

        draw = random.Random(f"{action.junction_id}:{action.edge}:{action.duration_s}:{vehicle_id}").random()
        if draw >= compliance_rate:
            continue

        penalty_travel_time_s = conn.edge.getTraveltime(action.edge) * _VMS_TRAVELTIME_PENALTY_FACTOR
        # Positional, not `begTime=`/`endTime=` kwargs: traci's Python
        # wrapper accepts both, but libsumo's binding (this project's
        # default backend, sim/conn.py) only accepts positional args here
        # -- verified empirically, TypeError on the kwarg spelling.
        conn.vehicle.setAdaptedTraveltime(
            vehicle_id, action.edge, penalty_travel_time_s, sim_time, sim_time + action.duration_s
        )
        conn.vehicle.rerouteTraveltime(vehicle_id, currentTravelTimes=False)
