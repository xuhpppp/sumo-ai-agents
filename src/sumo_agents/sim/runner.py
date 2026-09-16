"""SimRunner -- async loop + metric collection + controller wiring
(STEPS.md Steps 5, 8, and 14).

The 3 deterministic baselines (Step 8) implement the synchronous
`Controller.decide(snapshot, sim_time) -> list[Action]` protocol
(baselines/base.py) and are applied through the validator gate (Step 7)
directly inside this loop.

`mode="llm"` (Step 14) does NOT use that protocol. IMPLEMENTATION_PLAN.md
section 3.2's "sim never waits for LLM" rule means a decision cycle has to
run as a background `asyncio.Task` this loop polls, rather than a plain
synchronous call the loop blocks on -- the loop already had the async shape
for this (`await asyncio.sleep(0)` yielding every step) since Step 5/8, so
this only adds the task itself, not a rewrite. If the previous cycle's task
isn't done yet when the next one is due, it's skipped (never queued --
deciding off a stale snapshot is worse than not deciding at all, plan
section 3.2) and counted in `runs.summary.n_skipped_cycles`.

Usage:
    python -m sumo_agents.sim.runner --scenario grid_4x4 --mode fixed --seed 42
    python -m sumo_agents.sim.runner --scenario grid_4x4 --mode actuated --seed 42
    python -m sumo_agents.sim.runner --scenario grid_4x4 --mode maxpressure --seed 42
    python -m sumo_agents.sim.runner --scenario grid_4x4 --mode llm --seed 42
    python -m sumo_agents.sim.runner --replay <run_id>   # STEPS.md Step 17, $0, no OpenAI calls
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
import uuid
from pathlib import Path

from sqlalchemy import func, select

from sumo_agents.agents.junction import JunctionAgent
from sumo_agents.agents.orchestrator import CycleDecision, run_decision_cycle
from sumo_agents.agents.protocol import Proposal
from sumo_agents.agents.supervisor import SupervisorAgent
from sumo_agents.agents.topology import NeighborLink, neighbor_links, signalized_neighbor_map
from sumo_agents.baselines.actuated import ActuatedController
from sumo_agents.baselines.base import Controller
from sumo_agents.baselines.fixed import FixedController
from sumo_agents.baselines.maxpressure import MaxPressureController
from sumo_agents.obs.db import make_async_engine, make_session_factory
from sumo_agents.obs.models import LlmCall
from sumo_agents.obs.replay import ReplaySource, load_replay_source
from sumo_agents.obs.store import Store
from sumo_agents.safety.validator import (
    Action,
    AdjustPhaseSplit,
    NoAction,
    RequestVms,
    SetCycleLength,
    SetGreenBounds,
    SetOffset,
    TlsState,
    validate,
)
from sumo_agents.sim.actuators import apply_action, per_phase_traffic, phase_lane_groups, read_tls_state
from sumo_agents.sim.conn import Backend, SumoConnection
from sumo_agents.sim.incidents import IncidentInjector, load_incident_schedule
from sumo_agents.sim.state import JunctionSnapshot, collect_state, traffic_light_ids

# How often (in simulated seconds) a metrics row is written per junction.
# Matches plan section 6.1 / STEPS.md Step 5.
METRIC_SAMPLE_INTERVAL_S = 10.0

# How often a controller gets to decide (STEPS.md Step 8) -- one full grid_4x4
# TLS cycle (42+3+42+3=90s, see net.xml), matching what "max_delta_per_cycle_s"
# in safety/validator.py means by "per cycle". Must be a multiple of
# METRIC_SAMPLE_INTERVAL_S so a decision point always lands on a metrics
# sample (both are checked together, no extra TraCI round trip).
CONTROL_INTERVAL_S = 90.0

NETWORKS_DIR = Path(__file__).resolve().parents[3] / "networks"

# mode -> .sumocfg filename under networks/<scenario>/. Only "actuated"
# needs a different network file (net_actuated.xml -- every <tlLogic>
# rebuilt to type="actuated" via netconvert, see sim_actuated.sumocfg's
# header comment); `fixed`/`maxpressure` control the standard static network
# via Action/validator instead.
#
# `llm` ALSO loads the actuated network (STEPS.md Step 14 actuated-hybrid
# follow-up) -- a real full-hour run found periodic retiming on the static
# network structurally capped well below `actuated`'s result (~134-148s vs
# 107s mean travel time, three separate runs), because ANY fixed-interval
# controller (ours included, however well-informed) can only change future
# cycles' plan, never react within one the way SUMO's own per-step
# induction-loop gap-out logic does. So `llm` mode now runs the SAME
# actuated base layer for all 36 junctions (not just the 6 LLM-controlled
# ones), and JunctionAgent's action space (agents/protocol.py's
# `ActionUnion`) changed from "set an exact duration" to `SetGreenBounds`
# -- the strategic min/max the actuated engine is allowed to vary within.
_SUMOCFG_BY_MODE: dict[str, str] = {"actuated": "sim_actuated.sumocfg", "llm": "sim_actuated.sumocfg"}
_DEFAULT_SUMOCFG = "sim.sumocfg"

# The 4-6 signalized junctions that get an LLM JunctionAgent in mode="llm"
# (plan section 2: "4-6 la tran thuc dung" -- cost/latency scale with
# n_agents). Same 2x3 connected block used throughout Steps 12/13's
# verification scripts, so their neighbor relationships (agents/topology.py)
# are meaningful, not arbitrary. Every other signalized junction runs SUMO's
# own actuated logic, unmanaged by any agent -- same as the `actuated`
# baseline treats the whole network.
LLM_AGENT_JUNCTIONS = ["B0", "B1", "B2", "C0", "C1", "C2"]


def make_controller(mode: str, conn: SumoConnection) -> Controller:
    if mode == "fixed":
        return FixedController()
    if mode == "actuated":
        return ActuatedController()
    if mode == "maxpressure":
        return MaxPressureController(conn)
    raise ValueError(f"unknown mode {mode!r} (mode='llm' does not use the Controller protocol -- see run())")


def _metric_dict(snapshot: JunctionSnapshot) -> dict[str, float | int]:
    return {
        "queue_len": snapshot.queue_len,
        "mean_waiting_s": snapshot.mean_waiting_s,
        "mean_speed": snapshot.mean_speed,
        "throughput": snapshot.throughput,
    }


# STEPS.md Step 17 (--replay): maps a `decisions.action_type`/
# `final_action_type` string back to the Pydantic model that constructed it,
# so a recorded (type, junction_id, params) row can be turned back into a
# real `Action` and re-applied via `sim/actuators.py`'s `apply_action`.
_ACTION_TYPES: dict[str, type[Action]] = {
    cls.model_fields["type"].default: cls
    for cls in (AdjustPhaseSplit, SetCycleLength, SetOffset, SetGreenBounds, RequestVms, NoAction)
}


def _action_from_row(action_type: str, junction_id: str, params: dict) -> Action:
    return _ACTION_TYPES[action_type](junction_id=junction_id, **params)


# One deferred replay apply: (target_sim_time, decision_id, action, effect
# to write once applied). See `_replay_decision_cycle`/`_apply_due_replay_actions`.
_PendingReplayApply = tuple[float, int, Action, dict | None]


async def _replay_decision_cycle(
    conn: SumoConnection,
    store: Store,
    run_id: uuid.UUID,
    cycle_id: int,
    sim_time: float,
    replay_source: ReplaySource,
) -> tuple[bool, list[_PendingReplayApply]]:
    """Reproduce ONE decision cycle from `replay_source` at $0 API cost --
    copies its `messages`/`llm_calls` verbatim (new run_id, same sim_time)
    and writes a `decisions` row per source decision immediately, but does
    NOT push any action to TraCI yet -- that must wait until the simulation
    clock reaches `Decision.applied_sim_time`, the sim_time the ORIGINAL
    run's action actually took effect at (which, for mode="llm", is later
    than `sim_time` here: the decision cycle runs as a background asyncio
    task, and pushing to TraCI only happens once the main loop notices it
    finished -- see sim/runner.py's module docstring). Applying at
    decide-time instead of that later actual-apply-time measurably changes
    the replayed trajectory (~1% off on mean_travel_time_s, observed while
    building this). A decision from an OLDER run that predates the
    `applied_sim_time`/`final_action_type` columns falls back to applying
    immediately at `sim_time` with the originally-proposed `action_type`/
    `params` -- exact whenever nothing was clamped/modified/delayed, and
    the best information available otherwise; STEPS.md Step 17 documents
    this as a known approximation for pre-Step-17 runs.

    Returns `(had_activity, pending_applies)`: `had_activity=False` means
    this cycle was a skipped cycle in the source run (STEPS.md Step 14's
    `n_skipped_cycles`), which the caller should count the same way;
    `pending_applies` are handed to `_apply_due_replay_actions` by the
    caller, checked every simulation step (not just every control
    interval), since `applied_sim_time` can fall anywhere in between."""
    messages, decisions, llm_calls = replay_source.at(sim_time)
    if not messages and not decisions and not llm_calls:
        return False, []

    for m in messages:
        await store.add_message(
            run_id=run_id,
            sim_time=sim_time,
            cycle_id=cycle_id,
            round=m.round,
            sender=m.sender,
            recipients=m.recipients,
            intent=m.intent,
            payload=m.payload,
            rationale=m.rationale,
        )
    for c in llm_calls:
        await store.add_llm_call(
            run_id=run_id,
            sim_time=sim_time,
            agent_id=c.agent_id,
            role=c.role,
            model=c.model,
            effort=c.effort,
            input_tokens=c.input_tokens,
            output_tokens=c.output_tokens,
            reasoning_tokens=c.reasoning_tokens,
            cached_tokens=c.cached_tokens,
            latency_ms=c.latency_ms,
            status=c.status,
            error=c.error,
            cost_usd=c.cost_usd,
        )
    pending_applies: list[_PendingReplayApply] = []
    for d in decisions:
        decision_id = await store.add_decision(
            run_id=run_id,
            sim_time=sim_time,
            cycle_id=cycle_id,
            junction_id=d.junction_id,
            action_type=d.action_type,
            params=d.params,
            validator_status=d.validator_status,
            validator_violations=d.validator_violations,
            supervisor_verdict=d.supervisor_verdict,
            supervisor_reason=d.supervisor_reason,
            applied=False,
        )
        if d.applied:
            final_action_type = d.final_action_type or d.action_type
            final_action_params = d.final_action_params if d.final_action_type is not None else d.params
            action = _action_from_row(final_action_type, d.junction_id, final_action_params)
            target_sim_time = d.applied_sim_time if d.applied_sim_time is not None else sim_time
            pending_applies.append((target_sim_time, decision_id, action, d.effect))
        elif d.effect is not None:
            await store.update_decision(decision_id, effect=d.effect)
    return True, pending_applies


async def _apply_due_replay_actions(
    conn: SumoConnection,
    store: Store,
    pending: list[_PendingReplayApply],
    sim_time: float,
) -> list[_PendingReplayApply]:
    """Push to TraCI any replay action whose `applied_sim_time` has now
    been reached, and return the ones still waiting. `sim_time=inf` (used
    once the simulation ends, same as `_apply_llm_decisions`'s "last
    cycle's task" flush) forces every remaining entry through."""
    still_pending: list[_PendingReplayApply] = []
    for target_sim_time, decision_id, action, effect in pending:
        if sim_time >= target_sim_time:
            apply_action(conn, action)  # the source run already proved this exact action applies cleanly
            await store.update_decision(
                decision_id,
                applied=True,
                final_action_type=action.type,
                final_action_params=action.model_dump(exclude={"type", "junction_id"}),
                applied_sim_time=target_sim_time,
                effect=effect,
            )
        else:
            still_pending.append((target_sim_time, decision_id, action, effect))
    return still_pending


async def _run_llm_decision_cycle(
    agents: dict[str, JunctionAgent],
    neighbor_map: dict[str, list[str]],
    snapshots: dict[str, JunctionSnapshot],
    tls_states: dict[str, TlsState],
    supervisor: SupervisorAgent,
    sim_time: float,
    cycle_id: int,
    run_id: uuid.UUID,
    store: Store,
    history: dict[str, list[JunctionSnapshot]] | None = None,
    per_phase: dict[str, dict[str, dict[str, float | int]]] | None = None,
    neighbor_links: dict[str, dict[str, NeighborLink]] | None = None,
) -> list[CycleDecision]:
    """Round 1 (observe, parallel -- STEPS.md Step 12) + rounds 2-4
    (agents/orchestrator.py, Step 13), together as ONE background task --
    this whole function is plan section 3.2's `orchestrator.decide(snapshot)`.
    `history`: each junction's own snapshots from its last few decision
    cycles (STEPS.md Step 14 follow-up -- see `JunctionAgent.observe`).
    `per_phase`: each junction's queue/wait broken down by green phase_id
    (STEPS.md Step 14 follow-up #2). `neighbor_links`: real distance/
    travel-time between adjacent junctions (STEPS.md Step 14
    coordination-context follow-up)."""
    history = history or {}
    per_phase = per_phase or {}
    observe_results = await asyncio.gather(
        *(
            agents[jid].observe(
                snapshots[jid], tls_states[jid], sim_time, history=history.get(jid), per_phase=per_phase.get(jid)
            )
            for jid in agents
        )
    )
    proposals: dict[str, Proposal] = {}
    for jid, (proposal, usage) in zip(agents.keys(), observe_results, strict=True):
        proposals[jid] = proposal
        await store.add_llm_call(
            run_id=run_id,
            sim_time=sim_time,
            agent_id=jid,
            role="junction",
            model=usage.model,
            effort=usage.effort,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cached_tokens=usage.cached_tokens,
            latency_ms=usage.latency_ms,
            status=usage.status,
            error=usage.error,
            cost_usd=usage.cost_usd,
        )

    return await run_decision_cycle(
        proposals,
        agents,
        neighbor_map,
        snapshots,
        tls_states,
        supervisor,
        sim_time=sim_time,
        cycle_id=cycle_id,
        run_id=run_id,
        store=store,
        neighbor_links=neighbor_links,
    )


async def _apply_llm_decisions(
    conn: SumoConnection,
    store: Store,
    decisions: list[CycleDecision],
    decision_snapshots: dict[str, JunctionSnapshot],
    pending_effect: dict[str, tuple[int, dict]],
    sim_time: float,
) -> None:
    """Apply every decision's `final_action` -- the only place mode="llm"
    ever touches TraCI (`agents/orchestrator.py` never does; plan section
    3.1's "sim_process sở hữu DUY NHẤT kết nối TraCI"). Then remember
    (decision_id, metric-at-decision-time) per junction so the NEXT cycle
    can fill in `effect` once the "after" metric exists (STEPS.md Step 14:
    "Điền decisions.effect ở chu kỳ kế"). `sim_time` here is the CURRENT
    simulation clock -- i.e. when this actually gets pushed to TraCI, which
    for mode="llm" is later than the decision cycle's own `sim_time` (the
    background task may take a while to finish, see the module docstring)
    -- recorded as `applied_sim_time` so STEPS.md Step 17's `--replay` can
    re-apply it at the same simulated moment, not the moment it was decided."""
    for d in decisions:
        if d.final_action is None:
            continue
        try:
            apply_action(conn, d.final_action)
        except NotImplementedError as exc:
            # request_vms only, for now -- STEPS.md Step 19. Recorded, not
            # silently dropped: the dashboard should be able to show "the
            # pipeline approved this but it wasn't physically applied yet".
            await store.update_decision(d.decision_id, applied=False, effect={"note": str(exc)})
            continue
        await store.update_decision(
            d.decision_id,
            applied=True,
            final_action_type=d.final_action.type,
            final_action_params=d.final_action.model_dump(exclude={"type", "junction_id"}),
            applied_sim_time=sim_time,
        )
        pending_effect[d.junction_id] = (d.decision_id, _metric_dict(decision_snapshots[d.junction_id]))


async def run(
    *,
    scenario: str | None = None,
    mode: str | None = None,
    seed: int | None = None,
    gui: bool = False,
    replay_of: uuid.UUID | None = None,
) -> uuid.UUID:
    """Run one (scenario, mode, seed) simulation end to end and return its
    run_id -- scripts/compare.py (STEPS.md Step 9) calls this directly,
    in-process, once per (mode, seed) combination (sequentially: libsumo
    only supports one simulation per process at a time).

    `replay_of` (STEPS.md Step 17): replay a previous mode="llm" run's
    recorded decisions instead of calling the LLM again ($0 API cost).
    `scenario`/`mode`/`seed` are then read from that source run itself
    (see `main()`'s CLI validation -- they must not be passed alongside
    `replay_of`)."""
    engine = make_async_engine()
    session_factory = make_session_factory(engine)

    replay_source: ReplaySource | None = None
    if replay_of is not None:
        replay_source = await load_replay_source(session_factory, replay_of)
        scenario, seed, mode = replay_source.run.scenario, replay_source.run.seed, "llm"
    assert scenario is not None and mode is not None and seed is not None

    is_llm_mode = mode == "llm"
    scenario_dir = NETWORKS_DIR / scenario
    sumo_cfg = scenario_dir / _SUMOCFG_BY_MODE.get(mode, _DEFAULT_SUMOCFG)
    incidents_path = scenario_dir / "incidents.yaml"

    incidents = load_incident_schedule(incidents_path) if incidents_path.exists() else []
    injector = IncidentInjector(incidents)

    async with Store(session_factory) as store:
        run_id = await store.create_run(
            scenario=scenario,
            seed=seed,
            mode=mode,
            config={
                "gui": gui,
                "metric_sample_interval_s": METRIC_SAMPLE_INTERVAL_S,
                "control_interval_s": CONTROL_INTERVAL_S,
                "n_incidents": len(incidents),
                "llm_agent_junctions": LLM_AGENT_JUNCTIONS if is_llm_mode else None,
                "replay_of": str(replay_of) if replay_of is not None else None,
            },
        )
        print(f"run_id={run_id} scenario={scenario} mode={mode} seed={seed}")

        conn = SumoConnection(backend=Backend.TRACI if gui else Backend.LIBSUMO, gui=gui)
        # Whole-run summary (STEPS.md Step 9 -- mean travel time / throughput
        # aren't per-junction, so they don't fit the `metrics` table; tracked
        # here via TraCI's own depart/arrive events and written once at the
        # end, see Store.finish_run). Declared before `try` so a run that
        # errors out mid-loop still finishes with whatever was observed so far.
        depart_time_by_vehicle: dict[str, float] = {}
        travel_times_s: list[float] = []
        # mode="llm" only, from here down (Step 14) -- kept empty/unused for
        # the baseline modes rather than branching the whole function, since
        # everything except the decision block itself is shared.
        llm_agents: dict[str, JunctionAgent] = {}
        llm_neighbor_map: dict[str, list[str]] = {}
        llm_neighbor_links: dict[str, dict[str, NeighborLink]] = {}
        supervisor: SupervisorAgent | None = None
        pending_task: asyncio.Task[list[CycleDecision]] | None = None
        pending_task_snapshots: dict[str, JunctionSnapshot] = {}
        pending_effect: dict[str, tuple[int, dict]] = {}
        # STEPS.md Step 17 (--replay) only: actions waiting for the sim
        # clock to reach their recorded `applied_sim_time` -- see
        # `_replay_decision_cycle`/`_apply_due_replay_actions`.
        pending_replay_applies: list[_PendingReplayApply] = []
        time_since_last_green: dict[str, dict[str, float]] = {jid: {} for jid in LLM_AGENT_JUNCTIONS}
        # Per-junction trend history for JunctionAgent.observe() (STEPS.md
        # Step 14 follow-up): a real full-hour run found the model staying
        # passive through real congestion because each observe() call only
        # ever saw one isolated snapshot -- it explicitly reasoned "only one
        # cycle of data, not enough evidence of a persistent pattern" even
        # while queue_len/mean_waiting_s were clearly climbing. Kept as the
        # last SNAPSHOT_HISTORY_LEN decision-cycle snapshots (not 10s metric
        # samples -- the model's own complaint was about cycles, not noise
        # within one).
        snapshot_history: dict[str, list[JunctionSnapshot]] = {jid: [] for jid in LLM_AGENT_JUNCTIONS}
        SNAPSHOT_HISTORY_LEN = 3
        n_skipped_cycles = 0
        # Measured empirically (STEPS.md Step 14): an unthrottled libsumo run
        # of this scenario reaches sim_time=3600s+ in ~3-4 REAL seconds --
        # roughly 1000x real-time. A decision-cycle task takes ~10-20 real
        # seconds end to end (observe + coalition + supervise). Left
        # unthrottled, virtually every one of the ~40 control intervals
        # would find the previous cycle's task still in flight and skip it
        # (correct per plan section 3.2, but not useful if it fires on ~39
        # of 40 cycles -- the whole point of an "hour-long" llm run is for
        # most cycles to actually happen). So mode="llm" paces itself to a
        # bounded simulated-seconds-per-real-second ratio -- fast enough to
        # still finish well under a real hour, slow enough that a typical
        # decision cycle usually completes within one 90-simulated-second
        # control interval. Baseline modes are unaffected (unthrottled).
        LLM_REALTIME_SPEEDUP = 5.0  # ~90 simulated seconds every ~18 real seconds
        wall_clock_start = time.monotonic()
        try:
            conn.start(sumo_cfg, seed=seed)
            junction_ids = traffic_light_ids(conn)
            if is_llm_mode and replay_source is None:
                # Same lane/link topology as net.xml (netconvert only
                # changed tls.default-type) -- read from the actuated file
                # for consistency with what `conn` actually has loaded.
                llm_neighbor_map = signalized_neighbor_map(scenario_dir / "net_actuated.xml")
                llm_neighbor_links = neighbor_links(scenario_dir / "net_actuated.xml")
                llm_agents = {jid: JunctionAgent(jid, llm_neighbor_map[jid]) for jid in LLM_AGENT_JUNCTIONS}
                supervisor = SupervisorAgent()
                controller_observe = None
                # Link/lane topology per junction never changes over a run
                # (STEPS.md Step 14 follow-up #2, same reasoning as
                # MaxPressureController's own `_movements` cache) -- build
                # once, reuse every decision cycle.
                llm_phase_lane_groups = {jid: phase_lane_groups(conn, jid) for jid in LLM_AGENT_JUNCTIONS}
            elif is_llm_mode:
                # Replay mode (STEPS.md Step 17): no live agents/supervisor
                # at all -- every decision cycle is read back from
                # `replay_source` instead of asking a real JunctionAgent.
                controller_observe = None
            else:
                controller = make_controller(mode, conn)
                # Optional per-controller hook, not part of the shared Controller
                # protocol (baselines/base.py): called every metric-sample tick so a
                # controller can average a signal over the whole control interval
                # instead of reading it once at decide() time (STEPS.md Step 8 --
                # MaxPressureController needs this, fixed/actuated don't define it).
                controller_observe = getattr(controller, "observe", None)
            last_sample_time = -METRIC_SAMPLE_INTERVAL_S  # force sampling at t=0
            last_control_time = -CONTROL_INTERVAL_S  # force a decision at t=0
            cycle_id = 0

            # Standard TraCI loop condition: keep stepping while any vehicle
            # has departed-but-not-arrived or is still scheduled to depart.
            # More robust than a fixed step count -- demand ends at t=3600
            # (see trips.xml) but vehicles already on the road keep going
            # past that until they arrive, so the run naturally runs longer.
            while conn.simulation.getMinExpectedNumber() > 0:
                sim_time = conn.simulation.getTime()
                injector.apply(conn, sim_time)
                conn.simulation_step()
                sim_time = conn.simulation.getTime()

                for vehicle_id in conn.simulation.getDepartedIDList():
                    depart_time_by_vehicle[vehicle_id] = sim_time
                for vehicle_id in conn.simulation.getArrivedIDList():
                    depart_time = depart_time_by_vehicle.pop(vehicle_id, None)
                    if depart_time is not None:
                        travel_times_s.append(sim_time - depart_time)

                if sim_time - last_sample_time >= METRIC_SAMPLE_INTERVAL_S:
                    snapshots = collect_state(conn, junction_ids)
                    for junction_id, snapshot in snapshots.items():
                        await store.add_metric(
                            run_id=run_id,
                            sim_time=sim_time,
                            junction_id=junction_id,
                            mean_waiting_s=snapshot.mean_waiting_s,
                            queue_len=snapshot.queue_len,
                            throughput=snapshot.throughput,
                            mean_speed=snapshot.mean_speed,
                            co2_mg=snapshot.co2_mg,
                        )
                    last_sample_time = sim_time

                    if is_llm_mode and replay_source is None:
                        # Starvation history for validator.py's anti-hogging
                        # check (safety/validator.py's TlsState docstring --
                        # "a missing key is treated as recently green", which
                        # is fine for baselines but not for an unattended
                        # hour-long agent run). Cheap: pure TraCI reads, no
                        # LLM/network involved. Not needed in replay mode --
                        # nothing calls validate()/observe() again there.
                        for jid in LLM_AGENT_JUNCTIONS:
                            current_phase = str(snapshots[jid].current_phase)
                            tracked = time_since_last_green[jid]
                            for phase in read_tls_state(conn, jid).phases:
                                if phase.kind != "green":
                                    continue
                                tracked[phase.phase_id] = (
                                    0.0
                                    if phase.phase_id == current_phase
                                    else tracked.get(phase.phase_id, 0.0) + METRIC_SAMPLE_INTERVAL_S
                                )
                    elif controller_observe is not None:
                        controller_observe(snapshots, sim_time)

                    if sim_time - last_control_time >= CONTROL_INTERVAL_S:
                        if replay_source is not None:
                            had_activity, new_pending = await _replay_decision_cycle(
                                conn, store, run_id, cycle_id, sim_time, replay_source
                            )
                            if not had_activity:
                                n_skipped_cycles += 1
                            pending_replay_applies.extend(new_pending)
                        elif is_llm_mode:
                            # Resolve `effect` for the PREVIOUS cycle's applied
                            # decisions using this cycle's fresh snapshots
                            # (STEPS.md Step 14: "Điền effect ở chu kỳ kế").
                            for jid, (decision_id, before) in pending_effect.items():
                                await store.update_decision(
                                    decision_id, effect={"before": before, "after": _metric_dict(snapshots[jid])}
                                )
                            pending_effect.clear()

                            if pending_task is None:
                                agent_snapshots = {jid: snapshots[jid] for jid in LLM_AGENT_JUNCTIONS}
                                agent_tls_states = {
                                    jid: read_tls_state(
                                        conn, jid, time_since_last_green_s=time_since_last_green[jid]
                                    )
                                    for jid in LLM_AGENT_JUNCTIONS
                                }
                                # Snapshot BEFORE appending this cycle's own
                                # reading -- history must never include the
                                # observation the model is being asked about.
                                agent_history = {jid: list(snapshot_history[jid]) for jid in LLM_AGENT_JUNCTIONS}
                                for jid in LLM_AGENT_JUNCTIONS:
                                    snapshot_history[jid].append(agent_snapshots[jid])
                                    del snapshot_history[jid][:-SNAPSHOT_HISTORY_LEN]
                                agent_per_phase = {
                                    jid: per_phase_traffic(conn, llm_phase_lane_groups[jid])
                                    for jid in LLM_AGENT_JUNCTIONS
                                }
                                pending_task = asyncio.create_task(
                                    _run_llm_decision_cycle(
                                        llm_agents,
                                        llm_neighbor_map,
                                        agent_snapshots,
                                        agent_tls_states,
                                        supervisor,  # type: ignore[arg-type]
                                        sim_time,
                                        cycle_id,
                                        run_id,
                                        store,
                                        history=agent_history,
                                        per_phase=agent_per_phase,
                                        neighbor_links=llm_neighbor_links,
                                    )
                                )
                                pending_task_snapshots = agent_snapshots
                            else:
                                # Previous cycle's task hasn't finished -- per
                                # plan section 3.2, DO NOT queue a second one
                                # (deciding off a stale snapshot is worse than
                                # not deciding at all). Just count it.
                                n_skipped_cycles += 1
                        else:
                            for action in controller.decide(snapshots, sim_time):
                                tls_state = read_tls_state(conn, action.junction_id)
                                result = validate(action, tls_state)
                                status = "ok" if result.ok else "rejected"
                                if result.ok and result.violations:
                                    status = "clamped"
                                if result.clamped_action is not None:
                                    apply_action(conn, result.clamped_action)
                                await store.add_decision(
                                    run_id=run_id,
                                    sim_time=sim_time,
                                    cycle_id=cycle_id,
                                    junction_id=action.junction_id,
                                    action_type=action.type,
                                    params=action.model_dump(exclude={"type", "junction_id"}),
                                    validator_status=status,
                                    validator_violations={"violations": result.violations}
                                    if result.violations
                                    else None,
                                    applied=result.clamped_action is not None,
                                )
                        last_control_time = sim_time
                        cycle_id += 1

                if is_llm_mode and pending_task is not None and pending_task.done():
                    decisions = pending_task.result()
                    await _apply_llm_decisions(conn, store, decisions, pending_task_snapshots, pending_effect, sim_time)
                    pending_task = None

                if replay_source is not None and pending_replay_applies:
                    # Checked every step, not just every control interval --
                    # `applied_sim_time` can fall anywhere in between.
                    pending_replay_applies = await _apply_due_replay_actions(
                        conn, store, pending_replay_applies, sim_time
                    )

                if is_llm_mode and replay_source is None:
                    # Pace to LLM_REALTIME_SPEEDUP (see the comment above the
                    # loop) instead of a bare yield -- gives pending decision
                    # tasks real wall-clock time to actually finish.
                    target_elapsed = sim_time / LLM_REALTIME_SPEEDUP
                    actual_elapsed = time.monotonic() - wall_clock_start
                    await asyncio.sleep(max(0.0, target_elapsed - actual_elapsed))
                else:
                    # Baseline speed for baselines AND replay (STEPS.md Step
                    # 17) -- replay never awaits any real async work (no
                    # OpenAI calls), so there is nothing to pace for; running
                    # it at full libsumo speed is the whole point ($0, fast).
                    await asyncio.sleep(0)  # yield the event loop (plan section 3.2)

            # The last cycle's task may still be running when demand ends --
            # await it rather than dropping its decisions on the floor
            # (conn is still open here, needed to actually apply them).
            if is_llm_mode and pending_task is not None:
                decisions = await pending_task
                await _apply_llm_decisions(conn, store, decisions, pending_task_snapshots, pending_effect, sim_time)

            if replay_source is not None and pending_replay_applies:
                # Same reasoning as the "last cycle's task" flush above --
                # the source run's last decision cycle's `applied_sim_time`
                # is guaranteed to be <= its own final_sim_time (the
                # original run always eventually applied it too), so
                # sim_time=inf forces every remaining entry through rather
                # than risking dropping it because of a float boundary.
                pending_replay_applies = await _apply_due_replay_actions(
                    conn, store, pending_replay_applies, float("inf")
                )

            final_sim_time = conn.simulation.getTime()
        finally:
            conn.close()
            summary = {
                "n_completed_trips": len(travel_times_s),
                "mean_travel_time_s": statistics.fmean(travel_times_s) if travel_times_s else None,
            }
            if is_llm_mode:
                summary["n_skipped_cycles"] = n_skipped_cycles
                async with session_factory() as session:
                    total_cost, n_calls = (
                        await session.execute(
                            select(func.sum(LlmCall.cost_usd), func.count(LlmCall.id)).where(LlmCall.run_id == run_id)
                        )
                    ).one()
                summary["total_llm_cost_usd"] = float(total_cost) if total_cost is not None else 0.0
                summary["n_llm_calls"] = n_calls
            await store.finish_run(run_id, summary=summary)

    await engine.dispose()
    print(f"done: final_sim_time={final_sim_time}s")
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", help="e.g. grid_4x4 (a folder under networks/) -- omit with --replay")
    parser.add_argument("--mode", help="fixed | actuated | maxpressure | llm -- omit with --replay (always 'llm')")
    parser.add_argument("--seed", type=int, help="omit with --replay")
    parser.add_argument("--gui", action="store_true", help="watch it live via sumo-gui instead of running headless")
    parser.add_argument(
        "--replay",
        metavar="RUN_ID",
        help="STEPS.md Step 17: replay a previous mode=llm run's recorded decisions instead of "
        "calling the LLM again ($0 API cost). scenario/mode/seed are read from that run.",
    )
    args = parser.parse_args()

    if args.replay is not None:
        if args.scenario or args.mode or args.seed is not None:
            parser.error("--replay cannot be combined with --scenario/--mode/--seed (read from the source run)")
        asyncio.run(run(replay_of=uuid.UUID(args.replay), gui=args.gui))
    else:
        if not (args.scenario and args.mode and args.seed is not None):
            parser.error("--scenario, --mode, and --seed are required unless --replay is given")
        asyncio.run(run(scenario=args.scenario, mode=args.mode, seed=args.seed, gui=args.gui))


if __name__ == "__main__":
    main()
