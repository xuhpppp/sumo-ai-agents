"""Unit tests for agents/supervisor.py's SupervisorAgent (STEPS.md Step 13).

Same fake-OpenAI-client injection pattern as tests/test_llm.py /
tests/test_junction.py -- `output_parsed` only needs a `.verdicts` attribute
(supervisor.py never calls Pydantic-specific methods on it), so a
`SimpleNamespace` stands in without needing the module's private
`_SupervisorReview` class.
"""

from __future__ import annotations

from types import SimpleNamespace

from sumo_agents.agents.protocol import SetGreenBounds, Verdict
from sumo_agents.agents.supervisor import SupervisorAgent, SupervisorCandidate, _build_user_prompt
from sumo_agents.agents.topology import NeighborLink
from sumo_agents.safety.validator import AdjustPhaseSplit

_CANDIDATE_A = SupervisorCandidate(
    junction_id="B1",
    proposed_action=AdjustPhaseSplit(junction_id="B1", phase_id="0", delta_s=10.0),
    clamped_action=AdjustPhaseSplit(junction_id="B1", phase_id="0", delta_s=10.0),
    urgency="high",
    rationale="Hàng đợi hướng Bắc tăng nhanh.",
    validator_violations=[],
)
_CANDIDATE_B = SupervisorCandidate(
    junction_id="C1",
    proposed_action=AdjustPhaseSplit(junction_id="C1", phase_id="0", delta_s=8.0),
    clamped_action=AdjustPhaseSplit(junction_id="C1", phase_id="0", delta_s=8.0),
    urgency="medium",
    rationale="Cần thêm thời gian xanh hướng Tây.",
    validator_violations=[],
)


def _fake_response(verdicts: list[Verdict]):
    return SimpleNamespace(
        output_parsed=SimpleNamespace(verdicts=verdicts),
        usage=SimpleNamespace(
            input_tokens=1800,
            output_tokens=120,
            input_tokens_details=SimpleNamespace(cached_tokens=1500),
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


async def test_review_with_no_candidates_makes_no_call_and_returns_empty() -> None:
    client = _FakeClient(RuntimeError("should never be called"))
    supervisor = SupervisorAgent()

    verdicts, usage = await supervisor.review([], [], sim_time=90.0, client=client)

    assert verdicts == {}
    assert usage is None
    assert client.responses.calls == []


async def test_review_uses_the_models_verdicts_when_they_cover_every_candidate() -> None:
    reply = [
        Verdict(junction_id="B1", decision="approved", reason="Trong giới hạn, không xung đột."),
        Verdict(
            junction_id="C1",
            decision="modified",
            modified_action=SetGreenBounds(junction_id="C1", phase_id="0", min_green_s=7.0, max_green_s=40.0),
            reason="Giảm bớt để tránh xung đột với B1.",
        ),
    ]
    client = _FakeClient(_fake_response(reply))
    supervisor = SupervisorAgent()

    verdicts, usage = await supervisor.review([_CANDIDATE_A, _CANDIDATE_B], [], sim_time=90.0, client=client)

    assert verdicts["B1"].decision == "approved"
    assert verdicts["C1"].decision == "modified"
    assert usage.status == "ok"
    assert usage.cached_tokens == 1500


async def test_review_defaults_an_unaddressed_candidate_to_approved() -> None:
    # The model only ruled on B1 -- C1 must still get a safe default verdict.
    reply = [Verdict(junction_id="B1", decision="approved", reason="OK.")]
    client = _FakeClient(_fake_response(reply))
    supervisor = SupervisorAgent()

    verdicts, _usage = await supervisor.review([_CANDIDATE_A, _CANDIDATE_B], [], sim_time=90.0, client=client)

    assert verdicts["C1"].decision == "approved"
    assert "did not address" in verdicts["C1"].reason.lower()


async def test_review_ignores_a_verdict_for_an_unknown_junction_id() -> None:
    reply = [
        Verdict(junction_id="B1", decision="approved", reason="OK."),
        Verdict(junction_id="NOT_A_CANDIDATE", decision="denied", reason="hallucinated"),
    ]
    client = _FakeClient(_fake_response(reply))
    supervisor = SupervisorAgent()

    verdicts, _usage = await supervisor.review([_CANDIDATE_A], [], sim_time=90.0, client=client)

    assert set(verdicts) == {"B1"}


async def test_review_falls_back_to_approved_for_every_candidate_on_llm_failure() -> None:
    client = _FakeClient(RuntimeError("boom"))
    supervisor = SupervisorAgent()

    verdicts, usage = await supervisor.review([_CANDIDATE_A, _CANDIDATE_B], [], sim_time=90.0, client=client)

    assert verdicts["B1"].decision == "approved"
    assert verdicts["C1"].decision == "approved"
    assert usage.status == "error"


def test_corridor_section_shows_the_link_between_two_candidates() -> None:
    links = {"B1": {"C1": NeighborLink(neighbor_id="C1", distance_m=179.2, travel_time_s=12.9)}}

    user = _build_user_prompt([_CANDIDATE_A, _CANDIDATE_B], [], sim_time=90.0, links=links)

    assert "B1 <-> C1" in user
    assert "distance_m=179" in user
    assert "free_flow_travel_time_s=13" in user


def test_corridor_section_omits_a_link_to_a_non_candidate_junction() -> None:
    # D1 isn't a candidate this cycle -- its link to B1 must not be rendered
    # even though `links` carries it (real neighbor_links() includes every
    # neighbor, not just today's congested ones).
    links = {
        "B1": {
            "C1": NeighborLink(neighbor_id="C1", distance_m=179.2, travel_time_s=12.9),
            "D1": NeighborLink(neighbor_id="D1", distance_m=200.0, travel_time_s=15.0),
        }
    }

    user = _build_user_prompt([_CANDIDATE_A, _CANDIDATE_B], [], sim_time=90.0, links=links)

    assert "D1" not in user


def test_corridor_section_says_so_explicitly_when_no_candidates_are_linked() -> None:
    user = _build_user_prompt([_CANDIDATE_A, _CANDIDATE_B], [], sim_time=90.0, links={})

    assert "none -- no two candidates" in user


async def test_review_passes_cache_key_and_schema_through_to_ask() -> None:
    reply = [Verdict(junction_id="B1", decision="approved", reason="OK.")]
    client = _FakeClient(_fake_response(reply))
    supervisor = SupervisorAgent()

    await supervisor.review([_CANDIDATE_A], [], sim_time=90.0, client=client)

    (call,) = client.responses.calls
    assert call["prompt_cache_key"] == "supervisor:v2"
    assert call["model"] == "gpt-5.6-terra"
    assert call["reasoning"] == {"effort": "medium"}


async def test_review_passes_links_through_to_the_prompt() -> None:
    reply = [
        Verdict(junction_id="B1", decision="approved", reason="OK."),
        Verdict(junction_id="C1", decision="approved", reason="OK."),
    ]
    client = _FakeClient(_fake_response(reply))
    supervisor = SupervisorAgent()
    links = {"B1": {"C1": NeighborLink(neighbor_id="C1", distance_m=179.2, travel_time_s=12.9)}}

    await supervisor.review([_CANDIDATE_A, _CANDIDATE_B], [], sim_time=90.0, links=links, client=client)

    (call,) = client.responses.calls
    assert "B1 <-> C1" in call["input"][1]["content"]
