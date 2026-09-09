#!/usr/bin/env python
"""Smoke test for `sumo_agents.sim.conn.SumoConnection`.

Does not depend on `networks/grid_4x4` (STEPS.md Step 3, may not exist yet) —
generates a minimal 2x2-junction network via `netgenerate` into a temp
directory, then runs BOTH backends (traci, libsumo) for a few hundred steps
and cross-checks the results.

Run: .venv/bin/python scripts/smoke_test_conn.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import sumolib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sumo_agents.sim.conn import Backend, SumoConnection, SumoConnectionError  # noqa: E402

N_STEPS = 200
SEED = 42


def build_minimal_network(workdir: Path) -> Path:
    """Generate a minimal 2x2 grid network + a fixed vehicle flow. Returns the .sumocfg path."""
    net_file = workdir / "net.xml"
    subprocess.run(
        [
            sumolib.checkBinary("netgenerate"),
            "--grid",
            "--grid.number=2",
            "--grid.length=200",
            "--tls.guess=true",
            "--default.lanenumber=1",
            "--output-file", str(net_file),
        ],
        check=True,
        capture_output=True,
    )

    # A fixed vehicle flow (no need for randomTrips in a smoke test) between
    # two edges that actually have a route between them. A 2x2 grid with
    # 1 lane/direction has no turning connections, so it forms SEPARATE
    # closed loops (verified by printing each edge's getOutgoing()) — unlike
    # a larger grid, "any two boundary edges" won't do. BFS guarantees
    # to_edge is reachable from from_edge before writing the route file.
    net = sumolib.net.readNet(str(net_file))
    edges = net.getEdges()
    from_edge_obj = edges[0]
    seen = {from_edge_obj.getID()}
    queue = [from_edge_obj]
    reachable: list = []
    while queue:
        current = queue.pop(0)
        for nxt in current.getOutgoing():
            if nxt.getID() not in seen:
                seen.add(nxt.getID())
                reachable.append(nxt)
                queue.append(nxt)
    if not reachable:
        raise RuntimeError(f"Edge '{from_edge_obj.getID()}' cannot reach any other edge")
    from_edge, to_edge = from_edge_obj.getID(), reachable[-1].getID()

    routes_file = workdir / "routes.xml"
    routes_file.write_text(
        f'''<routes>
  <vType id="car" accel="2.6" decel="4.5" sigma="0.5" length="5" maxSpeed="20"/>
  <flow id="f0" type="car" begin="0" end="{N_STEPS}" vehsPerHour="600"
        from="{from_edge}" to="{to_edge}"/>
</routes>
'''
    )

    sumocfg = workdir / "sim.sumocfg"
    sumocfg.write_text(
        f'''<configuration>
  <input>
    <net-file value="{net_file.name}"/>
    <route-files value="{routes_file.name}"/>
  </input>
  <time>
    <begin value="0"/>
    <end value="{N_STEPS}"/>
  </time>
</configuration>
'''
    )
    return sumocfg


def run_backend(backend: Backend, sumo_cfg: Path) -> dict:
    """Run N_STEPS steps with the given backend, return a few metrics for comparison."""
    with SumoConnection(backend=backend) as conn:
        assert not conn.is_started
        assert conn.backend is backend

        conn.start(sumo_cfg, seed=SEED)
        assert conn.is_started
        assert conn.step_count == 0

        total_departed = 0
        for _ in range(N_STEPS):
            conn.simulation_step()
            total_departed += conn.simulation.getDepartedNumber()

        result = {
            "backend": backend.value,
            "step_count": conn.step_count,
            "total_departed": total_departed,
            "sim_time": conn.simulation.getTime(),
        }

    assert not conn.is_started, "close() must reset is_started back to False"
    return result


def check_gui_guard() -> None:
    """Confirm: requesting gui=True + backend=libsumo is rejected clearly."""
    try:
        SumoConnection(backend=Backend.LIBSUMO, gui=True)
    except ValueError as exc:
        print(f"  [ok] gui=True + libsumo correctly rejected: {exc}")
    else:
        raise AssertionError("gui=True + backend=libsumo should have raised ValueError")


def check_double_start_guard(sumo_cfg: Path) -> None:
    """Confirm: calling start() twice in a row doesn't silently leak a process."""
    conn = SumoConnection(backend=Backend.LIBSUMO)
    conn.start(sumo_cfg, seed=SEED)
    try:
        conn.start(sumo_cfg, seed=SEED)
    except SumoConnectionError as exc:
        print(f"  [ok] double start() correctly rejected: {exc}")
    else:
        raise AssertionError("start() called twice should have raised SumoConnectionError")
    finally:
        conn.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sumo_conn_smoke_") as tmp:
        workdir = Path(tmp)
        print(f"[1/4] Generating minimal network at {workdir} ...")
        sumo_cfg = build_minimal_network(workdir)

        print("[2/4] Running backend=libsumo ...")
        result_libsumo = run_backend(Backend.LIBSUMO, sumo_cfg)
        print(f"      {result_libsumo}")

        print("[3/4] Running backend=traci ...")
        result_traci = run_backend(Backend.TRACI, sumo_cfg)
        print(f"      {result_traci}")

        assert result_libsumo["step_count"] == result_traci["step_count"] == N_STEPS
        assert result_libsumo["total_departed"] == result_traci["total_departed"], (
            "Same seed, same config -> both backends must report the same "
            f"departed count (libsumo={result_libsumo['total_departed']}, "
            f"traci={result_traci['total_departed']})"
        )
        print("      [ok] both backends produced identical results (same seed)")

        print("[4/4] Checking design guards ...")
        check_gui_guard()
        check_double_start_guard(sumo_cfg)

    print("\nOK — SumoConnection works correctly on both backends.")


if __name__ == "__main__":
    main()
