"""Aggregate polling health without displaying account information."""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from coursewatch.config import settings
from coursewatch.db import engine
from coursewatch.models import Course, Watch, now


def polling_status(database_engine=None):
    timestamp = now()
    with Session(database_engine or engine()) as db:
        courses = db.scalars(
            select(Course).where(select(Watch.id).where(Watch.course_id == Course.id).exists())
        ).all()
        due = [c for c in courses if c.next_check_at <= timestamp]
        return {
            "watched_courses": len(courses),
            "due_courses": len(due),
            "oldest_due_seconds": round(
                max(((timestamp - c.next_check_at).total_seconds() for c in due), default=0), 1
            ),
            "active_leases": sum(
                c.lease_expires_at is not None and c.lease_expires_at > timestamp for c in courses
            ),
            "fresh_observations": sum(
                c.observed_at is not None
                and (timestamp - c.observed_at).total_seconds() <= settings().poll_interval_seconds
                for c in courses
            ),
            "unpublished_courses": sum(
                c.state in {"term_unpublished", "course_unpublished"} for c in courses
            ),
            "failed_courses": sum(c.state == "error" for c in courses),
            "max_last_check_seconds": max((c.last_check_seconds or 0 for c in courses), default=0),
            "last_check_requests_total": sum(c.last_request_count or 0 for c in courses),
            "poll_concurrency": settings().poll_concurrency,
            "global_request_interval_seconds": settings().spire_request_interval_seconds,
        }


def main():
    print(json.dumps(polling_status(), indent=2))


if __name__ == "__main__":
    main()
