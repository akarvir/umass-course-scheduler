import argparse
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from coursewatch.availability import open_bundles
from coursewatch.config import settings
from coursewatch.db import engine
from coursewatch.email import send_email
from coursewatch.models import (
    Course,
    Delivery,
    LoginCode,
    LoginSession,
    TermProbe,
    User,
    Watch,
    now,
)
from coursewatch.polling import Heartbeat, LeaseLost, claim_course, next_due, wait_for_request
from coursewatch.spire import CourseUnavailable, SpireClient, TermUnavailable

log = logging.getLogger(__name__)
WORKER_LOCK = 731920271
stop = threading.Event()


def observe(db: Session, course: Course, snapshot: dict):
    timestamp = now()
    course.snapshot = snapshot
    course.observed_at = timestamp
    course.state = "observed"
    course.detail = "Availability checked. Related sections are inferred; verify in SPIRE."
    course.failures = 0
    course.next_check_at = timestamp + timedelta(seconds=settings().poll_interval_seconds)
    sections = {s["class_number"]: s for s in snapshot["sections"]}
    for watch in db.scalars(select(Watch).where(Watch.course_id == course.id).with_for_update()):
        opened = open_bundles(snapshot, watch.class_number)
        keys = [b["key"] for b in opened]
        # The first successful check establishes a baseline. No alert for already-open seats.
        if watch.previous_open is not None:
            new = [b for b in opened if b["key"] not in watch.previous_open]
            if new:
                watch.event_number += 1
                lines = [
                    f"{course.subject} {course.catalog_number} — {course.term}",
                    f"Observed: {timestamp.isoformat()}",
                    "",
                ]
                for bundle in new:
                    lines.append("Opening: " + " + ".join(bundle["class_numbers"]))
                    for number in bundle["class_numbers"]:
                        section = sections[number]
                        lines.extend(
                            [
                                f"{section['section']}-{section['component']} (class {number}): "
                                f"{section['available_seats']} available seats",
                                "Meetings: " + "; ".join(section["meetings"]),
                                "Restrictions: " + (section["restrictions"] or "Check SPIRE"),
                            ]
                        )
                    if bundle["inferred"]:
                        lines.append(
                            "PAIRING INFERRED from section labels. "
                            "Verify lecture/lab compatibility in SPIRE."
                        )
                    if bundle["schedule_unknown"]:
                        lines.append("Some meeting times are unknown; check schedule conflicts.")
                    lines.append("")
                lines.extend(
                    [
                        "An opening does not guarantee eligibility or enrollment. "
                        "Check prerequisites, "
                        "reserved seats, registration appointments, and holds.",
                        "SPIRE: https://www.spire.umass.edu/",
                        "Manage watches: " + settings().app_origin,
                    ]
                )
                user = db.get(User, watch.user_id)
                db.add(
                    Delivery(
                        watch_id=watch.id,
                        event_number=watch.event_number,
                        recipient=user.email,
                        subject=f"Opening: {course.subject} {course.catalog_number}",
                        body="\n".join(lines),
                    )
                )
        watch.previous_open = keys


def deliver(db: Session):
    pending = db.scalars(
        select(Delivery)
        .where(Delivery.sent_at.is_(None), Delivery.next_attempt_at <= now())
        .order_by(Delivery.id)
        .limit(50)
        .with_for_update()
    ).all()
    for item in pending:
        # A sign-in code that sat in the queue past its lifetime cannot be used.
        if item.watch_id is None and item.created_at < now() - timedelta(minutes=10):
            db.delete(item)
            continue
        try:
            send_email(item.recipient, item.subject, item.body, item.id)
        except (httpx.HTTPError, OSError) as exc:
            item.attempts += 1
            item.next_attempt_at = now() + timedelta(
                seconds=min(3600, 30 * 2 ** min(item.attempts, 7))
            )
            log.warning("Delivery %s failed (%s); retry scheduled", item.id, type(exc).__name__)
        else:
            item.sent_at = now()
            if item.watch_id is None:
                item.body = "Sign-in code delivered"
        db.commit()
    db.commit()


def run_delivery_cycle():
    with engine().connect() as connection:
        key = WORKER_LOCK + 1
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        connection.commit()
        if not acquired:
            return
        try:
            with Session(bind=connection) as db:
                deliver(db)
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            connection.commit()


def delivery_loop():
    while not stop.is_set():
        try:
            run_delivery_cycle()
        except Exception:
            log.exception("Email cycle failed; retrying")
        stop.wait(2)


def finish_claim(claim, snapshot=None, error=None, duration=0, request_count=0):
    with Session(engine()) as db, db.begin():
        course = db.scalar(select(Course).where(Course.id == claim.course_id).with_for_update())
        timestamp = db.scalar(select(func.clock_timestamp()))
        if (
            course is None
            or course.lease_token != claim.token
            or course.lease_expires_at is None
            or course.lease_expires_at <= timestamp
        ):
            log.warning("Discarding stale result for course %s", claim.course_id)
            return False
        course.checked_at = timestamp
        course.last_check_seconds, course.last_request_count = duration, request_count
        if error is None:
            observe(db, course, snapshot)
            course.next_check_at = next_due(claim, timestamp)
        elif isinstance(error, TermUnavailable):
            from sqlalchemy.dialects.postgresql import insert

            probe = db.get(TermProbe, claim.term)
            retry_at = (
                probe.retry_at
                if probe and probe.retry_at > timestamp
                else timestamp + timedelta(hours=1)
            )
            db.execute(
                insert(TermProbe)
                .values(term=claim.term, retry_at=retry_at)
                .on_conflict_do_update(index_elements=[TermProbe.term], set_={"retry_at": retry_at})
            )
            course.state, course.detail = "term_unpublished", str(error)
            course.next_check_at = retry_at
        elif isinstance(error, CourseUnavailable):
            course.state, course.detail = "course_unpublished", str(error)
            course.next_check_at = timestamp + timedelta(hours=1)
        else:
            course.failures += 1
            course.state = "error"
            course.detail = "SPIRE check failed; last successful observation retained"
            course.next_check_at = timestamp + timedelta(
                seconds=min(3600, settings().poll_interval_seconds * 2 ** min(course.failures, 4))
            )
            log.warning("Course %s check failed (%s)", course.id, type(error).__name__)
        course.lease_token = None
        course.lease_expires_at = None
    return True


def poll_one(_=None):
    if stop.is_set():
        return
    claim = claim_course(engine())
    if claim is None:
        return
    started = time.monotonic()
    requests = 0
    snapshot, error = None, None
    try:
        with Heartbeat(engine(), claim) as heartbeat:
            with Session(engine()) as db:
                probe = db.get(TermProbe, claim.term)
                if probe and probe.retry_at > now():
                    raise TermUnavailable(
                        f"{claim.term} is not published in SPIRE guest search yet"
                    )
            with SpireClient() as client:
                client.before_request = lambda: wait_for_request(
                    engine(), lambda: stop.is_set() or heartbeat.lost.is_set()
                )
                client.cached_snapshot = claim.snapshot
                snapshot = client.collect(claim.term, claim.subject, claim.catalog_number)
                requests = client.request_count
            if heartbeat.lost.is_set():
                raise LeaseLost("Lease renewal failed")
    except LeaseLost:
        log.warning("Abandoning course %s after ownership loss or shutdown", claim.course_id)
        return
    except Exception as exc:
        error = exc
        if "client" in locals():
            requests = client.request_count
    duration = time.monotonic() - started
    saved = finish_claim(claim, snapshot, error, duration, requests)
    log.info(
        "Course %s check: saved=%s seconds=%.1f requests=%s lateness_seconds=%.1f mode=%s",
        claim.course_id,
        saved,
        duration,
        requests,
        max(0, (now() - claim.due_at).total_seconds() - duration),
        snapshot.get("collection_mode", "full") if snapshot else "unavailable",
    )


def run_cycle():
    with Session(engine()) as db:
        due = db.scalar(
            select(func.count())
            .select_from(Course)
            .where(
                Course.next_check_at <= now(),
                select(Watch.id).where(Watch.course_id == Course.id).exists(),
            )
        )
    if due:
        with ThreadPoolExecutor(max_workers=settings().poll_concurrency) as pool:
            list(pool.map(poll_one, range(min(due, 1000))))
    with Session(engine()) as db, db.begin():
        db.execute(delete(LoginSession).where(LoginSession.expires_at < now()))
        db.execute(delete(LoginCode).where(LoginCode.created_at < now() - timedelta(days=1)))


def main():
    parser = argparse.ArgumentParser(description="Poll watched courses and deliver queued emails")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if not args.once:
        threading.Thread(target=delivery_loop, daemon=True).start()
    else:
        run_delivery_cycle()
    while not stop.is_set():
        try:
            run_cycle()
        except Exception:
            log.exception("Worker cycle failed; retrying")
            if args.once:
                raise
        if args.once:
            run_delivery_cycle()
            return
        stop.wait(5)


if __name__ == "__main__":
    main()
