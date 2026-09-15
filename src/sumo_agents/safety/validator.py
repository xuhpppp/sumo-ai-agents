"""Deterministic safety validator (STEPS.md Step 7).

This module must not import anything LLM-related: it exists and is fully
tested *before* Phase 2 ever calls a model (STEPS.md Step 10+). It stays in
the pipeline afterward as the mandatory "round 3 -- validate" step (plan
section 5 / STEPS.md Step 13): every agent-proposed action is checked here
BEFORE it reaches SupervisorAgent, and a violation is rejected outright --
it never gets a chance to influence the supervisor's decision.

Policy (plan section 5): clamp a value that is merely out of bound, reject
an action that is structurally or semantically invalid (unknown phase,
wrong phase kind, a VMS route that loops back on itself, ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Union

from pydantic import BaseModel, Field

HARD_CONSTRAINTS = {
    "min_green_s": 7,  # never violated
    "max_green_s": 90,
    "yellow_s": 3,  # FIXED -- agents cannot touch it
    "all_red_s": 2,  # FIXED
    "min_cycle_s": 40,
    "max_cycle_s": 150,
    "max_delta_per_cycle_s": 15,  # anti-oscillation
    "max_starvation_s": 120,  # every direction must see green within this window
}

PhaseKind = Literal["green", "yellow", "all_red"]


@dataclass(frozen=True, slots=True)
class PhaseState:
    phase_id: str
    duration_s: float
    kind: PhaseKind
    # Actuated-mode bounds (STEPS.md Step 14 actuated-hybrid follow-up) --
    # every SUMO tlLogic phase carries minDur/maxDur regardless of program
    # `type`, but only an `type="actuated"` program actually enforces them
    # (a `type="static"` program ignores them entirely). Default 0.0 for
    # callers that never populate them (baselines on the static network).
    min_dur_s: float = 0.0
    max_dur_s: float = 0.0


@dataclass(frozen=True, slots=True)
class TlsState:
    """Minimal per-junction TLS snapshot the validator needs -- not the
    full simulation snapshot from sim/state.py.

    `time_since_last_green_s` only needs entries for phases at risk of
    starvation; a missing key is treated as "recently green" (0.0).
    """

    junction_id: str
    phases: tuple[PhaseState, ...]
    time_since_last_green_s: dict[str, float] = field(default_factory=dict)

    def phase(self, phase_id: str) -> PhaseState | None:
        return next((p for p in self.phases if p.phase_id == phase_id), None)


class AdjustPhaseSplit(BaseModel):
    type: Literal["adjust_phase_split"] = "adjust_phase_split"
    junction_id: str
    phase_id: str
    delta_s: float


class SetCycleLength(BaseModel):
    type: Literal["set_cycle_length"] = "set_cycle_length"
    junction_id: str
    cycle_s: float


class SetOffset(BaseModel):
    type: Literal["set_offset"] = "set_offset"
    junction_id: str
    offset_s: float


class SetGreenBounds(BaseModel):
    """Set one GREEN phase's actuated min/max duration -- STEPS.md Step 14
    actuated-hybrid follow-up. Only meaningful on a `type="actuated"`
    program (SUMO's own induction-loop gap-out logic reads these bounds
    every simulation step and extends/truncates the phase live between
    them); on a `type="static"` program the values are accepted but never
    enforced. Replaces `AdjustPhaseSplit`/`SetCycleLength`/`SetOffset` in
    `agents/protocol.py`'s `ActionUnion` (what JunctionAgent/SupervisorAgent
    can actually propose) -- those three stay defined here for any other
    caller, they are just no longer offered to the LLM."""

    type: Literal["set_green_bounds"] = "set_green_bounds"
    junction_id: str
    phase_id: str
    min_green_s: float
    max_green_s: float


class RequestVms(BaseModel):
    type: Literal["request_vms"] = "request_vms"
    junction_id: str
    edge: str
    alt_route: list[str] = Field(default_factory=list)
    duration_s: float


class NoAction(BaseModel):
    """Must always be valid -- and is encouraged: without it a model always
    "does something" and the system oscillates (plan section 5)."""

    type: Literal["no_action"] = "no_action"
    junction_id: str


Action = Union[AdjustPhaseSplit, SetCycleLength, SetOffset, SetGreenBounds, RequestVms, NoAction]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    violations: list[str]
    clamped_action: Action | None  # None iff ok is False (rejected)


def validate(action: Action, tls_state: TlsState) -> ValidationResult:
    """Deterministic, synchronous, no I/O. `tls_state` must describe the
    same junction as `action.junction_id`; the caller (orchestrator, or a
    test) is responsible for routing the right state to the right action."""
    if isinstance(action, NoAction):
        return ValidationResult(ok=True, violations=[], clamped_action=action)
    if isinstance(action, AdjustPhaseSplit):
        return _validate_adjust_phase_split(action, tls_state)
    if isinstance(action, SetCycleLength):
        return _validate_set_cycle_length(action)
    if isinstance(action, SetOffset):
        return _validate_set_offset(action)
    if isinstance(action, SetGreenBounds):
        return _validate_set_green_bounds(action, tls_state)
    if isinstance(action, RequestVms):
        return _validate_request_vms(action)
    raise TypeError(f"unhandled action type: {type(action)!r}")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _validate_adjust_phase_split(action: AdjustPhaseSplit, tls_state: TlsState) -> ValidationResult:
    phase = tls_state.phase(action.phase_id)
    if phase is None:
        return ValidationResult(
            ok=False,
            violations=[f"unknown phase_id {action.phase_id!r} at junction {tls_state.junction_id!r}"],
            clamped_action=None,
        )
    if phase.kind != "green":
        return ValidationResult(
            ok=False,
            violations=[f"phase {action.phase_id!r} is {phase.kind!r}, agents cannot adjust yellow/all-red phases"],
            clamped_action=None,
        )

    violations: list[str] = []
    delta_s = action.delta_s
    max_delta = HARD_CONSTRAINTS["max_delta_per_cycle_s"]
    if abs(delta_s) > max_delta:
        violations.append(f"delta_s={delta_s} exceeds max_delta_per_cycle_s={max_delta}, clamped")
        delta_s = _clamp(delta_s, -max_delta, max_delta)

    if delta_s < 0:
        starved_for = tls_state.time_since_last_green_s.get(action.phase_id, 0.0)
        max_starvation = HARD_CONSTRAINTS["max_starvation_s"]
        if starved_for >= max_starvation:
            return ValidationResult(
                ok=False,
                violations=[
                    f"phase {action.phase_id!r} already starved for {starved_for}s "
                    f">= max_starvation_s={max_starvation}, refusing to shrink it further"
                ],
                clamped_action=None,
            )

    new_duration = phase.duration_s + delta_s
    min_green, max_green = HARD_CONSTRAINTS["min_green_s"], HARD_CONSTRAINTS["max_green_s"]
    clamped_duration = _clamp(new_duration, min_green, max_green)
    if clamped_duration != new_duration:
        violations.append(
            f"resulting green {new_duration}s outside [{min_green}, {max_green}], clamped to {clamped_duration}s"
        )
    clamped_delta = clamped_duration - phase.duration_s

    return ValidationResult(ok=True, violations=violations, clamped_action=action.model_copy(update={"delta_s": clamped_delta}))


def _validate_set_cycle_length(action: SetCycleLength) -> ValidationResult:
    min_cycle, max_cycle = HARD_CONSTRAINTS["min_cycle_s"], HARD_CONSTRAINTS["max_cycle_s"]
    clamped = _clamp(action.cycle_s, min_cycle, max_cycle)
    violations = []
    if clamped != action.cycle_s:
        violations.append(f"cycle_s={action.cycle_s} outside [{min_cycle}, {max_cycle}], clamped to {clamped}")
    return ValidationResult(ok=True, violations=violations, clamped_action=action.model_copy(update={"cycle_s": clamped}))


def _validate_set_offset(action: SetOffset) -> ValidationResult:
    hi = HARD_CONSTRAINTS["max_cycle_s"]  # an offset can never exceed one full cycle
    clamped = _clamp(action.offset_s, 0.0, hi)
    violations = []
    if clamped != action.offset_s:
        violations.append(f"offset_s={action.offset_s} outside [0, {hi}], clamped to {clamped}")
    return ValidationResult(ok=True, violations=violations, clamped_action=action.model_copy(update={"offset_s": clamped}))


def _validate_set_green_bounds(action: SetGreenBounds, tls_state: TlsState) -> ValidationResult:
    phase = tls_state.phase(action.phase_id)
    if phase is None:
        return ValidationResult(
            ok=False,
            violations=[f"unknown phase_id {action.phase_id!r} at junction {tls_state.junction_id!r}"],
            clamped_action=None,
        )
    if phase.kind != "green":
        return ValidationResult(
            ok=False,
            violations=[f"phase {action.phase_id!r} is {phase.kind!r}, agents cannot set green bounds on yellow/all-red phases"],
            clamped_action=None,
        )
    if action.min_green_s > action.max_green_s:
        return ValidationResult(
            ok=False,
            violations=[f"min_green_s={action.min_green_s} > max_green_s={action.max_green_s}"],
            clamped_action=None,
        )

    violations: list[str] = []
    lo, hi = HARD_CONSTRAINTS["min_green_s"], HARD_CONSTRAINTS["max_green_s"]
    min_g = _clamp(action.min_green_s, lo, hi)
    max_g = _clamp(action.max_green_s, lo, hi)
    if min_g != action.min_green_s or max_g != action.max_green_s:
        violations.append(
            f"[min_green_s, max_green_s]=[{action.min_green_s}, {action.max_green_s}] outside "
            f"[{lo}, {hi}], clamped to [{min_g}, {max_g}]"
        )

    # Anti-oscillation, same idea as AdjustPhaseSplit's max_delta_per_cycle_s
    # -- applied to each bound independently, against the CURRENT bounds
    # (`phase.min_dur_s`/`max_dur_s`), not the requested values.
    max_delta = HARD_CONSTRAINTS["max_delta_per_cycle_s"]
    clamped_min = _clamp(min_g, phase.min_dur_s - max_delta, phase.min_dur_s + max_delta)
    clamped_max = _clamp(max_g, phase.max_dur_s - max_delta, phase.max_dur_s + max_delta)
    if clamped_min != min_g or clamped_max != max_g:
        violations.append(
            f"bounds moved more than max_delta_per_cycle_s={max_delta} from current "
            f"[{phase.min_dur_s}, {phase.max_dur_s}], clamped to [{clamped_min}, {clamped_max}]"
        )

    if clamped_max < phase.max_dur_s:
        starved_for = tls_state.time_since_last_green_s.get(action.phase_id, 0.0)
        max_starvation = HARD_CONSTRAINTS["max_starvation_s"]
        if starved_for >= max_starvation:
            return ValidationResult(
                ok=False,
                violations=[
                    f"phase {action.phase_id!r} already starved for {starved_for}s "
                    f">= max_starvation_s={max_starvation}, refusing to shrink its max_green_s further"
                ],
                clamped_action=None,
            )

    if clamped_min > clamped_max:
        clamped_min = clamped_max  # the two independent per-bound clamps above can cross at the edges

    return ValidationResult(
        ok=True,
        violations=violations,
        clamped_action=action.model_copy(update={"min_green_s": clamped_min, "max_green_s": clamped_max}),
    )


def _validate_request_vms(action: RequestVms) -> ValidationResult:
    if not action.alt_route:
        return ValidationResult(ok=False, violations=["alt_route must not be empty"], clamped_action=None)
    if len(set(action.alt_route)) != len(action.alt_route):
        return ValidationResult(
            ok=False, violations=["alt_route revisits an edge -- would create a routing loop"], clamped_action=None
        )

    # A VMS detour cannot outlast the whole simulated run -- generous cap,
    # not a tuned constant (plan doesn't specify one for this action).
    upper_bound = HARD_CONSTRAINTS["max_cycle_s"] * 24
    clamped_duration = _clamp(action.duration_s, 0.0, upper_bound)
    violations = []
    if clamped_duration != action.duration_s:
        violations.append(f"duration_s={action.duration_s} outside [0, {upper_bound}], clamped to {clamped_duration}")

    return ValidationResult(
        ok=True, violations=violations, clamped_action=action.model_copy(update={"duration_s": clamped_duration})
    )
