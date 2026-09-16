"""Unit tests for STEPS.md Step 17 (`--replay`)'s DB-only pieces.

`sim/runner.py`'s replay loop itself needs a real SUMO/libsumo process (same
reason `sim/runner.py` has no dedicated test module at all -- see Step 5/8/14's
verification scripts instead), but `obs/replay.py` (loading a source run) and
`sim/runner.py`'s `_action_from_row`/`_ACTION_TYPES` (reconstructing an
`Action` from a `decisions` row) are pure DB/Pydantic logic -- same style as
test_web.py, against the in-memory SQLite `store`/`session_factory` fixtures.
"""

from __future__ import annotations

import uuid

import pytest

from sumo_agents.obs.models import Decision
from sumo_agents.obs.replay import ReplaySource, load_replay_source
from sumo_agents.obs.store import Store
from sumo_agents.safety.validator import NoAction, SetGreenBounds
from sumo_agents.sim import runner as sim_runner
from sumo_agents.sim.runner import _action_from_row, _apply_due_replay_actions, _replay_decision_cycle


async def _get_decision(session_factory, decision_id: int) -> Decision:
    async with session_factory() as session:
        return await session.get(Decision, decision_id)


async def test_load_replay_source_raises_for_unknown_run(session_factory) -> None:
    with pytest.raises(ValueError, match="not found"):
        await load_replay_source(session_factory, uuid.uuid4())


async def test_load_replay_source_raises_for_non_llm_run(store: Store, session_factory) -> None:
    run_id = await store.create_run(scenario="grid_4x4", seed=42, mode="fixed", config={})
    with pytest.raises(ValueError, match="mode='fixed'"):
        await load_replay_source(session_factory, run_id)


async def test_replay_source_groups_rows_by_sim_time(store: Store, session_factory) -> None:
    run_id = await store.create_run(scenario="grid_4x4", seed=42, mode="llm", config={})
    await store.add_message(
        run_id=run_id, sim_time=90.0, cycle_id=0, round=2, sender="B1", recipients=["B0"],
        intent="request_help", payload={}, rationale="B1 dang tac.",
    )
    await store.add_decision(
        run_id=run_id, sim_time=90.0, cycle_id=0, junction_id="B1", action_type="set_green_bounds",
        params={"phase_id": "0", "min_green_s": 20.0, "max_green_s": 60.0}, validator_status="ok",
        supervisor_verdict="approved", applied=True, final_action_type="set_green_bounds",
        final_action_params={"phase_id": "0", "min_green_s": 20.0, "max_green_s": 60.0},
    )
    await store.add_llm_call(
        run_id=run_id, sim_time=90.0, agent_id="B1", role="junction", model="gpt-5.6-luna",
        effort="low", input_tokens=100, output_tokens=10, cached_tokens=0, latency_ms=2000,
        status="ok", cost_usd=0.0003,
    )

    source = await load_replay_source(session_factory, run_id)
    assert isinstance(source, ReplaySource)
    assert source.run.run_id == run_id

    messages, decisions, llm_calls = source.at(90.0)
    assert [m.sender for m in messages] == ["B1"]
    assert [d.junction_id for d in decisions] == ["B1"]
    assert [c.agent_id for c in llm_calls] == ["B1"]

    # A control interval with nothing recorded -- i.e. a skipped cycle in
    # the source run (STEPS.md Step 14's n_skipped_cycles) -- comes back
    # empty across all three, not a KeyError.
    assert source.at(180.0) == ([], [], [])


def test_action_from_row_reconstructs_set_green_bounds() -> None:
    action = _action_from_row("set_green_bounds", "B1", {"phase_id": "0", "min_green_s": 20.0, "max_green_s": 60.0})
    assert action == SetGreenBounds(junction_id="B1", phase_id="0", min_green_s=20.0, max_green_s=60.0)


def test_action_from_row_reconstructs_no_action() -> None:
    assert _action_from_row("no_action", "C1", {}) == NoAction(junction_id="C1")


async def test_replay_decision_cycle_defers_apply_to_applied_sim_time(
    store: Store, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this mechanism fixes (found by replaying a real historical
    run, STEPS.md Step 17): mode="llm" decides at one sim_time but only
    actually pushes to TraCI later, once its background task finishes --
    applying it immediately at decide-time instead measurably changed the
    replayed trajectory (~1% off on mean_travel_time_s)."""
    applied_actions: list = []
    monkeypatch.setattr(sim_runner, "apply_action", lambda conn, action: applied_actions.append(action))

    source_run_id = await store.create_run(scenario="grid_4x4", seed=42, mode="llm", config={})
    await store.add_decision(
        run_id=source_run_id, sim_time=90.0, cycle_id=0, junction_id="B1", action_type="set_green_bounds",
        params={"phase_id": "0", "min_green_s": 25.0, "max_green_s": 55.0}, validator_status="ok",
        supervisor_verdict="approved", applied=True, final_action_type="set_green_bounds",
        final_action_params={"phase_id": "0", "min_green_s": 25.0, "max_green_s": 55.0},
        applied_sim_time=150.0,  # decided at t=90, only actually applied at t=150 in the source run
    )
    source = await load_replay_source(session_factory, source_run_id)

    replay_run_id = await store.create_run(scenario="grid_4x4", seed=42, mode="llm", config={})
    had_activity, pending = await _replay_decision_cycle(None, store, replay_run_id, 0, 90.0, source)
    assert had_activity is True
    assert len(pending) == 1
    target_sim_time, decision_id, action, _effect = pending[0]
    assert target_sim_time == 150.0
    assert action == SetGreenBounds(junction_id="B1", phase_id="0", min_green_s=25.0, max_green_s=55.0)

    # Not due yet at sim_time=120 -- must NOT be applied early.
    still_pending = await _apply_due_replay_actions(None, store, pending, 120.0)
    assert still_pending == pending
    assert applied_actions == []
    row = await _get_decision(session_factory, decision_id)
    assert row.applied is False

    # Due once the replay's own sim clock reaches 150.
    still_pending = await _apply_due_replay_actions(None, store, still_pending, 150.0)
    assert still_pending == []
    assert applied_actions == [action]
    row = await _get_decision(session_factory, decision_id)
    assert row.applied is True
    assert row.applied_sim_time == 150.0


async def test_replay_decision_cycle_falls_back_to_proposed_action_for_old_runs(
    store: Store, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision from a run recorded before this step's
    `final_action_type`/`applied_sim_time` columns existed has both `None`
    -- replay must still work, applying the originally-proposed action
    immediately at decide-time (an accepted approximation for old runs,
    documented in STEPS.md)."""
    applied_actions: list = []
    monkeypatch.setattr(sim_runner, "apply_action", lambda conn, action: applied_actions.append(action))

    source_run_id = await store.create_run(scenario="grid_4x4", seed=42, mode="llm", config={})
    await store.add_decision(
        run_id=source_run_id, sim_time=90.0, cycle_id=0, junction_id="B1", action_type="no_action",
        params={}, validator_status="ok", supervisor_verdict="approved", applied=True,
    )
    source = await load_replay_source(session_factory, source_run_id)
    replay_run_id = await store.create_run(scenario="grid_4x4", seed=42, mode="llm", config={})
    _had_activity, pending = await _replay_decision_cycle(None, store, replay_run_id, 0, 90.0, source)
    assert len(pending) == 1
    target_sim_time, _decision_id, action, _effect = pending[0]
    assert target_sim_time == 90.0  # falls back to decide-time, no applied_sim_time recorded
    assert action == NoAction(junction_id="B1")


async def test_replay_decision_cycle_reports_skipped_cycle(store: Store, session_factory) -> None:
    source_run_id = await store.create_run(scenario="grid_4x4", seed=42, mode="llm", config={})
    source = await load_replay_source(session_factory, source_run_id)
    replay_run_id = await store.create_run(scenario="grid_4x4", seed=42, mode="llm", config={})
    had_activity, pending = await _replay_decision_cycle(None, store, replay_run_id, 0, 90.0, source)
    assert had_activity is False
    assert pending == []
