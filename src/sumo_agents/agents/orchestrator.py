"""Rounds 2-4 of one decision cycle: coalition, validate, approve
(IMPLEMENTATION_PLAN.md section 3.1/3.2, STEPS.md Step 13).

Round 1 ("observe") is `JunctionAgent.observe()` (Step 12) -- this module
takes its output (`proposals`) as INPUT rather than re-running it, for two
reasons: (1) it keeps round 1 (per-junction, independent) cleanly separate
from rounds 2-4 (which need the whole cycle's proposals together), and (2)
it lets a caller -- a live `SimRunner` cycle in Step 14, or the Step 13 DoD
script -- inject synthetic/adversarial proposals directly, which is exactly
what the DoD's "inject a deliberately-violating action" check needs: there
is no reliable way to make a real LLM call return an invalid action on
demand, so the verification script constructs one by hand.

`run_decision_cycle()` writes `messages` and `decisions` rows itself (the
plan's "ghi messages + decisions đầy đủ, mọi vòng") -- unlike `ask()`, which
deliberately never touches the DB (agents/llm.py), this module sits at the
orchestration layer, which is exactly where plan section 6.1's row-per-event
writes are supposed to happen (see obs/store.py's docstring).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from openai import AsyncOpenAI

from sumo_agents.agents.junction import JunctionAgent
from sumo_agents.agents.llm import Usage
from sumo_agents.agents.protocol import Message, Proposal
from sumo_agents.agents.supervisor import SupervisorAgent, SupervisorCandidate
from sumo_agents.obs.store import Store
from sumo_agents.safety.validator import Action, TlsState, validate
from sumo_agents.sim.state import JunctionSnapshot

# Coalition messages count as round 2 in the `messages` table (round 1,
# "observe", produces Proposals, not Messages -- there is nothing to log in
# that table for it). See obs/models.py's Message docstring.
_COALITION_ROUND = 2


@dataclass(frozen=True, slots=True)
class CycleDecision:
    """One junction's outcome for one decision cycle -- mirrors one row of
    the `decisions` table (obs/models.py), plus `final_action`: what Step 14
    should actually push to TraCI (`None` if rejected or denied)."""

    junction_id: str
    proposed_action: Action
    validator_status: str  # ok | clamped | rejected
    validator_violations: list[str]
    supervisor_verdict: str | None  # approved | modified | denied | None (rejected before reaching supervisor)
    supervisor_reason: str | None
    final_action: Action | None


async def run_decision_cycle(
    proposals: dict[str, Proposal],
    agents: dict[str, JunctionAgent],
    neighbor_map: dict[str, list[str]],
    snapshots: dict[str, JunctionSnapshot],
    tls_states: dict[str, TlsState],
    supervisor: SupervisorAgent,
    *,
    sim_time: float,
    cycle_id: int,
    run_id: uuid.UUID,
    store: Store,
    client: AsyncOpenAI | None = None,
) -> list[CycleDecision]:
    """Run rounds 2-4 for one decision cycle and persist every message and
    decision. `proposals`/`agents`/`snapshots`/`tls_states` must all be
    keyed by the same set of junction_ids (the active LLM-agent set for this
    run)."""
    messages, message_usages = await _run_coalition_round(
        proposals, agents, neighbor_map, snapshots, tls_states, sim_time=sim_time, client=client
    )
    for m in messages:
        await store.add_message(
            run_id=run_id,
            sim_time=sim_time,
            cycle_id=cycle_id,
            round=_COALITION_ROUND,
            sender=m.sender,
            recipients=m.recipients,
            intent=m.intent,
            payload=m.payload,
            rationale=m.rationale,
        )
    for agent_id, usage in message_usages:
        await _log_llm_call(store, run_id=run_id, sim_time=sim_time, agent_id=agent_id, role="junction", usage=usage)

    # Round 3 -- validate (deterministic, safety.validator.py, Step 7). A
    # rejected proposal is filtered out HERE, before the supervisor ever
    # sees it -- this is the DoD's "inject a violation -> rejected before
    # supervisor" requirement.
    decisions: list[CycleDecision] = []
    candidates: list[SupervisorCandidate] = []
    # junction_id -> (proposal, clamped_action, validator_violations)
    validated: dict[str, tuple[Proposal, Action, list[str]]] = {}
    for jid, proposal in proposals.items():
        result = validate(proposal.action, tls_states[jid])
        if not result.ok:
            decisions.append(
                CycleDecision(
                    junction_id=jid,
                    proposed_action=proposal.action,
                    validator_status="rejected",
                    validator_violations=result.violations,
                    supervisor_verdict=None,
                    supervisor_reason=None,
                    final_action=None,
                )
            )
            continue
        validated[jid] = (proposal, result.clamped_action, result.violations)
        candidates.append(
            SupervisorCandidate(
                junction_id=jid,
                proposed_action=proposal.action,
                clamped_action=result.clamped_action,
                urgency=proposal.urgency,
                rationale=proposal.rationale,
                validator_violations=result.violations,
            )
        )

    # Round 4 -- approve (SupervisorAgent, one call for the whole cycle).
    verdicts, supervisor_usage = await supervisor.review(candidates, messages, sim_time=sim_time, client=client)
    if supervisor_usage is not None:
        await _log_llm_call(
            store, run_id=run_id, sim_time=sim_time, agent_id="supervisor", role="supervisor", usage=supervisor_usage
        )

    for jid, (proposal, clamped_action, violations) in validated.items():
        verdict = verdicts[jid]
        final_action: Action | None = None
        if verdict.decision == "approved":
            final_action = clamped_action
        elif verdict.decision == "modified":
            # Defensive, same reasoning as JunctionAgent's own normalization
            # (agents/junction.py): pin junction_id to what we know is true
            # rather than trust the model's echo -- a wrong id here would
            # silently misroute which junction Step 14 actually applies to.
            final_action = verdict.modified_action.model_copy(update={"junction_id": jid})  # type: ignore[union-attr]
        # decision == "denied" -> final_action stays None.

        decisions.append(
            CycleDecision(
                junction_id=jid,
                proposed_action=proposal.action,
                validator_status="clamped" if violations else "ok",
                validator_violations=violations,
                supervisor_verdict=verdict.decision,
                supervisor_reason=verdict.reason,
                final_action=final_action,
            )
        )

    for d in decisions:
        await store.add_decision(
            run_id=run_id,
            sim_time=sim_time,
            cycle_id=cycle_id,
            junction_id=d.junction_id,
            action_type=d.proposed_action.type,
            params=d.proposed_action.model_dump(exclude={"type", "junction_id"}),
            validator_status=d.validator_status,
            validator_violations={"violations": d.validator_violations} if d.validator_violations else None,
            supervisor_verdict=d.supervisor_verdict,
            supervisor_reason=d.supervisor_reason,
            applied=False,  # Step 14 sets this once the action is actually pushed via TraCI
        )

    return decisions


async def _run_coalition_round(
    proposals: dict[str, Proposal],
    agents: dict[str, JunctionAgent],
    neighbor_map: dict[str, list[str]],
    snapshots: dict[str, JunctionSnapshot],
    tls_states: dict[str, TlsState],
    *,
    sim_time: float,
    client: AsyncOpenAI | None,
) -> tuple[list[Message], list[tuple[str, Usage]]]:
    """Round 2: only congested junctions (proposed something other than
    no_action) broadcast to their active-agent neighbors -- uncongested
    junctions stay silent, which is exactly the plan's "cắt 50-70% chi phí"
    cost-cutting mechanism (plan section 3.1's coalition round). The
    broadcast itself is NOT a fresh LLM call: it's the junction's own
    round-1 proposal (action + rationale) relayed as-is, since re-asking
    "what do you want to do" a second time would be redundant. Recipients DO
    each make one real LLM call (`JunctionAgent.reply()`) to decide how to
    respond -- this is the "at most 2 rounds" the plan allows: one
    deterministic relay, one real reply, no further back-and-forth."""
    congested = [jid for jid, p in proposals.items() if p.action.type != "no_action"]

    broadcasts: list[Message] = []
    for jid in congested:
        recipients = [n for n in neighbor_map.get(jid, []) if n in proposals and n != jid]
        if not recipients:
            continue
        proposal = proposals[jid]
        intent = "propose" if proposal.action.type == "set_offset" else "request_help"
        broadcasts.append(
            Message(
                sender=jid,
                recipients=recipients,
                intent=intent,
                payload=proposal.action.model_dump(),
                rationale=proposal.rationale,
            )
        )

    if not broadcasts:
        return [], []

    reply_targets: list[tuple[str, Message]] = [
        (recipient, broadcast) for broadcast in broadcasts for recipient in broadcast.recipients
    ]
    reply_results = await asyncio.gather(
        *(
            agents[recipient].reply(incoming, snapshots[recipient], tls_states[recipient], sim_time, client=client)
            for recipient, incoming in reply_targets
        )
    )
    replies = [message for message, _usage in reply_results]
    usages = [
        (recipient, usage) for (recipient, _incoming), (_message, usage) in zip(reply_targets, reply_results, strict=True)
    ]
    return broadcasts + replies, usages


async def _log_llm_call(store: Store, *, run_id: uuid.UUID, sim_time: float, agent_id: str, role: str, usage: Usage) -> None:
    await store.add_llm_call(
        run_id=run_id,
        sim_time=sim_time,
        agent_id=agent_id,
        role=role,
        model=usage.model,
        effort=usage.effort,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cached_tokens=usage.cached_tokens,
        latency_ms=usage.latency_ms,
        status=usage.status,
        error=usage.error,
        cost_usd=usage.cost_usd,
    )
