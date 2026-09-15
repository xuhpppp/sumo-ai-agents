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
