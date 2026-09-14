"""Unit tests for the pure-logic pieces of the Step 8 baselines -- no SUMO
connection needed, matching the rest of this suite's "no Docker/SUMO
needed" style. The full "runs 3600s, maxpressure beats fixed" DoD is a
manual/integration check (see STEPS.md Step 8), not something worth
reproducing here as an automated test (it would need a real SUMO process
and take minutes to run).
"""

from __future__ import annotations

from sumo_agents.baselines.actuated import ActuatedController
from sumo_agents.baselines.fixed import FixedController
from sumo_agents.baselines.maxpressure import phase_deltas
from sumo_agents.sim.actuators import phase_kind


def test_phase_kind_green_has_no_yellow_char() -> None:
    assert phase_kind("GGggrrrrGGGg") == "green"


def test_phase_kind_yellow_even_with_a_permissive_green() -> None:
    # grid_4x4's actual yellow-transition phase for junction A1: one minor
    # movement ('G') stays green throughout, but the phase is still yellow.
    assert phase_kind("yyyyrrrrGyyy") == "yellow"


def test_phase_kind_all_red() -> None:
    assert phase_kind("rrrrrrrr") == "all_red"


def test_fixed_controller_never_acts() -> None:
    assert FixedController().decide({}, 0.0) == []


def test_actuated_controller_never_acts() -> None:
    assert ActuatedController().decide({}, 0.0) == []


def test_phase_deltas_nudges_toward_higher_pressure_phase() -> None:
    deltas = phase_deltas({0: 10.0, 1: 2.0})
    assert deltas[0] == 5.0
    assert deltas[1] == -5.0


def test_phase_deltas_nudges_the_other_way_when_reversed() -> None:
    deltas = phase_deltas({0: 2.0, 1: 10.0})
    assert deltas[0] == -5.0
    assert deltas[1] == 5.0


def test_phase_deltas_no_op_when_pressures_equal() -> None:
    assert phase_deltas({0: 5.0, 1: 5.0}) == {0: 0.0, 1: 0.0}


def test_phase_deltas_no_op_when_gap_below_threshold() -> None:
    assert phase_deltas({0: 5.0, 1: 5.5}) == {0: 0.0, 1: 0.0}


def test_phase_deltas_no_op_with_fewer_than_two_phases() -> None:
    assert phase_deltas({0: 10.0}) == {0: 0.0}


def test_phase_deltas_picks_max_and_min_among_three_phases() -> None:
    deltas = phase_deltas({0: 3.0, 1: 10.0, 2: 1.0})
    assert deltas == {0: 0.0, 1: 5.0, 2: -5.0}
