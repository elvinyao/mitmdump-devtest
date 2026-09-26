"""Exercise the checked-in client scenarios over a real proxy connection."""

import json

import httpx
import pytest

from fault_engine.config import load_config
from fault_engine.engine import Engine


def example_rule(rule_id):
    config = load_config("examples/scenarios.yaml")
    return next(r for r in config.rules if r.id == rule_id).model_dump(exclude_unset=True)


def test_every_catalog_rule_is_reachable_in_the_full_ordered_configuration():
    config = load_config("examples/scenarios.yaml")
    engine = Engine(config)
    regex_paths = {
        "targeted-write-retry": "/payments/12",
        "maintenance-window": "/maintenance/orders",
    }
    # Most integration tests isolate one rule. This catches a new broad match or
    # reordering that silently shadows another example in the complete demo.
    for rule in config.rules:
        path = rule.match.path if rule.match.path is not None else regex_paths[rule.id]
        headers = dict(rule.match.headers)
        if rule.scope != "global":
            headers[rule.scope] = "catalog-check"
        decision = engine.decide(
            rule.service,
            rule.match.methods[0] if rule.match.methods else "GET",
            path,
            headers,
            list(rule.match.query.items()),
        )
        assert decision is not None, rule.id
        assert decision.rule_id == rule.id, f"{rule.id} was shadowed by {decision.rule_id}"


@pytest.mark.parametrize(
    ("rule_id", "method", "path", "status", "body_fragment"),
    [
        ("authentication-required", "GET", "/unauthorized", 401, b"token_expired"),
        ("write-conflict", "POST", "/conflict", 409, b"version_conflict"),
        ("retry-budget", "GET", "/always-unavailable", 503, b"unavailable"),
        ("html-gateway-error", "GET", "/html-error", 502, b"<html>"),
        ("business-error", "GET", "/business-error", 200, b"quota_exceeded"),
        ("gateway-timeout", "GET", "/gateway-timeout", 504, b"gateway_timeout"),
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


async def test_example_targeted_retry_only_counts_matching_requests(proxy, upstream):
    async with proxy([example_rule("targeted-write-retry")]) as app:
        async with httpx.AsyncClient(base_url=app.url("orders")) as client:
            # A nonmatching request does not need a scope and must not advance the sequence.
            for method, path, headers in [
                ("GET", "/payments/12?mode=fault", {"X-Client": "mobile"}),
                ("POST", "/payments/12/extra?mode=fault", {"X-Client": "mobile"}),
                ("POST", "/payments/12?mode=normal", {"X-Client": "mobile"}),
                ("POST", "/payments/12?mode=fault", {"X-Client": "web"}),
            ]:
                response = await client.request(method, path, headers=headers)
                assert response.status_code == 200
            assert len(upstream[1]) == 4
            assert app.engine.snapshot() == []

            headers = {"x-client": "mobile", "X-Test-Run-ID": "targeted-a"}
            # Any equal occurrence of a repeated query key matches, including URL decoding.
            path = "/payments/12?mode=normal&mode=f%61ult"
            for method, status in [("POST", 429), ("PUT", 429), ("POST", 200)]:
                response = await client.request(method, path, headers=headers)
                assert response.status_code == status
                if status == 429:
                    assert response.headers["retry-after"] == "1"
                    assert response.json() == {"error": "targeted_rate_limit"}
            assert len(upstream[1]) == 5
            assert "x-test-run-id" not in {h.lower() for h in upstream[1][-1]["headers"]}

            headers["X-Test-Run-ID"] = "targeted-b"
            response = await client.post(path, headers=headers)
            assert response.status_code == 429
            assert {row["scope"]: row["count"] for row in app.engine.snapshot()} == {
                "targeted-a": 3,
                "targeted-b": 1,
            }
            assert len(upstream[1]) == 5


async def test_example_maintenance_exception_precedes_general_rule(proxy, upstream):
    # Preserve catalog ordering so moving the exception below maintenance is caught.
    rules = [
        item.model_dump(exclude_unset=True)
        for item in load_config("examples/scenarios.yaml").rules
        if item.id in {"maintenance-health-exception", "maintenance-window"}
    ]
    async with proxy(rules) as app, httpx.AsyncClient(base_url=app.url("orders")) as client:
        for _ in range(3):
            health = await client.get("/maintenance/health")
            assert health.status_code == 200
            response = await client.get("/maintenance/orders")
            assert response.status_code == 503
            assert response.headers["retry-after"] == "5"
        response = await client.post("/maintenance/health")
        assert response.status_code == 503
        assert len(upstream[1]) == 3
        assert {row["rule"]: row["count"] for row in app.engine.snapshot()} == {
            "maintenance-health-exception": 3,
            "maintenance-window": 4,
        }


async def test_example_recovery_cycle_warmup_scope_isolation_and_reset(proxy, upstream):
    async with proxy([example_rule("recovery-cycle")]) as app:
        async with httpx.AsyncClient(base_url=app.url("orders")) as client:
            for expected in [200, 200, 503, 503, 200, 200, 503, 503, 200, 200]:
                response = await client.get("/recovery-cycle", headers={"X-Test-Run-ID": "cycle-a"})
                assert response.status_code == expected
            for expected in [200, 200, 503]:
                response = await client.get("/recovery-cycle", headers={"X-Test-Run-ID": "cycle-b"})
                assert response.status_code == expected
            assert len(upstream[1]) == 8

            response = await client.post(
                f"http://127.0.0.1:{app.config.admin.port}/reset",
                headers={"Authorization": "Bearer test-token"},
                json={"service": "orders", "rule": "recovery-cycle", "scope": "cycle-a"},
            )
            assert response.status_code == 200
            assert response.json() == {"reset": 1}
            # Only A returns to its warmup; B keeps its already allocated position.
            for scope, expected in [("cycle-a", 200), ("cycle-b", 503)]:
                response = await client.get("/recovery-cycle", headers={"X-Test-Run-ID": scope})
                assert response.status_code == expected
            assert len(upstream[1]) == 9
            assert {row["scope"]: row["count"] for row in app.engine.snapshot()} == {
                "cycle-a": 1,
                "cycle-b": 4,
            }


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_example_cache_validation_has_no_body(proxy, upstream, method):
    async with proxy([example_rule("cached-resource")]) as app:
        async with httpx.AsyncClient(base_url=app.url("orders")) as client:
            response = await client.request(
                method, "/cached", headers={"If-None-Match": '"demo-v1"'}
            )
            assert response.status_code == 304
            assert response.headers["etag"] == '"demo-v1"'
            assert response.content == b""
            assert not upstream[1]
            for headers in [{}, {"If-None-Match": '"other-version"'}]:
                response = await client.request(method, "/cached", headers=headers)
                assert response.status_code == 200
            assert len(upstream[1]) == 2
            assert app.engine.snapshot()[0]["count"] == 1


async def test_example_delete_success_has_no_body_and_does_not_match_get(proxy, upstream):
    async with proxy([example_rule("delete-no-content")]) as app:
        async with httpx.AsyncClient(base_url=app.url("orders")) as client:
            for _ in range(2):
                response = await client.delete("/no-content")
                assert response.status_code == 204
                assert response.content == b""
            assert not upstream[1]
            response = await client.get("/no-content")
            assert response.status_code == 200
            assert len(upstream[1]) == 1
            assert app.engine.snapshot()[0]["count"] == 2


async def test_example_binary_head_then_get_preserves_connection_framing(proxy, upstream):
    async with proxy([example_rule("binary-download")]) as app:
        async with httpx.AsyncClient(base_url=app.url("orders")) as client:
            response = await client.head("/binary")
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/octet-stream"
            assert response.headers["content-length"] == "4"
            assert response.content == b""
            response = await client.get("/binary")
            assert response.status_code == 200
            assert response.content == b"\x00\x01\xff\x80"
            assert not upstream[1]
