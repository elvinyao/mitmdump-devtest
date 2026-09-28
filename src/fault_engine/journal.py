"""Bounded, metadata-only observations and deterministic retry assertions."""

from __future__ import annotations

import math
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from typing import NotRequired, TypedDict


class JournalPage(TypedDict):
    requests: list[dict[str, object]]
    checkpoint: str
    complete: bool
    next_cursor: NotRequired[str]


class ActualResult(TypedDict):
    count: int
    statuses: list[int | None]
    intervals_seconds: list[float]


class VerificationResult(TypedDict):
    matched: bool
    complete: bool
    checkpoint: str
    pending: int
    incomplete_reasons: list[str]
    failures: list[str]
    actual: ActualResult


@dataclass(slots=True)
class Observation:
    id: int
    request_id: str
    service: str
    rule: str | None
    scope: str | None
    method: str
    ordinal: int | None
    action: str
    sampled: bool | None
    started_at: float
    status: int | None = None
    outcome: str = "pending"
    duration_seconds: float | None = None
    upstream_received: bool = False


class Journal:
    """Single-event-loop storage. No headers, paths, query values or bodies enter it.

    Times are monotonic seconds, meaningful only within this journal instance.
    A checkpoint is an exclusive lower bound, not a frozen upper bound for paging.
    """

    def __init__(self, capacity: int, *, clock: Callable[[], float] = time.monotonic):
        if type(capacity) is not int or not 0 <= capacity <= 100_000:
            raise ValueError("journal capacity must be between 0 and 100000")
        self.capacity = capacity
        self.clock = clock
        self._instance = uuid.uuid4().hex
        self._tip = 0
        self._dropped_through = 0
        self._rows: OrderedDict[str, Observation] = OrderedDict()

    @property
    def checkpoint(self) -> str:
        return self._cursor(self._tip)

    def _cursor(self, position: int) -> str:
        return f"{self._instance}:{position}"

    def _position(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        if not re.fullmatch(r"[0-9a-f]{32}:(?:0|[1-9][0-9]{0,19})", cursor):
            raise ValueError("invalid journal cursor")
        instance, raw_position = cursor.split(":")
        position = int(raw_position)
        if instance != self._instance or position > self._tip:
            raise ValueError("journal cursor belongs to another instance or the future")
        return position

    def start(
        self,
        request_id: str,
        *,
        service: str,
        rule: str | None,
        scope: str | None,
        method: str,
        ordinal: int | None,
        action: str,
        sampled: bool | None,
    ) -> None:
        if not self.capacity or request_id in self._rows:
            return
        self._tip += 1
        self._rows[request_id] = Observation(
            id=self._tip,
            request_id=request_id,
            service=service,
            rule=rule,
            scope=scope,
            method=method,
            ordinal=ordinal,
            action=action,
            sampled=sampled,
            started_at=self.clock(),
        )
        if len(self._rows) > self.capacity:
            _, dropped = self._rows.popitem(last=False)
            self._dropped_through = dropped.id

    def mark_upstream(self, request_id: str) -> None:
        row = self._rows.get(request_id)
        if row is not None and row.outcome == "pending":
            row.upstream_received = True

    def finish(self, request_id: str, outcome: str, *, status: int | None = None) -> None:
        row = self._rows.get(request_id)
        if row is not None and row.outcome == "pending":
            row.status = status
            row.outcome = outcome
            row.duration_seconds = max(0.0, self.clock() - row.started_at)

    def clear(self) -> str:
        self._rows.clear()
        self._dropped_through = self._tip
        return self.checkpoint

    def _selected(
        self, position: int, service: str | None, rule: str | None, scope: str | None
    ) -> Iterator[Observation]:
        return (
            row
            for row in self._rows.values()
            if row.id > position
            and (service is None or row.service == service)
            and (rule is None or row.rule == rule)
            and (scope is None or row.scope == scope)
        )

    def page(
        self,
        *,
        service: str | None = None,
        rule: str | None = None,
        scope: str | None = None,
        after: str | None = None,
        limit: int = 100,
    ) -> JournalPage:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("journal page limit must be between 1 and 1000")
        position = self._position(after)
        rows: list[dict[str, object]] = []
        result: JournalPage = {
            "requests": rows,
            "checkpoint": self.checkpoint,
            "complete": bool(self.capacity) and position >= self._dropped_through,
        }
        last_id = position
        for row in self._selected(position, service, rule, scope):
            if len(rows) == limit:
                result["next_cursor"] = self._cursor(last_id)
                break
            rows.append(asdict(row))
            last_id = row.id
        result["requests"] = rows
        return result

    def verify(
        self,
        *,
        count: int,
        service: str | None = None,
        rule: str | None = None,
        scope: str | None = None,
        after: str | None = None,
        statuses: list[int] | None = None,
        min_interval_seconds: float | None = None,
    ) -> VerificationResult:
        if type(count) is not int or count < 0:
            raise ValueError("count must be a nonnegative integer")
        if statuses is not None and (
            len(statuses) != count
            or any(type(status) is not int or not 100 <= status <= 599 for status in statuses)
        ):
            raise ValueError("statuses must contain count HTTP status codes")
        if min_interval_seconds is not None and (
            isinstance(min_interval_seconds, bool)
            or not math.isfinite(min_interval_seconds)
            or min_interval_seconds < 0
        ):
            raise ValueError("min_interval_seconds must be finite and nonnegative")
        position = self._position(after)
        rows = list(self._selected(position, service, rule, scope))
        actual_statuses = [row.status for row in rows]
        intervals = [b.started_at - a.started_at for a, b in zip(rows, rows[1:], strict=False)]
        pending = sum(row.outcome == "pending" for row in rows)
        incomplete = []
        if not self.capacity:
            incomplete.append("journal_disabled")
        if position < self._dropped_through:
            incomplete.append("history_lost")
        if pending:
            incomplete.append("pending_requests")
        failures = []
        if len(rows) != count:
            failures.append("count")
        if statuses is not None and actual_statuses != statuses:
            failures.append("statuses")
        if min_interval_seconds is not None and any(
            gap < min_interval_seconds for gap in intervals
        ):
            failures.append("min_interval_seconds")
        return {
            "matched": not incomplete and not failures,
            "complete": not incomplete,
            "checkpoint": self.checkpoint,
            "pending": pending,
            "incomplete_reasons": incomplete,
            "failures": failures,
            "actual": {
                "count": len(rows),
                "statuses": actual_statuses,
                "intervals_seconds": intervals,
            },
        }
