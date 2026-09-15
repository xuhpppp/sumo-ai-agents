"""The single LLM call site (IMPLEMENTATION_PLAN.md section 3.3, STEPS.md Step 10).

Every model call in this project goes through `ask()` below -- no agent
constructs an `AsyncOpenAI` client or calls `responses.parse` directly. This
is not a provider-abstraction layer; it exists so that:

  - a future provider swap touches one file, not every agent (plan section 1.2)
  - prompt-cache-breaking changes (an unstable `system` prefix, a reordered
    tool list, a timestamp leaking into the cached part of the prompt) have
    exactly one place to look
  - `Usage` is always shaped the same way, so callers can log it to the
    `llm_calls` table (obs/models.py) without re-deriving token/cost fields
    themselves

Caching (OpenAI Responses API, verified via scripts/verify_llm_cache.py):
  - Automatic once input hits OpenAI's ~1024-token threshold -- unlike
    Anthropic, there is no manual `cache_control` to attach.
  - Requires a byte-stable prefix: `system` must not change across calls
    meant to share a cache entry. Anything that varies per decision cycle
    (queue lengths, sim_time, incoming coalition messages) belongs in
    `user`, never in `system`.
  - `prompt_cache_key` should be stable per node, e.g. `"junction:J12:v1"`,
    so repeated calls for the same junction route to the same cache shard;
    bump the trailing version when the system prompt text itself changes.
  - Verified with `usage.input_tokens_details.cached_tokens` -- if that is
    always 0 across repeated same-prefix calls, something is breaking the
    prefix. STEPS.md Step 10's DoD is exactly this check; if it fails, stop
    and fix the prefix before running anything that spends real money (the
    90% cache discount is the project's single biggest cost lever).

A failed or malformed call never raises out of `ask()` -- it returns
`(None, usage_with_status_error)` instead. Per plan section 3.2 ("sim is
never blocked by the LLM"), a caller (e.g. a JunctionAgent) is expected to
treat `None` as a cue to fall back to `no_action` for that cycle rather than
propagate an exception into the simulation loop.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeVar

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel

from sumo_agents.obs.cost import compute_cost_usd

T = TypeVar("T", bound=BaseModel)

# role -> (model, reasoning effort). Allocation from IMPLEMENTATION_PLAN.md
# section 1.2: `junction` is the high-volume, narrow-schema role
# (n_agents x n_cycles x n_rounds calls per run) so it gets the cheapest
# model; `supervisor` runs once per cycle and has to arbitrate conflicts
# (mid-tier); `scenario` runs offline, once per scenario, where quality
# matters more than price (flagship).
MODELS: dict[str, tuple[str, str]] = {
    "junction": ("gpt-5.6-luna", "low"),
    "supervisor": ("gpt-5.6-terra", "medium"),
    "scenario": ("gpt-5.6-sol", "high"),
}

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """Lazily construct the shared AsyncOpenAI client.

    Mirrors obs/db.py's `database_url()` pattern: load `.env` without
    overriding a real environment variable, then fail loudly (not with a
    silent/guessed fallback) if the key is missing.
    """
    global _client
    if _client is None:
        load_dotenv(override=False)
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to your .env (see .env.example). "
                "Never hardcode it in code."
            )
        _client = AsyncOpenAI()
    return _client


@dataclass(frozen=True, slots=True)
class Usage:
    """One `ask()` call's accounting -- maps directly onto obs/models.py's
    `LlmCall` columns. `status`/`error` cover the failed-call case (network
    error, refusal, schema mismatch) where every token/cost field is None."""

    model: str
    effort: str
    status: str  # "ok" | "error"
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    latency_ms: int | None = None
    cost_usd: Decimal | None = None
    error: str | None = None


async def ask(
    role: str,
    system: str,
    user: str,
    schema: type[T],
    *,
    cache_key: str,
    client: AsyncOpenAI | None = None,
) -> tuple[T | None, Usage]:
    """Make one structured-output call for `role` and return (parsed, usage).

    `client` is injectable for tests; production callers omit it and get the
    shared lazily-constructed client.
    """
    model, effort = MODELS[role]
    active_client = client if client is not None else _get_client()
    started = time.monotonic()
    try:
        resp = await active_client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text_format=schema,
            reasoning={"effort": effort},
            prompt_cache_key=cache_key,
        )
    except Exception as exc:  # noqa: BLE001 -- any failure here must degrade to no_action upstream, not crash the sim loop
        latency_ms = int((time.monotonic() - started) * 1000)
        return None, Usage(model=model, effort=effort, status="error", latency_ms=latency_ms, error=str(exc))

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = resp.usage
    if usage is None:
        return resp.output_parsed, Usage(model=model, effort=effort, status="ok", latency_ms=latency_ms)

    cached_tokens = usage.input_tokens_details.cached_tokens
    reasoning_tokens = usage.output_tokens_details.reasoning_tokens
    cost_usd = compute_cost_usd(
        model,
        input_tokens=usage.input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=usage.output_tokens,
    )
    return resp.output_parsed, Usage(
        model=model,
        effort=effort,
        status="ok",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
    )
