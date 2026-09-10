"""Database engine / session factories.

Reads `DATABASE_URL` from the environment (see `.env.example`). A single
`postgresql+psycopg://user:pass@host:port/dbname` URL works for both the
sync engine (used by Alembic migrations, which run synchronously) and the
async engine (used by `Store` at runtime) -- psycopg 3 supports both a sync
and an async API under the same SQLAlchemy dialect name, so there is no
separate "async URL" to maintain.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def database_url() -> str:
    """Read DATABASE_URL from the environment, failing loudly if unset.

    Loads `.env` (via python-dotenv) first, without overriding any variable
    already set in the environment -- so an explicit `export DATABASE_URL=...`
    always wins over the file. Deliberately does not fall back to a
    hardcoded default: a silent fallback to e.g. localhost with a guessed
    password is exactly the kind of thing that quietly points a run at the
    wrong database.
    """
    load_dotenv(override=False)
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Add it to your .env "
            "(see .env.example), e.g.:\n"
            "  DATABASE_URL=postgresql+psycopg://postgres:<password>@localhost:5432/sumo"
        )
    return url


def make_async_engine(*, echo: bool = False) -> AsyncEngine:
    """Async engine for application code (Store, SimRunner, dashboard)."""
    return create_async_engine(database_url(), echo=echo)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def make_sync_engine(*, echo: bool = False) -> Engine:
    """Sync engine -- used ONLY by Alembic (migrations run synchronously)."""
    return create_engine(database_url(), echo=echo)
