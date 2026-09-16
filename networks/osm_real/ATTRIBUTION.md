# Data attribution — `networks/osm_real`

The road network in this directory (`map.osm`, and everything derived from it:
`net.xml`, `net_actuated.xml`, `trips.xml`, `incidents.yaml`,
`agent_junctions.yaml`) is built from real **OpenStreetMap** data.

- **Source**: © OpenStreetMap contributors.
- **License**: [Open Data Commons Open Database License (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/).
- **Area extracted**: a small block of the Hoàn Kiếm district, Hanoi,
  Vietnam — bounding box (WGS84, `west,south,east,north`):
  `105.8515,21.0225,105.8555,21.0255` (roughly 330m × 330m, just south-east
  of Hoàn Kiếm Lake).
- **Fetched**: 2026-09-16, via the Overpass API
  (`https://overpass-api.de/api/map?bbox=105.8515,21.0225,105.8555,21.0255`).
  Overpass reported `osm_base="2026-09-16T02:55:02Z"` as the underlying
  database snapshot timestamp (see the `<meta>` element in `map.osm`).

## Why this matters (see IMPLEMENTATION_PLAN.md §12.2)

ODbL is a **share-alike license for the data itself** (not just derivative
databases in the strict legal sense — the "produced work" clause is more
permissive, but `net.xml`/`net_actuated.xml` here are close enough to being
a re-expression of the underlying map data that they should be treated as
covered). This repository already tracks `networks/osm_real/` under its
existing (permissive) repo license for internal POC use, but:

> **If you redistribute `net.xml`, `net_actuated.xml`, `map.osm`, or any
> file in this directory derived from them OUTSIDE this repository
> (e.g. publishing a fork, a dataset, or a public artifact built from
> them), confirm ODbL share-alike compliance first** — in particular
> whether that redistribution counts as a "derivative database" requiring
> the same ODbL terms, per https://opendatacommons.org/licenses/odbl/.

No OSM data (or anything derived from it) is sent to the OpenAI API or any
other external service by this project — see IMPLEMENTATION_PLAN.md §12.1
for the (separate) note about synthetic simulation state being sent to
OpenAI in `--mode llm`.
