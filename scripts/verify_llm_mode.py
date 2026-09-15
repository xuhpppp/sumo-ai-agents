"""Step 14 short smoke check (STEPS.md): exercise the ACTUAL production code
path (`sim.runner`'s `_run_llm_decision_cycle`/`_apply_llm_decisions`) for a
couple of cycles before recommending a real full-hour `--mode llm` run --
Steps 12/13 both found real API-only bugs (an OpenAI structured-output
schema rejection) that only surfaced on a live call, so a short, cheap check
here is meant to catch anything similar before the ~$2, unattended full run.

Checks:
  1a/1b. `apply_action` for `set_cycle_length`/`set_offset` against the
      STATIC network -- kept as general regression checks for
      `sim/actuators.py`, even though `llm` mode no longer proposes either
      (STEPS.md Step 14 actuated-hybrid follow-up retired them from
      JunctionAgent's action space). $0, no LLM.
  1c. `phase_lane_groups`/`per_phase_traffic` against the ACTUATED network
      (what `llm` mode actually loads now) -- $0, no LLM: confirms the
      per-phase lane grouping covers every controlled lane exactly once
      (summed per-phase queue_len must equal the whole-junction total
      `collect_state` already reports).
  1d. `apply_action(SetGreenBounds)` against the ACTUATED network -- the
      one action `llm` mode's JunctionAgent can now actually propose. $0,
      no LLM: confirms `setProgramLogic` changing minDur/maxDur live is
      read back correctly (the actual live-enforcement behavior was
      verified separately via a real, non-committed probe -- see
      `_apply_set_green_bounds`'s docstring).
  2. Two real decision cycles through `sim.runner`'s actual internal
     functions (not reimplemented here), against the ACTUATED network --
     confirms `decision_id`/`effect` wiring and the new `set_green_bounds`
     action space end to end, real API calls included. Real OpenAI calls,
     small cost (~$0.05-0.15: 12 observe calls + up to a few coalition
     replies + up to 2 supervisor calls).

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
from sumo_agents.safety.validator import SetCycleLength, SetGreenBounds, SetOffset  # noqa: E402
from sumo_agents.sim.actuators import apply_action, per_phase_traffic, phase_lane_groups, read_tls_state  # noqa: E402
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


def check_phase_lane_groups() -> None:
    print("--- check 1c: phase_lane_groups/per_phase_traffic against real TraCI (actuated network) ---")
    conn = SumoConnection(backend=Backend.LIBSUMO)
    try:
        conn.start(NETWORKS_DIR / SCENARIO / "sim_actuated.sumocfg", seed=42)
        jid = LLM_AGENT_JUNCTIONS[0]
        groups = phase_lane_groups(conn, jid)
        print(f"  {jid}: green phase_ids={sorted(groups)} lanes={groups}")
        assert set(groups) == {"0", "2"}, f"expected 2 green phases (0,2) for grid_4x4, got {sorted(groups)}"
        assert all(lanes for lanes in groups.values()), "every green phase must serve at least one lane"

        for _ in range(300):  # let real traffic build up before comparing
            conn.simulation_step()

        traffic = per_phase_traffic(conn, groups)
        whole = collect_state(conn, [jid])[jid]
        summed_queue = sum(m["queue_len"] for m in traffic.values())
        print(f"  per_phase_traffic={traffic}")
        print(f"  sum(per-phase queue_len)={summed_queue} vs collect_state whole-junction queue_len={whole.queue_len}")
        assert summed_queue == whole.queue_len, (
            "per-phase queue sum must equal the whole-junction total -- same underlying lanes, just grouped"
        )
        print("  PASS")
    finally:
        conn.close()


def check_apply_set_green_bounds() -> None:
    print("--- check 1d: apply_action(SetGreenBounds) against real TraCI (actuated network) ---")
    conn = SumoConnection(backend=Backend.LIBSUMO)
    try:
        conn.start(NETWORKS_DIR / SCENARIO / "sim_actuated.sumocfg", seed=42)
        jid = LLM_AGENT_JUNCTIONS[0]
        before = read_tls_state(conn, jid)
        phase = next(p for p in before.phases if p.kind == "green")
        print(f"  {jid} phase {phase.phase_id}: current bounds=[{phase.min_dur_s}, {phase.max_dur_s}]")

        new_min, new_max = 20.0, 60.0
        apply_action(conn, SetGreenBounds(junction_id=jid, phase_id=phase.phase_id, min_green_s=new_min, max_green_s=new_max))

        after = read_tls_state(conn, jid)
        after_phase = after.phase(phase.phase_id)
        print(f"  readback: bounds=[{after_phase.min_dur_s}, {after_phase.max_dur_s}] (target [{new_min}, {new_max}])")
        assert after_phase.min_dur_s == new_min and after_phase.max_dur_s == new_max
        print("  PASS")
    finally:
        conn.close()


async def check_two_real_llm_cycles() -> None:
    print("\n--- check 2: 2 real decision cycles through sim.runner's actual functions (actuated network) ---")
    net_file = NETWORKS_DIR / SCENARIO / "net_actuated.xml"
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
            conn.start(NETWORKS_DIR / SCENARIO / "sim_actuated.sumocfg", seed=42)
            junction_ids = traffic_light_ids(conn)
            lane_groups = {jid: phase_lane_groups(conn, jid) for jid in LLM_AGENT_JUNCTIONS}
            history: dict[str, list] = {jid: [] for jid in LLM_AGENT_JUNCTIONS}

            for cycle_id in (1, 2):
                conn.simulation_step(until=CONTROL_INTERVAL_S * cycle_id)
                sim_time = conn.simulation.getTime()
                snapshots = collect_state(conn, junction_ids)
                agent_snapshots = {jid: snapshots[jid] for jid in LLM_AGENT_JUNCTIONS}
                agent_tls_states = {jid: read_tls_state(conn, jid) for jid in LLM_AGENT_JUNCTIONS}
                agent_per_phase = {jid: per_phase_traffic(conn, lane_groups[jid]) for jid in LLM_AGENT_JUNCTIONS}
                agent_history = {jid: list(history[jid]) for jid in LLM_AGENT_JUNCTIONS}
                for jid in LLM_AGENT_JUNCTIONS:
                    history[jid].append(agent_snapshots[jid])

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
                    history=agent_history, per_phase=agent_per_phase,
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
    check_phase_lane_groups()
    check_apply_set_green_bounds()
    asyncio.run(check_two_real_llm_cycles())


if __name__ == "__main__":
    main()
