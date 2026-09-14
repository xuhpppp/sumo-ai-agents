"""SimRunner -- async loop + metric collection + controller wiring
(STEPS.md Steps 5 and 8).

`orchestrator` (the LLM-driven mode) is not wired in yet (that's Phase 2,
STEPS.md Steps 10-14) -- but the 3 deterministic baselines (Step 8) already
plug into the exact same `Controller.decide(snapshot, sim_time) -> list[Action]`
shape the LLM agent will use, applied through the SAME validator gate
(Step 7). The loop already has the async shape from plan section 3.2
(`await asyncio.sleep(0)` yielding the event loop every step) so Phase 2
only adds a `decide()` task inside it; it does not need to rewrite the loop.

Usage:
    python -m sumo_agents.sim.runner --scenario grid_4x4 --mode fixed --seed 42
    python -m sumo_agents.sim.runner --scenario grid_4x4 --mode actuated --seed 42
    python -m sumo_agents.sim.runner --scenario grid_4x4 --mode maxpressure --seed 42
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sumo_agents.baselines.actuated import ActuatedController
from sumo_agents.baselines.base import Controller
from sumo_agents.baselines.fixed import FixedController
from sumo_agents.baselines.maxpressure import MaxPressureController
from sumo_agents.obs.db import make_async_engine, make_session_factory
from sumo_agents.obs.store import Store
from sumo_agents.safety.validator import validate
from sumo_agents.sim.actuators import apply_action, read_tls_state
from sumo_agents.sim.conn import Backend, SumoConnection
from sumo_agents.sim.incidents import IncidentInjector, load_incident_schedule
from sumo_agents.sim.state import collect_state, traffic_light_ids

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
# header comment); every other mode controls the standard static network
# via Action/validator instead.
_SUMOCFG_BY_MODE: dict[str, str] = {"actuated": "sim_actuated.sumocfg"}
_DEFAULT_SUMOCFG = "sim.sumocfg"


def make_controller(mode: str, conn: SumoConnection) -> Controller:
    if mode == "fixed":
        return FixedController()
    if mode == "actuated":
        return ActuatedController()
    if mode == "maxpressure":
        return MaxPressureController(conn)
    raise ValueError(f"unknown mode {mode!r} (Phase 2 will add 'llm')")


async def run(*, scenario: str, mode: str, seed: int, gui: bool = False) -> None:
    scenario_dir = NETWORKS_DIR / scenario
    sumo_cfg = scenario_dir / _SUMOCFG_BY_MODE.get(mode, _DEFAULT_SUMOCFG)
    incidents_path = scenario_dir / "incidents.yaml"

    engine = make_async_engine()
    session_factory = make_session_factory(engine)

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
            },
        )
        print(f"run_id={run_id} scenario={scenario} mode={mode} seed={seed}")

        conn = SumoConnection(backend=Backend.TRACI if gui else Backend.LIBSUMO, gui=gui)
        try:
            conn.start(sumo_cfg, seed=seed)
            junction_ids = traffic_light_ids(conn)
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

                    if controller_observe is not None:
                        controller_observe(snapshots, sim_time)

                    if sim_time - last_control_time >= CONTROL_INTERVAL_S:
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
                                validator_violations={"violations": result.violations} if result.violations else None,
                                applied=result.clamped_action is not None,
                            )
                        last_control_time = sim_time
                        cycle_id += 1

                await asyncio.sleep(0)  # yield the event loop (plan section 3.2)

            final_sim_time = conn.simulation.getTime()
        finally:
            conn.close()
            await store.finish_run(run_id)

    await engine.dispose()
    print(f"done: final_sim_time={final_sim_time}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, help="e.g. grid_4x4 (a folder under networks/)")
    parser.add_argument(
        "--mode",
        required=True,
        help="controller label recorded on the run, e.g. fixed (no controller wired in until Phase 2)",
    )
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--gui", action="store_true", help="watch it live via sumo-gui instead of running headless")
    args = parser.parse_args()

    asyncio.run(run(scenario=args.scenario, mode=args.mode, seed=args.seed, gui=args.gui))


if __name__ == "__main__":
    main()
