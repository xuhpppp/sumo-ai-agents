"""Step 12 DoD check (STEPS.md): 6 JunctionAgents run one real decision
cycle each, twice in a row, against a real grid_4x4 SUMO simulation.

Checks the 3 things STEPS.md's Step 12 DoD line asks for:
  1. All 6 agents produce a valid `Proposal` (no exceptions escape).
  2. `cached_tokens > 0` for every agent from the 2nd cycle onward (same
     precondition as Step 10 -- if this is 0, the system prompt isn't
     byte-stable across cycles and caching silently isn't engaging).
  3. At least one lightly-loaded junction proposes `no_action` -- if NONE
     do, the system prompt is pushing the model to "always do something"
     (STEPS.md's own warning under Step 12), which would make the real
     system oscillate once this is wired into SimRunner (Step 14).

Drives SUMO directly (libsumo, no controller applying anything -- Step 12
is observe-only, nothing gets written back to the simulation yet) to get
real traffic snapshots at two different points in time, 90 simulated
seconds apart (one grid_4x4 TLS cycle, matching CONTROL_INTERVAL_S).

Makes 12 real OpenAI calls (6 junctions x 2 cycles, role="junction" ->
gpt-5.6-luna) -- costs a small amount of real money.

Usage:
    python scripts/verify_junction_agents.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sumo_agents.agents.junction import JunctionAgent  # noqa: E402
from sumo_agents.agents.protocol import Proposal  # noqa: E402
from sumo_agents.agents.topology import signalized_neighbor_map  # noqa: E402
from sumo_agents.obs.db import make_async_engine, make_session_factory  # noqa: E402
from sumo_agents.obs.store import Store  # noqa: E402
from sumo_agents.safety.validator import NoAction  # noqa: E402
from sumo_agents.sim.actuators import read_tls_state  # noqa: E402
from sumo_agents.sim.conn import Backend, SumoConnection  # noqa: E402
from sumo_agents.sim.state import collect_state  # noqa: E402

NETWORKS_DIR = Path(__file__).resolve().parents[1] / "networks"
SCENARIO = "grid_4x4"
CONTROL_INTERVAL_S = 90.0  # matches sim/runner.py's CONTROL_INTERVAL_S

# A connected 2x3 block (verified against net.xml via agents/topology.py) --
# not "the first 6 alphabetically", so neighbor relationships are actually
# meaningful for Step 13's coalition round later.
AGENT_JUNCTIONS = ["B0", "B1", "B2", "C0", "C1", "C2"]


async def main() -> None:
    net_file = NETWORKS_DIR / SCENARIO / "net.xml"
    neighbor_map = signalized_neighbor_map(net_file)
    agents = {jid: JunctionAgent(jid, neighbor_map[jid]) for jid in AGENT_JUNCTIONS}
    for jid in AGENT_JUNCTIONS:
        print(f"{jid}: neighbors={neighbor_map[jid]}")
    print()

    engine = make_async_engine()
    session_factory = make_session_factory(engine)

    cached_tokens_by_cycle: list[dict[str, int]] = []
    no_action_seen = False

    async with Store(session_factory) as store:
        run_id = await store.create_run(
            scenario=f"_{SCENARIO}_junction_agent_check",
            seed=42,
            mode="junction_agent_check",
            config={"purpose": "Step 12 DoD: 6 JunctionAgents, 2 cycles, real SUMO state"},
        )
        print(f"run_id={run_id}\n")

        conn = SumoConnection(backend=Backend.LIBSUMO)
        try:
            conn.start(NETWORKS_DIR / SCENARIO / "sim.sumocfg", seed=42)

            for cycle in (1, 2):
                conn.simulation_step(until=CONTROL_INTERVAL_S * cycle)
                sim_time = conn.simulation.getTime()
                snapshots = collect_state(conn, AGENT_JUNCTIONS)
                tls_states = {jid: read_tls_state(conn, jid) for jid in AGENT_JUNCTIONS}

                print(f"--- cycle {cycle} (sim_time={sim_time:.0f}s) ---")
                # Parallel, not sequential -- the Step 10 recommendation
                # (STEPS.md) on why real JunctionAgents must call asyncio.gather.
                results = await asyncio.gather(
                    *(agents[jid].observe(snapshots[jid], tls_states[jid], sim_time) for jid in AGENT_JUNCTIONS)
                )

                cached_this_cycle: dict[str, int] = {}
                for jid, (proposal, usage) in zip(AGENT_JUNCTIONS, results, strict=True):
                    assert isinstance(proposal, Proposal)
                    if isinstance(proposal.action, NoAction):
                        no_action_seen = True
                    print(
                        f"  {jid}: action={proposal.action.type} urgency={proposal.urgency} "
                        f"queue_len={snapshots[jid].queue_len} status={usage.status} "
                        f"cached_tokens={usage.cached_tokens} latency_ms={usage.latency_ms} "
                        f"cost_usd={usage.cost_usd}"
                    )
                    print(f"    rationale: {proposal.rationale}")
                    cached_this_cycle[jid] = usage.cached_tokens or 0

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
                cached_tokens_by_cycle.append(cached_this_cycle)
                print()
        finally:
            conn.close()
            await store.finish_run(run_id)

    await engine.dispose()

    print("cached_tokens by cycle:", cached_tokens_by_cycle)
    print("no_action seen at least once:", no_action_seen)

    failures = []
    second_cycle_cached = cached_tokens_by_cycle[1]
    if not all(v > 0 for v in second_cycle_cached.values()):
        failures.append(
            "cached_tokens is 0 for at least one junction on cycle 2 -- caching isn't engaging "
            "(per-junction system prompt may not be byte-stable, or is under the ~1024 token threshold)."
        )
    if not no_action_seen:
        failures.append(
            "NOT ONE of the 6 junctions proposed no_action across 2 cycles -- per STEPS.md Step 12's "
            "warning, the system prompt is likely pushing the model to always act. Review the prompt "
            "before wiring this into SimRunner (Step 14)."
        )

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nPASS")


if __name__ == "__main__":
    asyncio.run(main())
