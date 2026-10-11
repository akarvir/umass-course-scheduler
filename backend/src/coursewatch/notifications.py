"""Publish course observations to persisted watch subscribers and the email outbox."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from coursewatch.availability import open_bundles
from coursewatch.config import settings
from coursewatch.models import Delivery, User, Watch


@dataclass(frozen=True)
class CourseAvailabilityEvent:
    course_id: int
    term: str
    subject: str
    catalog_number: str
    snapshot: dict
    observed_at: datetime


@dataclass(frozen=True)
class CourseOpeningEvent:
    course: CourseAvailabilityEvent
    watch_id: int
    event_number: int
    recipient: str
    openings: list[dict]


class WatchSubscriber:
    def __init__(self, watch: Watch, email: str):
        self.watch = watch
        self.email = email

    def on_availability(self, event: CourseAvailabilityEvent):
        opened = open_bundles(event.snapshot, self.watch.class_number)
        previous = self.watch.previous_open
        self.watch.previous_open = [bundle["key"] for bundle in opened]
        if previous is None:
            return None
        new = [bundle for bundle in opened if bundle["key"] not in previous]
        if not new:
            return None
        self.watch.event_number += 1
        return CourseOpeningEvent(event, self.watch.id, self.watch.event_number, self.email, new)


def opening_email_body(event: CourseOpeningEvent):
    sections = {section["class_number"]: section for section in event.course.snapshot["sections"]}
    lines = [
        f"{event.course.subject} {event.course.catalog_number} — {event.course.term}",
        f"Observed: {event.course.observed_at.isoformat()}",
        "",
    ]
    for bundle in event.openings:
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
                "PAIRING INFERRED from section labels. Verify lecture/lab compatibility in SPIRE."
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
    return "\n".join(lines)


class CourseOpeningPublisher:
    def __init__(self, db: Session):
        self.db = db

    def publish(self, event: CourseAvailabilityEvent):
        """Queue relevant openings without committing the caller's observation transaction."""
        subscriptions = self.db.execute(
            select(Watch, User.email)
            .join(User, User.id == Watch.user_id)
            .where(Watch.course_id == event.course_id)
            .order_by(Watch.id)
            .with_for_update(of=Watch)
        ).all()
        published = 0
        for watch, email in subscriptions:
            opening = WatchSubscriber(watch, email).on_availability(event)
            if opening is None:
                continue
            self.db.add(
                Delivery(
                    watch_id=opening.watch_id,
                    event_number=opening.event_number,
                    recipient=opening.recipient,
                    subject=f"Opening: {event.subject} {event.catalog_number}",
                    body=opening_email_body(opening),
                )
            )
            published += 1
        return published
