"""Unit tests for the deterministic safety validator (STEPS.md Step 7).

DoD: >= 15 test cases pass, validator.py coverage >= 90%, and the module
itself must not import anything LLM-related -- the last item is checked
mechanically here (test_module_has_no_llm_imports) rather than left as a
manual promise, since it's a static property that's easy to enforce.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from sumo_agents.safety import validator
from sumo_agents.safety.validator import (
    GREEN_BOUNDS_DECAY_PER_CYCLE_S,
    HARD_CONSTRAINTS,
    AdjustPhaseSplit,
    NoAction,
    PhaseState,
    RequestVms,
    SetCycleLength,
    SetGreenBounds,
    SetOffset,
    TlsState,
    decay_green_bounds,
    validate,
)


def _tls_state(**time_since_last_green_s: float) -> TlsState:
    return TlsState(
        junction_id="J12",
        phases=(
            PhaseState(phase_id="NS", duration_s=30.0, kind="green"),
            PhaseState(phase_id="NS_Y", duration_s=3.0, kind="yellow"),
            PhaseState(phase_id="AR1", duration_s=2.0, kind="all_red"),
            PhaseState(phase_id="EW", duration_s=25.0, kind="green"),
        ),
        time_since_last_green_s=time_since_last_green_s,
    )


# -- no_action -----------------------------------------------------------


def test_no_action_always_valid() -> None:
    result = validate(NoAction(junction_id="J12"), _tls_state())
    assert result.ok
    assert result.violations == []
    assert result.clamped_action == NoAction(junction_id="J12")


# -- adjust_phase_split ----------------------------------------------------


def test_adjust_phase_split_within_bounds_passes_unchanged() -> None:
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NS", delta_s=5.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.violations == []
    assert result.clamped_action.delta_s == 5.0


def test_adjust_phase_split_positive_delta_clamped_to_max_delta_per_cycle() -> None:
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NS", delta_s=50.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.clamped_action.delta_s == HARD_CONSTRAINTS["max_delta_per_cycle_s"]
    assert any("max_delta_per_cycle_s" in v for v in result.violations)


def test_adjust_phase_split_negative_delta_clamped_to_max_delta_per_cycle() -> None:
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NS", delta_s=-50.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.clamped_action.delta_s == -HARD_CONSTRAINTS["max_delta_per_cycle_s"]


def test_adjust_phase_split_result_clamped_to_min_green() -> None:
    # duration 30 - 15 = 15, still >= min_green(7), so push further via a
    # phase that starts close to the floor.
    state = TlsState(
        junction_id="J12",
        phases=(PhaseState(phase_id="NS", duration_s=10.0, kind="green"),),
    )
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NS", delta_s=-15.0)
    result = validate(action, state)
    assert result.ok
    assert result.clamped_action.delta_s == HARD_CONSTRAINTS["min_green_s"] - 10.0
    assert any("min_green" in v or "outside" in v for v in result.violations)


def test_adjust_phase_split_result_clamped_to_max_green() -> None:
    state = TlsState(
        junction_id="J12",
        phases=(PhaseState(phase_id="NS", duration_s=85.0, kind="green"),),
    )
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NS", delta_s=15.0)
    result = validate(action, state)
    assert result.ok
    assert result.clamped_action.delta_s == HARD_CONSTRAINTS["max_green_s"] - 85.0


def test_adjust_phase_split_rejects_unknown_phase() -> None:
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NOPE", delta_s=5.0)
    result = validate(action, _tls_state())
    assert not result.ok
    assert result.clamped_action is None
    assert "unknown phase_id" in result.violations[0]


def test_adjust_phase_split_rejects_yellow_phase() -> None:
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NS_Y", delta_s=1.0)
    result = validate(action, _tls_state())
    assert not result.ok
    assert result.clamped_action is None
    assert "yellow" in result.violations[0]


def test_adjust_phase_split_rejects_all_red_phase() -> None:
    action = AdjustPhaseSplit(junction_id="J12", phase_id="AR1", delta_s=1.0)
    result = validate(action, _tls_state())
    assert not result.ok
    assert result.clamped_action is None


def test_adjust_phase_split_rejects_shrinking_already_starved_phase() -> None:
    state = _tls_state(NS=HARD_CONSTRAINTS["max_starvation_s"])
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NS", delta_s=-5.0)
    result = validate(action, state)
    assert not result.ok
    assert result.clamped_action is None
    assert "starved" in result.violations[0]


def test_adjust_phase_split_allows_growing_a_starved_phase() -> None:
    state = _tls_state(NS=HARD_CONSTRAINTS["max_starvation_s"])
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NS", delta_s=5.0)
    result = validate(action, state)
    assert result.ok
    assert result.clamped_action.delta_s == 5.0


def test_adjust_phase_split_allows_shrinking_a_non_starved_phase() -> None:
    state = _tls_state(NS=HARD_CONSTRAINTS["max_starvation_s"] - 1)
    action = AdjustPhaseSplit(junction_id="J12", phase_id="NS", delta_s=-5.0)
    result = validate(action, state)
    assert result.ok


# -- set_cycle_length ------------------------------------------------------


def test_set_cycle_length_within_bounds_passes_unchanged() -> None:
    action = SetCycleLength(junction_id="J12", cycle_s=90.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.violations == []
    assert result.clamped_action.cycle_s == 90.0


def test_set_cycle_length_clamped_to_min_cycle() -> None:
    action = SetCycleLength(junction_id="J12", cycle_s=10.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.clamped_action.cycle_s == HARD_CONSTRAINTS["min_cycle_s"]
    assert result.violations


def test_set_cycle_length_clamped_to_max_cycle() -> None:
    action = SetCycleLength(junction_id="J12", cycle_s=999.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.clamped_action.cycle_s == HARD_CONSTRAINTS["max_cycle_s"]


# -- set_offset --------------------------------------------------------------


def test_set_offset_within_bounds_passes_unchanged() -> None:
    action = SetOffset(junction_id="J12", offset_s=20.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.clamped_action.offset_s == 20.0


def test_set_offset_negative_clamped_to_zero() -> None:
    action = SetOffset(junction_id="J12", offset_s=-5.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.clamped_action.offset_s == 0.0


def test_set_offset_above_max_cycle_clamped() -> None:
    action = SetOffset(junction_id="J12", offset_s=999.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.clamped_action.offset_s == HARD_CONSTRAINTS["max_cycle_s"]


# -- set_green_bounds (STEPS.md Step 14 actuated-hybrid follow-up) ----------


def _tls_state_with_bounds(**time_since_last_green_s: float) -> TlsState:
    """Like `_tls_state()`, but the green phases carry realistic current
    min_dur_s/max_dur_s (net_actuated.xml's real defaults: [7, 90], already
    the widest legal range) -- `_tls_state()`'s phases default to
    min_dur_s=max_dur_s=0.0, which would make every SetGreenBounds request
    look like a huge jump against max_delta_per_cycle_s."""
    return TlsState(
        junction_id="J12",
        phases=(
            PhaseState(phase_id="NS", duration_s=30.0, kind="green", min_dur_s=7.0, max_dur_s=90.0),
            PhaseState(phase_id="NS_Y", duration_s=3.0, kind="yellow"),
            PhaseState(phase_id="AR1", duration_s=2.0, kind="all_red"),
            PhaseState(phase_id="EW", duration_s=25.0, kind="green", min_dur_s=7.0, max_dur_s=90.0),
        ),
        time_since_last_green_s=time_since_last_green_s,
    )


def test_set_green_bounds_within_bounds_passes_unchanged() -> None:
    action = SetGreenBounds(junction_id="J12", phase_id="NS", min_green_s=15.0, max_green_s=90.0)
    result = validate(action, _tls_state_with_bounds())
    assert result.ok
    assert result.violations == []
    assert result.clamped_action.min_green_s == 15.0
    assert result.clamped_action.max_green_s == 90.0


def test_set_green_bounds_rejects_min_greater_than_max() -> None:
    action = SetGreenBounds(junction_id="J12", phase_id="NS", min_green_s=50.0, max_green_s=10.0)
    result = validate(action, _tls_state_with_bounds())
    assert not result.ok
    assert result.clamped_action is None
    assert "min_green_s" in result.violations[0]


def test_set_green_bounds_clamped_to_hard_constraints() -> None:
    action = SetGreenBounds(junction_id="J12", phase_id="NS", min_green_s=1.0, max_green_s=999.0)
    result = validate(action, _tls_state_with_bounds())
    assert result.ok
    assert result.clamped_action.min_green_s == HARD_CONSTRAINTS["min_green_s"]
    assert result.clamped_action.max_green_s == HARD_CONSTRAINTS["max_green_s"]
    assert result.violations


def test_set_green_bounds_anti_oscillation_clamps_large_jump() -> None:
    # Current bounds [7, 90]; requesting min_green_s=40 is a 33s jump, well
    # over max_delta_per_cycle_s=15.
    action = SetGreenBounds(junction_id="J12", phase_id="NS", min_green_s=40.0, max_green_s=90.0)
    result = validate(action, _tls_state_with_bounds())
    assert result.ok
    assert result.clamped_action.min_green_s == 7.0 + HARD_CONSTRAINTS["max_delta_per_cycle_s"]
    assert any("max_delta_per_cycle_s" in v for v in result.violations)


def test_set_green_bounds_rejects_shrinking_max_on_an_already_starved_phase() -> None:
    state = _tls_state_with_bounds(NS=HARD_CONSTRAINTS["max_starvation_s"])
    action = SetGreenBounds(junction_id="J12", phase_id="NS", min_green_s=7.0, max_green_s=80.0)
    result = validate(action, state)
    assert not result.ok
    assert result.clamped_action is None
    assert "starved" in result.violations[0]


def test_set_green_bounds_allows_raising_max_on_a_starved_phase() -> None:
    state = _tls_state_with_bounds(NS=HARD_CONSTRAINTS["max_starvation_s"])
    action = SetGreenBounds(junction_id="J12", phase_id="NS", min_green_s=15.0, max_green_s=90.0)
    result = validate(action, state)
    assert result.ok


def test_set_green_bounds_rejects_unknown_phase() -> None:
    action = SetGreenBounds(junction_id="J12", phase_id="NOPE", min_green_s=10.0, max_green_s=50.0)
    result = validate(action, _tls_state_with_bounds())
    assert not result.ok
    assert result.clamped_action is None


def test_set_green_bounds_rejects_yellow_phase() -> None:
    action = SetGreenBounds(junction_id="J12", phase_id="NS_Y", min_green_s=10.0, max_green_s=50.0)
    result = validate(action, _tls_state_with_bounds())
    assert not result.ok
    assert result.clamped_action is None


# -- decay_green_bounds (STEPS.md Step 18 follow-up) ------------------------


def _tls_state_with(*, ns_min: float, ns_max: float) -> TlsState:
    return TlsState(
        junction_id="J12",
        phases=(
            PhaseState(phase_id="NS", duration_s=30.0, kind="green", min_dur_s=ns_min, max_dur_s=ns_max),
            PhaseState(phase_id="NS_Y", duration_s=3.0, kind="yellow"),
            PhaseState(phase_id="AR1", duration_s=2.0, kind="all_red"),
            PhaseState(
                phase_id="EW",
                duration_s=25.0,
                kind="green",
                min_dur_s=HARD_CONSTRAINTS["min_green_s"],
                max_dur_s=HARD_CONSTRAINTS["max_green_s"],
            ),
        ),
    )


def test_decay_green_bounds_no_op_when_every_phase_already_at_default() -> None:
    state = _tls_state_with(ns_min=HARD_CONSTRAINTS["min_green_s"], ns_max=HARD_CONSTRAINTS["max_green_s"])
    assert decay_green_bounds(state) == []


def test_decay_green_bounds_steps_min_down_toward_default() -> None:
    # An agent ratcheted NS's min_green_s up to 75 over several cycles
    # (STEPS.md Step 18's real finding); decay pulls it back down.
    state = _tls_state_with(ns_min=75.0, ns_max=HARD_CONSTRAINTS["max_green_s"])
    actions = decay_green_bounds(state)
    assert len(actions) == 1
    assert actions[0].junction_id == "J12"
    assert actions[0].phase_id == "NS"
    assert actions[0].min_green_s == 75.0 - GREEN_BOUNDS_DECAY_PER_CYCLE_S
    assert actions[0].max_green_s == HARD_CONSTRAINTS["max_green_s"]  # already at default, untouched


def test_decay_green_bounds_steps_max_up_toward_default() -> None:
    # A phase's max_green_s was lowered (e.g. to free up cycle time for a
    # competing phase) below the default widest range; decay raises it back.
    state = _tls_state_with(ns_min=HARD_CONSTRAINTS["min_green_s"], ns_max=40.0)
    actions = decay_green_bounds(state)
    assert len(actions) == 1
    assert actions[0].min_green_s == HARD_CONSTRAINTS["min_green_s"]
    assert actions[0].max_green_s == 40.0 + GREEN_BOUNDS_DECAY_PER_CYCLE_S


def test_decay_green_bounds_does_not_overshoot_past_default() -> None:
    # Only 2s away from default (7) -- a 5s step must land exactly on 7,
    # never past it to some lower, illegal value.
    state = _tls_state_with(ns_min=9.0, ns_max=HARD_CONSTRAINTS["max_green_s"])
    actions = decay_green_bounds(state)
    assert len(actions) == 1
    assert actions[0].min_green_s == HARD_CONSTRAINTS["min_green_s"]


def test_decay_green_bounds_custom_step_can_reach_default_in_one_call() -> None:
    state = _tls_state_with(ns_min=75.0, ns_max=HARD_CONSTRAINTS["max_green_s"])
    actions = decay_green_bounds(state, decay_step_s=1000.0)
    assert actions[0].min_green_s == HARD_CONSTRAINTS["min_green_s"]


def test_decay_green_bounds_skips_yellow_and_all_red_phases() -> None:
    # NS_Y/AR1 above default to 0.0/0.0 (never populated for non-green
    # phases in practice) -- decay must never touch them regardless.
    state = _tls_state_with(ns_min=HARD_CONSTRAINTS["min_green_s"], ns_max=HARD_CONSTRAINTS["max_green_s"])
    actions = decay_green_bounds(state)
    assert all(a.phase_id not in ("NS_Y", "AR1") for a in actions)


def test_decay_green_bounds_result_passes_validate_unchanged() -> None:
    # Defense-in-depth check (sim/runner.py still runs decay's output
    # through validate()): a decayed action must never itself get
    # clamped/rejected -- it is already legal and moving toward, not away
    # from, the safe default.
    state = _tls_state_with(ns_min=75.0, ns_max=HARD_CONSTRAINTS["max_green_s"])
    action = decay_green_bounds(state)[0]
    result = validate(action, state)
    assert result.ok
    assert result.violations == []
    assert result.clamped_action == action


# -- request_vms -------------------------------------------------------------


def test_request_vms_valid_route_passes() -> None:
    action = RequestVms(junction_id="J12", edge="B1B2", alt_route=["B1A1", "A1A2", "A2B2"], duration_s=300.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.violations == []
    assert result.clamped_action.duration_s == 300.0


def test_request_vms_rejects_empty_route() -> None:
    action = RequestVms(junction_id="J12", edge="B1B2", alt_route=[], duration_s=300.0)
    result = validate(action, _tls_state())
    assert not result.ok
    assert result.clamped_action is None


def test_request_vms_rejects_looping_route() -> None:
    action = RequestVms(junction_id="J12", edge="B1B2", alt_route=["B1A1", "A1A2", "B1A1"], duration_s=300.0)
    result = validate(action, _tls_state())
    assert not result.ok
    assert result.clamped_action is None
    assert "loop" in result.violations[0]


def test_request_vms_negative_duration_clamped_to_zero() -> None:
    action = RequestVms(junction_id="J12", edge="B1B2", alt_route=["B1A1"], duration_s=-10.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.clamped_action.duration_s == 0.0


def test_request_vms_excessive_duration_clamped() -> None:
    action = RequestVms(junction_id="J12", edge="B1B2", alt_route=["B1A1"], duration_s=999_999.0)
    result = validate(action, _tls_state())
    assert result.ok
    assert result.clamped_action.duration_s == HARD_CONSTRAINTS["max_cycle_s"] * 24


# -- constants sanity ----------------------------------------------------


def test_yellow_and_all_red_durations_are_fixed_constants() -> None:
    assert HARD_CONSTRAINTS["yellow_s"] == 3
    assert HARD_CONSTRAINTS["all_red_s"] == 2


def test_tls_state_phase_lookup_returns_none_for_missing_phase() -> None:
    assert _tls_state().phase("NOPE") is None


# -- structural property ------------------------------------------------


def test_module_has_no_llm_imports() -> None:
    """Static check for the Step 7 DoD: validator.py must not import
    anything LLM-related (no LLM calls exist yet in this project)."""
    source = inspect.getsource(validator)
    tree = ast.parse(source)
    forbidden = ("openai", "anthropic", "llm")

    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.append(node.module)

    for name in imported_names:
        lowered = name.lower()
        assert not any(bad in lowered for bad in forbidden), f"unexpected LLM-related import: {name}"


def test_validate_raises_on_unhandled_action_type() -> None:
    with pytest.raises(TypeError):
        validate(object(), _tls_state())  # type: ignore[arg-type]
