"""Exclusive loans of guest sessions shared across polling cycles."""

from contextlib import contextmanager
from queue import Empty, Queue

from coursewatch.spire import SpireClient


class SpireSessionPool:
    def __init__(self, size, client_factory=SpireClient):
        if size < 1:
            raise ValueError("Session pool needs at least one slot")
        self.clients = Queue(maxsize=size)
        self.client_factory = client_factory
        self.closed = False
        for _ in range(size):
            self.clients.put(None)

    @contextmanager
    def acquire(self):
        while True:
            if self.closed:
                raise RuntimeError("SPIRE session pool is closed")
            try:
                client = self.clients.get(timeout=0.5)
                break
            except Empty:
                continue
        try:
            if client is None:
                client = self.client_factory()
            client.request_count = 0
            yield client
        except BaseException:
            if client is not None:
                client.close()
            client = None
            raise
        finally:
            if client is not None:
                client.before_request = None
                client.cached_snapshot = None
            if self.closed:
                if client is not None:
                    client.close()
            else:
                self.clients.put(client)

    def close(self):
        self.closed = True
        while True:
            try:
                client = self.clients.get_nowait()
            except Empty:
                return
            if client is not None:
                client.close()
