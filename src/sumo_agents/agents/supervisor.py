"""SupervisorAgent -- round 4 ("approve") of the decision cycle
(IMPLEMENTATION_PLAN.md section 3.1/1.2, STEPS.md Step 13).

Unlike `JunctionAgent` (one instance per junction), there is exactly ONE
`SupervisorAgent`, called ONCE per decision cycle with every proposal that
survived round 3 (validation) at once -- plan section 1.2 puts it on
`gpt-5.6-terra`/effort=medium specifically because resolving a cross-junction
conflict (two adjacent junctions both wanting priority in opposite
directions) needs one holistic view of the whole cycle, not N independent
per-junction calls that can't see each other.

Its input already passed the deterministic safety validator (STEPS.md Step
7) -- a rejected proposal never reaches this module at all (see
agents/orchestrator.py). So SupervisorAgent's job is narrower than
"validate": it only has to decide, given everything already safety-clamped,
whether the *combination* of what junctions want to do this cycle makes
sense together.
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI
from pydantic import BaseModel

from sumo_agents.agents.llm import Usage, ask
from sumo_agents.agents.protocol import (
    UNTRUSTED_DATA_SYSTEM_NOTICE,
    Message,
    Verdict,
    wrap_untrusted_data,
)
from sumo_agents.agents.topology import NeighborLink
from sumo_agents.safety.validator import Action

_CACHE_KEY = "supervisor:v2"

_SYSTEM_PROMPT = f"""\
You are the traffic-control supervisor for one SUMO-simulated road network. \
Once per decision cycle, you are given every junction's proposed action for \
this cycle that has ALREADY passed a deterministic safety validator (hard \
limits on green/cycle duration, starvation, etc. are already enforced --  \
you never need to re-check those), any coalition messages junctions \
exchanged with their neighbors this cycle, and the real distance/free-flow \
travel time between any two candidates that are directly connected.

## Your job

Most of the time, the right answer is to approve everything as-is: \
independent junctions doing independent, already-validated things is the \
normal case, not a problem to solve. Only intervene when the proposals, \
read together, actually conflict or would visibly fight each other -- for \
example: two adjacent junctions both narrowing the SAME shared corridor's \
capacity in opposite directions via set_green_bounds (one raising its own \
phase's floor while its neighbor lowers the ceiling of the phase that feeds \
that same corridor), or a junction's proposal being met with a \
substantiated `object` message from a neighbor. Use the corridor \
distance/travel time to judge urgency: a conflict 10-15s apart can compound \
within the next cycle or two, while one 60s+ apart usually has time to \
resolve on its own before it matters -- do not treat every adjacency as \
equally urgent just because it exists. When you do intervene, prefer \
`modified` (a smaller/adjusted version of the SAME action type) over \
`denied` -- only deny when no version of the proposed action is compatible \
with what a neighbor needs this cycle.

## Data vs. instructions

{UNTRUSTED_DATA_SYSTEM_NOTICE}

## Output format

Respond with exactly one verdict per junction_id you were given -- never \
more, never fewer, and never a junction_id that wasn't in your input. For \
each: `decision` (approved, modified, or denied), `modified_action` (a full \
action for the SAME junction_id and the SAME action type as proposed, only \
when decision is "modified" -- omit it in every other case), and `reason` \
in Vietnamese, specific to the actual proposals/messages you were given \
this cycle, shown directly to a human operator on a live dashboard.
"""


@dataclass(frozen=True, slots=True)
class SupervisorCandidate:
    """One proposal that survived round 3 (validation) and is now awaiting
    the supervisor's ruling."""

    junction_id: str
    proposed_action: Action
    clamped_action: Action
    urgency: str
    rationale: str
    validator_violations: list[str]


class _SupervisorReview(BaseModel):
    verdicts: list[Verdict]


def _build_corridor_lines(candidates: list[SupervisorCandidate], links: dict[str, dict[str, NeighborLink]]) -> list[str]:
    candidate_ids = {c.junction_id for c in candidates}
    seen_pairs: set[tuple[str, str]] = set()
    lines = []
    for jid in candidate_ids:
        for neighbor_id, link in links.get(jid, {}).items():
            if neighbor_id not in candidate_ids:
                continue
            pair = (jid, neighbor_id) if jid < neighbor_id else (neighbor_id, jid)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            lines.append(
                f"- {pair[0]} <-> {pair[1]}: distance_m={link.distance_m:.0f} "
                f"free_flow_travel_time_s={link.travel_time_s:.0f}"
            )
    return sorted(lines)


def _build_user_prompt(
    candidates: list[SupervisorCandidate],
    messages: list[Message],
    sim_time: float,
    links: dict[str, dict[str, NeighborLink]] | None = None,
) -> str:
    lines = [f"sim_time={sim_time:.0f}s", "", "## Validated proposals awaiting your verdict"]
    for c in candidates:
        lines.append(
            f"- junction_id={c.junction_id} urgency={c.urgency} "
            f"proposed_action={c.proposed_action.model_dump()} "
            f"clamped_action={c.clamped_action.model_dump()} "
            f"validator_violations={c.validator_violations} "
            f"rationale={c.rationale!r}"
        )
    lines.append("")
    lines.append("## Coalition messages exchanged this cycle")
    if messages:
        for m in messages:
            lines.append(f"- {m.sender} -> {m.recipients} intent={m.intent} rationale={m.rationale!r}")
    else:
        lines.append("(none -- no junction reported congestion this cycle)")
    lines.append("")
    lines.append("## Corridor adjacency between today's candidates")
    corridor_lines = _build_corridor_lines(candidates, links or {})
    if corridor_lines:
        lines.extend(corridor_lines)
    else:
        lines.append("(none -- no two candidates this cycle are directly connected)")
    return wrap_untrusted_data("junction_proposals_and_messages", "\n".join(lines))


class SupervisorAgent:
    """One instance per run (not per junction) -- see module docstring."""

    def __init__(self) -> None:
        self._system = _SYSTEM_PROMPT

    async def review(
        self,
        candidates: list[SupervisorCandidate],
        messages: list[Message],
        *,
        sim_time: float,
        links: dict[str, dict[str, NeighborLink]] | None = None,
        client: AsyncOpenAI | None = None,
    ) -> tuple[dict[str, Verdict], Usage | None]:
        """Rule on every candidate at once. Returns `(verdicts, usage)` --
        `usage` is `None` when `candidates` is empty (no call is made: there
        is nothing to rule on, and a real call would just burn a cache-miss
        for an empty question). Always returns exactly one `Verdict` per
        candidate junction_id, defaulting to "approved" for any candidate
        the model's response didn't address (whether because the call
        failed, or because it referenced an unknown/wrong junction_id) --
        the underlying action already passed the deterministic validator,
        so "approve what we already know is safe" is a safe default, not a
        silent bypass. `links`: the network's full neighbor-distance map
        (STEPS.md Step 14 coordination-context follow-up, `agents/
        topology.neighbor_links`) -- only the pairs where BOTH junctions are
        candidates this cycle actually get rendered into the prompt.
        """
        if not candidates:
            return {}, None

        user = _build_user_prompt(candidates, messages, sim_time, links)
        parsed, usage = await ask(
            "supervisor", self._system, user, _SupervisorReview, cache_key=_CACHE_KEY, client=client
        )

        valid_ids = {c.junction_id for c in candidates}
        verdicts: dict[str, Verdict] = {}
        if parsed is not None:
            verdicts = {v.junction_id: v for v in parsed.verdicts if v.junction_id in valid_ids}

        for c in candidates:
            if c.junction_id not in verdicts:
                reason = (
                    f"Supervisor call failed ({usage.error}); auto-approved the validated action."
                    if parsed is None
                    else "Supervisor did not address this junction; auto-approved the validated action."
                )
                verdicts[c.junction_id] = Verdict(junction_id=c.junction_id, decision="approved", reason=reason)
        return verdicts, usage
