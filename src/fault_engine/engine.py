"""Synchronous, event-loop-confined scenario decisions; network awaits happen later."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import uuid
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from fault_engine.config import Config
from fault_engine.plan import PASSTHROUGH, ActionPlan, ExecutionPlan, RulePlan, compile_plan

logger = logging.getLogger(__name__)
EXPIRY_BATCH_SIZE = 1000


class ScenarioError(Exception):
    """An explicit scenario contract failure, not a simulated upstream failure."""


@dataclass(frozen=True)
class Decision:
    service: str
    rule_id: str
    scope: str
    ordinal: int
    action: ActionPlan
    sampled: bool = True


@dataclass
class Counter:
    count: int
    touched: float
    identifier: int


class Engine:
    def __init__(
        self, config: Config | ExecutionPlan, *, clock: Callable[[], float] = time.monotonic
    ):
        self.plan = compile_plan(config)
        self.config = self.plan
        self._clock = clock
        self._counters: OrderedDict[tuple[str, str, str], Counter] = OrderedDict()
        self._counter_ids: OrderedDict[int, tuple[str, str, str]] = OrderedDict()
        self._next_counter_id = 1
        self._cursor_instance = uuid.uuid4().hex
        self._rules = {
            s.id: tuple(r for r in self.plan.rules if r.service == s.id) for s in self.plan.services
        }

    def _expire(self, now: float) -> None:
        expired = 0
        while self._counters and expired < EXPIRY_BATCH_SIZE:
            key, counter = next(iter(self._counters.items()))
            if now - counter.touched < self.config.state.ttl_seconds:
                break
            self._remove(key)
            expired += 1
        if expired:
            logger.warning("state_expired count=%s", expired)

    def _remove(self, key: tuple[str, str, str]) -> None:
        counter = self._counters.pop(key)
        del self._counter_ids[counter.identifier]

    def decide(
        self,
        service: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        query: Sequence[tuple[str, str]],
    ) -> Decision | None:
        """Allocate a sequence position atomically within one asyncio event loop.

        No awaits or background threads may be added here without introducing locking.
        Matching query values use any equal occurrence; path_regex is a full match.
        """
        normalized = {k.lower(): v for k, v in headers.items()}
        pairs = set(query)
        for rule in self._rules.get(service, []):
            if self._match_failures(rule, method, path, normalized, pairs):
                continue
            scope = self._scope(rule, normalized)
            now = self._clock()
            self._expire(now)
            key = (service, rule.id, scope)
            counter = self._counters.get(key)
            if counter is not None and now - counter.touched >= self.config.state.ttl_seconds:
                # The shared cleanup budget may end before this particular old scope.
                self._remove(key)
                logger.warning("state_expired count=1")
                counter = None
            if counter is None:
                if len(self._counters) >= self.config.state.capacity:
                    logger.warning("state_capacity service=%s rule=%s", service, rule.id)
                    raise ScenarioError("scenario state capacity reached; reset or wait for TTL")
                counter = Counter(0, now, self._next_counter_id)
                self._counter_ids[counter.identifier] = key
                self._next_counter_id += 1
                self._counters[key] = counter
            counter.count += 1
            counter.touched = now
            self._counters.move_to_end(key)
            return self._decision(rule, scope, counter.count)
        return None

    @staticmethod
    def _match_failures(
        rule: RulePlan,
        method: str,
        path: str,
        headers: Mapping[str, str],
        query: set[tuple[str, str]],
    ) -> list[str]:
        """Describe failed dimensions without including request or configured values."""
        match = rule.match
        failures = []
        if match.methods is not None and method not in match.methods:
            failures.append("method")
        if (match.path is not None and path != match.path) or (
            match.pattern is not None and not match.pattern.fullmatch(path)
        ):
            failures.append("path")
        if any(headers.get(k) != v for k, v in match.headers.items()):
            failures.append("header")
        if any((k, v) not in query for k, v in match.query.items()):
            failures.append("query")
        return failures

    @staticmethod
    def _scope(rule: RulePlan, headers: Mapping[str, str]) -> str:
        scope = "global" if rule.scope == "global" else headers.get(rule.scope.lower(), "")
        if not scope or len(scope) > 256 or any(ord(ch) < 32 for ch in scope):
            raise ScenarioError("missing or invalid scope header (maximum 256 characters)")
        return scope

    def _decision(self, rule: RulePlan, scope: str, ordinal: int) -> Decision:
        """Select and sample at a given position without reading or changing state."""
        action = self._select(rule, ordinal)
        sampled = rule.probability == 1 or (
            rule.probability > 0
            and self._fraction(rule, scope, ordinal, "probability") < rule.probability
        )
        if not sampled:
            action = PASSTHROUGH
        elif action.jitter_seconds:
            extra = action.jitter_seconds * self._fraction(rule, scope, ordinal, "delay")
            action = replace(
                action,
                seconds=action.seconds + extra if action.seconds is not None else None,
                delay_seconds=action.delay_seconds + extra if action.status is not None else 0,
                jitter_seconds=0,
            )
        return Decision(rule.service, rule.id, scope, ordinal, action, sampled)

    def explain(
        self,
        service: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        query: Sequence[tuple[str, str]],
        ordinal: int = 1,
    ) -> dict:
        """Explain a hypothetical request without counters, expiry, clock, or network I/O.

        Header and query values, including the test scope, are never returned.
        Response bodies and headers are omitted from the selected action as well.
        """
        if service not in self._rules:
            raise ValueError("unknown service")
        if type(ordinal) is not int or ordinal < 1:
            raise ValueError("ordinal must be a positive integer")
        normalized = {k.lower(): v for k, v in headers.items()}
        pairs = set(query)
        candidates = []
        selected = None
        for rule in self._rules[service]:
            failures = self._match_failures(rule, method, path, normalized, pairs)
            candidates.append({"rule": rule.id, "matches": not failures, "failures": failures})
            if not failures and selected is None:
                selected = rule
        result = {"matched_rule": None, "candidates": candidates, "scope": None, "decision": None}
        if selected is None:
            return result
        result["matched_rule"] = selected.id
        try:
            scope = self._scope(selected, normalized)
        except ScenarioError as exc:
            result["scope"] = {"valid": False, "error": str(exc)}
            return result
        decision = self._decision(selected, scope, ordinal)
        result["scope"] = {"valid": True}
        result["decision"] = {
            "service": decision.service,
            "rule": decision.rule_id,
            "ordinal": decision.ordinal,
            "action": decision.action.action,
            "status": decision.action.status,
            "seconds": decision.action.seconds,
            "delay_seconds": decision.action.delay_seconds,
            "sampled": decision.sampled,
        }
        return result

    @staticmethod
    def _fraction(rule: RulePlan, scope: str, ordinal: int, channel: str) -> float:
        # Stateless, versioned sampling: other scopes and scheduling cannot advance
        # a shared RNG. Separate channels decouple delay from probability selection.
        material = json.dumps(
            ["fault-sample-v1", rule.seed, rule.service, rule.id, scope, ordinal, channel],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
        value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") >> 11
        return value / (1 << 53)

    @staticmethod
    def _select(rule: RulePlan, ordinal: int) -> ActionPlan:
        index = ordinal - rule.start_at
        if index < 0:
            return PASSTHROUGH
        total = rule.total_repeats
        if index >= total:
            if rule.after_sequence == "passthrough":
                return PASSTHROUGH
            if rule.after_sequence == "repeat_last":
                return rule.sequence[-1]
            index %= total
        return rule.sequence[bisect_right(rule.cumulative_repeats, index)]

    def snapshot(
        self, *, service: str | None = None, rule: str | None = None, scope: str | None = None
    ) -> list[dict[str, str | int]]:
        """Read live counters using the same exact selection as reset, without touching TTL."""
        return [
            {"service": key[0], "rule": key[1], "scope": key[2], "count": self._counters[key].count}
            for key in self._matching_keys(service=service, rule=rule, scope=scope)
        ]

    def reset(
        self, *, service: str | None = None, rule: str | None = None, scope: str | None = None
    ) -> int:
        keys = self._matching_keys(service=service, rule=rule, scope=scope)
        for key in keys:
            self._remove(key)
        return len(keys)

    def snapshot_page(
        self,
        *,
        service: str | None = None,
        rule: str | None = None,
        scope: str | None = None,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, str | int]], str | None]:
        """Read a bounded live page in creation order, without renewing any TTL.

        A cursor binds the filters and original highest counter ID. New scopes are
        excluded until the next fresh query; reset/expiry remove rows, not positions.
        Sparse queries may return an empty page with a continuation cursor.
        """
        if type(limit) is not int or not 1 <= limit <= self.plan.limits.state_page_size:
            raise ValueError("invalid page limit")
        selection = hashlib.sha256(json.dumps([service, rule, scope]).encode()).hexdigest()
        now = self._clock()
        self._expire(now)
        position = next(iter(self._counter_ids), self._next_counter_id)
        end = self._next_counter_id - 1
        if cursor is not None:
            position, end = self._read_cursor(cursor, selection)
        rows: list[dict[str, str | int]] = []
        budget = max(EXPIRY_BATCH_SIZE, limit * 10)
        identifiers: Iterable[int]
        if len(self._counter_ids) <= budget:
            # Small live sets can skip arbitrarily large holes left by old resets.
            identifiers = (
                identifier for identifier in self._counter_ids if position <= identifier <= end
            )
            scan_end = end + 1
        else:
            scan_end = min(end + 1, position + budget)
            identifiers = range(position, scan_end)
        for identifier in identifiers:
            key = self._counter_ids.get(identifier)
            position = identifier + 1
            if key is None or not self._matches(key, service, rule, scope):
                continue
            counter = self._counters[key]
            if now - counter.touched >= self.plan.state.ttl_seconds:
                continue
            rows.append(
                {"service": key[0], "rule": key[1], "scope": key[2], "count": counter.count}
            )
            if len(rows) == limit:
                break
        else:
            position = scan_end
        next_cursor = None
        if position <= end:
            payload = [self._cursor_instance, position, end, selection]
            next_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return rows, next_cursor

    def _read_cursor(self, cursor: str, selection: str) -> tuple[int, int]:
        try:
            if len(cursor) > 512:
                raise ValueError
            payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(payload, list) or len(payload) != 4:
                raise ValueError
            instance, position, end, selected = payload
            if (
                instance != self._cursor_instance
                or selected != selection
                or type(position) is not int
                or type(end) is not int
                or not 1 <= position <= end < self._next_counter_id
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError):
            raise ValueError("invalid or mismatched state cursor") from None
        return position, end

    @staticmethod
    def _matches(
        key: tuple[str, str, str], service: str | None, rule: str | None, scope: str | None
    ) -> bool:
        return (
            (service is None or key[0] == service)
            and (rule is None or key[1] == rule)
            and (scope is None or key[2] == scope)
        )

    def _matching_keys(
        self, *, service: str | None, rule: str | None, scope: str | None
    ) -> list[tuple[str, str, str]]:
        now = self._clock()
        self._expire(now)
        return [
            key
            for key in self._counters
            if now - self._counters[key].touched < self.plan.state.ttl_seconds
            and self._matches(key, service, rule, scope)
        ]
