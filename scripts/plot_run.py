"""Waiting-time plot from the DB (STEPS.md Step 6).

The raw per-junction `mean_waiting_s` sample (every 10 sim seconds, see
sim/runner.py) is dominated by the traffic-light phase cycle itself
(green/red sawtooth -- grid_4x4's fixed-time TLS cycle is 90s, see net.xml)
long before any incident effect would show up. To actually see an
incident's effect on congestion this script:

1. averages `mean_waiting_s` across all junctions for each sampled sim_time
   (network-wide mean), and
2. applies a centered rolling mean over a window spanning several signal
   cycles (default 180s = 2 cycles), not just the raw 10s series.

Incident windows are read from `networks/<scenario>/incidents.yaml` (STEPS.md
Step 5) and shaded on the plot -- nothing about them is hardcoded here.

Usage:
    python scripts/plot_run.py --scenario grid_4x4
    python scripts/plot_run.py --scenario grid_4x4 --run-id <uuid> --out data/plots/foo.png
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from statistics import fmean

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from sumo_agents.obs.db import make_async_engine, make_session_factory  # noqa: E402
from sumo_agents.obs.models import Metric, Run  # noqa: E402
from sumo_agents.sim.incidents import Incident, load_incident_schedule  # noqa: E402

NETWORKS_DIR = Path(__file__).resolve().parents[1] / "networks"
DEFAULT_SMOOTH_WINDOW_S = 180.0  # >= 2 signal cycles (grid_4x4's fixed TLS cycle is 90s)


async def resolve_run_id(session_factory: async_sessionmaker, scenario: str, run_id: str | None) -> uuid.UUID:
    if run_id is not None:
        return uuid.UUID(run_id)
    async with session_factory() as session:
        found = (
            await session.execute(
                select(Run.run_id).where(Run.scenario == scenario).order_by(Run.started_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    if found is None:
        raise SystemExit(f"No run found for scenario={scenario!r} -- run sim.runner first (STEPS.md Step 5).")
    return found


async def fetch_network_mean_waiting(
    session_factory: async_sessionmaker, run_id: uuid.UUID
) -> tuple[list[float], list[float]]:
    """One (sim_time, mean_waiting_s) point per sample, averaged across every junction."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Metric.sim_time, Metric.mean_waiting_s)
                .where(Metric.run_id == run_id)
                .order_by(Metric.sim_time)
            )
        ).all()

    by_time: dict[float, list[float]] = {}
    for sim_time, mean_waiting_s in rows:
        if mean_waiting_s is None:
            continue
        by_time.setdefault(sim_time, []).append(mean_waiting_s)

    times = sorted(by_time)
    values = [fmean(by_time[t]) for t in times]
    return times, values


def rolling_mean(times: list[float], values: list[float], window_s: float) -> list[float]:
    """Centered rolling mean over a window measured in sim SECONDS (not sample
    count) -- stays correct even if the sampling interval ever changes."""
    half = window_s / 2
    smoothed = []
    for t in times:
        lo, hi = t - half, t + half
        window_values = [v for tt, v in zip(times, values) if lo <= tt <= hi]
        smoothed.append(fmean(window_values))
    return smoothed


def load_incidents(scenario: str) -> list[Incident]:
    incidents_path = NETWORKS_DIR / scenario / "incidents.yaml"
    return load_incident_schedule(incidents_path) if incidents_path.exists() else []


def plot(
    *,
    run_id: uuid.UUID,
    scenario: str,
    times: list[float],
    values: list[float],
    smoothed: list[float],
    smooth_window_s: float,
    incidents: list[Incident],
    out_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(times, values, color="tab:blue", alpha=0.3, linewidth=1, label="raw (10s sample, network mean)")
    ax.plot(times, smoothed, color="tab:blue", linewidth=2, label=f"smoothed ({smooth_window_s:.0f}s window)")

    for i, incident in enumerate(incidents):
        ax.axvspan(
            incident.begin,
            incident.begin + incident.duration,
            color="tab:red",
            alpha=0.15,
            label="incident" if i == 0 else None,
        )

    ax.set_xlabel("sim time (s)")
    ax.set_ylabel("mean waiting time (s), averaged over all junctions")
    ax.set_title(f"{scenario} -- run {run_id}")
    ax.legend()
    fig.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True, help="e.g. grid_4x4 (a folder under networks/)")
    parser.add_argument("--run-id", default=None, help="defaults to the latest run for --scenario")
    parser.add_argument("--out", default=None, help="defaults to data/plots/<scenario>_<run_id>.png")
    parser.add_argument("--smooth-s", type=float, default=DEFAULT_SMOOTH_WINDOW_S)
    args = parser.parse_args()

    engine = make_async_engine()
    session_factory = make_session_factory(engine)

    async def _load() -> tuple[uuid.UUID, list[float], list[float]]:
        run_id = await resolve_run_id(session_factory, args.scenario, args.run_id)
        times, values = await fetch_network_mean_waiting(session_factory, run_id)
        await engine.dispose()
        return run_id, times, values

    run_id, times, values = asyncio.run(_load())
    if not times:
        raise SystemExit(f"run_id={run_id} has no metrics rows -- did sim.runner finish?")

    smoothed = rolling_mean(times, values, args.smooth_s)
    incidents = load_incidents(args.scenario)

    out_path = Path(args.out) if args.out else Path("data/plots") / f"{args.scenario}_{run_id}.png"
    plot(
        run_id=run_id,
        scenario=args.scenario,
        times=times,
        values=values,
        smoothed=smoothed,
        smooth_window_s=args.smooth_s,
        incidents=incidents,
        out_path=out_path,
    )
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
