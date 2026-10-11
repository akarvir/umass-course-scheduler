from copy import deepcopy
from datetime import timedelta

from coursewatch.models import Course, Delivery, User, Watch, now
from coursewatch.worker import deliver, observe
from sqlalchemy import select


def setup_watch(db):
    user = User(email="student@umass.edu")
    course = Course(catalog_number="220")
    db.add_all([user, course])
    db.flush()
    watch = Watch(user_id=user.id, course_id=course.id)
    db.add(watch)
    db.commit()
    return course, watch


def snapshot(opened):
    return {
        "sections": [
            {
                "class_number": "10001",
                "section": "01",
                "component": "LEC",
                "available_seats": 2,
                "meetings": [],
                "restrictions": "Majors only",
            },
            {
                "class_number": "10002",
                "section": "01LL",
                "component": "LAB",
                "available_seats": 1,
                "meetings": [],
                "restrictions": "",
            },
        ],
        "bundles": [
            {
                "key": "10001:10002",
                "class_numbers": ["10001", "10002"],
                "open": opened,
                "inferred": True,
                "schedule_unknown": True,
            }
        ],
    }


def test_baseline_then_opening_deduplicated_then_reopening(db):
    course, watch = setup_watch(db)
    observe(db, course, snapshot(False))
    db.commit()
    assert db.scalar(select(Delivery)) is None
    observe(db, course, snapshot(True))
    db.commit()
    assert len(db.scalars(select(Delivery)).all()) == 1
    observe(db, course, snapshot(True))
    db.commit()
    assert len(db.scalars(select(Delivery)).all()) == 1
    observe(db, course, snapshot(False))
    db.commit()
    observe(db, course, snapshot(True))
    db.commit()
    assert len(db.scalars(select(Delivery)).all()) == 2
    assert watch.event_number == 2
    body = db.scalar(select(Delivery)).body
    assert "PAIRING INFERRED" in body and "Majors only" in body and "10002" in body


def test_initial_open_baseline_no_alert(db):
    course, _ = setup_watch(db)
    observe(db, course, snapshot(True))
    db.commit()
    assert db.scalar(select(Delivery)) is None


def test_new_lab_opening_alerts_even_if_other_pair_open(db):
    course, _ = setup_watch(db)
    first = snapshot(True)
    observe(db, course, first)
    db.commit()
    second = deepcopy(first)
    second["sections"].append({**second["sections"][1], "class_number": "10003"})
    second["bundles"].append(
        {**second["bundles"][0], "key": "10001:10003", "class_numbers": ["10001", "10003"]}
    )
    observe(db, course, second)
    db.commit()
    delivery = db.scalar(select(Delivery))
    assert "10003" in delivery.body and "10002" not in delivery.body


def test_failure_between_successes_keeps_baseline(db):
    course, watch = setup_watch(db)
    observe(db, course, snapshot(True))
    db.commit()
    course.state, course.detail = "error", "Network unavailable"
    db.commit()
    assert watch.previous_open == ["10001:10002"]
    observe(db, course, snapshot(True))
    db.commit()
    assert db.scalar(select(Delivery)) is None


def test_email_failure_retry_and_success(db, monkeypatch):
    item = Delivery(
        watch_id=1, event_number=1, recipient="student@umass.edu", subject="Opening", body="test"
    )
    db.add(item)
    db.commit()

    def fail(*args):
        raise OSError("temporary failure")

    monkeypatch.setattr("coursewatch.worker.send_email", fail)
    deliver(db)
    assert item.sent_at is None and item.attempts == 1 and item.next_attempt_at > now()
    item.next_attempt_at = now() - timedelta(seconds=1)
    db.commit()
    sent = []
    monkeypatch.setattr("coursewatch.worker.send_email", lambda *args: sent.append(args))
    deliver(db)
    assert item.sent_at is not None and len(sent) == 1
    deliver(db)
    assert len(sent) == 1


def test_worker_failure_retains_snapshot_and_backoff(db_engine, monkeypatch):
    import coursewatch.worker as worker
    from coursewatch.spire import SourceError
    from sqlalchemy.orm import Session

    with Session(db_engine) as db:
        course, _ = setup_watch(db)
        observe(db, course, snapshot(True))
        course.next_check_at = now() - timedelta(seconds=1)
        original = course.snapshot
        db.commit()
        cid = course.id
    monkeypatch.setattr(worker, "engine", lambda: db_engine)

    def fail(*args):
        raise SourceError("maintenance")

    monkeypatch.setattr(worker.SpireClient, "collect", fail)
    worker.run_cycle()
    with Session(db_engine) as db:
        course = db.get(Course, cid)
        assert course.state == "error" and course.snapshot == original
        assert course.next_check_at > now() and course.failures == 1


def test_worker_only_queries_shared_course_once(db_engine, monkeypatch):
    import coursewatch.worker as worker
    from sqlalchemy.orm import Session

    with Session(db_engine) as db:
        course, _ = setup_watch(db)
        other = User(email="other@umass.edu")
        db.add(other)
        db.flush()
        db.add(Watch(user_id=other.id, course_id=course.id))
        db.commit()
    calls = []
    monkeypatch.setattr(worker, "engine", lambda: db_engine)

    def collect(*args):
        calls.append(args)
        return snapshot(False)

    monkeypatch.setattr(worker.SpireClient, "collect", collect)
    worker.run_cycle()
    assert len(calls) == 1


def test_worker_term_unpublished_is_shared_and_unknown(db_engine, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("coursewatch.worker.settings", lambda: SimpleNamespace(poll_concurrency=1))
    import coursewatch.worker as worker
    from coursewatch.spire import TermUnavailable
    from sqlalchemy.orm import Session

    with Session(db_engine) as db:
        first, watch = setup_watch(db)
        second = Course(catalog_number="250")
        db.add(second)
        db.flush()
        db.add(Watch(user_id=watch.user_id, course_id=second.id))
        db.commit()
    calls = []

    def unavailable(*args):
        calls.append(args)
        raise TermUnavailable("Spring is not published")

    monkeypatch.setattr(worker, "engine", lambda: db_engine)
    monkeypatch.setattr(worker.SpireClient, "collect", unavailable)
    worker.run_cycle()
    assert len(calls) == 1
    with Session(db_engine) as db:
        courses = db.scalars(select(Course)).all()
        assert all(c.state == "term_unpublished" and c.snapshot is None for c in courses)
        assert all(c.next_check_at > now() + timedelta(minutes=50) for c in courses)


def test_active_course_lease_blocks_duplicate_collection(db_engine, monkeypatch):
    import coursewatch.worker as worker
    from coursewatch.polling import claim_course
    from sqlalchemy.orm import Session

    with Session(db_engine) as db:
        setup_watch(db)
    monkeypatch.setattr(worker, "engine", lambda: db_engine)
    assert claim_course(db_engine) is not None
    calls = []
    monkeypatch.setattr(worker.SpireClient, "collect", lambda *args: calls.append(args))
    worker.run_cycle()
    assert calls == []
