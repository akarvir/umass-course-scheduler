import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from coursewatch.spire_pool import SpireSessionPool


class Client:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_sessions_reused_without_retaining_claim_callbacks_or_snapshots():
    pool = SpireSessionPool(1, Client)
    with pool.acquire() as first:
        first.cached_snapshot = {"course": "220"}
        first.before_request = lambda: None
        first.request_count = 10
    with pool.acquire() as second:
        assert second is first
        assert second.cached_snapshot is None and second.before_request is None
        assert second.request_count == 0
    pool.close()
    assert first.closed


def test_failed_collection_discards_session_and_returns_slot():
    pool = SpireSessionPool(1, Client)
    with pytest.raises(RuntimeError, match="Expired"):
        with pool.acquire() as failed:
            raise RuntimeError("Expired")
    assert failed.closed
    with pool.acquire() as fresh:
        assert fresh is not failed and not fresh.closed
    pool.close()


def test_two_collectors_cannot_share_a_borrowed_guest_session():
    pool = SpireSessionPool(1, Client)
    started, entered = threading.Event(), threading.Event()

    def borrow():
        started.set()
        with pool.acquire() as client:
            entered.set()
            return client

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pool.acquire() as first:
            other = executor.submit(borrow)
            assert started.wait(2)
            assert not entered.wait(0.05)
        assert other.result(timeout=2) is first
    pool.close()


def test_shutdown_closes_borrowed_session_and_rejects_new_loans():
    pool = SpireSessionPool(1, Client)
    with pool.acquire() as client:
        pool.close()
        assert not client.closed
    assert client.closed
    with pytest.raises(RuntimeError, match="closed"):
        with pool.acquire():
            pass
