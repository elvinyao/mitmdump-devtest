"""Synchronous, event-loop-confined scenario decisions; network awaits happen later."""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from fault_engine.config import Action, Config, Pass, Rule

logger = logging.getLogger(__name__)


class ScenarioError(Exception):
    """An explicit scenario contract failure, not a simulated upstream failure."""


@dataclass(frozen=True)
class Decision:
    service: str
    rule_id: str
    scope: str
    ordinal: int
    action: Action


@dataclass
class Counter:
    count: int
    touched: float


class Engine:
    def __init__(self, config: Config, *, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._clock = clock
        self._counters: OrderedDict[tuple[str, str, str], Counter] = OrderedDict()
        self._rules = {
            s.id: [r for r in config.rules if r.service == s.id] for s in config.services
        }
        self._patterns = {
            r.id: re.compile(r.match.path_regex)
            for r in config.rules
            if r.match.path_regex is not None
        }

    def _expire(self, now: float) -> None:
        while self._counters:
            key, counter = next(iter(self._counters.items()))
            if now - counter.touched < self.config.state.ttl_seconds:
                break
            self._counters.popitem(last=False)
            logger.warning("state_expired service=%s rule=%s", key[0], key[1])

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
            match = rule.match
            if match.methods is not None and method not in match.methods:
                continue
            if match.path is not None and path != match.path:
                continue
            if rule.id in self._patterns and not self._patterns[rule.id].fullmatch(path):
                continue
            if any(normalized.get(k) != v for k, v in match.headers.items()):
                continue
            if any((k, v) not in pairs for k, v in match.query.items()):
                continue
            scope = "global" if rule.scope == "global" else normalized.get(rule.scope.lower(), "")
            if not scope or len(scope) > 256 or any(ord(ch) < 32 for ch in scope):
                raise ScenarioError("missing or invalid scope header (maximum 256 characters)")
            now = self._clock()
            self._expire(now)
            key = (service, rule.id, scope)
            counter = self._counters.get(key)
            if counter is None:
                if len(self._counters) >= self.config.state.capacity:
                    logger.warning("state_capacity service=%s rule=%s", service, rule.id)
                    raise ScenarioError("scenario state capacity reached; reset or wait for TTL")
                counter = Counter(0, now)
                self._counters[key] = counter
            counter.count += 1
            counter.touched = now
            self._counters.move_to_end(key)
            return Decision(
                service, rule.id, scope, counter.count, self._select(rule, counter.count)
            )
        return None

    @staticmethod
    def _select(rule: Rule, ordinal: int) -> Action:
        index = ordinal - rule.start_at
        if index < 0:
            return Pass()
        total = sum(a.repeat for a in rule.sequence)
        if index >= total:
            if rule.after_sequence == "passthrough":
                return Pass()
            if rule.after_sequence == "repeat_last":
                return rule.sequence[-1]
            index %= total
        for action in rule.sequence:
            if index < action.repeat:
                return action
            index -= action.repeat
        raise AssertionError("validated nonempty sequence has no selected action")

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
            del self._counters[key]
        return len(keys)

    def _matching_keys(
        self, *, service: str | None, rule: str | None, scope: str | None
    ) -> list[tuple[str, str, str]]:
        self._expire(self._clock())
        return [
            key
            for key in self._counters
            if (service is None or key[0] == service)
            and (rule is None or key[1] == rule)
            and (scope is None or key[2] == scope)
        ]
