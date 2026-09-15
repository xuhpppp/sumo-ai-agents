"""Unit tests for agents/junction.py's JunctionAgent (STEPS.md Step 12).

Same fake-OpenAI-client injection pattern as tests/test_llm.py -- these test
JunctionAgent's own logic (prompt building, the no_action fallback on LLM
failure, junction_id normalization), not OpenAI's actual behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

from sumo_agents.agents.junction import JunctionAgent, _build_system_prompt
from sumo_agents.agents.protocol import Message, Proposal
from sumo_agents.safety.validator import NoAction, PhaseState, SetGreenBounds, TlsState
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


def test_system_prompt_carries_the_untrusted_data_notice() -> None:
    # Step 13: coalition messages are the first real untrusted content a
    # JunctionAgent sees, so the notice must actually be in the prompt.
    prompt = _build_system_prompt("B1", ["A1"])
    assert "<<<UNTRUSTED_DATA" in prompt


async def test_observe_returns_the_parsed_proposal_on_success() -> None:
    reply = Proposal(
        junction_id="B1",
        action=SetGreenBounds(junction_id="B1", phase_id="0", min_green_s=20.0, max_green_s=90.0),
        urgency="medium",
        rationale="Hàng đợi hướng chính đang tăng.",
    )
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", ["A1", "B0", "B2", "C1"])

    proposal, usage = await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    assert proposal.junction_id == "B1"
    assert isinstance(proposal.action, SetGreenBounds)
    assert usage.status == "ok"
    assert usage.cached_tokens == 1400


async def test_observe_normalizes_a_junction_id_mismatch() -> None:
    # The model echoed the wrong junction_id -- must not silently propagate.
    reply = Proposal(
        junction_id="WRONG",
        action=SetGreenBounds(junction_id="WRONG", phase_id="0", min_green_s=20.0, max_green_s=90.0),
        urgency="medium",
        rationale="x",
    )
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", [])

    proposal, _usage = await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    assert proposal.junction_id == "B1"
    assert proposal.action.junction_id == "B1"


async def test_observe_with_no_history_says_so_explicitly() -> None:
    # STEPS.md Step 14 follow-up: a real full-hour run found the model
    # treating "only one cycle of data" as a reason to never act, even
    # through real congestion. The first cycle of a run has no history --
    # the prompt must say that plainly rather than silently omitting the
    # section (an omission reads as "no data provided" more than "n/a").
    reply = Proposal(junction_id="B1", action=NoAction(junction_id="B1"), urgency="low", rationale="x")
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", ["A1"])

    await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    user_prompt = client.responses.calls[0]["input"][1]["content"]
    assert "trend_last_cycles: (none" in user_prompt


async def test_observe_passes_trend_history_into_the_user_prompt() -> None:
    reply = Proposal(junction_id="B1", action=NoAction(junction_id="B1"), urgency="low", rationale="x")
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", ["A1"])
    history = [
        JunctionSnapshot(
            junction_id="B1", current_phase=0, queue_len=12, mean_waiting_s=20.1, throughput=10, mean_speed=8.2, co2_mg=1.0
        ),
        JunctionSnapshot(
            junction_id="B1", current_phase=0, queue_len=28, mean_waiting_s=55.3, throughput=8, mean_speed=3.1, co2_mg=1.0
        ),
    ]

    await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=180.0, history=history, client=client)

    user_prompt = client.responses.calls[0]["input"][1]["content"]
    assert "trend_last_2_cycles" in user_prompt
    assert "queue_len=[12, 28]" in user_prompt
    assert "mean_waiting_s=[20.1, 55.3]" in user_prompt


async def test_observe_passes_per_phase_breakdown_into_the_user_prompt() -> None:
    # STEPS.md Step 14 follow-up #2: queue_len/mean_waiting_s above are
    # junction-wide totals -- adjust_phase_split needs a per-phase_id
    # breakdown to pick the right phase instead of guessing.
    reply = Proposal(junction_id="B1", action=NoAction(junction_id="B1"), urgency="low", rationale="x")
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", ["A1"])
    per_phase = {
        "0": {"queue_len": 40, "mean_waiting_s": 90.5},
        "2": {"queue_len": 3, "mean_waiting_s": 1.2},
    }

    await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, per_phase=per_phase, client=client)

    user_prompt = client.responses.calls[0]["input"][1]["content"]
    assert "phase 0: queue_len=40 mean_waiting_s=90.5" in user_prompt
    assert "phase 2: queue_len=3 mean_waiting_s=1.2" in user_prompt


async def test_observe_omits_per_phase_section_when_not_given() -> None:
    reply = Proposal(junction_id="B1", action=NoAction(junction_id="B1"), urgency="low", rationale="x")
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", ["A1"])

    await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    user_prompt = client.responses.calls[0]["input"][1]["content"]
    assert "per_phase_queue" not in user_prompt


async def test_observe_falls_back_to_no_action_on_llm_failure() -> None:
    client = _FakeClient(RuntimeError("boom"))
    agent = JunctionAgent("B1", ["A1"])

    proposal, usage = await agent.observe(_SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    assert isinstance(proposal.action, NoAction)
    assert proposal.junction_id == "B1"
    assert usage.status == "error"


_INCOMING = Message(
    sender="A1",
    recipients=["B1"],
    intent="request_help",
    payload={"type": "adjust_phase_split", "delta_s": 10.0},
    rationale="A1 đang tắc, cần B1 hỗ trợ.",
)


async def test_reply_builds_the_message_from_the_models_intent_and_rationale() -> None:
    # The model only decides `intent`/`rationale` now (protocol.Message's
    # `payload: dict` isn't OpenAI-structured-output-compatible -- see
    # junction.py's _CoalitionReplyDecision docstring); sender/recipients are
    # always set deterministically, so there's nothing for the model to get
    # wrong there anymore.
    reply = SimpleNamespace(intent="object", rationale="B1 cần ưu tiên ngược hướng lúc này.")
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", ["A1"])

    message, usage = await agent.reply(_INCOMING, _SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    assert message.sender == "B1"
    assert message.recipients == ["A1"]
    assert message.intent == "object"
    assert message.rationale == "B1 cần ưu tiên ngược hướng lúc này."
    assert usage.status == "ok"


async def test_reply_falls_back_to_ack_on_llm_failure() -> None:
    client = _FakeClient(RuntimeError("boom"))
    agent = JunctionAgent("B1", ["A1"])

    message, usage = await agent.reply(_INCOMING, _SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    assert message.intent == "ack"
    assert message.sender == "B1"
    assert message.recipients == ["A1"]
    assert usage.status == "error"


async def test_reply_uses_the_same_system_prompt_and_cache_key_as_observe() -> None:
    reply = SimpleNamespace(intent="report", rationale="x")
    client = _FakeClient(_fake_response(reply))
    agent = JunctionAgent("B1", ["A1"])

    await agent.reply(_INCOMING, _SNAPSHOT, _TLS_STATE, sim_time=90.0, client=client)

    (call,) = client.responses.calls
    assert call["prompt_cache_key"] == "junction:B1:v6"
    assert call["input"][0]["content"] == agent._system


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
    assert call["prompt_cache_key"] == "junction:B1:v6"
    assert call["text_format"] is Proposal
    assert call["model"] == "gpt-5.6-luna"
    assert call["input"][0]["role"] == "system"
    assert call["input"][1]["content"]  # the per-cycle user prompt, non-empty
