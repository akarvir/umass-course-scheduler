"""Own the lifecycle of a course claim and fence stale completion attempts."""

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from coursewatch.config import settings
from coursewatch.models import Course, Watch


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


class LeaseManager:
    def __init__(self, database_engine):
        self.database_engine = database_engine

    def claim(self):
        with Session(self.database_engine) as db, db.begin():
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

    def renew(self, claim):
        with self.database_engine.begin() as conn:
            timestamp = conn.scalar(select(func.clock_timestamp()))
            result = conn.execute(
                update(Course)
                .where(
                    Course.id == claim.course_id,
                    Course.lease_token == claim.token,
                    Course.lease_expires_at > timestamp,
                )
                .values(
                    lease_expires_at=timestamp + timedelta(seconds=settings().poll_lease_seconds)
                )
            )
            return result.rowcount == 1

    def heartbeat(self, claim):
        return Heartbeat(self, claim)

    def complete(self, claim, save_result):
        """Validate ownership and run the result callback in one locked transaction."""
        with Session(self.database_engine) as db, db.begin():
            course = db.scalar(select(Course).where(Course.id == claim.course_id).with_for_update())
            timestamp = db.scalar(select(func.clock_timestamp()))
            if (
                course is None
                or course.lease_token != claim.token
                or course.lease_expires_at is None
                or course.lease_expires_at <= timestamp
            ):
                return False
            save_result(db, course, timestamp)
            course.lease_token = None
            course.lease_expires_at = None
        return True


class Heartbeat:
    def __init__(self, manager, claim):
        self.manager, self.claim = manager, claim
        self.done = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.done.wait(settings().poll_lease_seconds / 3):
            try:
                if not self.manager.renew(self.claim):
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
