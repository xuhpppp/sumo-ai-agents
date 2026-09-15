"""Neighbor topology inferred from the road network (STEPS.md Step 12).

`JunctionAgent`'s system prompt needs to know which other signalized
junctions are actually adjacent to it -- not every other agent in the run
(all-to-all doesn't scale and is meaningless: coordinating an offset with a
junction three blocks away has no effect), and not a hand-typed list (that
would silently go stale the moment the network changes, e.g. when
`mixed_district`/`osm_real` are added later, plan section 4). `sumolib.net`
already has the real graph loaded from the same `net.xml` SUMO itself
drives, so topology is read from there directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import sumolib


def signalized_neighbor_map(net_file: str | Path) -> dict[str, list[str]]:
    """One-hop neighbor junction IDs for every signalized (traffic_light)
    node in `net_file`, restricted to other signalized nodes.

    Two junctions are "neighbors" here iff an edge directly connects them
    (either direction) -- this is the only relationship a coordination
    action (`set_offset`) or a future coalition message (STEPS.md Step 13)
    can actually act on.
    """
    net = sumolib.net.readNet(str(net_file))
    tls_ids = {node.getID() for node in net.getNodes() if node.getType() == "traffic_light"}

    neighbor_map: dict[str, list[str]] = {}
    for node in net.getNodes():
        if node.getID() not in tls_ids:
            continue
        connected = {edge.getToNode().getID() for edge in node.getOutgoing()}
        connected |= {edge.getFromNode().getID() for edge in node.getIncoming()}
        connected.discard(node.getID())
        neighbor_map[node.getID()] = sorted(connected & tls_ids)
    return neighbor_map


@dataclass(frozen=True, slots=True)
class NeighborLink:
    """Physical proximity between two directly-connected signalized
    junctions -- STEPS.md Step 14 coordination-context follow-up: the
    coalition round and `SupervisorAgent` previously knew WHICH junctions
    are adjacent (`signalized_neighbor_map`) but never HOW FAR (in time) a
    change at one would take to reach the other, which is exactly the
    information a local `actuated` junction structurally cannot have and a
    genuinely coordinated decision needs. Direction (upstream/downstream) is
    deliberately not modeled here: `grid_4x4`'s connecting edges run both
    ways between every adjacent pair (see
    test_topology.test_neighbor_relationship_is_symmetric), so a static
    per-pair direction label would not correspond to any one real, stable
    traffic-flow direction -- only distance/travel_time is well-defined
    regardless of which of the two edges (or which lane within a phase) a
    given movement actually uses."""

    neighbor_id: str
    distance_m: float
    travel_time_s: float


def _consider_link(by_neighbor: dict[str, NeighborLink], other_node, edge, tls_ids: set[str], self_id: str) -> None:
    other_id = other_node.getID()
    if other_id not in tls_ids or other_id == self_id:
        return
    distance_m = edge.getLength()
    speed_mps = edge.getSpeed() or 1.0  # guard: a real network never has a 0 speed limit, but don't divide by it if it did
    travel_time_s = distance_m / speed_mps
    existing = by_neighbor.get(other_id)
    if existing is None or distance_m < existing.distance_m:
        # Multiple edges can connect the same pair (e.g. a two-way street
        # modeled as two separate one-way edges) -- keep the shorter one, a
        # coordination decision should reason about the fastest a change
        # could actually propagate, not an arbitrary edge.
        by_neighbor[other_id] = NeighborLink(neighbor_id=other_id, distance_m=distance_m, travel_time_s=travel_time_s)


def neighbor_links(net_file: str | Path) -> dict[str, dict[str, NeighborLink]]:
    """Free-flow distance/travel-time from every signalized node in
    `net_file` to each of its directly-connected signalized neighbors (same
    restriction as `signalized_neighbor_map` -- only a direct edge is a
    relationship a coordination decision can act on)."""
    net = sumolib.net.readNet(str(net_file))
    tls_ids = {node.getID() for node in net.getNodes() if node.getType() == "traffic_light"}

    links: dict[str, dict[str, NeighborLink]] = {}
    for node in net.getNodes():
        if node.getID() not in tls_ids:
            continue
        by_neighbor: dict[str, NeighborLink] = {}
        for edge in node.getOutgoing():
            _consider_link(by_neighbor, edge.getToNode(), edge, tls_ids, node.getID())
        for edge in node.getIncoming():
            _consider_link(by_neighbor, edge.getFromNode(), edge, tls_ids, node.getID())
        links[node.getID()] = by_neighbor
    return links
