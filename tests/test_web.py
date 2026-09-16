"""Unit tests for web/app.py's FastAPI dashboard backend (STEPS.md Step 15).

Runs against the same in-memory SQLite `store`/`session_factory` fixtures as
every other obs-layer test (conftest.py) -- `get_session_factory` is
overridden via FastAPI's `app.dependency_overrides` so no real Postgres is
needed. `POLL_INTERVAL_S` is monkeypatched down to keep the WebSocket tests
fast; the running loop reads the module-level name fresh on every
iteration (a plain global lookup, not a value captured at import time), so
reassigning it from a test takes effect immediately.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.websockets import WebSocketDisconnect

from sumo_agents.obs.models import Run
from sumo_agents.obs.store import Store
from sumo_agents.web import app as web_app


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "POLL_INTERVAL_S", 0.02)


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
    web_app.app.dependency_overrides[web_app.get_session_factory] = lambda: session_factory
    with TestClient(web_app.app) as c:
        yield c
    web_app.app.dependency_overrides.clear()


async def _seed_run(store: Store, *, mode: str = "llm") -> str:
    run_id = await store.create_run(scenario="_test", seed=0, mode=mode, config={})
    return str(run_id)


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_list_runs_returns_most_recent_first(
    client: TestClient, store: Store, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await _seed_run(store, mode="fixed")
    second = await _seed_run(store, mode="llm")
    # `Run.started_at` uses `server_default=func.now()` -- on PostgreSQL that
    # has microsecond resolution, but SQLite's CURRENT_TIMESTAMP (used for
    # this in-memory test DB, see models.py's GUID/JSONType cross-dialect
    # notes) only has second resolution, so two rows created back-to-back in
    # the same test can tie. Back-date `first` explicitly instead of
    # sleeping a full second to force a real gap.
    async with session_factory() as session:
        await session.execute(
            update(Run).where(Run.run_id == first).values(started_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()

    run_ids = [r["run_id"] for r in client.get("/runs").json()]

    assert run_ids.index(second) < run_ids.index(first)


async def test_list_runs_filters_by_mode(client: TestClient, store: Store) -> None:
    await _seed_run(store, mode="fixed")
    llm_run = await _seed_run(store, mode="llm")

    resp = client.get("/runs", params={"mode": "llm"})

    assert [r["run_id"] for r in resp.json()] == [llm_run]


def test_get_run_404_for_unknown_id(client: TestClient) -> None:
    resp = client.get("/runs/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_get_run_returns_full_record(client: TestClient, store: Store) -> None:
    run_id = await _seed_run(store)

    resp = client.get(f"/runs/{run_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["mode"] == "llm"
    assert body["finished_at"] is None


async def test_list_messages_since_id_only_returns_newer_rows(client: TestClient, store: Store) -> None:
    run_id = await _seed_run(store)
    for i in range(3):
        await store.add_message(
            run_id=run_id,
            sim_time=float(i),
            cycle_id=i,
            round=2,
            sender="B1",
            recipients=["A1"],
            intent="ack",
            payload={},
            rationale=f"msg {i}",
        )

    all_rows = client.get(f"/runs/{run_id}/messages").json()
    assert [r["rationale"] for r in all_rows] == ["msg 0", "msg 1", "msg 2"]

    since_first = client.get(f"/runs/{run_id}/messages", params={"since_id": all_rows[0]["id"]}).json()
    assert [r["rationale"] for r in since_first] == ["msg 1", "msg 2"]


async def test_list_decisions_returns_rows_for_the_run(client: TestClient, store: Store) -> None:
    run_id = await _seed_run(store)
    await store.add_decision(
        run_id=run_id,
        sim_time=90.0,
        cycle_id=1,
        junction_id="B1",
        action_type="set_green_bounds",
        params={},
        validator_status="ok",
        applied=True,
    )

    rows = client.get(f"/runs/{run_id}/decisions").json()

    assert len(rows) == 1
    assert rows[0]["junction_id"] == "B1"
    assert rows[0]["validator_status"] == "ok"


async def test_list_llm_calls_returns_rows_for_the_run(client: TestClient, store: Store) -> None:
    run_id = await _seed_run(store)
    await store.add_llm_call(
        run_id=run_id,
        sim_time=90.0,
        agent_id="B1",
        role="junction",
        model="gpt-5.6-luna",
        status="ok",
        cost_usd=0.001234,
    )

    rows = client.get(f"/runs/{run_id}/llm_calls").json()

    assert len(rows) == 1
    assert rows[0]["cost_usd"] == pytest.approx(0.001234)


async def test_list_metrics_since_sim_time_only_returns_newer_rows(client: TestClient, store: Store) -> None:
    run_id = await _seed_run(store)
    for t in (0.0, 10.0, 20.0):
        await store.add_metric(run_id=run_id, sim_time=t, junction_id="B1", queue_len=1)
    await store.close()

    all_rows = client.get(f"/runs/{run_id}/metrics").json()
    assert [r["sim_time"] for r in all_rows] == [0.0, 10.0, 20.0]

    since_zero = client.get(f"/runs/{run_id}/metrics", params={"since_sim_time": 0.0}).json()
    assert [r["sim_time"] for r in since_zero] == [10.0, 20.0]


def test_ws_rejects_an_unknown_run_id(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect(
        "/ws/runs/00000000-0000-0000-0000-000000000000"
    ):
        pass

    assert exc_info.value.code == 4404


async def test_ws_only_streams_rows_written_after_connecting(client: TestClient, store: Store) -> None:
    run_id = await _seed_run(store)
    await store.add_message(
        run_id=run_id,
        sim_time=0.0,
        cycle_id=0,
        round=2,
        sender="B1",
        recipients=["A1"],
        intent="ack",
        payload={},
        rationale="before connecting",
    )

    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        await store.add_message(
            run_id=run_id,
            sim_time=10.0,
            cycle_id=1,
            round=2,
            sender="B1",
            recipients=["A1"],
            intent="ack",
            payload={},
            rationale="after connecting",
        )
        event = ws.receive_json()

    assert event["type"] == "messages"
    assert [m["rationale"] for m in event["data"]] == ["after connecting"]


async def test_ws_streams_a_new_decision_too(client: TestClient, store: Store) -> None:
    run_id = await _seed_run(store)

    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        await store.add_decision(
            run_id=run_id,
            sim_time=90.0,
            cycle_id=1,
            junction_id="B1",
            action_type="set_green_bounds",
            params={},
            validator_status="ok",
            applied=True,
        )
        event = ws.receive_json()

    assert event["type"] == "decisions"
    assert event["data"][0]["junction_id"] == "B1"


async def test_ws_sends_run_finished_once_the_run_is_marked_done(client: TestClient, store: Store) -> None:
    run_id = await _seed_run(store)

    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        await store.finish_run(run_id, summary={"n_completed_trips": 4500})
        event = ws.receive_json()

    assert event["type"] == "run_finished"
    assert event["data"]["finished_at"] is not None
    assert event["data"]["summary"] == {"n_completed_trips": 4500}
