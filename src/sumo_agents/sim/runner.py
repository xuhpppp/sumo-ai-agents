"""SimRunner -- bare async loop + metric collection (STEPS.md Step 5).

`orchestrator` is not wired in yet (that's Phase 2, STEPS.md Steps 10-14) --
this step only proves that the sim loop, metric sampling, and seeded
incident injection are cheap, deterministic, and correctly persisted. The
loop already has the async shape from plan section 3.2 (`await
asyncio.sleep(0)` yielding the event loop every step) so Phase 2 only adds a
`decide()` task inside it; it does not need to rewrite the loop.

Usage:
    python -m sumo_agents.sim.runner --scenario grid_4x4 --mode fixed --seed 42
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sumo_agents.obs.db import make_async_engine, make_session_factory
from sumo_agents.obs.store import Store
from sumo_agents.sim.conn import Backend, SumoConnection
from sumo_agents.sim.incidents import IncidentInjector, load_incident_schedule
from sumo_agents.sim.state import collect_state, traffic_light_ids

# How often (in simulated seconds) a metrics row is written per junction.
# Matches plan section 6.1 / STEPS.md Step 5.
METRIC_SAMPLE_INTERVAL_S = 10.0

NETWORKS_DIR = Path(__file__).resolve().parents[3] / "networks"


async def run(*, scenario: str, mode: str, seed: int, gui: bool = False) -> None:
    scenario_dir = NETWORKS_DIR / scenario
    sumo_cfg = scenario_dir / "sim.sumocfg"
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
                "n_incidents": len(incidents),
            },
        )
        print(f"run_id={run_id} scenario={scenario} mode={mode} seed={seed}")

        conn = SumoConnection(backend=Backend.TRACI if gui else Backend.LIBSUMO, gui=gui)
        try:
            conn.start(sumo_cfg, seed=seed)
            junction_ids = traffic_light_ids(conn)
            last_sample_time = -METRIC_SAMPLE_INTERVAL_S  # force sampling at t=0

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
