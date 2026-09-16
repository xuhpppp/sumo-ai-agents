"""FastAPI dashboard backend (STEPS.md Step 15).

REST serves run history straight from Postgres; the WebSocket endpoint
polls the same tables for rows newer than what that connection has already
seen and pushes them as they appear. This requires no changes to
`sim/runner.py` or `obs/store.py` -- the simulator keeps writing through
`Store` exactly as it always has (STEPS.md Step 4/5/14), the dashboard is
just another reader on the same database, and Postgres's default READ
COMMITTED isolation means a fresh session sees a commit from another
process as soon as it lands. A fresh `AsyncSession` per poll (rather than
one long-lived session for the connection's lifetime) is what makes that
guarantee hold -- a session that stayed open across polls would risk
re-using a snapshot that predates rows the simulator just committed.

Run this directly for local development:
    uvicorn sumo_agents.web.app:app --reload
or:
    python -m sumo_agents.web.app
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sumo_agents.obs.db import make_async_engine, make_session_factory
from sumo_agents.obs.models import Decision, LlmCall, Message, Metric, Run

STATIC_DIR = Path(__file__).parent / "static"

# How often the WebSocket loop re-queries Postgres for new rows. A fixed
# poll cadence is enough to satisfy Step 15's DoD (events show up within
# about a second of being written) without adding a config knob Step 16
# doesn't need yet.
POLL_INTERVAL_S = 1.0

# How many rows a single REST page / WebSocket push can return -- generous
# enough for one dashboard screen, small enough that a client can't
# accidentally pull an entire run's `metrics` table (thousands of rows per
# hour-long run) in one response.
DEFAULT_LIMIT = 1000

app = FastAPI(title="sumo-agents dashboard")

# The frontend (STEPS.md Step 16) is a separate dev server on its own port
# -- see IMPLEMENTATION_PLAN.md's architecture diagram, "FastAPI + WebSocket
# -> Web dashboard" as two boxes. This is a local POC, never exposed
# publicly, so a permissive CORS policy is fine here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------- #
# session wiring -- built lazily so importing this module (e.g. from a
# test, which overrides get_session_factory entirely) never requires
# DATABASE_URL to be set.
# ---------------------------------------------------------------------- #

_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = make_session_factory(make_async_engine())
    return _session_factory


async def get_session(
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> AsyncSession:
    async with session_factory() as session:
        yield session


# ---------------------------------------------------------------------- #
# row -> JSON-safe dict. Returned as plain dicts (not Pydantic response
# models) since the shape already exactly mirrors obs/models.py -- these
# just handle the few types FastAPI's default encoder can't take as-is
# (UUID, datetime, Decimal).
# ---------------------------------------------------------------------- #


def _run_dict(run: Run) -> dict[str, Any]:
    return {
        "run_id": str(run.run_id),
        "scenario": run.scenario,
        "seed": run.seed,
        "mode": run.mode,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "git_sha": run.git_sha,
        "config": run.config,
        "summary": run.summary,
    }


def _message_dict(m: Message) -> dict[str, Any]:
    return {
        "id": m.id,
        "run_id": str(m.run_id),
        "sim_time": m.sim_time,
        "cycle_id": m.cycle_id,
        "round": m.round,
        "sender": m.sender,
        "recipients": m.recipients,
        "intent": m.intent,
        "payload": m.payload,
        "rationale": m.rationale,
    }


def _decision_dict(d: Decision) -> dict[str, Any]:
    return {
        "id": d.id,
        "run_id": str(d.run_id),
        "sim_time": d.sim_time,
        "cycle_id": d.cycle_id,
        "junction_id": d.junction_id,
        "action_type": d.action_type,
        "params": d.params,
        "validator_status": d.validator_status,
        "validator_violations": d.validator_violations,
        "supervisor_verdict": d.supervisor_verdict,
        "supervisor_reason": d.supervisor_reason,
        "applied": d.applied,
        "effect": d.effect,
    }


def _llm_call_dict(c: LlmCall) -> dict[str, Any]:
    return {
        "id": c.id,
        "run_id": str(c.run_id),
        "sim_time": c.sim_time,
        "agent_id": c.agent_id,
        "role": c.role,
        "model": c.model,
        "effort": c.effort,
        "input_tokens": c.input_tokens,
        "output_tokens": c.output_tokens,
        "reasoning_tokens": c.reasoning_tokens,
        "cached_tokens": c.cached_tokens,
        "latency_ms": c.latency_ms,
        "status": c.status,
        "error": c.error,
        "cost_usd": float(c.cost_usd) if c.cost_usd is not None else None,
    }


def _metric_dict(m: Metric) -> dict[str, Any]:
    return {
        "run_id": str(m.run_id),
        "sim_time": m.sim_time,
        "junction_id": m.junction_id,
        "mean_waiting_s": m.mean_waiting_s,
        "queue_len": m.queue_len,
        "throughput": m.throughput,
        "mean_speed": m.mean_speed,
        "co2_mg": m.co2_mg,
    }


# ---------------------------------------------------------------------- #
# shared query helpers -- used by both the REST list endpoints (paging via
# `since_id`/`since_sim_time`) and the WebSocket loop (polling via the same
# cursors, just remembered across iterations instead of passed by a client).
# ---------------------------------------------------------------------- #


async def _get_run_or_404(session: AsyncSession, run_id: uuid.UUID) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return run


async def _rows_since_id(
    session: AsyncSession,
    model: type[Message] | type[Decision] | type[LlmCall],
    run_id: uuid.UUID,
    since_id: int,
    limit: int,
) -> list[Any]:
    stmt = select(model).where(model.run_id == run_id, model.id > since_id).order_by(model.id).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def _max_id(session: AsyncSession, model: type[Message] | type[Decision] | type[LlmCall], run_id: uuid.UUID) -> int:
    result = await session.execute(select(func.max(model.id)).where(model.run_id == run_id))
    return result.scalar() or 0


async def _metrics_since(
    session: AsyncSession, run_id: uuid.UUID, since_sim_time: float, limit: int, junction_id: str | None = None
) -> list[Metric]:
    stmt = (
        select(Metric)
        .where(Metric.run_id == run_id, Metric.sim_time > since_sim_time)
        .order_by(Metric.sim_time)
        .limit(limit)
    )
    if junction_id is not None:
        stmt = stmt.where(Metric.junction_id == junction_id)
    return list((await session.execute(stmt)).scalars().all())


async def _max_sim_time(session: AsyncSession, run_id: uuid.UUID) -> float:
    result = await session.execute(select(func.max(Metric.sim_time)).where(Metric.run_id == run_id))
    value = result.scalar()
    # -1.0, not 0.0: sim_time=0.0 is a real first sample and must still be
    # picked up by a `sim_time > since` comparison.
    return value if value is not None else -1.0


# ---------------------------------------------------------------------- #
# REST -- run history
# ---------------------------------------------------------------------- #


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/runs")
async def list_runs(
    mode: str | None = None,
    limit: int = DEFAULT_LIMIT,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    stmt = select(Run).order_by(Run.started_at.desc()).limit(limit)
    if mode is not None:
        stmt = stmt.where(Run.mode == mode)
    rows = (await session.execute(stmt)).scalars().all()
    return [_run_dict(r) for r in rows]


@app.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    run = await _get_run_or_404(session, run_id)
    return _run_dict(run)


@app.get("/runs/{run_id}/messages")
async def list_messages(
    run_id: uuid.UUID,
    since_id: int = 0,
    limit: int = DEFAULT_LIMIT,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _get_run_or_404(session, run_id)
    rows = await _rows_since_id(session, Message, run_id, since_id, limit)
    return [_message_dict(r) for r in rows]


@app.get("/runs/{run_id}/decisions")
async def list_decisions(
    run_id: uuid.UUID,
    since_id: int = 0,
    limit: int = DEFAULT_LIMIT,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _get_run_or_404(session, run_id)
    rows = await _rows_since_id(session, Decision, run_id, since_id, limit)
    return [_decision_dict(r) for r in rows]


@app.get("/runs/{run_id}/llm_calls")
async def list_llm_calls(
    run_id: uuid.UUID,
    since_id: int = 0,
    limit: int = DEFAULT_LIMIT,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _get_run_or_404(session, run_id)
    rows = await _rows_since_id(session, LlmCall, run_id, since_id, limit)
    return [_llm_call_dict(r) for r in rows]


@app.get("/runs/{run_id}/metrics")
async def list_metrics(
    run_id: uuid.UUID,
    since_sim_time: float = -1.0,
    junction_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _get_run_or_404(session, run_id)
    rows = await _metrics_since(session, run_id, since_sim_time, limit, junction_id)
    return [_metric_dict(r) for r in rows]


# ---------------------------------------------------------------------- #
# WebSocket -- live events for one run
# ---------------------------------------------------------------------- #


@app.websocket("/ws/runs/{run_id}")
async def run_events_ws(
    websocket: WebSocket,
    run_id: uuid.UUID,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> None:
    # Checked (and the socket closed without ever accepting) before
    # `accept()` so a bad run_id gets a clean rejection instead of a socket
    # that opens and then immediately says goodbye.
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            await websocket.close(code=4404, reason=f"run {run_id} not found")
            return
        last_message_id = await _max_id(session, Message, run_id)
        last_decision_id = await _max_id(session, Decision, run_id)
        last_llm_call_id = await _max_id(session, LlmCall, run_id)
        last_sim_time = await _max_sim_time(session, run_id)

    await websocket.accept()
    try:
        while True:
            async with session_factory() as session:
                new_messages = await _rows_since_id(session, Message, run_id, last_message_id, DEFAULT_LIMIT)
                new_decisions = await _rows_since_id(session, Decision, run_id, last_decision_id, DEFAULT_LIMIT)
                new_llm_calls = await _rows_since_id(session, LlmCall, run_id, last_llm_call_id, DEFAULT_LIMIT)
                new_metrics = await _metrics_since(session, run_id, last_sim_time, DEFAULT_LIMIT)
                run = await _get_run_or_404(session, run_id)

            if new_messages:
                last_message_id = new_messages[-1].id
                await websocket.send_json({"type": "messages", "data": [_message_dict(m) for m in new_messages]})
            if new_decisions:
                last_decision_id = new_decisions[-1].id
                await websocket.send_json({"type": "decisions", "data": [_decision_dict(d) for d in new_decisions]})
            if new_llm_calls:
                last_llm_call_id = new_llm_calls[-1].id
                await websocket.send_json({"type": "llm_calls", "data": [_llm_call_dict(c) for c in new_llm_calls]})
            if new_metrics:
                last_sim_time = new_metrics[-1].sim_time
                await websocket.send_json({"type": "metrics", "data": [_metric_dict(m) for m in new_metrics]})

            # Once the run is marked finished AND this poll turned up
            # nothing new, there is nothing left to wait for -- say so and
            # close, rather than polling a finished run forever.
            if run.finished_at is not None and not (new_messages or new_decisions or new_llm_calls or new_metrics):
                await websocket.send_json({"type": "run_finished", "data": _run_dict(run)})
                await websocket.close()
                return

            await asyncio.sleep(POLL_INTERVAL_S)
    except WebSocketDisconnect:
        return


# Mounted last, deliberately -- Starlette matches routes in registration
# order, so every REST/WebSocket route above must exist before this catch-
# all so a request like GET /runs is never shadowed by the static handler.
# `html=True` serves `static/index.html` for `/` and any other unmatched
# path, which is all the STEPS.md Step 16 frontend needs (no SPA router).
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(
        "sumo_agents.web.app:app",
        host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("DASHBOARD_PORT", "8000")),
        reload=True,
    )
