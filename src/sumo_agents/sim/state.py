"""Deterministic, cheap per-junction state snapshots (STEPS.md Step 5).

`collect_state()` is called once per sampling/decision cycle (plan section
3.2) and must stay CHEAP: it only reads SUMO's own last-step per-lane
aggregates (already computed internally by SUMO every step), never anything
that requires iterating the full vehicle list. Phase 2 (STEPS.md Step 12)
will hand these snapshots -- keyed by junction_id -- straight to
JunctionAgent without any recomputation; a neighbor lookup is just indexing
into this same dict by the neighbor's junction_id (topology itself comes
from `sumolib.net`, not from here).
"""

from __future__ import annotations

from dataclasses import dataclass

from sumo_agents.sim.conn import SumoConnection


@dataclass(frozen=True, slots=True)
class JunctionSnapshot:
    """One junction's state at one instant of sim time."""

    junction_id: str
    current_phase: int
    queue_len: int
    mean_waiting_s: float
    throughput: int
    mean_speed: float
    co2_mg: float


def traffic_light_ids(conn: SumoConnection) -> list[str]:
    """IDs of every signalized junction in the loaded network."""
    return list(conn.trafficlight.getIDList())


def collect_state(conn: SumoConnection, junction_ids: list[str]) -> dict[str, JunctionSnapshot]:
    """Build one snapshot per junction from its controlled (incoming) lanes.

    `throughput` here means "vehicles currently present on an incoming lane
    in the last simulation step" -- a cheap instantaneous flow proxy, not a
    cumulative arrival count (that would need per-lane detectors we don't
    have in this network). `mean_waiting_s` is the accumulated per-lane
    waiting time divided by the vehicle count on that lane, i.e. an
    approximate mean waiting time per vehicle.
    """
    snapshots: dict[str, JunctionSnapshot] = {}
    for jid in junction_ids:
        # getControlledLanes can list the same lane more than once (one
        # entry per traffic-light link using that lane) -- de-dup before summing.
        lanes = list(dict.fromkeys(conn.trafficlight.getControlledLanes(jid)))

        queue_len = 0
        throughput = 0
        total_waiting_s = 0.0
        speed_sum = 0.0
        co2_sum_mg = 0.0
        for lane in lanes:
            queue_len += conn.lane.getLastStepHaltingNumber(lane)
            n_vehicles = conn.lane.getLastStepVehicleNumber(lane)
            throughput += n_vehicles
            total_waiting_s += conn.lane.getWaitingTime(lane)
            speed_sum += conn.lane.getLastStepMeanSpeed(lane)
            co2_sum_mg += conn.lane.getCO2Emission(lane)

        mean_waiting_s = total_waiting_s / throughput if throughput else 0.0
        mean_speed = speed_sum / len(lanes) if lanes else 0.0

        snapshots[jid] = JunctionSnapshot(
            junction_id=jid,
            current_phase=conn.trafficlight.getPhase(jid),
            queue_len=queue_len,
            mean_waiting_s=mean_waiting_s,
            throughput=throughput,
            mean_speed=mean_speed,
            co2_mg=co2_sum_mg,
        )
    return snapshots
