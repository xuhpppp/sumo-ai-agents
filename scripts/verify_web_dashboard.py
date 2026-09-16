"""Step 15 smoke check (STEPS.md): exercise the REAL FastAPI app + a REAL
WebSocket client against REAL Postgres -- no SUMO, no OpenAI, $0.

STEPS.md's DoD for this step is "wscat nhận được sự kiện realtime khi sim
đang chạy" (wscat receives realtime events while a sim is running). A short
baseline run (`fixed`/`actuated`) finishes in a few seconds of *wall* time
(libsumo runs ~1000x faster than real time), which is too fast to reliably
attach a WebSocket client mid-run -- only `--mode llm` stays open for real
wall-clock minutes (STEPS.md Step 14's `LLM_REALTIME_SPEEDUP` throttle), and
that costs real OpenAI money to run, so it is not something to spend on a
routine smoke check.

Instead, this script proves the actual mechanism end to end against real
infrastructure: it starts uvicorn in a background thread, opens a real
`websockets` client connection to `/ws/runs/<run_id>`, and then writes
messages/decisions/metrics through the exact same `Store` class the real
`sim/runner.py` uses -- confirming each write shows up over the socket
within a couple of seconds, the REST history endpoints agree, and the
socket gets a `run_finished` event and closes once the run is marked done.
Unit tests (`tests/test_web.py`) cover the same logic against in-memory
SQLite; this script is the one thing they can't cover -- a real Postgres
commit from one connection actually becoming visible, live, to another.

Usage:
    python scripts/verify_web_dashboard.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx2 as httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from sumo_agents.obs.db import make_async_engine, make_session_factory  # noqa: E402
from sumo_agents.obs.models import Run  # noqa: E402
from sumo_agents.obs.store import Store  # noqa: E402
from sumo_agents.web.app import app  # noqa: E402

HOST = "127.0.0.1"
PORT = 8931  # off the default 8000 so this never collides with a dev server
RECV_TIMEOUT_S = 5.0


def _start_server() -> uvicorn.Server:
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    return server


async def _wait_until_up(base_url: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(f"{base_url}/health", timeout=1.0)).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.2)
    raise RuntimeError(f"dashboard server did not come up within {timeout_s}s")


async def _expect_event(ws: websockets.WebSocketClientProtocol, event_type: str) -> dict:
    event = json.loads(await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S))
    assert event["type"] == event_type, f"expected a {event_type!r} event, got {event!r}"
    return event


async def main() -> None:
    engine = make_async_engine()
    session_factory = make_session_factory(engine)
    store = Store(session_factory)
    base_url = f"http://{HOST}:{PORT}"
    ws_url = f"ws://{HOST}:{PORT}/ws/runs"

    print(f"[1/5] starting FastAPI app on {base_url} (uvicorn, background thread)...")
    server = _start_server()
    await _wait_until_up(base_url)
    print("      up.")

    print("[2/5] creating a real run row in Postgres...")
    run_id = await store.create_run(scenario="_verify_web", seed=0, mode="llm", config={})
    print(f"      run_id={run_id}")

    try:
        print("[3/5] REST: GET /runs and /runs/{id} agree, unknown id -> 404...")
        async with httpx.AsyncClient(base_url=base_url) as client:
            runs = (await client.get("/runs")).json()
            assert str(run_id) in [r["run_id"] for r in runs], "new run missing from GET /runs"
            one = (await client.get(f"/runs/{run_id}")).json()
            assert one["mode"] == "llm" and one["finished_at"] is None
            assert (await client.get("/runs/00000000-0000-0000-0000-000000000000")).status_code == 404
        print("      OK.")

        print(f"[4/5] WebSocket: connecting to {ws_url}/{run_id}, then writing live rows via Store...")
        async with websockets.connect(f"{ws_url}/{run_id}") as ws:
            await store.add_message(
                run_id=run_id,
                sim_time=90.0,
                cycle_id=1,
                round=2,
                sender="B1",
                recipients=["A1"],
                intent="report",
                payload={},
                rationale="verify_web_dashboard: a message written while a client is connected",
            )
            event = await _expect_event(ws, "messages")
            print(f"      got 'messages' within {RECV_TIMEOUT_S:.0f}s: {event['data'][0]['rationale']!r}")

            await store.add_decision(
                run_id=run_id,
                sim_time=90.0,
                cycle_id=1,
                junction_id="B1",
                action_type="set_green_bounds",
                params={"min_green_s": 20.0, "max_green_s": 60.0},
                validator_status="ok",
                applied=True,
            )
            event = await _expect_event(ws, "decisions")
            print(f"      got 'decisions': junction_id={event['data'][0]['junction_id']!r}")

            await store.add_metric(run_id=run_id, sim_time=90.0, junction_id="B1", queue_len=5)
            await store.close()  # flush the metrics buffer -- see obs/store.py's batching docstring
            event = await _expect_event(ws, "metrics")
            print(f"      got 'metrics': queue_len={event['data'][0]['queue_len']!r}")

            print("[5/5] marking the run finished -- expecting 'run_finished' + a clean server-side close...")
            await store.finish_run(run_id, summary={"note": "verify_web_dashboard smoke check"})
            await _expect_event(ws, "run_finished")
            print("      got 'run_finished'.")

            try:
                await asyncio.wait_for(ws.recv(), timeout=2.0)
            except TimeoutError:
                raise AssertionError("server should have closed the socket right after run_finished") from None
            except websockets.exceptions.ConnectionClosed:
                pass
            print("      socket closed cleanly.")
    finally:
        async with session_factory() as session:
            await session.execute(delete(Run).where(Run.run_id == run_id))
            await session.commit()
        await engine.dispose()
        server.should_exit = True

    print("\nPASS -- REST + WebSocket verified against real Postgres and a real FastAPI/uvicorn server (no SUMO, no OpenAI, $0).")
    print("\nThis is not the same as watching an actual simulation live -- to see that (matching STEPS.md's DoD")
    print("wording exactly), run the dashboard for real in one terminal:")
    print("  uvicorn sumo_agents.web.app:app --reload")
    print("...start a run in another (only `--mode llm` stays open long enough in wall-clock time to watch --")
    print("baseline modes finish in a few seconds):")
    print("  python -m sumo_agents.sim.runner --scenario grid_4x4 --mode llm --seed 42")
    print("...and once you have that run's run_id (e.g. from `GET /runs`), in a third terminal:")
    print("  wscat -c ws://127.0.0.1:8000/ws/runs/<run_id>")


if __name__ == "__main__":
    asyncio.run(main())
