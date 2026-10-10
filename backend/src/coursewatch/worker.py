import argparse
import logging
import signal
import threading
from datetime import timedelta

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from coursewatch.availability import open_bundles
from coursewatch.config import settings
from coursewatch.db import engine
from coursewatch.email import send_email
from coursewatch.models import Course, Delivery, LoginCode, LoginSession, User, Watch, now
from coursewatch.spire import CourseUnavailable, SourceError, SpireClient, TermUnavailable

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


def run_cycle():
    with engine().connect() as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK}
        )
        connection.commit()
        if not acquired:
            log.info("Another worker owns the polling lock")
            return
        try:
            with Session(bind=connection) as db:
                courses = db.scalars(
                    select(Course)
                    .where(
                        Course.next_check_at <= now(),
                        select(Watch.id).where(Watch.course_id == Course.id).exists(),
                    )
                    .order_by(Course.next_check_at)
                ).all()
                unavailable_terms = set()
                for course in courses:
                    if stop.is_set():
                        break
                    course.checked_at = now()
                    try:
                        if course.term in unavailable_terms:
                            raise TermUnavailable(
                                f"{course.term} is not published in guest search yet"
                            )
                        with SpireClient() as client:
                            snapshot = client.collect(
                                course.term, course.subject, course.catalog_number
                            )
                        observe(db, course, snapshot)
                    except TermUnavailable as exc:
                        unavailable_terms.add(course.term)
                        course.state, course.detail = "term_unpublished", str(exc)
                        course.next_check_at = now() + timedelta(hours=1)
                    except CourseUnavailable as exc:
                        course.state, course.detail = "course_unpublished", str(exc)
                        course.next_check_at = now() + timedelta(hours=1)
                    except (SourceError, httpx.HTTPError) as exc:
                        course.failures += 1
                        course.state = "error"
                        course.detail = "SPIRE check failed; last successful observation retained"
                        course.next_check_at = now() + timedelta(
                            seconds=min(
                                3600,
                                settings().poll_interval_seconds * 2 ** min(course.failures, 4),
                            )
                        )
                        log.warning("Course %s check failed (%s)", course.id, type(exc).__name__)
                    db.commit()
                db.execute(delete(LoginSession).where(LoginSession.expires_at < now()))
                db.execute(
                    delete(LoginCode).where(LoginCode.created_at < now() - timedelta(days=1))
                )
                db.commit()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
            connection.commit()


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
