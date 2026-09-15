"""Unit tests for agents/llm.py's `ask()` (STEPS.md Step 10).

Uses a fake OpenAI client (injected via `ask(..., client=...)`) instead of a
real network call -- these test `ask()`'s own logic (usage extraction, cost
computation, the never-raise error path), not OpenAI's API itself. The real
end-to-end cache behaviour is verified separately and manually via
scripts/verify_llm_cache.py (Step 10's DoD needs a real API key and costs a
small amount of real money, so it is not something to run on every `pytest`).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from sumo_agents.agents.llm import Usage, ask


class _Reply(BaseModel):
    headline: str


def _fake_response(*, output_parsed: object, input_tokens: int, cached_tokens: int, output_tokens: int, reasoning_tokens: int):
    return SimpleNamespace(
        output_parsed=output_parsed,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        ),
    )


class _FakeResponses:
    def __init__(self, response) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeClient:
    def __init__(self, response) -> None:
        self.responses = _FakeResponses(response)


async def test_ask_success_extracts_usage_and_computes_cost() -> None:
    reply = _Reply(headline="ok")
    response = _fake_response(
        output_parsed=reply, input_tokens=2000, cached_tokens=1000, output_tokens=100, reasoning_tokens=20
    )
    client = _FakeClient(response)

    parsed, usage = await ask("junction", "system prompt", "user prompt", _Reply, cache_key="junction:J1:v1", client=client)

    assert parsed == reply
    assert usage.status == "ok"
    assert usage.model == "gpt-5.6-luna"
    assert usage.effort == "low"
    assert usage.input_tokens == 2000
    assert usage.cached_tokens == 1000
    assert usage.output_tokens == 100
    assert usage.reasoning_tokens == 20
    assert usage.cost_usd is not None
    assert usage.cost_usd > 0


async def test_ask_passes_system_user_schema_and_cache_key_through() -> None:
    response = _fake_response(output_parsed=None, input_tokens=1, cached_tokens=0, output_tokens=1, reasoning_tokens=0)
    client = _FakeClient(response)

    await ask("supervisor", "sys", "usr", _Reply, cache_key="supervisor:v1", client=client)

    (call,) = client.responses.calls
    assert call["model"] == "gpt-5.6-terra"
    assert call["reasoning"] == {"effort": "medium"}
    assert call["prompt_cache_key"] == "supervisor:v1"
    assert call["text_format"] is _Reply
    assert call["input"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]


async def test_ask_never_raises_on_api_failure() -> None:
    client = _FakeClient(RuntimeError("boom"))

    parsed, usage = await ask("junction", "sys", "usr", _Reply, cache_key="junction:J1:v1", client=client)

    assert parsed is None
    assert usage.status == "error"
    assert usage.error == "boom"
    assert usage.input_tokens is None
    assert usage.cost_usd is None


async def test_ask_handles_missing_usage() -> None:
    response = SimpleNamespace(output_parsed=None, usage=None)
    client = _FakeClient(response)

    parsed, usage = await ask("junction", "sys", "usr", _Reply, cache_key="junction:J1:v1", client=client)

    assert usage.status == "ok"
    assert usage.input_tokens is None
    assert usage.cost_usd is None


def test_usage_is_a_plain_dataclass() -> None:
    usage = Usage(model="gpt-5.6-luna", effort="low", status="ok")
    assert usage.input_tokens is None


@pytest.mark.parametrize("role", ["junction", "supervisor", "scenario"])
async def test_every_plan_defined_role_resolves_to_a_model(role: str) -> None:
    response = _fake_response(output_parsed=None, input_tokens=1, cached_tokens=0, output_tokens=1, reasoning_tokens=0)
    client = _FakeClient(response)
    await ask(role, "sys", "usr", _Reply, cache_key=f"{role}:v1", client=client)  # must not raise


async def test_unknown_role_raises() -> None:
    with pytest.raises(KeyError):
        await ask("not-a-role", "sys", "usr", _Reply, cache_key="x", client=_FakeClient(RuntimeError("unreachable")))
