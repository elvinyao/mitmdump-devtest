from dataclasses import FrozenInstanceError
from typing import cast

import pytest
from pydantic import ValidationError

from fault_engine.config import Config, Respond
from fault_engine.engine import EXPIRY_BATCH_SIZE, Engine
from fault_engine.plan import compile_plan


def source_config():
    return Config.model_validate(
        {
            "services": [{"id": "s", "port": 8080, "upstream": "http://localhost"}],
            "rules": [
                {
                    "id": "r",
                    "service": "s",
                    "match": {"methods": ["GET"], "headers": {"X-Mode": "test"}},
                    "sequence": [
                        {
                            "action": "respond",
                            "status": 200,
                            "headers": {"X-Value": "original"},
                            "json_body": {"items": [1, 2]},
                            "repeat": 2,
                        },
                        {"action": "reset", "repeat": 3},
                    ],
                }
            ],
        }
    )


def test_source_mutation_cannot_change_matching_or_inflight_response():
    config = source_config()
    engine = Engine(config)
    decision = engine.decide("s", "GET", "/", {"x-mode": "test"}, [])
    assert decision is not None
    response = config.rules[0].sequence[0]
    assert isinstance(response, Respond)
    assert isinstance(response.json_body, dict)
    response.json_body["items"] = [999]
    response.headers["X-Value"] = "changed"
    config.rules[0].match.methods.clear()
    config.rules[0].match.headers.clear()
    config.rules[0].sequence.clear()
    config.rules.clear()
    config.services.clear()

    assert decision.action.body_bytes() == b'{"items": [1, 2]}'
    assert decision.action.headers["X-Value"] == "original"
    assert engine.decide("s", "POST", "/", {"x-mode": "test"}, []) is None
    assert engine.decide("s", "GET", "/", {}, []) is None
    following = engine.decide("s", "GET", "/", {"x-mode": "test"}, [])
    assert following is not None
    assert following.action is decision.action
    assert len(engine.plan.services) == len(engine.plan.rules) == 1


def test_plan_collections_and_nested_fields_are_read_only():
    plan = compile_plan(source_config())
    assert compile_plan(plan) is plan
    assert Engine(plan).plan is plan
    assert isinstance(plan.rules, tuple)
    assert isinstance(plan.rules[0].match.methods, tuple)
    for target, attribute in ((plan, "body_limit"), (plan.rules[0], "scope")):
        with pytest.raises(FrozenInstanceError):
            setattr(target, attribute, "changed")
    with pytest.raises(TypeError):
        cast(dict[str, str], plan.rules[0].sequence[0].headers)["X-Value"] = "changed"
    with pytest.raises(TypeError):
        cast(dict[str, str], plan.rules[0].match.headers)["x-mode"] = "changed"
    assert plan.rules[0].cumulative_repeats == (2, 5)
    assert plan.rules[0].total_repeats == 5


@pytest.mark.parametrize(
    "body,expected",
    [({"json_body": None}, b"null"), ({"body_base64": "AP8="}, b"\x00\xff")],
)
def test_response_encoding_is_materialized_once(body, expected):
    config = source_config()
    config.rules[0].sequence[:] = [
        Respond.model_validate({"action": "respond", "status": 200, **body})
    ]
    action = compile_plan(config).rules[0].sequence[0]
    assert action.body_bytes() == expected
    assert action.body_bytes() is action.body
    assert action.json_body_set is ("json_body" in body)
    if "json_body" in body:
        assert (b"Content-Type", b"application/json") in action.wire_headers


@pytest.mark.parametrize("field", ["max_connections", "max_inflight_requests", "state_page_size"])
@pytest.mark.parametrize("value", [0, -1, True, 1_000_001])
def test_resource_limits_require_bounded_positive_integers(field, value):
    data = source_config().model_dump(exclude_unset=True)
    data["limits"] = {field: value}
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def scoped_engine(*, clock=lambda: 0.0, capacity=10000):
    data = source_config().model_dump(exclude_unset=True)
    data["rules"][0]["scope"] = "X-Test-ID"
    data["rules"][0]["match"] = {}
    data["state"] = {"capacity": capacity, "ttl_seconds": 10.0}
    return Engine(Config.model_validate(data), clock=clock)


def hit(engine, scope):
    return engine.decide("s", "GET", "/", {"X-Test-ID": scope}, [])


def test_expired_selected_scope_restarts_beyond_cleanup_batch(caplog):
    now = [0.0]
    engine = scoped_engine(clock=lambda: now[0], capacity=EXPIRY_BATCH_SIZE + 5)
    for index in range(EXPIRY_BATCH_SIZE + 5):
        hit(engine, str(index))
    now[0] = 11.0
    selected = hit(engine, str(EXPIRY_BATCH_SIZE + 4))
    assert selected is not None
    assert selected.ordinal == 1
    notices = [record.message for record in caplog.records if "state_expired" in record.message]
    assert notices == [f"state_expired count={EXPIRY_BATCH_SIZE}", "state_expired count=1"]
    assert engine.snapshot() == [
        {"service": "s", "rule": "r", "scope": str(EXPIRY_BATCH_SIZE + 4), "count": 1}
    ]


def test_expired_backlog_neither_consumes_capacity_nor_counts_as_reset():
    now = [0.0]
    engine = scoped_engine(clock=lambda: now[0], capacity=EXPIRY_BATCH_SIZE * 2 + 5)
    for index in range(EXPIRY_BATCH_SIZE * 2 + 5):
        hit(engine, str(index))
    now[0] = 11.0
    assert hit(engine, "new").ordinal == 1
    assert engine.reset(scope=str(EXPIRY_BATCH_SIZE * 2 + 4)) == 0
    assert engine.snapshot()[0]["scope"] == "new"


def test_state_cursor_ignores_lru_changes_and_excludes_new_counter_ids():
    engine = scoped_engine()
    for scope in ("a", "b", "c"):
        hit(engine, scope)
    first, cursor = engine.snapshot_page(limit=1)
    assert first[0]["scope"] == "a"
    assert cursor is not None
    hit(engine, "a")
    engine.reset(scope="b")
    hit(engine, "d")
    last, cursor = engine.snapshot_page(limit=1, cursor=cursor)
    assert [row["scope"] for row in last] == ["c"]
    assert cursor is None


def test_fresh_state_query_skips_all_historical_reset_ids():
    engine = scoped_engine()
    for index in range(EXPIRY_BATCH_SIZE + 5):
        hit(engine, str(index))
    engine.reset()
    hit(engine, "new")
    rows, cursor = engine.snapshot_page(limit=1)
    assert [row["scope"] for row in rows] == ["new"]
    assert cursor is None


def test_state_cursor_sparse_scan_is_bounded_and_progresses():
    engine = scoped_engine()
    for index in range(EXPIRY_BATCH_SIZE + 5):
        hit(engine, str(index))
    hit(engine, "new")
    empty, cursor = engine.snapshot_page(limit=1, scope="new")
    assert empty == []
    assert cursor is not None
    last, cursor = engine.snapshot_page(limit=1, cursor=cursor, scope="new")
    assert [row["scope"] for row in last] == ["new"]
    assert cursor is None


def test_small_live_set_skips_large_internal_reset_gaps():
    engine = scoped_engine()
    hit(engine, "old")
    for index in range(EXPIRY_BATCH_SIZE + 5):
        hit(engine, str(index))
        engine.reset(scope=str(index))
    hit(engine, "new")
    rows, cursor = engine.snapshot_page(limit=1)
    assert [row["scope"] for row in rows] == ["old"]
    assert cursor is not None
    last, cursor = engine.snapshot_page(limit=1, cursor=cursor)
    assert [row["scope"] for row in last] == ["new"]
    assert cursor is None


def test_state_cursor_rejects_changed_selection_other_engine_and_malformed_tokens():
    engine = scoped_engine()
    hit(engine, "a")
    hit(engine, "b")
    _, cursor = engine.snapshot_page(limit=1)
    assert cursor is not None
    with pytest.raises(ValueError, match="cursor"):
        engine.snapshot_page(limit=1, cursor=cursor, scope="b")
    with pytest.raises(ValueError, match="cursor"):
        scoped_engine().snapshot_page(limit=1, cursor=cursor)
    for invalid in ("%%%", "W10=", "a" * 513):
        with pytest.raises(ValueError, match="cursor"):
            engine.snapshot_page(limit=1, cursor=invalid)
