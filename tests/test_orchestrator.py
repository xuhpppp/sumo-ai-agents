"""Unit tests for agents/orchestrator.py's run_decision_cycle (STEPS.md
Step 13) -- rounds 2 (coalition), 3 (validate), 4 (approve).

Runs against the in-memory SQLite `store` fixture (conftest.py, same as
tests/test_models.py) for real `messages`/`decisions` persistence, but uses
lightweight fake stand-ins for JunctionAgent.reply()/SupervisorAgent.review()
instead of real LLM calls -- those two methods' own logic is already covered
by tests/test_junction.py and tests/test_supervisor.py; this file tests
orchestrator.py's OWN logic: who gets to broadcast, who replies to whom, the
validator gate, and how verdicts turn into `final_action`.
"""

from __future__ import annotations

from sqlalchemy import select

from sumo_agents.agents.llm import Usage
from sumo_agents.agents.orchestrator import run_decision_cycle
from sumo_agents.agents.protocol import Message, Proposal, Verdict
from sumo_agents.obs.models import Decision
from sumo_agents.obs.models import Message as MessageRow
from sumo_agents.obs.store import Store
from sumo_agents.safety.validator import AdjustPhaseSplit, NoAction, PhaseState, TlsState

_TLS = TlsState(
    junction_id="_",
    phases=(
        PhaseState(phase_id="0", duration_s=42.0, kind="green"),
        PhaseState(phase_id="1", duration_s=3.0, kind="yellow"),
        PhaseState(phase_id="2", duration_s=42.0, kind="green"),
        PhaseState(phase_id="3", duration_s=3.0, kind="yellow"),
    ),
)


class _FakeSnapshot:
    """Only the fields _build_own_state_text/_run_coalition_round touch."""

    current_phase = 0
    queue_len = 10
    mean_waiting_s = 8.0
    mean_speed = 5.0
    throughput = 6
    co2_mg = 1000.0


class _FakeAgent:
    """Stand-in for JunctionAgent -- always acks whatever it's sent, and
    records what it was asked to reply to (JunctionAgent.reply()'s own
    behavior is covered by tests/test_junction.py, not re-tested here)."""

    def __init__(self, junction_id: str) -> None:
        self.junction_id = junction_id
        self.reply_calls: list[Message] = []

    async def reply(self, incoming, snapshot, tls_state, sim_time, *, client=None):
        self.reply_calls.append(incoming)
        reply = Message(
            sender=self.junction_id,
            recipients=[incoming.sender],
            intent="ack",
            payload={},
            rationale=f"{self.junction_id} xác nhận đã nhận thông tin từ {incoming.sender}.",
        )
        usage = Usage(model="gpt-5.6-luna", effort="low", status="ok", cached_tokens=100, cost_usd=None)
        return reply, usage


class _FakeSupervisor:
    """Approves everything it's asked to review -- the interesting verdict
    logic (modified/denied, missing/unknown junction_ids) is already covered
    by tests/test_supervisor.py."""

    def __init__(self) -> None:
        self.seen_candidates: list[str] = []
        self.seen_messages: list[Message] = []

    async def review(self, candidates, messages, *, sim_time, client=None):
        self.seen_candidates = [c.junction_id for c in candidates]
        self.seen_messages = list(messages)
        if not candidates:
            return {}, None
        verdicts = {c.junction_id: Verdict(junction_id=c.junction_id, decision="approved", reason="OK.") for c in candidates}
        usage = Usage(model="gpt-5.6-terra", effort="medium", status="ok", cached_tokens=50, cost_usd=None)
        return verdicts, usage


def _proposals() -> dict[str, Proposal]:
    return {
        "J1": Proposal(
            junction_id="J1",
            action=AdjustPhaseSplit(junction_id="J1", phase_id="0", delta_s=10.0),  # valid, congested
            urgency="high",
            rationale="Hàng đợi J1 tăng nhanh.",
        ),
        "J2": Proposal(
            junction_id="J2",
            action=NoAction(junction_id="J2"),  # uncongested
            urgency="low",
            rationale="J2 thông thoáng.",
        ),
        "J3": Proposal(
            junction_id="J3",
            # Deliberately invalid: phase_id "1" is yellow (see _TLS) --
            # validator.py must reject this before it ever reaches the
            # supervisor. This is the Step 13 DoD's required case.
            action=AdjustPhaseSplit(junction_id="J3", phase_id="1", delta_s=5.0),
            urgency="high",
            rationale="J3 cố ý vi phạm để test validator.",
        ),
    }


async def _run(store: Store) -> tuple[list, _FakeSupervisor, dict[str, _FakeAgent]]:
    run_id = await store.create_run(scenario="_test", seed=0, mode="llm", config={})
    proposals = _proposals()
    neighbor_map = {"J1": ["J2", "J3"], "J2": ["J1"], "J3": ["J1"]}
    snapshots = {jid: _FakeSnapshot() for jid in proposals}
    tls_states = {jid: TlsState(junction_id=jid, phases=_TLS.phases) for jid in proposals}
    agents = {jid: _FakeAgent(jid) for jid in proposals}
    supervisor = _FakeSupervisor()

    decisions = await run_decision_cycle(
        proposals,
        agents,
        neighbor_map,
        snapshots,
        tls_states,
        supervisor,
        sim_time=90.0,
        cycle_id=1,
        run_id=run_id,
        store=store,
    )
    return decisions, supervisor, agents, run_id


async def test_invalid_action_is_rejected_before_reaching_the_supervisor(store: Store) -> None:
    decisions, supervisor, _agents, _run_id = await _run(store)

    by_id = {d.junction_id: d for d in decisions}
    assert by_id["J3"].validator_status == "rejected"
    assert by_id["J3"].supervisor_verdict is None
    assert by_id["J3"].final_action is None
    assert "J3" not in supervisor.seen_candidates  # never handed to the supervisor at all


async def test_valid_proposals_get_a_supervisor_verdict_and_final_action(store: Store) -> None:
    decisions, _supervisor, _agents, _run_id = await _run(store)

    by_id = {d.junction_id: d for d in decisions}
    for jid in ("J1", "J2"):
        assert by_id[jid].validator_status in ("ok", "clamped")
        assert by_id[jid].supervisor_verdict == "approved"
        assert by_id[jid].final_action is not None


async def test_only_congested_junctions_broadcast_to_active_neighbors(store: Store) -> None:
    # J1 and J3 proposed a real action (congested); J2 proposed no_action and
    # must stay silent -- this is the plan's cost-cutting mechanism.
    _decisions, _supervisor, agents, _run_id = await _run(store)

    # J2 never broadcasts (uncongested), so it only ever appears as a
    # *recipient* -- it must have been asked to reply to J1's broadcast.
    assert len(agents["J2"].reply_calls) == 1
    assert agents["J2"].reply_calls[0].sender == "J1"

    # J1 and J3 are neighbors of each other and both congested, so each
    # replies to the other's broadcast.
    assert len(agents["J1"].reply_calls) == 1
    assert agents["J1"].reply_calls[0].sender == "J3"
    assert len(agents["J3"].reply_calls) == 1
    assert agents["J3"].reply_calls[0].sender == "J1"


async def test_messages_and_decisions_are_persisted(store: Store, session_factory) -> None:
    decisions, _supervisor, _agents, run_id = await _run(store)

    async with session_factory() as session:
        messages = (await session.execute(select(MessageRow).where(MessageRow.run_id == run_id))).scalars().all()
        decision_rows = (await session.execute(select(Decision).where(Decision.run_id == run_id))).scalars().all()

    # 2 broadcasts (J1 -> [J2,J3], J3 -> [J1]) + 3 replies (J2->J1, J3->J1, J1->J3).
    assert len(messages) == 5
    assert len(decision_rows) == 3 == len(decisions)
    assert all(row.round == 2 for row in messages)

    rejected_row = next(r for r in decision_rows if r.junction_id == "J3")
    assert rejected_row.validator_status == "rejected"
    assert rejected_row.supervisor_verdict is None
    assert rejected_row.applied is False  # Step 14 will flip this once actually applied
