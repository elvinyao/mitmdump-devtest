"""Immutable runtime snapshots, independent of the validated configuration models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import accumulate
from types import MappingProxyType

from fault_engine.config import Action, Config, Delay, Respond
from fault_engine.matching import PathPattern, compile_path_pattern


@dataclass(frozen=True, slots=True)
class ServicePlan:
    id: str
    host: str
    port: int
    upstream: str


@dataclass(frozen=True, slots=True)
class StatePlan:
    capacity: int
    ttl_seconds: float


@dataclass(frozen=True, slots=True)
class LimitsPlan:
    max_connections: int
    max_inflight_requests: int
    state_page_size: int


@dataclass(frozen=True, slots=True)
class AdminPlan:
    host: str
    port: int
    token_env: str


@dataclass(frozen=True, slots=True)
class MatchPlan:
    methods: tuple[str, ...] | None
    path: str | None
    path_regex: str | None
    headers: Mapping[str, str]
    query: Mapping[str, str]
    pattern: PathPattern | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ActionPlan:
    action: str
    repeat: int = 1
    status: int | None = None
    seconds: float | None = None
    delay_seconds: float = 0
    jitter_seconds: float = 0
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    wire_headers: tuple[tuple[bytes, bytes], ...] = ()
    body: bytes = field(default=b"", repr=False)
    json_body_set: bool = False

    def body_bytes(self) -> bytes:
        return self.body


PASSTHROUGH = ActionPlan("passthrough")


@dataclass(frozen=True, slots=True)
class RulePlan:
    id: str
    service: str
    match: MatchPlan
    scope: str
    start_at: int
    sequence: tuple[ActionPlan, ...]
    after_sequence: str
    cumulative_repeats: tuple[int, ...]
    total_repeats: int
    probability: float
    seed: int


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    version: int
    services: tuple[ServicePlan, ...]
    rules: tuple[RulePlan, ...]
    state: StatePlan
    admin: AdminPlan
    limits: LimitsPlan
    body_limit: int
    upstream_ca: str | None


def _action_plan(action: Action) -> ActionPlan:
    if isinstance(action, Respond):
        headers = dict(action.headers)
        json_body_set = "json_body" in action.model_fields_set
        if json_body_set and not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = "application/json"
        return ActionPlan(
            action=action.action,
            repeat=action.repeat,
            status=action.status,
            delay_seconds=action.delay_seconds,
            jitter_seconds=action.jitter_seconds,
            headers=MappingProxyType(headers),
            wire_headers=tuple(
                (name.encode("ascii"), value.encode("latin-1")) for name, value in headers.items()
            ),
            body=action.body_bytes(),
            json_body_set=json_body_set,
        )
    return ActionPlan(
        action=action.action,
        repeat=action.repeat,
        seconds=action.seconds if isinstance(action, Delay) else None,
        jitter_seconds=action.jitter_seconds if isinstance(action, Delay) else 0,
    )


def compile_plan(config: Config | ExecutionPlan) -> ExecutionPlan:
    """Copy every mutable input collection; an existing plan is shared unchanged."""
    if isinstance(config, ExecutionPlan):
        return config
    rules = []
    for rule in config.rules:
        match = rule.match
        sequence = tuple(_action_plan(action) for action in rule.sequence)
        cumulative_repeats = tuple(accumulate(action.repeat for action in sequence))
        rules.append(
            RulePlan(
                id=rule.id,
                service=rule.service,
                match=MatchPlan(
                    methods=tuple(match.methods) if match.methods is not None else None,
                    path=match.path,
                    path_regex=match.path_regex,
                    headers=MappingProxyType(dict(match.headers)),
                    query=MappingProxyType(dict(match.query)),
                    pattern=(
                        compile_path_pattern(match.path_regex)
                        if match.path_regex is not None
                        else None
                    ),
                ),
                scope=rule.scope,
                start_at=rule.start_at,
                sequence=sequence,
                after_sequence=rule.after_sequence,
                cumulative_repeats=cumulative_repeats,
                total_repeats=cumulative_repeats[-1],
                probability=rule.probability,
                seed=rule.seed,
            )
        )
    return ExecutionPlan(
        version=config.version,
        services=tuple(ServicePlan(s.id, s.host, s.port, s.upstream) for s in config.services),
        rules=tuple(rules),
        state=StatePlan(config.state.capacity, config.state.ttl_seconds),
        admin=AdminPlan(config.admin.host, config.admin.port, config.admin.token_env),
        limits=LimitsPlan(
            config.limits.max_connections,
            config.limits.max_inflight_requests,
            config.limits.state_page_size,
        ),
        body_limit=config.body_limit,
        upstream_ca=config.upstream_ca,
    )
