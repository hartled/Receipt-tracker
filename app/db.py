from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (Date, DateTime, ForeignKey, Integer, String, Text,
                        create_engine, event, inspect, text)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    inbound_alias: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    plan: Mapped[str] = mapped_column(String(20), default="free", server_default="free")
    # Bumped on password reset so every existing session cookie stops working.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Monthly usage counter for the free-tier limit. Counts receipts *created* in usage_month ("YYYY-MM", UTC);
    # deleting a receipt does not give the slot back.
    usage_month: Mapped[str] = mapped_column(String(7), default="", server_default="")
    usage_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    receipts: Mapped[list["Receipt"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True)


class Receipt(Base):
    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    stored_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vendor: Mapped[str] = mapped_column(String(255), default="")
    date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(50), default="uncategorized", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    raw_text: Mapped[str] = mapped_column(Text, default="")
    extraction_source: Mapped[str] = mapped_column(String(50), default="manual")
    needs_review: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(back_populates="receipts")

    @property
    def amount(self) -> float:
        return (self.amount_cents or 0) / 100


class ThrottleEvent(Base):
    """One row per throttled event, e.g. a failed login ("login-email:<addr>", "login-ip:<addr>")
    or a password-reset request ("reset-email:...", "reset-ip:...")."""
    __tablename__ = "auth_throttle"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(300), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PasswordResetToken(Base):
    """Only the SHA-256 hash of the token is stored. Tokens are single-use and expire."""
    __tablename__ = "password_reset_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Columns added after the first release. create_all() doesn't alter existing tables, so add them here.
_USER_COLUMNS = {
    "plan": "VARCHAR(20) NOT NULL DEFAULT 'free'",
    "session_epoch": "INTEGER NOT NULL DEFAULT 0",
    "usage_month": "VARCHAR(7) NOT NULL DEFAULT ''",
    "usage_count": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate(engine) -> None:
    insp = inspect(engine)
    if "users" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("users")}
    with engine.begin() as conn:
        for name, ddl in _USER_COLUMNS.items():
            if name not in have:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat naive datetimes read back from the DB as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def make_sessionmaker(database_url: str):
    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # enforce ON DELETE CASCADE in SQLite
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    _migrate(engine)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)
