"""JunctionAgent -- round 1 of the 4-round decision cycle: "observe"
(IMPLEMENTATION_PLAN.md section 3.1, STEPS.md Step 12).

Each JunctionAgent looks only at its OWN junction's state this round and
proposes an Action (or `no_action`) via `agents/llm.py`'s single call site.
Rounds 2-4 (coalition, validate, approve -- STEPS.md Step 13) are not
implemented here; this module's public surface is deliberately just
`observe()`, matching the plan's "vòng 1: mỗi JunctionAgent tự đánh giá"
(each agent self-assesses, no inter-agent talk yet).

The system prompt is built once per agent (at construction, from its
`junction_id` and its neighbor list -- STEPS.md Step 12's other deliverable,
`agents/topology.py`) and never changes afterward: everything that varies
per cycle (sim_time, current phase durations, traffic metrics) goes into the
`user` prompt instead, which is the whole precondition for OpenAI's prompt
caching to engage (agents/llm.py's docstring, verified Step 10).
"""

from __future__ import annotations

from openai import AsyncOpenAI

from sumo_agents.agents.llm import Usage, ask
from sumo_agents.agents.protocol import Proposal
from sumo_agents.safety.validator import HARD_CONSTRAINTS, NoAction, TlsState
from sumo_agents.sim.state import JunctionSnapshot

_SYSTEM_PROMPT_TEMPLATE = """\
You are a traffic-signal control agent for one signalized junction, \
"{junction_id}", in a SUMO-simulated road network. You are one of several \
JunctionAgents active in this run; other signalized junctions in the \
network may be controlled by a different (non-LLM) policy instead.

## Your position in the network

Your directly-connected neighboring signalized junctions, inferred from the \
actual road network topology (not hardcoded): {neighbor_ids}. These are the \
only junctions a coordination action like `set_offset` can meaningfully \
target -- a junction you are not directly connected to would not be \
affected by anything you do.

## What you will be shown each cycle

In the user message (never here -- this system message must stay \
byte-identical across the whole run for prompt caching to work), you will \
be given: sim_time, the duration of every phase in your current signal \
program (so you know how much room you have before hitting the min/max \
green limits below), and traffic metrics aggregated over your controlled \
lanes (queue_len, mean_waiting_s, mean_speed, throughput, co2_mg).

## Your action space (intentionally narrow)

Propose exactly ONE of the following actions per decision cycle:
  - adjust_phase_split(phase_id, delta_s): change one GREEN phase's \
duration by delta_s seconds (positive = longer). abs(delta_s) must be \
<= {max_delta_per_cycle_s} seconds per cycle, and the resulting duration \
must stay within [{min_green_s}, {max_green_s}] seconds. Yellow \
({yellow_s}s, fixed) and all-red ({all_red_s}s, fixed) phases can never be \
adjusted.
  - set_cycle_length(cycle_s): change the total cycle length; must stay \
within [{min_cycle_s}, {max_cycle_s}] seconds.
  - set_offset(offset_s): shift your cycle's offset relative to one of the \
neighbors listed above, to help form a green wave.
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

Do not propose a change just to appear active: oscillating the signal \
timing every cycle (e.g. alternating +Xs then -Xs) makes congestion worse, \
not better, because vehicles already accelerating into a green phase get \
caught by an unexpected early yellow. Only propose a change when the \
observed pattern clearly and persistently justifies it -- a single noisy \
reading is not enough justification on its own.

## Output format

Respond with: the action you propose (or no_action), an urgency level \
(low, medium, or high) reflecting how confident you are that intervention \
is needed at all this cycle, and a short rationale written in Vietnamese \
(this rationale is shown directly to a human operator on a live dashboard, \
so it must be clear, concrete, and refer to the actual numbers you were \
given this cycle -- not a generic templated sentence that could apply to \
any cycle).
"""


def _build_system_prompt(junction_id: str, neighbor_ids: list[str]) -> str:
    neighbor_text = ", ".join(neighbor_ids) if neighbor_ids else "(none -- this junction sits at the edge of the network)"
    return _SYSTEM_PROMPT_TEMPLATE.format(
        junction_id=junction_id,
        neighbor_ids=neighbor_text,
        **HARD_CONSTRAINTS,
    )


def _build_user_prompt(snapshot: JunctionSnapshot, tls_state: TlsState, sim_time: float) -> str:
    phases = "; ".join(f"phase {p.phase_id} ({p.kind})={p.duration_s:.0f}s" for p in tls_state.phases)
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


class JunctionAgent:
    """One LLM-driven junction, round 1 ("observe") only."""

    def __init__(self, junction_id: str, neighbor_ids: list[str]) -> None:
        self.junction_id = junction_id
        self.neighbor_ids = list(neighbor_ids)
        # Built once, kept byte-identical for the agent's whole lifetime --
        # see module docstring on why that matters for caching.
        self._system = _build_system_prompt(junction_id, self.neighbor_ids)
        self._cache_key = f"junction:{junction_id}:v1"

    async def observe(
        self,
        snapshot: JunctionSnapshot,
        tls_state: TlsState,
        sim_time: float,
        *,
        client: AsyncOpenAI | None = None,
    ) -> tuple[Proposal, Usage]:
        """Round 1: self-assessment only. Always returns a valid `Proposal`
        (never `None`) -- on an LLM failure this falls back to `no_action`
        for the cycle, per agents/llm.py's "sim never waits for LLM"
        contract (`client` is injectable for tests, same DI pattern as
        `ask()` itself)."""
        user = _build_user_prompt(snapshot, tls_state, sim_time)
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
