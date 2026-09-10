"""Round-trip and behavior tests for the observability schema (STEPS.md Step 4).

Runs entirely against the in-memory SQLite fixtures in conftest.py -- no
Docker/Postgres needed (DoD). Covers the cross-dialect type layer (GUID,
JSONType, StringArray in obs/models.py) and Store's metrics batching.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sumo_agents.obs.models import Decision, LlmCall, Message, Metric, Run
from sumo_agents.obs.store import Store


@pytest.mark.asyncio
async def test_run_roundtrip_uuid_and_json(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        run = Run(scenario="grid_4x4", seed=42, mode="fixed", config={"period": 0.8, "tags": ["poc"]})
        session.add(run)
        await session.commit()
        run_id = run.run_id

    assert isinstance(run_id, uuid.UUID)

    async with session_factory() as session:
        fetched = await session.get(Run, run_id)
        assert fetched is not None
        assert fetched.scenario == "grid_4x4"
        assert fetched.config == {"period": 0.8, "tags": ["poc"]}
        assert fetched.finished_at is None


@pytest.mark.asyncio
async def test_message_array_and_json_roundtrip(store: Store, session_factory) -> None:
    run_id = await store.create_run(scenario="grid_4x4", seed=1, mode="llm", config={})
    await store.add_message(
        run_id=run_id,
        sim_time=90.0,
        cycle_id=1,
        round=1,
        sender="J12",
        recipients=["J11", "J13"],
        intent="propose",
        payload={"action": "adjust_phase_split", "delta_s": 10},
        rationale="Hàng chờ hướng Bắc đang dài, đề xuất tăng 10s pha xanh.",
    )

    async with session_factory() as session:
        msg = (await session.execute(select(Message).where(Message.run_id == run_id))).scalar_one()
        assert msg.recipients == ["J11", "J13"]  # ARRAY on Postgres, JSON on SQLite -- same Python type
        assert msg.payload == {"action": "adjust_phase_split", "delta_s": 10}
        assert msg.round == 1


@pytest.mark.asyncio
async def test_decision_cascade_delete_on_run(store: Store, session_factory) -> None:
    run_id = await store.create_run(scenario="grid_4x4", seed=1, mode="llm", config={})
    await store.add_decision(
        run_id=run_id,
        sim_time=90.0,
        cycle_id=1,
        junction_id="J12",
        action_type="adjust_phase_split",
        params={"delta_s": 10},
        validator_status="ok",
    )

    async with session_factory() as session:
        count_before = len((await session.execute(select(Decision).where(Decision.run_id == run_id))).all())
        assert count_before == 1

        run = await session.get(Run, run_id)
        await session.delete(run)
        await session.commit()

    async with session_factory() as session:
        count_after = len((await session.execute(select(Decision).where(Decision.run_id == run_id))).all())
        assert count_after == 0, "ON DELETE CASCADE should remove decisions when their run is deleted"


@pytest.mark.asyncio
async def test_llm_call_numeric_cost(store: Store, session_factory) -> None:
    run_id = await store.create_run(scenario="grid_4x4", seed=1, mode="llm", config={})
    await store.add_llm_call(
        run_id=run_id,
        agent_id="J12",
        role="junction",
        model="gpt-5.6-luna",
        effort="low",
        input_tokens=1200,
        output_tokens=300,
        reasoning_tokens=120,
        cached_tokens=900,
        latency_ms=850,
        status="ok",
        cost_usd="0.001560",
    )

    async with session_factory() as session:
        call = (await session.execute(select(LlmCall).where(LlmCall.run_id == run_id))).scalar_one()
        assert call.cached_tokens == 900
        assert str(call.cost_usd) == "0.001560"


@pytest.mark.asyncio
async def test_metrics_batching_flushes_at_threshold(session_factory: async_sessionmaker[AsyncSession]) -> None:
    store = Store(session_factory, metrics_batch_size=3)
    run_id = await store.create_run(scenario="grid_4x4", seed=1, mode="fixed", config={})

    async def count_metrics() -> int:
        async with session_factory() as session:
            return len((await session.execute(select(Metric).where(Metric.run_id == run_id))).all())

    await store.add_metric(run_id=run_id, sim_time=10.0, junction_id="J12", mean_waiting_s=5.0)
    await store.add_metric(run_id=run_id, sim_time=20.0, junction_id="J12", mean_waiting_s=6.0)
    assert await count_metrics() == 0, "buffer below batch_size must not hit the DB yet"

    await store.add_metric(run_id=run_id, sim_time=30.0, junction_id="J12", mean_waiting_s=7.0)
    assert await count_metrics() == 3, "buffer reaching batch_size must auto-flush"

    # One more row below threshold, then an explicit close() (end-of-run) must
    # flush the remaining partial batch instead of silently dropping it.
    await store.add_metric(run_id=run_id, sim_time=40.0, junction_id="J12", mean_waiting_s=8.0)
    assert await count_metrics() == 3
    await store.close()
    assert await count_metrics() == 4


@pytest.mark.asyncio
async def test_finish_run_sets_timestamp(store: Store, session_factory) -> None:
    run_id = await store.create_run(scenario="grid_4x4", seed=1, mode="fixed", config={})
    await store.finish_run(run_id)

    async with session_factory() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        assert run.finished_at is not None
