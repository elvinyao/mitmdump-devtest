"""Control-plane discovery and state selection contracts for concurrent test runs."""

import httpx
import pytest
from conftest import free_port, rule

from fault_engine.config import Config
from fault_engine.engine import Engine

AUTH = {"Authorization": "Bearer test-token"}


def state_config():
    return Config.model_validate(
        {
            "services": [
                {"id": "orders", "port": 8080, "upstream": "http://localhost:9000"},
                {"id": "inventory", "port": 8081, "upstream": "http://localhost:9001"},
            ],
            "rules": [
                rule({"action": "passthrough"}, id="orders-rule", scope="X-Test-ID"),
                rule(
                    {"action": "passthrough"},
                    id="inventory-rule",
                    service="inventory",
                    scope="X-Test-ID",
                ),
            ],
            "state": {"ttl_seconds": 10.0},
        }
    )


def populate(engine):
    for service, scope in [("orders", "a"), ("orders", "b"), ("inventory", "a")]:
        engine.decide(service, "GET", "/fault", {"X-Test-ID": scope}, [])


def test_reset_does_not_count_expired_state_or_refresh_live_state():
    now = [0.0]
    engine = Engine(state_config(), clock=lambda: now[0])
    engine.decide("orders", "GET", "/fault", {"X-Test-ID": "expired"}, [])
    now[0] = 5.0
    engine.decide("orders", "GET", "/fault", {"X-Test-ID": "live"}, [])
    now[0] = 10.0
    assert engine.reset(scope="expired") == 0
    assert engine.snapshot(scope="live")[0]["count"] == 1
    now[0] = 15.0
    assert engine.reset() == 0
    assert engine.snapshot() == []


@pytest.mark.parametrize(
    "filters,expected",
    [
        ({}, [("orders", "a"), ("orders", "b"), ("inventory", "a")]),
        ({"service": "orders"}, [("orders", "a"), ("orders", "b")]),
        ({"rule": "inventory-rule"}, [("inventory", "a")]),
        ({"scope": "a"}, [("orders", "a"), ("inventory", "a")]),
        ({"service": "orders", "rule": "orders-rule", "scope": "b"}, [("orders", "b")]),
        ({"service": "orders", "rule": "inventory-rule"}, []),
        ({"service": "unknown"}, []),
        ({"scope": ""}, []),
    ],
)
async def test_admin_state_filters_are_exact_and_leave_other_scopes_intact(
    proxy, filters, expected
):
    config = state_config()
    services = [service.model_copy(update={"port": free_port()}) for service in config.services]
    async with proxy(config.rules, services=services) as app, httpx.AsyncClient() as client:
        populate(app.engine)
        baseline = app.engine.snapshot()
        response = await client.get(app.admin_url + "/state", params=filters, headers=AUTH)
        assert response.status_code == 200
        assert [(row["service"], row["scope"]) for row in response.json()["counters"]] == expected
        assert app.engine.snapshot() == baseline


@pytest.mark.parametrize(
    "query",
    ["scpoe=a", "scope=a&scope=b", "scope=a&scope=a", "service=orders&service=orders"],
)
async def test_admin_state_rejects_ambiguous_or_unknown_filters_without_mutation(proxy, query):
    scenario = rule({"action": "passthrough"}, scope="X-Test-ID")
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        app.engine.decide("orders", "GET", "/fault", {"X-Test-ID": "a"}, [])
        baseline = app.engine.snapshot()
        response = await client.get(app.admin_url + "/state?" + query, headers=AUTH)
        assert response.status_code == 400
        assert "error" in response.json()
        assert app.engine.snapshot() == baseline
        response = await client.get(app.admin_url + "/state?" + query)
        assert response.status_code == 401


async def test_rules_discover_matching_and_step_parameters_without_sensitive_values(proxy):
    scenario = rule(
        {"action": "respond", "status": 429},
        match={
            "methods": ["POST"],
            "path_regex": "/orders/[0-9]+",
            "headers": {"Authorization": "SECRET_HEADER"},
            "query": {"token": "SECRET_QUERY"},
        },
        sequence=[
            {
                "action": "respond_after",
                "repeat": 2,
                "status": 503,
                "delay_seconds": 0.1,
                "headers": {"Set-Cookie": "SECRET_COOKIE"},
                "json_body": {"token": "SECRET_BODY"},
            },
            {"action": "delay_before", "seconds": 0.2},
            {"action": "reset"},
            {"action": "passthrough"},
        ],
    )
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        response = await client.get(app.admin_url + "/rules", headers=AUTH)
        assert response.status_code == 200
        assert "SECRET" not in response.text
        summary = response.json()["rules"][0]
        assert summary["actions"] == ["respond_after", "delay_before", "reset", "passthrough"]
        assert summary["match"] == {
            "methods": ["POST"],
            "path": None,
            "path_regex": "/orders/[0-9]+",
            "header_names": ["authorization"],
            "query_names": ["token"],
        }
        assert summary["sequence"] == [
            {"action": "respond_after", "repeat": 2, "status": 503, "delay_seconds": 0.1},
            {"action": "delay_before", "repeat": 1, "seconds": 0.2},
            {"action": "reset", "repeat": 1},
            {"action": "passthrough", "repeat": 1},
        ]
