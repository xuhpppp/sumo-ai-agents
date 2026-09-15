"""Unit tests for agents/topology.py (STEPS.md Step 12).

Reads the real, checked-in `networks/grid_4x4/net.xml` -- this is pure
`sumolib.net` XML parsing, no SUMO process/TraCI connection involved, so it
stays fast and deterministic like every other unit test here.
"""

from __future__ import annotations

from pathlib import Path

from sumo_agents.agents.topology import signalized_neighbor_map

NET_FILE = Path(__file__).resolve().parents[1] / "networks" / "grid_4x4" / "net.xml"


def test_every_signalized_junction_is_a_key() -> None:
    neighbor_map = signalized_neighbor_map(NET_FILE)
    # grid_4x4 has 12 signalized junctions (STEPS.md Step 3/12 notes).
    assert len(neighbor_map) == 12


def test_neighbors_match_known_grid_adjacency() -> None:
    # Cross-checked directly against net.xml's <junction>/<edge> topology.
    neighbor_map = signalized_neighbor_map(NET_FILE)
    assert neighbor_map["B1"] == ["A1", "B0", "B2", "C1"]
    assert neighbor_map["A1"] == ["A2", "B1"]


def test_a_junction_is_never_its_own_neighbor() -> None:
    neighbor_map = signalized_neighbor_map(NET_FILE)
    for junction_id, neighbors in neighbor_map.items():
        assert junction_id not in neighbors


def test_not_all_to_all() -> None:
    # A hardcoded/naive "everyone is everyone's neighbor" implementation
    # would give every junction all 11 others -- the real grid topology
    # must not look like that.
    neighbor_map = signalized_neighbor_map(NET_FILE)
    total = len(neighbor_map)
    for neighbors in neighbor_map.values():
        assert len(neighbors) < total - 1


def test_neighbor_relationship_is_symmetric() -> None:
    # grid_4x4's edges run in both directions between adjacent junctions,
    # so "A is B's neighbor" should always imply "B is A's neighbor".
    neighbor_map = signalized_neighbor_map(NET_FILE)
    for junction_id, neighbors in neighbor_map.items():
        for neighbor_id in neighbors:
            assert junction_id in neighbor_map[neighbor_id]
