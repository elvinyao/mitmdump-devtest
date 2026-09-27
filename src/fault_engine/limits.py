"""Event-loop-confined capacity accounting with idempotent ownership leases."""

from __future__ import annotations


class Lease:
    def __init__(self, capacity: Capacity):
        self._capacity: Capacity | None = capacity

    def release(self) -> None:
        if self._capacity is not None:
            self._capacity._used -= 1
            self._capacity = None


class Capacity:
    def __init__(self, limit: int):
        if limit < 1:
            raise ValueError("capacity must be positive")
        self.limit = limit
        self._used = 0

    @property
    def used(self) -> int:
        return self._used

    def acquire(self) -> Lease | None:
        if self._used >= self.limit:
            return None
        self._used += 1
        return Lease(self)


class ResourceBudget:
    def __init__(self, *, max_connections: int = 256, max_inflight_requests: int = 128):
        self.connections = Capacity(max_connections)
        self.requests = Capacity(max_inflight_requests)
