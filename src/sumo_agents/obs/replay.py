"""Load a previously-recorded mode="llm" run so `sim/runner.py` can replay
it without ever calling `agents/llm.py`'s `ask()` again (STEPS.md Step 17 /
IMPLEMENTATION_PLAN.md section 6.3).

Why this works: SUMO is deterministic given (scenario, seed) -- verified
since STEPS.md Step 5 ("chay 2 lan cung seed -> 0 khac biet"). So re-running
the same scenario/seed and, at each control interval, re-applying to TraCI
EXACTLY the action that actually changed the simulation the first time
(`Decision.final_action_type`/`final_action_params`, see obs/models.py)
reproduces a bit-identical trajectory -- without ever calling the model
again. The already-persisted `messages`/`decisions`/`llm_calls` rows of the
source run ARE the "LLM I/O cache" the plan asks for; no separate cache
table is needed, since together they already capture everything a decision
cycle produced (coalition rationale text, the chosen/applied action,
supervisor verdict, token/cost/latency). Rows are grouped here by
`sim_time`, which is unique per control interval within one run (fixed
`CONTROL_INTERVAL_S`, cycles never repeat a sim_time) and, because it comes
from the same deterministic simulation clock, lines up exactly between the
source run and a fresh replay run of the same (scenario, seed). A control
interval with nothing recorded at its sim_time was a skipped cycle in the
source run (STEPS.md Step 14's `n_skipped_cycles`) -- replay reproduces
that for free, by finding nothing to apply, with no separate bookkeeping.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sumo_agents.obs.models import Decision, LlmCall, Message, Run


class ReplaySource:
    """One source run's `messages`/`decisions`/`llm_calls`, indexed by
    `sim_time` for cheap one-control-interval-at-a-time lookup."""

    def __init__(self, run: Run, messages: list[Message], decisions: list[Decision], llm_calls: list[LlmCall]) -> None:
        self.run = run
        self._messages_by_time: dict[float, list[Message]] = defaultdict(list)
        self._decisions_by_time: dict[float, list[Decision]] = defaultdict(list)
        self._llm_calls_by_time: dict[float, list[LlmCall]] = defaultdict(list)
        for m in messages:
            self._messages_by_time[m.sim_time].append(m)
        for d in decisions:
            self._decisions_by_time[d.sim_time].append(d)
        for c in llm_calls:
            # sim_time is nullable on LlmCall in principle (obs/models.py),
            # but every call the runner actually makes always passes one.
            self._llm_calls_by_time[c.sim_time if c.sim_time is not None else -1.0].append(c)

    def at(self, sim_time: float) -> tuple[list[Message], list[Decision], list[LlmCall]]:
        """Everything the source run recorded for the control interval at
        `sim_time` -- empty across all three iff that cycle was skipped."""
        return (
            self._messages_by_time.get(sim_time, []),
            self._decisions_by_time.get(sim_time, []),
            self._llm_calls_by_time.get(sim_time, []),
        )


async def load_replay_source(session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> ReplaySource:
    """Fetch everything `--replay` needs for one source run. Raises
    `ValueError` (not a 404-style silent empty result) for an unknown
    run_id or a run that wasn't mode="llm" -- replaying a baseline run
    makes no sense (nothing to replay: baselines never call `ask()`)."""
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise ValueError(f"replay source run {run_id} not found")
        if run.mode != "llm":
            raise ValueError(f"replay source run {run_id} has mode={run.mode!r}, expected 'llm'")
        messages = (await session.execute(select(Message).where(Message.run_id == run_id))).scalars().all()
        decisions = (await session.execute(select(Decision).where(Decision.run_id == run_id))).scalars().all()
        llm_calls = (await session.execute(select(LlmCall).where(LlmCall.run_id == run_id))).scalars().all()
    return ReplaySource(run, list(messages), list(decisions), list(llm_calls))
