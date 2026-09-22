import asyncio
import logging

import pytest

from fault_engine.config import Config
from fault_engine.engine import Engine, ScenarioError


def make_engine(rule=None, state=None, clock=None):
    scenario = {
        "id": "r",
        "service": "s",
        "sequence": [
            {"action": "respond", "status": 429, "repeat": 2},
        ],
    }
    scenario.update(rule or {})
    data = {
        "services": [{"id": "s", "port": 8080, "upstream": "http://localhost:9000"}],
        "rules": [scenario],
    }
    if state:
        data["state"] = state
    return Engine(Config.model_validate(data), **({"clock": clock} if clock else {}))


def decide(engine, headers=None, method="GET", path="/", query=None):
    return engine.decide("s", method, path, headers or {}, query or [])


def test_nth_and_repeat_boundary():
    engine = make_engine({"start_at": 3})
    decisions = [decide(engine) for _ in range(6)]
    assert [d.action.action for d in decisions] == [
        "passthrough",
        "passthrough",
        "respond",
        "respond",
        "passthrough",
        "passthrough",
    ]
    assert [d.ordinal for d in decisions] == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize(
    "policy,expected",
    [
        ("passthrough", [400, 503, None, None]),
        ("repeat_last", [400, 503, 503, 503]),
        ("cycle", [400, 503, 400, 503]),
    ],
)
def test_end_policy(policy, expected):
    engine = make_engine(
        {
            "sequence": [
                {"action": "respond", "status": 400},
                {"action": "respond", "status": 503},
            ],
            "after_sequence": policy,
        }
    )
    assert [getattr(decide(engine).action, "status", None) for _ in range(4)] == expected


def test_match_all_fields_first_match_and_nonmatching_does_not_count():
    engine = make_engine(
        {
            "match": {
                "methods": ["POST", "PUT"],
                "path_regex": r"/orders/\d+",
                "headers": {"X-Client": "mobile"},
                "query": {"tag": "b"},
            }
        }
    )
    assert decide(engine) is None
    assert decide(engine, {"X-Client": "mobile"}, "POST", "/orders/1/extra", [("tag", "b")]) is None
    assert decide(engine, {"X-Client": "mobile"}, "POST", "/orders/1", [("tag", "a")]) is None
    decision = decide(
        engine, {"X-CLIENT": "mobile"}, "PUT", "/orders/12", [("tag", "a"), ("tag", "b")]
    )
    assert decision.ordinal == 1
    config = engine.config.model_dump(exclude_unset=True)
    config["rules"].append({"id": "other", "service": "s", "sequence": [{"action": "reset"}]})
    other = Engine(Config.model_validate(config))
    assert decide(other, {"X-Client": "mobile"}, "POST", "/orders/1", [("tag", "b")]).rule_id == "r"
    assert decide(other).rule_id == "other"


def test_exact_path_and_case_sensitive_method():
    engine = make_engine({"match": {"path": "/A", "methods": ["CUSTOM"]}})
    assert decide(engine, method="custom", path="/A") is None
    assert decide(engine, method="CUSTOM", path="/a") is None
    assert decide(engine, method="CUSTOM", path="/A").ordinal == 1


def test_scope_reset_and_missing_scope():
    engine = make_engine({"scope": "X-Test-ID"})
    a = decide(engine, {"x-test-id": "a"})
    assert decide(engine, {"X-Test-ID": "a"}).ordinal == 2
    assert decide(engine, {"X-Test-ID": "b"}).ordinal == 1
    with pytest.raises(ScenarioError, match="scope"):
        decide(engine)
    assert engine.reset(service="s", rule="r", scope="a") == 1
    assert a.ordinal == 1  # already assigned decisions remain stable
    assert decide(engine, {"X-Test-ID": "a"}).ordinal == 1
    assert decide(engine, {"X-Test-ID": "b"}).ordinal == 2
    assert len(engine.snapshot()) == 2
    assert engine.reset(service="unmatched") == 0
    assert engine.reset() == 2


async def test_concurrent_allocation_is_unique():
    engine = make_engine()

    async def request():
        decision = decide(engine)
        await asyncio.sleep(0)
        return decision.ordinal

    assert sorted(await asyncio.gather(*(request() for _ in range(100)))) == list(range(1, 101))


def test_capacity_expiry_and_explicit_notice(caplog):
    now = [0.0]
    engine = make_engine(
        {"scope": "X-Test-ID"}, {"capacity": 1, "ttl_seconds": 10.0}, lambda: now[0]
    )
    decide(engine, {"X-Test-ID": "a"})
    with pytest.raises(ScenarioError, match="capacity"):
        decide(engine, {"X-Test-ID": "b"})
    now[0] = 9
    assert decide(engine, {"X-Test-ID": "a"}).ordinal == 2
    now[0] = 19
    with caplog.at_level(logging.WARNING):
        assert decide(engine, {"X-Test-ID": "b"}).ordinal == 1
    assert "state_expired" in caplog.text
    assert "state_capacity" in caplog.text
    assert engine.snapshot()[0]["scope"] == "b"


def test_oversized_scope_is_rejected():
    engine = make_engine({"scope": "X-Test-ID"})
    with pytest.raises(ScenarioError, match="scope"):
        decide(engine, {"X-Test-ID": "a" * 257})
