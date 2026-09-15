"""Async writer for the observability schema (STEPS.md Step 4).

`metrics` inserts are batched instead of committed one row at a time --
SimRunner (Step 5) samples metrics every 10 simulated seconds per junction,
and every junction commits at the same cadence, so committing row-by-row
would multiply round trips for no benefit. `runs`, `messages`, `decisions`,
and `llm_calls` are written immediately, one row per call: each already
represents one meaningful event (one run starting, one agent message, one
decision, one LLM call), and delaying any of those would delay the
dashboard and the replay log (plan section 6.3) without saving anything
worth saving.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sumo_agents.obs.models import Decision, LlmCall, Message, Metric, Run


class Store:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        metrics_batch_size: int = 50,
    ) -> None:
        self._session_factory = session_factory
        self._metrics_batch_size = metrics_batch_size
        self._metrics_buffer: list[Metric] = []

    # ------------------------------------------------------------------ #
    # runs
    # ------------------------------------------------------------------ #

    async def create_run(
        self,
        *,
        scenario: str,
        seed: int,
        mode: str,
        config: dict[str, Any],
        git_sha: str | None = None,
    ) -> uuid.UUID:
        run = Run(scenario=scenario, seed=seed, mode=mode, config=config, git_sha=git_sha)
        async with self._session_factory() as session:
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return run.run_id

    async def finish_run(self, run_id: uuid.UUID, *, summary: dict[str, Any] | None = None) -> None:
        values: dict[str, Any] = {"finished_at": datetime.now(UTC)}
        if summary is not None:
            values["summary"] = summary
        async with self._session_factory() as session:
            await session.execute(update(Run).where(Run.run_id == run_id).values(**values))
            await session.commit()

    # ------------------------------------------------------------------ #
    # messages / decisions / llm_calls -- one row per call, written immediately
    # ------------------------------------------------------------------ #

    async def add_message(self, **fields: Any) -> None:
        async with self._session_factory() as session:
            session.add(Message(**fields))
            await session.commit()

    async def add_decision(self, **fields: Any) -> int:
        """Returns the new row's id -- STEPS.md Step 14 needs it to come
        back later (once the runner actually applies the action, and again
        one cycle after that once `effect` can be measured) via
        `update_decision`."""
        async with self._session_factory() as session:
            decision = Decision(**fields)
            session.add(decision)
            await session.commit()
            await session.refresh(decision)
            return decision.id

    async def update_decision(self, decision_id: int, **fields: Any) -> None:
        """Patch an existing `decisions` row -- STEPS.md Step 14 uses this
        for `applied` (once the action is actually pushed via TraCI, which
        `run_decision_cycle` itself never does -- only the sim loop touches
        TraCI, plan section 3.1) and `effect` (one cycle later, see
        obs/models.py's `Decision.effect` docstring)."""
        async with self._session_factory() as session:
            await session.execute(update(Decision).where(Decision.id == decision_id).values(**fields))
            await session.commit()

    async def add_llm_call(self, **fields: Any) -> None:
        async with self._session_factory() as session:
            session.add(LlmCall(**fields))
            await session.commit()

    # ------------------------------------------------------------------ #
    # metrics -- buffered, flushed once the batch fills or on close()
    # ------------------------------------------------------------------ #

    async def add_metric(self, **fields: Any) -> None:
        """Buffer one metrics row; flush automatically once the batch is full."""
        self._metrics_buffer.append(Metric(**fields))
        if len(self._metrics_buffer) >= self._metrics_batch_size:
            await self.flush_metrics()

    async def flush_metrics(self) -> None:
        if not self._metrics_buffer:
            return
        async with self._session_factory() as session:
            session.add_all(self._metrics_buffer)
            await session.commit()
        self._metrics_buffer.clear()

    async def close(self) -> None:
        """Flush any buffered metrics. Call at the end of a run so the last
        partial batch isn't silently dropped."""
        await self.flush_metrics()

    async def __aenter__(self) -> Store:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
