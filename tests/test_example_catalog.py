"""Exercise the checked-in client scenarios over a real proxy connection."""

import json

import httpx
import pytest

from fault_engine.config import load_config


def example_rule(rule_id):
    config = load_config("examples/scenarios.yaml")
    return next(r for r in config.rules if r.id == rule_id).model_dump(exclude_unset=True)


@pytest.mark.parametrize(
    ("rule_id", "method", "path", "status", "body_fragment"),
    [
        ("authentication-required", "GET", "/unauthorized", 401, b"token_expired"),
        ("write-conflict", "POST", "/conflict", 409, b"version_conflict"),
        ("retry-budget", "GET", "/always-unavailable", 503, b"unavailable"),
        ("html-gateway-error", "GET", "/html-error", 502, b"<html>"),
        ("business-error", "GET", "/business-error", 200, b"quota_exceeded"),
    ],
)
async def test_persistent_example_errors(
    proxy, upstream, rule_id, method, path, status, body_fragment
):
    async with proxy([example_rule(rule_id)]) as app, httpx.AsyncClient() as client:
        for _ in range(4):
            response = await client.request(method, app.url("orders") + path)
            assert response.status_code == status
            assert body_fragment in response.content
        assert not upstream[1]
        assert app.engine.snapshot()[0]["count"] == 4


@pytest.mark.parametrize(
    ("rule_id", "path", "body"),
    [
        ("malformed-json", "/invalid-json", b'{"incomplete":'),
        ("empty-json-response", "/empty-json", b""),
    ],
)
async def test_example_parse_failure_despite_http_success(proxy, upstream, rule_id, path, body):
    async with proxy([example_rule(rule_id)]) as app, httpx.AsyncClient() as client:
        response = await client.get(app.url("orders") + path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.content == body
        with pytest.raises(json.JSONDecodeError):
            response.json()
        assert not upstream[1]


async def test_example_redirect_preserves_post_and_body(proxy, upstream):
    async with proxy([example_rule("preserve-method-redirect")]) as app:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.post(app.url("orders") + "/redirect", content=b"write")
            assert response.status_code == 200
            assert [r.status_code for r in response.history] == [307]
            assert len(upstream[1]) == 1
            assert upstream[1][0]["method"] == "POST"
            assert upstream[1][0]["path"] == "/echo"
            assert upstream[1][0]["body"] == "d3JpdGU="


async def test_example_mixed_retry_progresses_after_timeout(proxy, upstream):
    async with proxy([example_rule("mixed-retry")]) as app:
        async with httpx.AsyncClient(timeout=0.1, headers={"X-Test-Run-ID": "mixed"}) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.get(app.url("orders") + "/mixed-retry")
            response = await client.get(app.url("orders") + "/mixed-retry")
            assert response.status_code == 503
            response = await client.get(app.url("orders") + "/mixed-retry")
            assert response.status_code == 200
            assert response.json() == {"ok": True}
            assert app.engine.snapshot()[0]["count"] == 3
            assert not upstream[1]
