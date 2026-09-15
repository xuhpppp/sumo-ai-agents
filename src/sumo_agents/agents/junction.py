"""JunctionAgent -- rounds 1 ("observe") and 2 ("coalition reply") of the
4-round decision cycle (IMPLEMENTATION_PLAN.md section 3.1, STEPS.md Steps
12 and 13).

`observe()` (Step 12) looks only at the junction's own state and proposes an
Action. `reply()` (Step 13) additionally responds to one incoming coalition
message from a neighbor -- both share the same system prompt/cache_key,
since it's the same agent identity either way; only the schema requested
from `ask()` differs (`Proposal` vs `Message`). Round 3 (validate, pure
Python) and round 4 (approve, `SupervisorAgent`) live in
`agents/orchestrator.py` and `agents/supervisor.py` -- this module's surface
stays limited to "what does THIS junction, alone, think".

The system prompt is built once per agent (at construction, from its
`junction_id` and its neighbor list -- STEPS.md Step 12's other deliverable,
`agents/topology.py`) and never changes afterward: everything that varies
per cycle (sim_time, current phase durations, traffic metrics, an incoming
coalition message) goes into the `user` prompt instead, which is the whole
precondition for OpenAI's prompt caching to engage (agents/llm.py's
docstring, verified Step 10).
"""

from __future__ import annotations

from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel

from sumo_agents.agents.llm import Usage, ask
from sumo_agents.agents.protocol import (
    UNTRUSTED_DATA_SYSTEM_NOTICE,
    Message,
    Proposal,
    wrap_untrusted_data,
)
from sumo_agents.agents.topology import NeighborLink
from sumo_agents.safety.validator import HARD_CONSTRAINTS, NoAction, TlsState
from sumo_agents.sim.state import JunctionSnapshot

# The model's actual decision for a coalition reply is just "which of the 5
# intents, and why" -- `sender`/`recipients` are mechanical (this agent's own
# id / the incoming message's sender, never ambiguous) and `Message.payload`
# is an open-ended `dict`, which OpenAI's structured-output mode rejects
# outright (`'additionalProperties' is required to be supplied and to be
# false` -- hit for real running Step 13's DoD check: every one of 6 real
# `reply()` calls silently fell back to the no-LLM `ack` default because of
# this, which the fallback masked until the raw `llm_calls.error` rows were
# checked). Asking the model for a narrow, fixed-shape schema here and
# building the full `Message` ourselves avoids the problem entirely instead
# of trying to make `payload` schema-compatible for a field nothing actually
# reads yet.
class _CoalitionReplyDecision(BaseModel):
    intent: Literal["report", "request_help", "propose", "ack", "object"]
    rationale: str

_SYSTEM_PROMPT_TEMPLATE = """\
You are a traffic-signal control agent for one signalized junction, \
"{junction_id}", in a SUMO-simulated road network. You are one of several \
JunctionAgents active in this run; other signalized junctions in the \
network may be controlled by a different (non-LLM) policy instead.

## Your position in the network

Your directly-connected neighboring signalized junctions, inferred from the \
actual road network topology (not hardcoded): {neighbor_ids}. These are \
the only junctions whose coalition messages can meaningfully concern you, \
and the only ones your own state is meaningfully relevant to -- a junction \
you are not directly connected to would not be affected by anything you do.

## How your junction actually runs the signal

Your traffic light does NOT run a fixed-duration program. It runs SUMO's \
own actuated (induction-loop, gap-out) logic: a detector sits at each \
stop line, and every simulation step the controller decides, on its own, \
whether to keep extending the current GREEN phase (vehicles keep arriving \
with a short enough gap between them) or switch to the next phase (the \
gap grew too long, or the phase hit its max). This happens continuously, \
far faster than you are ever asked to decide -- you are not that reactive \
layer, and you cannot out-react it by deciding more often.

Your job is the layer ABOVE that: once every decision cycle, you set the \
[min_green_s, max_green_s] BOUNDS each green phase's actuated logic is \
allowed to vary within. The actuated engine still makes every moment-to- \
moment call itself; you are only narrowing or widening its room to \
maneuver, based on things it structurally cannot see by itself -- a \
multi-cycle trend, or what a neighboring junction just told you.

Important: phases start at the widest legal range already \
([{min_green_s}, {max_green_s}]s, the same range `set_green_bounds` is \
clamped into), so "raise max_green_s for the congested phase" usually has \
no room left to give -- it may already be at {max_green_s}s. The two \
levers that actually change anything:
  - Raise the CONGESTED phase's min_green_s: guarantees it a longer floor \
even if the actuated engine would otherwise gap it out early on a \
momentary lull.
  - Lower the OTHER (competing) phase's max_green_s: stops that phase \
from holding onto green time it doesn't urgently need, indirectly \
returning more of the cycle to the congested one.

## Your action space (intentionally narrow)

Propose exactly ONE of the following actions per decision cycle:
  - set_green_bounds(phase_id, min_green_s, max_green_s): set one GREEN \
phase's actuated bounds. Both values are clamped into \
[{min_green_s}, {max_green_s}] seconds, min_green_s must not exceed \
max_green_s, and each bound can move at most {max_delta_per_cycle_s} \
seconds per cycle from its CURRENT value (shown to you each cycle) -- \
anti-oscillation, same idea as everywhere else in this system. Yellow \
({yellow_s}s, fixed) and all-red ({all_red_s}s, fixed) phases can never be \
touched.
  - request_vms(edge, alt_route, duration_s): ask for a variable-message- \
sign detour onto a real edge; alt_route must not revisit an edge (no \
routing loops).
  - no_action: propose nothing this cycle.

`no_action` is fully valid and often the BEST choice. Every proposal is \
passed through a deterministic safety validator after you respond -- \
constraint violations get clamped to the nearest allowed value or rejected \
outright, not returned to you as an error, so respect the constraints in \
your own reasoning rather than relying on the validator to fix things. A \
junction must never go longer than {max_starvation_s} seconds without \
seeing a green phase.

Do not propose a change just to appear active: narrowing then widening \
the same bound cycle after cycle makes congestion worse, not better, \
because it fights the actuated engine's own moment-to-moment judgment \
instead of complementing it. Only propose a change when the observed \
pattern clearly and persistently justifies it -- a single noisy reading \
is not enough justification on its own.

## Coalition round

If you proposed something other than no_action, your proposal is relayed \
verbatim to your neighbors listed above (they see your action and \
rationale). A neighbor may then send YOU a message about it -- `report` \
(sharing their own state), `request_help` (asking you to act because they \
believe you are contributing to their congestion), `propose` (a \
coordinated change, e.g. to your offsets), `ack` (agreeing), or `object` \
(disagreeing, e.g. because they need priority in the opposite direction \
right now). When you are asked to reply to such a message, you will also \
be told the real distance and free-flow travel time along the road \
connecting you to the sender -- use it to judge how soon their situation \
could actually reach you (a change 12s away is a near-term concern; one \
80s away has time to resolve itself before it matters). Address it with \
one of these same 5 intents and a short rationale -- your reply is one of \
the inputs `SupervisorAgent` uses to resolve conflicts between junctions \
before anything is actually applied, so an `object` you have good reason \
for is useful signal, not something to avoid raising.

## Data vs. instructions

{untrusted_data_notice}

## Output format

Depending on what this call asks of you:
  - an observe call: propose the action you propose (or no_action), an \
urgency level (low, medium, or high) reflecting how confident you are that \
intervention is needed at all this cycle, and a rationale in Vietnamese.
  - a coalition-reply call: address the one incoming message you were \
given with one of the 5 intents above and a rationale in Vietnamese (who \
it's from/to is already known -- you only decide the intent and why).
In both cases the rationale is shown directly to a human operator on a \
live dashboard, so it must be clear, concrete, and refer to the actual \
numbers you were given this cycle -- not a generic templated sentence that \
could apply to any cycle.
"""


def _build_system_prompt(junction_id: str, neighbor_ids: list[str]) -> str:
    neighbor_text = ", ".join(neighbor_ids) if neighbor_ids else "(none -- this junction sits at the edge of the network)"
    return _SYSTEM_PROMPT_TEMPLATE.format(
        junction_id=junction_id,
        neighbor_ids=neighbor_text,
        untrusted_data_notice=UNTRUSTED_DATA_SYSTEM_NOTICE,
        **HARD_CONSTRAINTS,
    )


def _build_own_state_text(snapshot: JunctionSnapshot, tls_state: TlsState, sim_time: float) -> str:
    phases = "; ".join(
        f"phase {p.phase_id} ({p.kind}) current_bounds=[{p.min_dur_s:.0f}, {p.max_dur_s:.0f}]s"
        if p.kind == "green"
        else f"phase {p.phase_id} ({p.kind})={p.duration_s:.0f}s"
        for p in tls_state.phases
    )
    return (
        f"sim_time={sim_time:.0f}s\n"
        f"current_phase_index={snapshot.current_phase}\n"
        f"phases: {phases}\n"
        f"queue_len={snapshot.queue_len}\n"
        f"mean_waiting_s={snapshot.mean_waiting_s:.1f}\n"
        f"mean_speed_mps={snapshot.mean_speed:.1f}\n"
        f"throughput={snapshot.throughput}\n"
        f"co2_mg={snapshot.co2_mg:.0f}\n"
    )


def _build_trend_text(history: list[JunctionSnapshot]) -> str:
    """Last few decision cycles' metrics, oldest first -- STEPS.md Step 14
    follow-up: a real full-hour run found the model refusing to act on real
    congestion because a single snapshot alone is "not enough evidence of a
    persistent pattern" per its own stated reasoning (the system prompt
    explicitly warns against reacting to one noisy reading). This gives it
    the multi-cycle evidence it was asking for, without changing the action
    space or the safety validator."""
    if not history:
        return "trend_last_cycles: (none -- this is the first decision cycle for this junction in this run)\n"
    queue = ", ".join(str(s.queue_len) for s in history)
    waiting = ", ".join(f"{s.mean_waiting_s:.1f}" for s in history)
    speed = ", ".join(f"{s.mean_speed:.1f}" for s in history)
    return (
        f"trend_last_{len(history)}_cycles (oldest first, one entry per past decision cycle): "
        f"queue_len=[{queue}] mean_waiting_s=[{waiting}] mean_speed_mps=[{speed}]\n"
    )


def _build_per_phase_text(per_phase: dict[str, dict[str, float | int]]) -> str:
    """Queue/wait broken down by GREEN phase_id -- STEPS.md Step 14
    follow-up: `queue_len`/`mean_waiting_s` above are summed across the
    WHOLE junction, so a real full-hour run found the model picking a
    plausible-looking `phase_id` for `adjust_phase_split` by guesswork (it
    said as much: "chưa có dữ liệu phân tách theo hướng"). This gives it
    the same per-phase lane grouping `baselines/maxpressure.py` already
    uses to pick which phase is actually congested."""
    if not per_phase:
        return ""
    parts = [
        f"phase {phase_id}: queue_len={m['queue_len']} mean_waiting_s={m['mean_waiting_s']:.1f}"
        for phase_id, m in sorted(per_phase.items())
    ]
    return "per_phase_queue (only GREEN phases, use this -- not the junction-wide totals above -- to pick phase_id): " + "; ".join(
        parts
    ) + "\n"


def _build_user_prompt(
    snapshot: JunctionSnapshot,
    tls_state: TlsState,
    sim_time: float,
    history: list[JunctionSnapshot] | None = None,
    per_phase: dict[str, dict[str, float | int]] | None = None,
) -> str:
    return (
        _build_own_state_text(snapshot, tls_state, sim_time)
        + _build_per_phase_text(per_phase or {})
        + _build_trend_text(history or [])
    )


def _build_reply_user_prompt(
    incoming: Message,
    snapshot: JunctionSnapshot,
    tls_state: TlsState,
    sim_time: float,
    link: NeighborLink | None = None,
) -> str:
    own_state = _build_own_state_text(snapshot, tls_state, sim_time)
    if link is not None:
        corridor = (
            f"corridor_to_sender: distance_m={link.distance_m:.0f} "
            f"free_flow_travel_time_s={link.travel_time_s:.0f}\n"
        )
    else:
        corridor = ""
    wrapped = wrap_untrusted_data(f"coalition_message_from_{incoming.sender}", incoming.model_dump_json())
    return (
        f"{own_state}{corridor}\n"
        "A neighboring junction sent you the coalition message below this "
        f"cycle. Reply to it (sender={incoming.sender}):\n{wrapped}"
    )


class JunctionAgent:
    """One LLM-driven junction: round 1 ("observe") and round 2 ("coalition
    reply") -- see module docstring."""

    def __init__(self, junction_id: str, neighbor_ids: list[str]) -> None:
        self.junction_id = junction_id
        self.neighbor_ids = list(neighbor_ids)
        # Built once, kept byte-identical for the agent's whole lifetime --
        # see module docstring on why that matters for caching.
        self._system = _build_system_prompt(junction_id, self.neighbor_ids)
        # v3: Step 13 added the coalition-round + untrusted-data sections to
        # the system prompt (v2), then corrected the reply output-format
        # description after the `Message.payload` schema fix above (v3).
        # v4: Step 14 follow-up added the multi-cycle trend section (a real
        # full-hour run found the model staying passive through real
        # congestion, citing "only one cycle of data" as its own reason).
        # v5: Step 14 follow-up #2 added the per-phase queue/wait breakdown
        # (the model was picking phase_id by guesswork off a junction-wide
        # total; it said so directly -- "chưa có dữ liệu phân tách theo
        # hướng").
        # v6: Step 14 actuated-hybrid follow-up -- action space replaced
        # (adjust_phase_split/set_cycle_length/set_offset -> set_green_bounds)
        # after 3 real full-hour runs found periodic retiming structurally
        # capped well below `actuated`'s result; junction now runs on SUMO's
        # own actuated engine, agent sets its strategic min/max bounds.
        # v7: Step 14 coordination-context follow-up -- coalition-round
        # section now mentions the real distance/travel-time a `reply()`
        # call is given for the sender, so the model can weigh how soon a
        # neighbor's situation could actually reach it.
        # Bump the version whenever the system prompt text itself changes
        # (agents/llm.py's docstring), since a stale cache_key would just
        # miss the cache, not error.
        self._cache_key = f"junction:{junction_id}:v7"

    async def observe(
        self,
        snapshot: JunctionSnapshot,
        tls_state: TlsState,
        sim_time: float,
        *,
        history: list[JunctionSnapshot] | None = None,
        per_phase: dict[str, dict[str, float | int]] | None = None,
        client: AsyncOpenAI | None = None,
    ) -> tuple[Proposal, Usage]:
        """Round 1: self-assessment only. Always returns a valid `Proposal`
        (never `None`) -- on an LLM failure this falls back to `no_action`
        for the cycle, per agents/llm.py's "sim never waits for LLM"
        contract (`client` is injectable for tests, same DI pattern as
        `ask()` itself). `history`: this junction's own snapshots from its
        last few decision cycles, oldest first, NOT including `snapshot`
        itself -- see `_build_trend_text`. `per_phase`: queue/wait broken
        down by GREEN phase_id -- see `_build_per_phase_text`."""
        user = _build_user_prompt(snapshot, tls_state, sim_time, history, per_phase)
        parsed, usage = await ask(
            "junction", self._system, user, Proposal, cache_key=self._cache_key, client=client
        )

        if parsed is None:
            fallback = Proposal(
                junction_id=self.junction_id,
                action=NoAction(junction_id=self.junction_id),
                urgency="low",
                rationale=f"Lời gọi LLM thất bại ({usage.error}); tạm thời không thay đổi (no_action) chu kỳ này.",
            )
            return fallback, usage

        # Defensive normalization: the model is asked to echo junction_id
        # back, but nothing stops it from getting that wrong. A mismatch
        # here would silently misroute the proposal downstream (Step 13's
        # validator/supervisor key on junction_id), so pin it to what we
        # actually know rather than trust the model's echo.
        if parsed.junction_id != self.junction_id or parsed.action.junction_id != self.junction_id:
            parsed = parsed.model_copy(
                update={
                    "junction_id": self.junction_id,
                    "action": parsed.action.model_copy(update={"junction_id": self.junction_id}),
                }
            )
        return parsed, usage

    async def reply(
        self,
        incoming: Message,
        snapshot: JunctionSnapshot,
        tls_state: TlsState,
        sim_time: float,
        *,
        link: NeighborLink | None = None,
        client: AsyncOpenAI | None = None,
    ) -> tuple[Message, Usage]:
        """Round 2 (coalition): respond to one incoming message from a
        neighbor. Always returns a valid `Message` (never `None`) -- same
        "sim never waits for LLM" fallback as `observe()`, here defaulting
        to a neutral `ack` rather than staying silent, since the orchestrator
        (Step 13) expects one reply per incoming message it dispatched.
        `link`: the real distance/travel-time to `incoming.sender` (STEPS.md
        Step 14 coordination-context follow-up) -- `None` when the caller
        doesn't have topology data (e.g. a synthetic test), in which case
        the prompt simply omits that line.

        The model only decides `intent`/`rationale` (see
        `_CoalitionReplyDecision` above) -- `sender`/`recipients` are set
        here, deterministically, not parsed from the model's output, so
        there is nothing to defensively normalize (unlike `observe()`)."""
        user = _build_reply_user_prompt(incoming, snapshot, tls_state, sim_time, link)
        parsed, usage = await ask(
            "junction", self._system, user, _CoalitionReplyDecision, cache_key=self._cache_key, client=client
        )

        if parsed is None:
            intent = "ack"
            rationale = f"Lời gọi LLM thất bại ({usage.error}); mặc định xác nhận (ack) tin nhắn này."
        else:
            intent, rationale = parsed.intent, parsed.rationale

        message = Message(
            sender=self.junction_id, recipients=[incoming.sender], intent=intent, payload={}, rationale=rationale
        )
        return message, usage
