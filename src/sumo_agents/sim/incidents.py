"""Seeded incident injection (STEPS.md Step 5).

Incidents come from a fixed YAML schedule, NOT from runtime randomness --
so a run and its baseline comparison (plan section 7) hit the exact same
incidents at the exact same sim time regardless of which controller mode is
under test. Randomness only enters through demand generation (STEPS.md Step
3's `randomTrips.py --seed`); incidents are deterministic on top of that.

An incident is modeled as a temporary speed reduction on every lane of one
edge -- the cheapest TraCI-native way to simulate something like a stalled
vehicle or a partial blockage without editing the network itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from sumo_agents.sim.conn import SumoConnection


@dataclass(frozen=True, slots=True)
class Incident:
    edge_id: str
    begin: float
    duration: float
    speed_factor: float  # fraction of each lane's normal max speed while active


def load_incident_schedule(path: Path) -> list[Incident]:
    """Load a list of incidents from a YAML file (empty list if the file lists none)."""
    data = yaml.safe_load(path.read_text()) or []
    return [Incident(**item) for item in data]


class IncidentInjector:
    """Applies/reverts scheduled lane speed reductions as sim time passes.

    `apply()` is meant to be called once per sim step. It is cheap: a
    handful of float comparisons against a short, fixed incident list, no
    randomness, and no state beyond "has this incident already started /
    already ended".
    """

    def __init__(self, incidents: list[Incident]) -> None:
        self._incidents = incidents
        self._active: set[int] = set()
        self._original_speeds: dict[int, dict[str, float]] = {}

    def apply(self, conn: SumoConnection, sim_time: float) -> None:
        for i, incident in enumerate(self._incidents):
            active_window = incident.begin <= sim_time < incident.begin + incident.duration
            if active_window and i not in self._active:
                self._start(conn, i, incident)
            elif not active_window and i in self._active:
                self._end(conn, i)

    @staticmethod
    def _lane_ids(conn: SumoConnection, edge_id: str) -> list[str]:
        n_lanes = conn.edge.getLaneNumber(edge_id)
        return [f"{edge_id}_{lane_idx}" for lane_idx in range(n_lanes)]

    def _start(self, conn: SumoConnection, i: int, incident: Incident) -> None:
        originals: dict[str, float] = {}
        for lane in self._lane_ids(conn, incident.edge_id):
            original_speed = conn.lane.getMaxSpeed(lane)
            originals[lane] = original_speed
            conn.lane.setMaxSpeed(lane, original_speed * incident.speed_factor)
        self._original_speeds[i] = originals
        self._active.add(i)

    def _end(self, conn: SumoConnection, i: int) -> None:
        for lane, original_speed in self._original_speeds.pop(i, {}).items():
            conn.lane.setMaxSpeed(lane, original_speed)
        self._active.discard(i)
