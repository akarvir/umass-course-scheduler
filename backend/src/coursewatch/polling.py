"""Short database claims and request pacing shared by every collector."""

import hashlib
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from coursewatch.config import settings
from coursewatch.models import Course, PollingBudget, Watch, now


class LeaseLost(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    course_id: int
    token: str
    term: str
    subject: str
    catalog_number: str
    due_at: datetime
    snapshot: dict | None


def claim_course(database_engine):
    with Session(database_engine) as db, db.begin():
        timestamp = db.scalar(select(func.clock_timestamp()))
        course = db.scalar(
            select(Course)
            .where(
                Course.next_check_at <= timestamp,
                or_(Course.lease_expires_at.is_(None), Course.lease_expires_at <= timestamp),
                select(Watch.id).where(Watch.course_id == Course.id).exists(),
            )
            .order_by(Course.next_check_at, Course.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if course is None:
            return None
        course.lease_token = uuid4().hex
        course.lease_expires_at = timestamp + timedelta(seconds=settings().poll_lease_seconds)
        return Claim(
            course.id,
            course.lease_token,
            course.term,
            course.subject,
            course.catalog_number,
            course.next_check_at,
            course.snapshot,
        )


def renew_lease(database_engine, claim):
    with database_engine.begin() as conn:
        timestamp = conn.scalar(select(func.clock_timestamp()))
        result = conn.execute(
            update(Course)
            .where(
                Course.id == claim.course_id,
                Course.lease_token == claim.token,
                Course.lease_expires_at > timestamp,
            )
            .values(lease_expires_at=timestamp + timedelta(seconds=settings().poll_lease_seconds))
        )
        return result.rowcount == 1


class Heartbeat:
    def __init__(self, database_engine, claim):
        self.database_engine, self.claim = database_engine, claim
        self.done = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.done.wait(settings().poll_lease_seconds / 3):
            try:
                if not renew_lease(self.database_engine, self.claim):
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.done.set()
        self.thread.join(timeout=5)


def wait_for_request(database_engine, cancelled):
    # Reserve one start at a time; no database transaction is held during HTTP or sleep.
    while not cancelled():
        with Session(database_engine) as db, db.begin():
            db.execute(
                insert(PollingBudget)
                .values(id=1, next_request_at=now())
                .on_conflict_do_nothing(index_elements=[PollingBudget.id])
            )
            budget = db.scalar(select(PollingBudget).where(PollingBudget.id == 1).with_for_update())
            timestamp = db.scalar(select(func.clock_timestamp()))
            delay = (budget.next_request_at - timestamp).total_seconds()
            if delay <= 0:
                budget.next_request_at = timestamp + timedelta(
                    seconds=settings().spire_request_interval_seconds
                )
                return
        threading.Event().wait(min(delay, 0.5))
    raise LeaseLost("Collector stopped or lost ownership")


def next_due(claim, completed_at):
    interval = settings().poll_interval_seconds
    key = f"{claim.term}:{claim.subject}:{claim.catalog_number}".encode()
    offset = int.from_bytes(hashlib.sha256(key).digest()[:4], "big") % interval
    seconds = completed_at.timestamp()
    next_tick = (int((seconds - offset) // interval) + 1) * interval + offset
    return datetime.fromtimestamp(next_tick, UTC)
