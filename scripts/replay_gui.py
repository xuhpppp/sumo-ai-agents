"""Visual replay via sumo-gui, no database involved (STEPS.md Step 5b).

`SimRunner` (Step 5) runs headless on `libsumo` for speed and writes to
Postgres. Because every run is seeded and reproducible (STEPS.md Step 3
DoD: same seed -> identical results), visually reviewing a run does not
need to record or replay anything from the database -- it is enough to
re-run the SAME scenario/seed/incidents on the `traci` backend with the GUI
open. This script does exactly that and nothing else: no Store, no
Postgres, no run_id -- just a way to *see* what a given (scenario, seed)
looks like.

`--delay` is SUMO's own GUI option (milliseconds of wall-clock time per
simulated step) -- needed because driving `traci` as fast as `libsumo`
otherwise renders far faster than the eye can follow.

Usage:
    python scripts/replay_gui.py --scenario grid_4x4 --seed 42
    python scripts/replay_gui.py --scenario grid_4x4 --seed 42 --delay 50
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sumo_agents.sim.conn import Backend, SumoConnection  # noqa: E402
from sumo_agents.sim.incidents import IncidentInjector, load_incident_schedule  # noqa: E402

NETWORKS_DIR = Path(__file__).resolve().parents[1] / "networks"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, help="e.g. grid_4x4 (a folder under networks/)")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument(
        "--delay", type=int, default=100, help="ms of wall-clock time per sim step (sumo-gui's own option)"
    )
    args = parser.parse_args()

    scenario_dir = NETWORKS_DIR / args.scenario
    sumo_cfg = scenario_dir / "sim.sumocfg"
    incidents_path = scenario_dir / "incidents.yaml"
    incidents = load_incident_schedule(incidents_path) if incidents_path.exists() else []
    injector = IncidentInjector(incidents)

    conn = SumoConnection(backend=Backend.TRACI, gui=True)
    try:
        conn.start(sumo_cfg, seed=args.seed, extra_args=["--delay", str(args.delay)])
        while conn.simulation.getMinExpectedNumber() > 0:
            injector.apply(conn, conn.simulation.getTime())
            conn.simulation_step()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
