"""Database models. SQLite by default; any SQLAlchemy URL (e.g. PostgreSQL) works."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        TypeDecorator, create_engine, event, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores timezone-aware UTC datetimes; always returns them aware."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; use timezone-aware UTC")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Base(DeclarativeBase):
    pass


class Event(Base):
    """One real-world story, possibly reported by several outlets."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    headline: Mapped[str] = mapped_column(Text)
    first_published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    last_published_at: Mapped[datetime] = mapped_column(UTCDateTime)
    publishers: Mapped[list] = mapped_column(JSON, default=list)
    n_independent: Mapped[int] = mapped_column(Integer, default=1)
    # pending | done | stale | filtered | budget | failed
    analysis_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_name: Mapped[str] = mapped_column(String(64))
    publisher: Mapped[str] = mapped_column(String(128), index=True)  # registered domain
    headline: Mapped[str] = mapped_column(Text)
    norm_headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024), unique=True)      # canonical URL
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), index=True)


class Analysis(Base):
    __tablename__ = "analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    engine: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64), default="")
    raw: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Impact(Base):
    """One asset an analysis says the story affects."""

    __tablename__ = "impacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    asset: Mapped[str] = mapped_column(String(64))          # as the analyzer named it
    asset_class: Mapped[str] = mapped_column(String(32), default="")
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # tradable symbol
    direction: Mapped[str] = mapped_column(String(8))       # bullish | bearish | neutral
    confidence: Mapped[float] = mapped_column(Float)
    horizon: Mapped[str] = mapped_column(String(8))         # minutes | hours | days
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Decision(Base):
    """Every decision the bot makes, with its reason (shown on the dashboard)."""

    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    stage: Mapped[str] = mapped_column(String(16))   # ingest | analysis | signal | risk | execution | system
    action: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[Any] = mapped_column(JSON, nullable=True)


class Bracket(Base):
    """A position with its entry, stop-loss and take-profit, from order to close."""

    __tablename__ = "brackets"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))              # long | short
    # pending (entry order working) | open | closed | canceled
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    qty: Mapped[float] = mapped_column(Float)                 # requested, then filled quantity
    limit_price: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float] = mapped_column(Float)
    take_profit_price: Mapped[float] = mapped_column(Float)
    entry_fee: Mapped[float] = mapped_column(Float, default=0.0)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fee: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str] = mapped_column(String(64), default="")
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_order_id: Mapped[str] = mapped_column(String(64), default="")
    stop_order_id: Mapped[str] = mapped_column(String(64), default="")
    tp_order_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    entry_deadline: Mapped[datetime] = mapped_column(UTCDateTime)
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    max_hold_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    horizon: Mapped[str] = mapped_column(String(8), default="hours")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")

    @property
    def is_active(self) -> bool:
        return self.status in ("pending", "open")


class EquitySnapshot(Base):
    __tablename__ = "equity"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    equity: Mapped[float] = mapped_column(Float)
    exposure: Mapped[float] = mapped_column(Float, default=0.0)


class KV(Base):
    """Small persistent state: risk day, halts, kill switch, LLM call budget."""

    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
            from pathlib import Path
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(conn, _):
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")  # dashboard can read while the bot writes
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self.Session()

    # ---- small helpers used everywhere ----

    def get_kv(self, key: str, default=None):
        with self.session() as s:
            row = s.get(KV, key)
            return row.value if row else default

    def set_kv(self, key: str, value) -> None:
        with self.session() as s:
            row = s.get(KV, key)
            if row:
                row.value = value
            else:
                s.add(KV(key=key, value=value))
            s.commit()

    def log_decision(self, stage: str, action: str, reason: str, symbol: str = "", data=None) -> None:
        with self.session() as s:
            s.add(Decision(stage=stage, action=action, symbol=symbol, reason=reason, data=data))
            s.commit()

    def active_brackets(self) -> list[Bracket]:
        with self.session() as s:
            return list(s.scalars(select(Bracket).where(Bracket.status.in_(("pending", "open")))))

    def save(self, *objs) -> None:
        with self.session() as s:
            for o in objs:
                s.merge(o)
            s.commit()
