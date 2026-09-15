"""Unit tests for agents/topology.py (STEPS.md Step 12).

Reads the real, checked-in `networks/grid_4x4/net.xml` -- this is pure
`sumolib.net` XML parsing, no SUMO process/TraCI connection involved, so it
stays fast and deterministic like every other unit test here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sumo_agents.agents.topology import neighbor_links, signalized_neighbor_map

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


def test_neighbor_links_keys_match_signalized_neighbor_map() -> None:
    links = neighbor_links(NET_FILE)
    neighbor_map = signalized_neighbor_map(NET_FILE)
    assert links.keys() == neighbor_map.keys()
    for junction_id, neighbors in neighbor_map.items():
        assert set(links[junction_id]) == set(neighbors)


def test_neighbor_links_match_known_grid_block_length() -> None:
    # Cross-checked directly against net.xml: grid_4x4 is a uniform grid,
    # every block edge is 179.2m at the network's 13.89 m/s (50 km/h) speed
    # limit -- so every direct link should report the same distance/time.
    links = neighbor_links(NET_FILE)
    link = links["B1"]["A1"]
    assert link.neighbor_id == "A1"
    assert link.distance_m == pytest.approx(179.2)
    assert link.travel_time_s == pytest.approx(179.2 / 13.89, rel=1e-3)


def test_neighbor_links_are_symmetric_and_positive() -> None:
    links = neighbor_links(NET_FILE)
    for junction_id, neighbors in links.items():
        for neighbor_id, link in neighbors.items():
            assert link.distance_m > 0
            assert link.travel_time_s > 0
            back = links[neighbor_id][junction_id]
            assert back.distance_m == pytest.approx(link.distance_m)
