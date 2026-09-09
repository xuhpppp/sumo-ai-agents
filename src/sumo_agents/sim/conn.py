"""Thin wrapper around `traci` / `libsumo`.

Why this wrapper exists (see IMPLEMENTATION_PLAN.md §1.1 and §10, STEPS.md Step 2):

- `traci`   : connects to a child `sumo`/`sumo-gui` process over a socket.
              Slower, but supports the GUI and multiple simultaneous
              connections (labels).
- `libsumo` : embeds SUMO directly in the current Python process (no
              socket). ~3-10x faster, but has NO GUI support and only runs
              one simulation per process.

Verified on this machine (eclipse-sumo / libsumo / traci 1.27.1): both
modules share the same module-level signatures — `start(cmd, ...)`,
`simulationStep(...)`, `close()` — and the same domain submodules
(`vehicle`, `trafficlight`, `lane`, `edge`, `junction`, `simulation`, ...).
So a thin attribute-forwarding facade is enough; no need to re-wrap each
individual API.

Backend selection is a config flag (the `backend=` argument or the
`SUMO_BACKEND=traci|libsumo` environment variable), NOT a hard import of
either module at module load time — the whole point is to switch backends
without touching call sites.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

import sumolib


class Backend(str, Enum):
    """The two available TraCI backends. See module docstring for tradeoffs."""

    TRACI = "traci"
    LIBSUMO = "libsumo"


class SumoConnectionError(RuntimeError):
    """Error related to the SUMO connection lifecycle (bad start/close order, etc.)."""


def _resolve_backend(backend: Backend | str | None, *, gui: bool) -> Backend:
    """Resolve the final backend from the argument, env var, and GUI constraint.

    Priority order: explicit argument > SUMO_BACKEND env var > default.
    Default is libsumo (faster) when no GUI is needed, traci when GUI is needed.
    """
    if backend is None:
        env_value = os.environ.get("SUMO_BACKEND")
        backend = Backend(env_value) if env_value else (Backend.TRACI if gui else Backend.LIBSUMO)
    else:
        backend = Backend(backend)

    if gui and backend is Backend.LIBSUMO:
        raise ValueError(
            "libsumo does not support the GUI. Use backend=Backend.TRACI "
            "(or drop gui=True)."
        )
    return backend


def _import_backend(backend: Backend) -> ModuleType:
    """Import the backend module lazily — avoids importing both when only one
    is needed, and lets a missing-package error (if any) surface only when
    actually required.
    """
    if backend is Backend.TRACI:
        import traci

        return traci
    import libsumo

    return libsumo


class SumoConnection:
    """Facade for a single SUMO connection that runs on both traci and libsumo.

    Use as a context manager to guarantee `close()` always runs, even if an
    exception is raised in between:

        with SumoConnection(backend="libsumo") as conn:
            conn.start(sumo_cfg="networks/grid_4x4/sim.sumocfg", seed=42)
            while conn.step_count < 3600:
                conn.simulation_step()
                ids = conn.vehicle.getIDList()

    Every domain API (`vehicle`, `trafficlight`, `lane`, `edge`, `junction`,
    `simulation`, ...) is forwarded verbatim to the backend module via
    `__getattr__` — look up TraCI docs as usual, no abstraction layer hides
    parameters or return values in between.
    """

    def __init__(
        self,
        *,
        backend: Backend | str | None = None,
        gui: bool = False,
        label: str = "default",
    ) -> None:
        self._backend = _resolve_backend(backend, gui=gui)
        self._gui = gui
        self._label = label
        self._module: ModuleType | None = None
        self._started = False
        self._step_count = 0

    # ------------------------------------------------------------------ #
    # Read-only properties
    # ------------------------------------------------------------------ #

    @property
    def backend(self) -> Backend:
        return self._backend

    @property
    def is_gui(self) -> bool:
        return self._gui

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def step_count(self) -> int:
        """Number of times `simulation_step()` has been called since `start()`."""
        return self._step_count

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(
        self,
        sumo_cfg: str | Path,
        *,
        extra_args: Sequence[str] = (),
        seed: int | None = None,
        step_length: float | None = None,
        port: int | None = None,
    ) -> None:
        """Start SUMO and open the connection.

        `sumo_cfg`: path to a `.sumocfg` file.
        `extra_args`: extra SUMO command-line flags, e.g. `["--no-warnings"]`.
        `seed`: sets `--seed` — MUST be passed explicitly everywhere results
                need to be reproducible (see plan §7 on baseline comparisons
                run with the same seed).
        `step_length`: sets `--step-length` (simulated seconds per step);
                SUMO defaults to 1.0 if not given.
        `port`: fixed TCP port for traci; ignored for libsumo (no socket used).
        """
        if self._started:
            raise SumoConnectionError(
                f"Connection '{self._label}' is already started — call close() "
                "before starting again."
            )

        binary = sumolib.checkBinary("sumo-gui" if self._gui else "sumo")
        cmd = [binary, "-c", str(sumo_cfg), *extra_args]
        if seed is not None:
            cmd += ["--seed", str(seed)]
        if step_length is not None:
            cmd += ["--step-length", str(step_length)]

        module = _import_backend(self._backend)
        start_kwargs: dict[str, Any] = {"label": self._label}
        # libsumo.start() accepts the same signature but silently ignores
        # traci-only arguments (port/label) on some builds — still pass them
        # to traci; libsumo doesn't use a socket so port is meaningless to it.
        if self._backend is Backend.TRACI and port is not None:
            start_kwargs["port"] = port

        module.start(cmd, **start_kwargs)
        self._module = module
        self._started = True
        self._step_count = 0

    def close(self) -> None:
        """Close the connection. Safe to call multiple times (later calls are no-ops)."""
        if not self._started or self._module is None:
            return
        try:
            self._module.close()
        finally:
            self._module = None
            self._started = False

    def __enter__(self) -> SumoConnection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Simulation loop
    # ------------------------------------------------------------------ #

    def simulation_step(self, until: float = 0) -> None:
        """Advance one simulation step (or run up to time `until` if > 0).

        See STEPS.md Step 5 / plan §3.2: this is a CHEAP call, invoked every
        step in the sim loop — don't confuse it with more expensive TraCI
        calls (like querying the full vehicle list) that aren't needed every
        step.
        """
        self._require_started()
        self._module.simulationStep(until)  # type: ignore[union-attr]
        self._step_count += 1

    # ------------------------------------------------------------------ #
    # Forward domain API (vehicle, trafficlight, lane, edge, junction, ...)
    # ------------------------------------------------------------------ #

    def __getattr__(self, name: str) -> Any:
        """Forward any attribute not defined here to the backend module.

        Enables calls like `conn.vehicle.getIDList()`,
        `conn.trafficlight.setPhase(...)`, etc., exactly as if calling
        `traci.vehicle...` / `libsumo.vehicle...` directly. Only triggers
        when the attribute isn't found normally (i.e. doesn't shadow the
        properties/methods defined above).
        """
        module = self.__dict__.get("_module")
        if module is None:
            raise SumoConnectionError(
                f"Connection '{self._label}' is not started — no '{name}' to use."
            )
        return getattr(module, name)

    def _require_started(self) -> None:
        if not self._started or self._module is None:
            raise SumoConnectionError(f"Connection '{self._label}' is not started().")

    def __repr__(self) -> str:
        state = "started" if self._started else "closed"
        gui = ", gui" if self._gui else ""
        return f"SumoConnection(backend={self._backend.value}{gui}, {state}, steps={self._step_count})"
