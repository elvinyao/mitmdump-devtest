"""Destructive admin operations must not silently broaden their selection."""

import httpx
import pytest
from conftest import rule

AUTH = {"Authorization": "Bearer test-token"}


async def populate_scopes(app, client):
    for scope in ["a", "b", "b"]:
        response = await client.get(app.url("orders") + "/fault", headers={"X-Test-ID": scope})
        assert response.status_code == 503
    response = await client.get(app.admin_url + "/state", headers=AUTH)
    assert response.status_code == 200
    counters = response.json()["counters"]
    assert [(counter["scope"], counter["count"]) for counter in counters] == [("a", 1), ("b", 2)]
    return response.json()


@pytest.mark.parametrize(
    "query",
    [
        "scope=a",
        "service=orders",
        "rule=r",
        "unknown=a",
        "scope=",
        "service=",
        "rule=",
        "unknown=",
        "scope",
        "scope=a&scope=b",
    ],
)
@pytest.mark.parametrize("body", [{}, {"service": "orders", "rule": "r", "scope": "a"}])
async def test_reset_rejects_query_parameters_without_changing_any_scope(proxy, query, body):
    scenario = rule({"action": "respond", "status": 503, "repeat": 2}, scope="X-Test-ID")
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        before = await populate_scopes(app, client)

        response = await client.post(app.admin_url + "/reset?" + query, headers=AUTH, json=body)

        assert response.status_code == 400
        assert "error" in response.json()
        after = await client.get(app.admin_url + "/state", headers=AUTH)
        assert after.status_code == 200
        assert after.json() == before


@pytest.mark.parametrize(
    ("body", "expected_reset", "remaining_scopes"),
    [
        ({"service": "orders", "rule": "r", "scope": "a"}, 1, ["b"]),
        ({}, 2, []),
    ],
)
async def test_reset_accepts_explicit_json_selection_and_global_reset(
    proxy, body, expected_reset, remaining_scopes
):
    scenario = rule({"action": "respond", "status": 503, "repeat": 2}, scope="X-Test-ID")
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        before = await populate_scopes(app, client)

        response = await client.post(app.admin_url + "/reset", headers=AUTH, json=body)

        assert response.status_code == 200
        assert response.json() == {"reset": expected_reset}
        after = await client.get(app.admin_url + "/state", headers=AUTH)
        assert after.status_code == 200
        assert after.json()["counters"] == [
            counter for counter in before["counters"] if counter["scope"] in remaining_scopes
        ]
