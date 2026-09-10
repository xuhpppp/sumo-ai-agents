"""SQLAlchemy 2.x ORM models for the observability schema.

Mirrors IMPLEMENTATION_PLAN.md section 6.1: `runs`, `messages`, `decisions`,
`llm_calls`, `metrics`. Column types use a small cross-dialect layer (GUID,
JSONType, StringArray below) so the exact same models run against both
PostgreSQL (production, via Docker) and SQLite in-memory (tests/conftest.py)
-- see STEPS.md Step 4 DoD: "pytest runs against SQLite without needing
Docker". PostgreSQL-only features (JSONB, native ARRAY, native UUID) fall
back to a SQLite-compatible equivalent purely for the test dialect; nothing
about the production schema changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    func,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class GUID(TypeDecorator):
    """Platform-independent UUID type.

    Uses PostgreSQL's native UUID type when available; otherwise stores the
    value as a 32-char hex string (CHAR(32)) for SQLite. This is the
    standard SQLAlchemy pattern for testing UUID-keyed models against
    SQLite -- the Python-side type is always `uuid.UUID`, only the storage
    representation differs.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.UUID())
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return str(value)
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return value.hex

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


# JSON payloads: JSONB on PostgreSQL (indexable with GIN), plain JSON on
# SQLite (tests only -- SQLite has no JSONB). Both round-trip as plain
# Python dicts.
JSONType = postgresql.JSONB().with_variant(sqlite.JSON(), "sqlite")

# TEXT[] on PostgreSQL, JSON array on SQLite (tests only). Both round-trip
# as list[str] on the Python side.
StringArray = postgresql.ARRAY(String).with_variant(sqlite.JSON(), "sqlite")

# Autoincrementing BIGSERIAL-equivalent primary key. SQLite only auto-assigns
# rowids for a column that resolves EXACTLY to "INTEGER PRIMARY KEY" -- a
# BigInteger primary key compiles to SQLite's BIGINT, which is NOT treated
# as the rowid alias, so autoincrement silently stops working (surfaces as
# a NOT NULL constraint failure on insert). PostgreSQL has no such
# restriction (BigInteger + autoincrement -> BIGSERIAL/IDENTITY as expected).
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class Run(Base):
    """One simulation run -- one row per `sumo`/`SimRunner` execution.

    `mode` distinguishes the controller under test: 'llm' | 'fixed' |
    'actuated' | 'maxpressure' (see plan section 7, the baseline comparison).
    """

    __tablename__ = "runs"

    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    scenario: Mapped[str] = mapped_column(String, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    git_sha: Mapped[str | None] = mapped_column(String)
    config: Mapped[dict] = mapped_column(JSONType, nullable=False)


class Message(Base):
    """One agent-to-agent message within a decision cycle (plan section 6.1).

    `payload` is the machine-readable part; `rationale` is the free-text
    part shown on the dashboard chat panel (plan section 6.2).
    """

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_run_cycle", "run_id", "cycle_id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    sim_time: Mapped[float] = mapped_column(Float, nullable=False)
    cycle_id: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    sender: Mapped[str] = mapped_column(String, nullable=False)
    recipients: Mapped[list[str]] = mapped_column(StringArray, nullable=False)
    intent: Mapped[str] = mapped_column(String, nullable=False)  # report|request_help|propose|ack|object
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)


class Decision(Base):
    """One controller decision for one junction in one cycle (plan section 6.1).

    `validator_status` records the deterministic safety layer's verdict
    (STEPS.md Step 7) BEFORE the supervisor ever sees the proposal --
    `supervisor_verdict` is only set for proposals that passed validation.
    """

    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_run_junction", "run_id", "junction_id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    sim_time: Mapped[float] = mapped_column(Float, nullable=False)
    cycle_id: Mapped[int] = mapped_column(Integer, nullable=False)
    junction_id: Mapped[str] = mapped_column(String, nullable=False)
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    params: Mapped[dict] = mapped_column(JSONType, nullable=False)
    validator_status: Mapped[str] = mapped_column(String, nullable=False)  # ok|clamped|rejected
    validator_violations: Mapped[dict | None] = mapped_column(JSONType)
    supervisor_verdict: Mapped[str | None] = mapped_column(String)  # approved|modified|denied
    supervisor_reason: Mapped[str | None] = mapped_column(Text)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    effect: Mapped[dict | None] = mapped_column(JSONType)


class LlmCall(Base):
    """One LLM API call, for cost/latency/cache monitoring (plan section 6.1, 10).

    Written by the single call site in `agents/llm.py` (STEPS.md Step 10) --
    every request, success or failure, gets exactly one row here.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_run_role", "run_id", "role"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    sim_time: Mapped[float | None] = mapped_column(Float)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # junction|supervisor|scenario
    model: Mapped[str] = mapped_column(String, nullable=False)
    effort: Mapped[str | None] = mapped_column(String)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))


class Metric(Base):
    """One traffic-metric sample for one junction at one point in sim time.

    Sampled every 10 simulated seconds per junction (plan section 6.1) --
    high volume relative to the other tables, which is why Store batches
    these inserts instead of committing one row at a time (STEPS.md Step 4).
    """

    __tablename__ = "metrics"

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    sim_time: Mapped[float] = mapped_column(Float, primary_key=True)
    junction_id: Mapped[str] = mapped_column(String, primary_key=True)
    mean_waiting_s: Mapped[float | None] = mapped_column(Float)
    queue_len: Mapped[int | None] = mapped_column(Integer)
    throughput: Mapped[int | None] = mapped_column(Integer)
    mean_speed: Mapped[float | None] = mapped_column(Float)
    co2_mg: Mapped[float | None] = mapped_column(Float)
