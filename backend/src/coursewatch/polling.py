"""Short database claims and request pacing shared by every collector."""

import hashlib
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from coursewatch.config import settings
from coursewatch.leases import LeaseLost
from coursewatch.models import PollingBudget, now


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
