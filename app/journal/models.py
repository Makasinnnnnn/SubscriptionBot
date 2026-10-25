from sqlalchemy import JSON, BigInteger, Boolean, Float, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Mutex(Base):
    __tablename__ = "journal_mutex"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Account(Base):
    __tablename__ = "journal_accounts"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class Slot(Base):
    __tablename__ = "journal_slots"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    baseline: Mapped[dict] = mapped_column(JSON)


class Trade(Base):
    __tablename__ = "journal_trades"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    author: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    synced_version: Mapped[int] = mapped_column(Integer, default=0)
    baseline: Mapped[dict] = mapped_column(JSON, default=dict)
    synced_at: Mapped[str | None] = mapped_column(String(50), nullable=True)


class Draft(Base):
    __tablename__ = "journal_drafts"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class Event(Base):
    __tablename__ = "journal_events"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    response: Mapped[dict] = mapped_column(JSON)


class Job(Base):
    __tablename__ = "journal_jobs"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_at: Mapped[float] = mapped_column(Float, default=0, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Screenshot(Base):
    __tablename__ = "journal_screenshots"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(40), index=True)
    telegram_file: Mapped[str] = mapped_column(String(300))
    filename: Mapped[str] = mapped_column(String(180))
    mime: Mapped[str] = mapped_column(String(60))
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    captured: Mapped[str] = mapped_column(String(50))
    comment: Mapped[str] = mapped_column(Text, default="")
    drive_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    drive_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sheet_row: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Audit(Base):
    __tablename__ = "journal_audit"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(40))
    trade_id: Mapped[str] = mapped_column(String(40))
    at: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON)
