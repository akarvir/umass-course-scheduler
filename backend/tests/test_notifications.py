from copy import deepcopy

from coursewatch.models import Course, Delivery, User, Watch
from coursewatch.worker import observe
from sqlalchemy import select
from test_worker import setup_watch, snapshot


def subscribe(db, email, course, class_number=""):
    user = User(email=email)
    db.add(user)
    db.flush()
    watch = Watch(user_id=user.id, course_id=course.id, class_number=class_number)
    db.add(watch)
    db.flush()
    return watch


def test_opening_fans_out_to_every_subscribed_user_once(db):
    course, _ = setup_watch(db)
    subscribe(db, "second@umass.edu", course)
    subscribe(db, "third@umass.edu", course)
    observe(db, course, snapshot(False))
    db.commit()
    observe(db, course, snapshot(True))
    db.commit()
    observe(db, course, snapshot(True))
    db.commit()
    deliveries = db.scalars(select(Delivery)).all()
    assert len(deliveries) == 3
    assert {d.recipient for d in deliveries} == {
        "student@umass.edu",
        "second@umass.edu",
        "third@umass.edu",
    }
    assert all(d.event_number == 1 for d in deliveries)


def test_section_filters_independent_baselines_and_course_isolation(db):
    course, _ = setup_watch(db)
    subscribe(db, "matching@umass.edu", course, "10002")
    subscribe(db, "different@umass.edu", course, "10003")
    other = Course(catalog_number="250")
    db.add(other)
    db.flush()
    subscribe(db, "unrelated@umass.edu", other)
    observe(db, course, snapshot(False))
    db.commit()
    subscribe(db, "new@umass.edu", course)
    observe(db, course, snapshot(True))
    db.commit()
    deliveries = db.scalars(select(Delivery)).all()
    assert {d.recipient for d in deliveries} == {"student@umass.edu", "matching@umass.edu"}

    second = deepcopy(snapshot(True))
    second["sections"].append({**second["sections"][1], "class_number": "10003"})
    second["bundles"].append(
        {**second["bundles"][0], "key": "10001:10003", "class_numbers": ["10001", "10003"]}
    )
    observe(db, course, second)
    db.commit()
    deliveries = db.scalars(select(Delivery).order_by(Delivery.id)).all()
    assert len(deliveries) == 5
    assert {d.recipient for d in deliveries[2:]} == {
        "student@umass.edu",
        "different@umass.edu",
        "new@umass.edu",
    }
    assert all("10003" in d.body and "10002" not in d.body for d in deliveries[2:])
    unrelated = db.scalar(select(Watch).where(Watch.course_id == other.id))
    assert unrelated.previous_open is None
