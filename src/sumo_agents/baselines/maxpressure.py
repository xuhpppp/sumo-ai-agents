"""Max-Pressure baseline (STEPS.md Step 8) -- SOTA non-learning control.

Classic Max-Pressure (Varaiya 2013) picks, at each junction, whichever
green phase currently has the highest "pressure" (queued vehicles upstream
minus queued vehicles downstream) to serve right now -- it can switch to
any phase at any time.

This POC's action space is intentionally narrow (safety/validator.py,
STEPS.md Step 7): agents adjust an existing fixed-time cycle's phase splits
by a bounded delta each cycle, they never freely pick which phase runs
next. So this is a Max-Pressure-INSPIRED controller that fits that same
space: each control interval it nudges green time from the least-pressured
phase to the most-pressured one by a small, fixed step
(MaxPressureController's docstring explains why -- both the fixed step
*and* averaging pressure over the whole interval, not sampling it once,
turned out to matter empirically).
"""

from __future__ import annotations

from dataclasses import dataclass

from sumo_agents.safety.validator import Action, AdjustPhaseSplit
from sumo_agents.sim.actuators import phase_kind
from sumo_agents.sim.conn import SumoConnection
from sumo_agents.sim.state import JunctionSnapshot

_MIN_PRESSURE_DIFF = 4.0  # ignore a max-min pressure gap below this many vehicles
_STEP_S = 5.0  # fixed per-cycle nudge, well under validator's max_delta_per_cycle_s=15


@dataclass(frozen=True, slots=True)
class _PhaseMovements:
    phase_index: int
    in_lanes: tuple[str, ...]
    out_lanes: tuple[str, ...]


def phase_deltas(pressures: dict[int, float]) -> dict[int, float]:
    """Shift a small fixed amount of green time from the least-pressured
    green phase to the most-pressured one, provided the gap between them is
    meaningful. Pure function (no SUMO) so it's unit-testable on its own --
    see MaxPressureController for how pressure is actually measured.

    Earlier versions tried jumping straight to a fully-proportional "ideal"
    split every cycle -- verified empirically to LOSE to `fixed` on mean
    waiting time (STEPS.md Step 8): a single congested reading (very easy
    to hit when one green phase happens to have near-zero measured
    pressure) drives that phase's target to zero, i.e. it proposes taking
    an entire direction's green away. A small constant step never proposes
    anything that extreme.
    """
    deltas = dict.fromkeys(pressures, 0.0)
    if len(pressures) < 2:
        return deltas
    max_phase = max(pressures, key=pressures.get)
    min_phase = min(pressures, key=pressures.get)
    if max_phase == min_phase or pressures[max_phase] - pressures[min_phase] <= _MIN_PRESSURE_DIFF:
        return deltas
    deltas[max_phase] = _STEP_S
    deltas[min_phase] = -_STEP_S
    return deltas


class MaxPressureController:
    """Two things turned out to matter empirically, beyond the small fixed
    step in phase_deltas() (STEPS.md Step 8):

    1. Pressure is measured via `observe()`, called every metric-sample
       tick (sim/runner.py's METRIC_SAMPLE_INTERVAL_S, 10s) between
       decide() calls, and averaged over the whole control interval (90s =
       one full TLS cycle) -- not read once, at the single instant
       decide() itself runs. `getLastStepHaltingNumber` is an instantaneous
       count, and decide() always lands on the same point in each phase's
       cycle (a control interval that equals the cycle length), so a
       single reading is systematically biased toward whichever phase just
       ended (its queue just built up over the whole time the other phase
       was green) -- not a fair comparison. Averaging over the full
       interval removes that bias.
    2. `_MIN_PRESSURE_DIFF` must be large enough to ignore the noise that's
       left after averaging -- too low and the controller reacts to every
       small fluctuation and net effect is a wash at best.

    `decide()` alone (the Controller protocol's only required method,
    baselines/base.py) can't do the averaging -- it only runs once per
    control interval. `observe()` is this controller's own extension,
    called by sim/runner.py when present (duck-typed, not part of the
    shared Controller protocol: `fixed`/`actuated` don't need per-tick
    data).
    """

    name = "maxpressure"

    def __init__(self, conn: SumoConnection) -> None:
        self._conn = conn
        # Link/lane topology per junction never changes over a run (only
        # phase *durations* do, via our own AdjustPhaseSplit actions) --
        # cache it instead of re-querying getControlledLinks every cycle.
        self._movements: dict[str, list[_PhaseMovements]] = {}
        # junction_id -> {phase_index: (sum_of_readings, n_readings)},
        # accumulated by observe() and consumed (and reset) by decide().
        self._pressure_accum: dict[str, dict[int, tuple[float, int]]] = {}

    def observe(self, snapshot: dict[str, JunctionSnapshot], sim_time: float) -> None:
        for junction_id in snapshot:
            movements = self._movements_for(junction_id)
            if len(movements) < 2:
                continue
            accum = self._pressure_accum.setdefault(junction_id, {})
            for m in movements:
                total, count = accum.get(m.phase_index, (0.0, 0))
                accum[m.phase_index] = (total + self._pressure(m), count + 1)

    def decide(self, snapshot: dict[str, JunctionSnapshot], sim_time: float) -> list[Action]:
        actions: list[Action] = []
        for junction_id in snapshot:
            actions.extend(self._decide_junction(junction_id))
        return actions

    def _decide_junction(self, junction_id: str) -> list[Action]:
        accum = self._pressure_accum.pop(junction_id, {})
        if len(accum) < 2:
            return []  # single green phase (or no observations yet) -- nothing to rebalance

        average_pressure = {phase_index: total / count for phase_index, (total, count) in accum.items() if count}
        deltas = phase_deltas(average_pressure)
        return [
            AdjustPhaseSplit(junction_id=junction_id, phase_id=str(phase_index), delta_s=delta)
            for phase_index, delta in deltas.items()
            if delta != 0
        ]

    def _movements_for(self, junction_id: str) -> list[_PhaseMovements]:
        movements = self._movements.get(junction_id)
        if movements is None:
            movements = self._build_movements(junction_id)
            self._movements[junction_id] = movements
        return movements

    def _build_movements(self, junction_id: str) -> list[_PhaseMovements]:
        logic = self._conn.trafficlight.getAllProgramLogics(junction_id)[0]
        links = self._conn.trafficlight.getControlledLinks(junction_id)  # one entry per signal index

        movements = []
        for phase_index, phase in enumerate(logic.phases):
            if phase_kind(phase.state) != "green":
                continue
            in_lanes: set[str] = set()
            out_lanes: set[str] = set()
            for signal_index, char in enumerate(phase.state):
                if char not in "Gg":
                    continue
                for in_lane, out_lane, _via in links[signal_index]:
                    in_lanes.add(in_lane)
                    out_lanes.add(out_lane)
            movements.append(_PhaseMovements(phase_index, tuple(in_lanes), tuple(out_lanes)))
        return movements

    def _pressure(self, movements: _PhaseMovements) -> float:
        lane = self._conn.lane
        incoming = sum(lane.getLastStepHaltingNumber(lane_id) for lane_id in movements.in_lanes)
        outgoing = sum(lane.getLastStepHaltingNumber(lane_id) for lane_id in movements.out_lanes)
        return max(incoming - outgoing, 0.0)
