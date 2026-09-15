"""Unit tests for agents/junction.py's JunctionAgent (STEPS.md Step 12).

Same fake-OpenAI-client injection pattern as tests/test_llm.py -- these test
JunctionAgent's own logic (prompt building, the no_action fallback on LLM
failure, junction_id normalization), not OpenAI's actual behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

from sumo_agents.agents.junction import JunctionAgent, _build_system_prompt
from sumo_agents.agents.protocol import Proposal
from sumo_agents.safety.validator import AdjustPhaseSplit, NoAction, PhaseState, TlsState
from sumo_agents.sim.state import JunctionSnapshot

_SNAPSHOT = JunctionSnapshot(
    junction_id="B1",
    current_phase=0,
    queue_len=12,
    mean_waiting_s=8.5,
    throughput=6,
    mean_speed=3.2,
    co2_mg=15000.0,
)
_TLS_STATE = TlsState(
    junction_id="B1",
    phases=(
        PhaseState(phase_id="0", duration_s=42.0, kind="green"),
        PhaseState(phase_id="1", duration_s=3.0, kind="yellow"),
        PhaseState(phase_id="2", duration_s=42.0, kind="green"),
        PhaseState(phase_id="3", duration_s=3.0, kind="yellow"),
    ),
)


def _fake_response(output_parsed: object):
    return SimpleNamespace(
        output_parsed=output_parsed,
        usage=SimpleNamespace(
            input_tokens=1500,
            output_tokens=50,
            input_tokens_details=SimpleNamespace(cached_tokens=1400),
            output_tokens_details=SimpleNamespace(reasoning_tokens=0),
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


def test_system_prompt_is_byte_stable_and_mentions_the_real_neighbors() -> None:
    a = _build_system_prompt("B1", ["A1", "B0", "B2", "C1"])
    b = _build_system_prompt("B1", ["A1", "B0", "B2", "C1"])
    assert a == b
    assert "B1" in a
    assert "A1, B0, B2, C1" in a


async def test_observe_returns_the_parsed_proposal_on_success() -> None:
    reply = Proposal(
        junction_id="B1",
        action=AdjustPhaseSplit(junction_id="B1", phase_id="0", delta_s=5.0),
        urgency="medium",
        rationale="Hàng đợi hướng chính đang tăng.",
    )
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", ["A1", "B0", "B2", "C1"])

    proposal, usage = await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    assert proposal.junction_id == "B1"
    assert isinstance(proposal.action, AdjustPhaseSplit)
    assert usage.status == "ok"
    assert usage.cached_tokens == 1400


async def test_observe_normalizes_a_junction_id_mismatch() -> None:
    # The model echoed the wrong junction_id -- must not silently propagate.
    reply = Proposal(
        junction_id="WRONG",
        action=AdjustPhaseSplit(junction_id="WRONG", phase_id="0", delta_s=5.0),
        urgency="medium",
        rationale="x",
    )
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", [])

    proposal, _usage = await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    assert proposal.junction_id == "B1"
    assert proposal.action.junction_id == "B1"


async def test_observe_falls_back_to_no_action_on_llm_failure() -> None:
    client = _FakeClient(RuntimeError("boom"))
    agent = JunctionAgent("B1", ["A1"])

    proposal, usage = await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    assert isinstance(proposal.action, NoAction)
    assert proposal.junction_id == "B1"
    assert usage.status == "error"


async def test_observe_passes_cache_key_and_schema_through_to_ask() -> None:
    reply = Proposal(
        junction_id="B1",
        action=NoAction(junction_id="B1"),
        urgency="low",
        rationale="Nút thông thoáng.",
    )
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", ["A1"])

    await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    (call,) = client.responses.calls
    assert call["prompt_cache_key"] == "junction:B1:v1"
    assert call["text_format"] is Proposal
    assert call["model"] == "gpt-5.6-luna"
    assert call["input"][0]["role"] == "system"
    assert call["input"][1]["content"]  # the per-cycle user prompt, non-empty
