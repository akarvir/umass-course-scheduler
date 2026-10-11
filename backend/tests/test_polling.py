import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

from coursewatch.leases import LeaseManager
from coursewatch.models import Course, Delivery, TermProbe, Watch, now
from coursewatch.polling import next_due, wait_for_request
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_worker import setup_watch, snapshot


def test_concurrent_claims_share_no_course(db_engine):
    with Session(db_engine) as db:
        course, watch = setup_watch(db)
        second = Course(catalog_number="250")
        db.add(second)
        db.flush()
        db.add(Watch(user_id=watch.user_id, course_id=second.id))
        db.commit()
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(lambda _: LeaseManager(db_engine).claim(), range(3)))
    assert len([c for c in claims if c]) == 2
    assert len({c.course_id for c in claims if c}) == 2


def test_expired_owner_cannot_save_or_renew_after_reclaim(db_engine, monkeypatch):
    import coursewatch.worker as worker

    with Session(db_engine) as db:
        course, _ = setup_watch(db)
        worker.observe(db, course, snapshot(False))
        course.next_check_at = now() - timedelta(seconds=1)
        db.commit()
        cid = course.id
    monkeypatch.setattr(worker, "engine", lambda: db_engine)
    old = LeaseManager(db_engine).claim()
    with Session(db_engine) as db, db.begin():
        db.get(Course, cid).lease_expires_at = now() - timedelta(seconds=1)
    fresh = LeaseManager(db_engine).claim()
    assert fresh.token != old.token
    assert not LeaseManager(db_engine).renew(old)
    assert not worker.finish_claim(old, snapshot(True))
    assert worker.finish_claim(fresh, snapshot(True))
    assert not worker.finish_claim(fresh, snapshot(True))
    with Session(db_engine) as db:
        assert len(db.scalars(select(Delivery)).all()) == 1
        assert db.get(Course, cid).lease_token is None


def test_expired_owner_cannot_save_before_reclaim(db_engine, monkeypatch):
    import coursewatch.worker as worker

    with Session(db_engine) as db:
        course, _ = setup_watch(db)
        cid = course.id
    claim = LeaseManager(db_engine).claim()
    with Session(db_engine) as db, db.begin():
        db.get(Course, cid).lease_expires_at = now() - timedelta(seconds=1)
    monkeypatch.setattr(worker, "engine", lambda: db_engine)
    assert not worker.finish_claim(claim, snapshot(True))


def test_renewal_extends_only_current_owner(db_engine):
    with Session(db_engine) as db:
        course, _ = setup_watch(db)
        cid = course.id
    claim = LeaseManager(db_engine).claim()
    with Session(db_engine) as db, db.begin():
        db.get(Course, cid).lease_expires_at = now() + timedelta(seconds=10)
    assert LeaseManager(db_engine).renew(claim)
    with Session(db_engine) as db:
        assert db.get(Course, cid).lease_expires_at > now() + timedelta(seconds=100)


def test_distinct_courses_collect_concurrently_and_alert_once(db_engine, monkeypatch):
    import coursewatch.worker as worker

    with Session(db_engine) as db:
        first, watch = setup_watch(db)
        second = Course(catalog_number="250")
        db.add(second)
        db.flush()
        db.add(Watch(user_id=watch.user_id, course_id=second.id))
        worker.observe(db, first, snapshot(False))
        worker.observe(db, second, snapshot(False))
        first.next_check_at = second.next_check_at = now() - timedelta(seconds=1)
        db.commit()
    barrier = threading.Barrier(2)
    calls = []

    def collect(*args):
        calls.append(args)
        barrier.wait(timeout=5)
        return snapshot(True)

    monkeypatch.setattr(worker, "engine", lambda: db_engine)
    monkeypatch.setattr(worker.SpireClient, "collect", collect)
    worker.run_cycle()
    assert len(calls) == 2
    with Session(db_engine) as db:
        assert len(db.scalars(select(Delivery)).all()) == 2
        assert all(c.lease_token is None for c in db.scalars(select(Course)))


def test_request_budget_is_shared_by_collectors(db_engine, monkeypatch):
    import coursewatch.polling as polling

    monkeypatch.setattr(
        polling, "settings", lambda: SimpleNamespace(spire_request_interval_seconds=0.05)
    )

    def reserve(_):
        wait_for_request(db_engine, lambda: False)
        return now()

    with ThreadPoolExecutor(max_workers=3) as pool:
        starts = sorted(pool.map(reserve, range(3)))
    assert all((b - a).total_seconds() >= 0.035 for a, b in zip(starts, starts[1:]))


def test_new_watches_do_not_extend_unpublished_term_retry(db_engine, monkeypatch):
    import coursewatch.worker as worker
    from coursewatch.spire import TermUnavailable

    with Session(db_engine) as db:
        course, _ = setup_watch(db)
        retry = now() + timedelta(minutes=30)
        db.add(TermProbe(term=course.term, retry_at=retry))
        db.commit()
    claim = LeaseManager(db_engine).claim()
    monkeypatch.setattr(worker, "engine", lambda: db_engine)
    assert worker.finish_claim(claim, error=TermUnavailable("Not published"))
    with Session(db_engine) as db:
        assert db.get(Course, claim.course_id).next_check_at == retry
        assert db.get(TermProbe, claim.term).retry_at == retry


def test_cadence_is_fixed_and_staggered():
    completed = now()
    claim = SimpleNamespace(term="2027 Spring", subject="COMPSCI", catalog_number="220")
    first = next_due(claim, completed)
    assert completed < first <= completed + timedelta(minutes=5)
    second = next_due(claim, first + timedelta(seconds=72))
    assert second == first + timedelta(minutes=5)
    claim.catalog_number = "250"
    assert next_due(claim, completed) != first


def test_second_worker_cannot_duplicate_inflight_opening(db_engine, monkeypatch):
    import coursewatch.worker as worker

    with Session(db_engine) as db:
        course, _ = setup_watch(db)
        worker.observe(db, course, snapshot(False))
        course.next_check_at = now() - timedelta(seconds=1)
        db.commit()
    started, proceed = threading.Event(), threading.Event()
    calls = []

    def collect(*args):
        calls.append(args)
        started.set()
        assert proceed.wait(5)
        return snapshot(True)

    monkeypatch.setattr(worker, "engine", lambda: db_engine)
    monkeypatch.setattr(worker.SpireClient, "collect", collect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker.run_cycle)
        assert started.wait(5)
        try:
            pool.submit(worker.run_cycle).result(timeout=5)
        finally:
            proceed.set()
        first.result(timeout=5)
    assert len(calls) == 1
    with Session(db_engine) as db:
        assert len(db.scalars(select(Delivery)).all()) == 1


def test_polling_status_reports_due_and_leased_courses(db_engine):
    from coursewatch.status import polling_status

    with Session(db_engine) as db:
        setup_watch(db)
    assert LeaseManager(db_engine).claim()
    metrics = polling_status(db_engine)
    assert metrics["watched_courses"] == metrics["due_courses"] == metrics["active_leases"] == 1
    assert metrics["fresh_observations"] == 0


def test_lease_completion_rolls_back_alerts_state_and_release_together(db_engine):
    import pytest
    from coursewatch.worker import observe

    with Session(db_engine) as db:
        course, watch = setup_watch(db)
        observe(db, course, snapshot(False))
        course.next_check_at = now() - timedelta(seconds=1)
        db.commit()
        watch_id = watch.id
    manager = LeaseManager(db_engine)
    claim = manager.claim()

    def fail_after_observation(db, course, timestamp):
        observe(db, course, snapshot(True))
        db.flush()
        raise RuntimeError("Interrupted completion")

    with pytest.raises(RuntimeError, match="Interrupted completion"):
        manager.complete(claim, fail_after_observation)
    with Session(db_engine) as db:
        assert db.scalar(select(Delivery)) is None
        assert db.get(Watch, watch_id).previous_open == []
        assert db.get(Course, claim.course_id).lease_token == claim.token
    assert manager.complete(
        claim, lambda db, course, timestamp: observe(db, course, snapshot(True))
    )
    with Session(db_engine) as db:
        assert len(db.scalars(select(Delivery)).all()) == 1
        assert db.get(Course, claim.course_id).lease_token is None
