"""Step 13 DoD check (STEPS.md): one full decision cycle through rounds 2-4
(coalition, validate, approve) against real SUMO state and real OpenAI calls.

Checks the 2 things STEPS.md's Step 13 DoD line asks for:
  1. One complete cycle produces N `messages` and N `decisions`, each
     decision carrying `validator_status` and (when applicable)
     `supervisor_verdict`.
  2. A deliberately-injected violating action is `rejected` BEFORE it ever
     reaches the supervisor.

Round 1 ("observe") proposals for this script are a deliberate MIX:
  - B0, B2, C0, C2 come from real `JunctionAgent.observe()` calls (real
    traffic state, real LLM) -- Step 12 already showed these come back
    `no_action` this early in the run (light traffic), which is realistic
    and fine: they still flow through validate()/supervisor as candidates.
  - B1 is hand-built as a congested, VALID proposal -- there is no reliable
    way to make the real LLM propose something non-trivial on demand, and
    the DoD needs at least one junction to actually trigger the coalition
    round (Step 12's own finding was that light early traffic makes every
    real proposal no_action).
  - C1 is hand-built as a deliberately INVALID proposal (touches a yellow
    phase, which safety.validator.py always rejects) -- the DoD's required
    "inject a violation -> rejected before supervisor" case. There's no
    reliable way to make the real LLM emit an invalid action either (that's
    the whole point of Step 11's schema-level typing).

Rounds 2 (coalition replies) and 4 (supervisor) ARE real OpenAI calls -- this
is what actually exercises agents/orchestrator.py's own logic.

Makes ~7 real OpenAI calls (4 observe + up to a few coalition replies + 1
supervisor) -- costs a small amount of real money.

Usage:
    python scripts/verify_coalition.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sumo_agents.agents.junction import JunctionAgent  # noqa: E402
from sumo_agents.agents.orchestrator import run_decision_cycle  # noqa: E402
from sumo_agents.agents.protocol import Proposal  # noqa: E402
from sumo_agents.agents.supervisor import SupervisorAgent  # noqa: E402
from sumo_agents.agents.topology import signalized_neighbor_map  # noqa: E402
from sqlalchemy import select  # noqa: E402

from sumo_agents.obs.db import make_async_engine, make_session_factory  # noqa: E402
from sumo_agents.obs.models import Message as MessageRow  # noqa: E402
from sumo_agents.obs.store import Store  # noqa: E402
from sumo_agents.safety.validator import AdjustPhaseSplit, NoAction  # noqa: E402
from sumo_agents.sim.actuators import read_tls_state  # noqa: E402
from sumo_agents.sim.conn import Backend, SumoConnection  # noqa: E402
from sumo_agents.sim.state import collect_state  # noqa: E402

NETWORKS_DIR = Path(__file__).resolve().parents[1] / "networks"
SCENARIO = "grid_4x4"
CONTROL_INTERVAL_S = 90.0

# Same 2x3 block as Step 12's verify_junction_agents.py -- B1's real
# neighbors here are A1 (not in this set), B0, B2, C1 (all in this set), so
# its coalition broadcast has real active-agent recipients to reach.
AGENT_JUNCTIONS = ["B0", "B1", "B2", "C0", "C1", "C2"]
REAL_OBSERVE_JUNCTIONS = ["B0", "B2", "C0", "C2"]  # B1, C1 are hand-built below


async def main() -> None:
    net_file = NETWORKS_DIR / SCENARIO / "net.xml"
    neighbor_map = signalized_neighbor_map(net_file)
    agents = {jid: JunctionAgent(jid, neighbor_map[jid]) for jid in AGENT_JUNCTIONS}
    supervisor = SupervisorAgent()

    engine = make_async_engine()
    session_factory = make_session_factory(engine)

    async with Store(session_factory) as store:
        run_id = await store.create_run(
            scenario=f"_{SCENARIO}_coalition_check",
            seed=42,
            mode="coalition_check",
            config={"purpose": "Step 13 DoD: coalition + validate + approve, real SUMO state"},
        )
        print(f"run_id={run_id}\n")

        conn = SumoConnection(backend=Backend.LIBSUMO)
        try:
            conn.start(NETWORKS_DIR / SCENARIO / "sim.sumocfg", seed=42)
            conn.simulation_step(until=CONTROL_INTERVAL_S)
            sim_time = conn.simulation.getTime()

            snapshots = collect_state(conn, AGENT_JUNCTIONS)
            tls_states = {jid: read_tls_state(conn, jid) for jid in AGENT_JUNCTIONS}
        finally:
            conn.close()

        print(f"sim_time={sim_time:.0f}s -- getting round-1 proposals (4 real, 2 hand-built)\n")
        real_results = await asyncio.gather(
            *(agents[jid].observe(snapshots[jid], tls_states[jid], sim_time) for jid in REAL_OBSERVE_JUNCTIONS)
        )
        proposals: dict[str, Proposal] = dict(zip(REAL_OBSERVE_JUNCTIONS, (p for p, _u in real_results), strict=True))
        for jid, (_p, usage) in zip(REAL_OBSERVE_JUNCTIONS, real_results, strict=True):
            await store.add_llm_call(
                run_id=run_id, sim_time=sim_time, agent_id=jid, role="junction", model=usage.model,
                effort=usage.effort, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens, cached_tokens=usage.cached_tokens,
                latency_ms=usage.latency_ms, status=usage.status, error=usage.error, cost_usd=usage.cost_usd,
            )

        proposals["B1"] = Proposal(
            junction_id="B1",
            action=AdjustPhaseSplit(junction_id="B1", phase_id="0", delta_s=10.0),  # valid -- triggers coalition
            urgency="high",
            rationale="[hand-built for DoD] Hàng đợi B1 giả lập tăng cao, cần tăng pha xanh.",
        )
        proposals["C1"] = Proposal(
            junction_id="C1",
            # phase_id "1" is a yellow phase at every grid_4x4 junction (see
            # net.xml's <tlLogic>) -- validator.py must reject this outright.
            action=AdjustPhaseSplit(junction_id="C1", phase_id="1", delta_s=5.0),
            urgency="high",
            rationale="[hand-built for DoD] Cố ý vi phạm: chỉnh pha vàng.",
        )

        for jid, p in proposals.items():
            print(f"  round1 {jid}: action={p.action.type} urgency={p.urgency}")
        print()

        decisions = await run_decision_cycle(
            proposals, agents, neighbor_map, snapshots, tls_states, supervisor,
            sim_time=sim_time, cycle_id=1, run_id=run_id, store=store,
        )
        await store.finish_run(run_id)

    async with session_factory() as session:
        messages = (await session.execute(select(MessageRow).where(MessageRow.run_id == run_id))).scalars().all()
    print("--- messages ---")
    for m in messages:
        print(f"  {m.sender} -> {m.recipients} intent={m.intent}")

    await engine.dispose()

    print("--- decisions ---")
    for d in decisions:
        print(
            f"  {d.junction_id}: validator_status={d.validator_status} "
            f"supervisor_verdict={d.supervisor_verdict} final_action={d.final_action}"
        )
        if d.validator_violations:
            print(f"    violations: {d.validator_violations}")
        if d.supervisor_reason:
            print(f"    supervisor_reason: {d.supervisor_reason}")

    by_id = {d.junction_id: d for d in decisions}
    failures = []

    if len(decisions) != len(AGENT_JUNCTIONS):
        failures.append(f"expected {len(AGENT_JUNCTIONS)} decisions, got {len(decisions)}")

    if not messages:
        failures.append("expected N > 0 messages (B1 is a congested proposal with active-agent neighbors)")

    c1 = by_id.get("C1")
    if c1 is None or c1.validator_status != "rejected" or c1.supervisor_verdict is not None:
        failures.append(
            "C1's deliberately-violating action must be validator_status='rejected' with "
            f"supervisor_verdict=None (got {c1})"
        )

    non_rejected = [d for d in decisions if d.validator_status != "rejected"]
    missing_verdict = [d.junction_id for d in non_rejected if d.supervisor_verdict is None]
    if missing_verdict:
        failures.append(f"non-rejected decisions missing a supervisor_verdict: {missing_verdict}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(
        f"\nPASS -- {len(decisions)} decisions, {len(messages)} messages, "
        "C1 rejected before reaching the supervisor."
    )


if __name__ == "__main__":
    asyncio.run(main())
