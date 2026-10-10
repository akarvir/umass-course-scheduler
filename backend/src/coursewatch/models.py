from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from coursewatch.db import Base


def now():
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LoginCode(Base):
    __tablename__ = "login_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), index=True)
    client_hash: Mapped[str] = mapped_column(String(64), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    used: Mapped[bool] = mapped_column(default=False)


class LoginSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("term", "subject", "catalog_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    term: Mapped[str] = mapped_column(String(32), default="2027 Spring")
    subject: Mapped[str] = mapped_column(String(20), default="COMPSCI")
    catalog_number: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(32), default="pending")
    detail: Mapped[str] = mapped_column(default="Waiting for the first check")
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    failures: Mapped[int] = mapped_column(default=0)


class Watch(Base):
    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("user_id", "course_id", "class_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    class_number: Mapped[str] = mapped_column(String(5), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    previous_open: Mapped[list | None] = mapped_column(JSON, nullable=True)
    event_number: Mapped[int] = mapped_column(default=0)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("watch_id", "event_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int | None] = mapped_column(nullable=True)
    event_number: Mapped[int] = mapped_column()
    recipient: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column()
    body: Mapped[str] = mapped_column()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    attempts: Mapped[int] = mapped_column(default=0)
