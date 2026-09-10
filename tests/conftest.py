"""Shared pytest fixtures.

Tests run against an in-memory SQLite database instead of a real PostgreSQL
container (STEPS.md Step 4 DoD: "pytest runs on SQLite, no Docker needed").
`StaticPool` is required here: plain in-memory SQLite creates a fresh,
empty database per connection, so without a shared pool each new connection
in the pool would see none of the tables the previous connection created.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from sumo_agents.obs.models import Base
from sumo_agents.obs.store import Store


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine]:
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # SQLite does NOT enforce FK constraints by default -- ON DELETE CASCADE
    # (used by messages/decisions/llm_calls/metrics -> runs) would silently
    # do nothing without this. PostgreSQL enforces FKs unconditionally, so
    # this is purely a SQLite-for-testing concern.
    @event.listens_for(eng.sync_engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
def store(session_factory: async_sessionmaker[AsyncSession]) -> Store:
    return Store(session_factory)
