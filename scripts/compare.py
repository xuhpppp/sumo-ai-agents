"""Baseline comparison harness (STEPS.md Step 9).

Runs every (mode, seed) combination -- same scenario, same seeded incident
schedule (networks/<scenario>/incidents.yaml) -- through sim/runner.py's
`run()` directly (in-process, sequentially: libsumo only supports one
simulation per process at a time), then aggregates each mode's results
across seeds into one markdown table, mean +/- an approximate 95% CI.

The CI is a normal approximation (1.96 * sample_stdev / sqrt(n)), not an
exact Student-t interval -- with the default of 3 seeds that's a rough
interval, not a precise one, and is labelled as such in the table. len(n)<2
prints the mean with no CI (need at least 2 seeds to estimate spread).

Per-run metrics:
  - mean_waiting_s, queue_p95, mean_co2_mg: aggregated from the `metrics`
    table (STEPS.md Step 5), network-wide (across all junctions/samples).
  - mean_travel_time_s, n_completed_trips ("throughput"): NOT derivable
    from `metrics` (they're whole-run, not per-junction) -- read from
    `runs.summary`, written once by sim/runner.py via TraCI vehicle
    depart/arrive events (STEPS.md Step 9).

Usage:
    python scripts/compare.py --scenario grid_4x4
    python scripts/compare.py --scenario grid_4x4 --modes fixed,actuated,maxpressure --seeds 42,43,44
    python scripts/compare.py --scenario grid_4x4 --out data/compare/grid_4x4.md --no-plot
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from sumo_agents.obs.db import make_async_engine, make_session_factory  # noqa: E402
from sumo_agents.obs.models import Metric, Run  # noqa: E402
from sumo_agents.sim.runner import run as run_sim  # noqa: E402

DEFAULT_MODES = ["fixed", "actuated", "maxpressure"]
DEFAULT_SEEDS = [42, 43, 44]

# (field on the per-run result dict, column label, lower_is_better)
METRICS: list[tuple[str, str, bool]] = [
    ("mean_waiting_s", "Mean waiting (s)", True),
    ("mean_travel_time_s", "Mean travel time (s)", True),
    ("n_completed_trips", "Throughput (veh completed)", False),
    ("queue_p95", "Queue length p95", True),
    ("mean_co2_mg", "Mean CO2 rate (mg/s)", True),
]


async def run_one(scenario: str, mode: str, seed: int) -> uuid.UUID:
    print(f"--- running scenario={scenario} mode={mode} seed={seed} ---")
    return await run_sim(scenario=scenario, mode=mode, seed=seed)


async def fetch_run_result(session_factory: async_sessionmaker, run_id: uuid.UUID) -> dict[str, float | None]:
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        mean_waiting_s, queue_p95, mean_co2_mg = (
            await session.execute(
                select(
                    func.avg(Metric.mean_waiting_s),
                    func.percentile_cont(0.95).within_group(Metric.queue_len.asc()),
                    func.avg(Metric.co2_mg),
                ).where(Metric.run_id == run_id)
            )
        ).one()

    summary = run.summary or {}
    return {
        "mean_waiting_s": mean_waiting_s,
        "queue_p95": queue_p95,
        "mean_co2_mg": mean_co2_mg,
        "mean_travel_time_s": summary.get("mean_travel_time_s"),
        "n_completed_trips": summary.get("n_completed_trips"),
    }


def mean_and_ci95(values: list[float]) -> tuple[float, float | None]:
    """Mean and an approximate 95% CI half-width (normal approximation --
    see module docstring). None for the CI when there's fewer than 2
    samples (can't estimate spread from one point)."""
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, None
    sem = statistics.stdev(values) / (len(values) ** 0.5)
    return mean, 1.96 * sem


def format_cell(mean: float | None, ci: float | None, *, precision: int) -> str:
    if mean is None:
        return "n/a"
    if ci is None:
        return f"{mean:.{precision}f}"
    return f"{mean:.{precision}f} ± {ci:.{precision}f}"


def build_markdown_table(aggregated: dict[str, dict[str, tuple[float | None, float | None]]], seeds: list[int]) -> str:
    modes = list(aggregated)
    lines = [
        f"# Baseline comparison ({len(modes)} modes x {len(seeds)} seeds: {seeds})",
        "",
        "CI is an approximate 95% interval (normal approximation, 1.96 * stdev/sqrt(n)) "
        f"across the {len(seeds)} seeds -- rough with this few samples, not an exact interval.",
        "",
        "| mode | " + " | ".join(label for _, label, _ in METRICS) + " |",
        "|---|" + "---|" * len(METRICS),
    ]
    for mode in modes:
        cells = [format_cell(*aggregated[mode][key], precision=1) for key, _, _ in METRICS]
        lines.append(f"| {mode} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def plot_comparison(
    aggregated: dict[str, dict[str, tuple[float | None, float | None]]], seeds: list[int], out_path: Path
) -> None:
    modes = list(aggregated)
    fig, axes = plt.subplots(1, len(METRICS), figsize=(4 * len(METRICS), 4.5))
    colors = plt.cm.tab10.colors

    for ax, (key, label, _) in zip(axes, METRICS, strict=True):
        means = [aggregated[mode][key][0] for mode in modes]
        errs = [aggregated[mode][key][1] or 0.0 for mode in modes]
        bar_colors = [colors[i % len(colors)] for i in range(len(modes))]
        ax.bar(modes, [m if m is not None else 0.0 for m in means], yerr=errs, capsize=4, color=bar_colors)
        ax.set_title(label, fontsize=10)
        ax.tick_params(axis="x", rotation=30)

    fig.suptitle(f"Baseline comparison -- {len(seeds)} seeds ({seeds}), 95% CI (normal approx.)")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)


async def main_async(scenario: str, modes: list[str], seeds: list[int]) -> dict[str, dict[str, tuple[float | None, float | None]]]:
    engine = make_async_engine()
    session_factory = make_session_factory(engine)

    per_mode_results: dict[str, list[dict[str, float | None]]] = {mode: [] for mode in modes}
    for mode in modes:
        for seed in seeds:
            run_id = await run_one(scenario, mode, seed)
            per_mode_results[mode].append(await fetch_run_result(session_factory, run_id))

    aggregated: dict[str, dict[str, tuple[float | None, float | None]]] = {}
    for mode, results in per_mode_results.items():
        aggregated[mode] = {}
        for key, _, _ in METRICS:
            values = [r[key] for r in results if r[key] is not None]
            aggregated[mode][key] = mean_and_ci95(values) if values else (None, None)

    await engine.dispose()
    return aggregated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", required=True, help="e.g. grid_4x4 (a folder under networks/)")
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES), help="comma-separated, e.g. fixed,actuated")
    parser.add_argument("--seeds", default=",".join(str(s) for s in DEFAULT_SEEDS), help="comma-separated ints")
    parser.add_argument("--out", default=None, help="markdown output path (default data/compare/<scenario>.md)")
    parser.add_argument("--no-plot", action="store_true", help="skip the PNG bar-chart comparison")
    parser.add_argument("--plot-out", default=None, help="PNG output path (default data/plots/compare_<scenario>.png)")
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]

    aggregated = asyncio.run(main_async(args.scenario, modes, seeds))

    table = build_markdown_table(aggregated, seeds)
    print()
    print(table)

    out_path = Path(args.out) if args.out else Path("data/compare") / f"{args.scenario}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(table + "\n")
    print(f"\nwrote {out_path}")

    if not args.no_plot:
        plot_out = Path(args.plot_out) if args.plot_out else Path("data/plots") / f"compare_{args.scenario}.png"
        plot_comparison(aggregated, seeds, plot_out)
        print(f"wrote {plot_out}")


if __name__ == "__main__":
    main()
