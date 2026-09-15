"""Step 14 short smoke check (STEPS.md): exercise the ACTUAL production code
path (`sim.runner`'s `_run_llm_decision_cycle`/`_apply_llm_decisions`) for a
couple of cycles before recommending a real full-hour `--mode llm` run --
Steps 12/13 both found real API-only bugs (an OpenAI structured-output
schema rejection) that only surfaced on a live call, so a short, cheap check
here is meant to catch anything similar before the ~$2, unattended full run.

Two independent checks:
  1. `apply_action` for `set_cycle_length`/`set_offset` -- brand new in
     Step 14, never exercised against a real TraCI connection before. No
     LLM involved, $0 cost: hand-built actions, real SUMO state before/after.
  2. Two real decision cycles through `sim.runner`'s actual internal
     functions (not reimplemented here) -- confirms `decision_id`/`effect`
     wiring, and that nothing crashes end to end. Real OpenAI calls, small
     cost (~$0.05-0.15: 12 observe calls + up to a few coalition replies +
     up to 2 supervisor calls).

Usage:
    python scripts/verify_llm_mode.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sumo_agents.agents.junction import JunctionAgent  # noqa: E402
from sumo_agents.agents.supervisor import SupervisorAgent  # noqa: E402
from sumo_agents.agents.topology import signalized_neighbor_map  # noqa: E402
from sumo_agents.obs.db import make_async_engine, make_session_factory  # noqa: E402
from sumo_agents.obs.store import Store  # noqa: E402
from sumo_agents.safety.validator import SetCycleLength, SetOffset  # noqa: E402
from sumo_agents.sim.actuators import apply_action, read_tls_state  # noqa: E402
from sumo_agents.sim.conn import Backend, SumoConnection  # noqa: E402
from sumo_agents.sim.runner import (  # noqa: E402
    CONTROL_INTERVAL_S,
    LLM_AGENT_JUNCTIONS,
    _apply_llm_decisions,
    _metric_dict,
    _run_llm_decision_cycle,
)
from sumo_agents.sim.state import collect_state, traffic_light_ids  # noqa: E402

NETWORKS_DIR = Path(__file__).resolve().parents[1] / "networks"
SCENARIO = "grid_4x4"


def check_apply_set_cycle_length() -> None:
    print("--- check 1a: apply_action(SetCycleLength) against real TraCI ---")
    conn = SumoConnection(backend=Backend.LIBSUMO)
    try:
        conn.start(NETWORKS_DIR / SCENARIO / "sim.sumocfg", seed=42)
        jid = LLM_AGENT_JUNCTIONS[0]
        before = read_tls_state(conn, jid)
        before_total = sum(p.duration_s for p in before.phases)

        target = 60.0
        apply_action(conn, SetCycleLength(junction_id=jid, cycle_s=target))

        after = read_tls_state(conn, jid)
        after_total = sum(p.duration_s for p in after.phases)
        print(f"  {jid}: cycle {before_total:.1f}s -> {after_total:.1f}s (target {target}s)")
        assert abs(after_total - target) < 0.01, f"expected total {target}s, got {after_total}s"
        yellows_unchanged = [
            p.duration_s for p in after.phases if p.kind != "green"
        ] == [p.duration_s for p in before.phases if p.kind != "green"]
        assert yellows_unchanged, "yellow/all-red phases must stay fixed"
        print("  PASS")
    finally:
        conn.close()


def check_apply_set_offset() -> None:
    print("--- check 1b: apply_action(SetOffset) against real TraCI ---")
    conn = SumoConnection(backend=Backend.LIBSUMO)
    try:
        conn.start(NETWORKS_DIR / SCENARIO / "sim.sumocfg", seed=42)
        jid = LLM_AGENT_JUNCTIONS[0]
        apply_action(conn, SetOffset(junction_id=jid, offset_s=20.0))  # must not raise
        print(f"  {jid}: setPhaseDuration(20.0) applied without error")
        print("  PASS")
    finally:
        conn.close()


async def check_two_real_llm_cycles() -> None:
    print("\n--- check 2: 2 real decision cycles through sim.runner's actual functions ---")
    net_file = NETWORKS_DIR / SCENARIO / "net.xml"
    neighbor_map = signalized_neighbor_map(net_file)
    agents = {jid: JunctionAgent(jid, neighbor_map[jid]) for jid in LLM_AGENT_JUNCTIONS}
    supervisor = SupervisorAgent()

    engine = make_async_engine()
    session_factory = make_session_factory(engine)

    async with Store(session_factory) as store:
        run_id = await store.create_run(
            scenario=f"_{SCENARIO}_llm_mode_smoke_check",
            seed=42,
            mode="llm_mode_smoke_check",
            config={"purpose": "Step 14 smoke check before a real full-hour --mode llm run"},
        )
        print(f"run_id={run_id}\n")

        conn = SumoConnection(backend=Backend.LIBSUMO)
        pending_effect: dict[str, tuple[int, dict]] = {}
        try:
            conn.start(NETWORKS_DIR / SCENARIO / "sim.sumocfg", seed=42)
            junction_ids = traffic_light_ids(conn)

            for cycle_id in (1, 2):
                conn.simulation_step(until=CONTROL_INTERVAL_S * cycle_id)
                sim_time = conn.simulation.getTime()
                snapshots = collect_state(conn, junction_ids)
                agent_snapshots = {jid: snapshots[jid] for jid in LLM_AGENT_JUNCTIONS}
                agent_tls_states = {jid: read_tls_state(conn, jid) for jid in LLM_AGENT_JUNCTIONS}

                if pending_effect:
                    print(f"  resolving effect for {len(pending_effect)} decision(s) from the previous cycle")
                    for jid, (decision_id, before) in pending_effect.items():
                        after = _metric_dict(agent_snapshots[jid])
                        await store.update_decision(decision_id, effect={"before": before, "after": after})
                        print(f"    decision_id={decision_id} ({jid}): before={before} after={after}")
                    pending_effect.clear()

                print(f"  cycle {cycle_id} (sim_time={sim_time:.0f}s): running full pipeline...")
                decisions = await _run_llm_decision_cycle(
                    agents, neighbor_map, agent_snapshots, agent_tls_states, supervisor,
                    sim_time, cycle_id, run_id, store,
                )
                for d in decisions:
                    print(
                        f"    {d.junction_id}: decision_id={d.decision_id} validator={d.validator_status} "
                        f"supervisor={d.supervisor_verdict} final_action={d.final_action}"
                    )
                    assert isinstance(d.decision_id, int)

                await _apply_llm_decisions(conn, store, decisions, agent_snapshots, pending_effect)
        finally:
            conn.close()
            await store.finish_run(run_id)

    await engine.dispose()
    print("\nPASS -- 2 real cycles ran through the exact sim.runner code path without crashing.")


def main() -> None:
    check_apply_set_cycle_length()
    check_apply_set_offset()
    asyncio.run(check_two_real_llm_cycles())


if __name__ == "__main__":
    main()
