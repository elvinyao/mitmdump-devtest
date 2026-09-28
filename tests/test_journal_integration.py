import asyncio
import json

import httpx
import pytest
from conftest import free_port, rule

AUTH = {"Authorization": "Bearer test-token"}


async def test_retry_journal_and_authenticated_verification(proxy, upstream):
    scenario = rule(
        {"action": "respond", "status": 503, "repeat": 2}, path="/secret-path", scope="X-Test-ID"
    )
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        for _ in range(3):
            await client.post(
                app.url("orders") + "/secret-path?password=secret-query",
                content=b"secret-body",
                headers={
                    "X-Test-ID": "run",
                    "Authorization": "Bearer secret-auth",
                    "Cookie": "session=secret-cookie",
                },
            )
        response = await client.get(app.admin_url + "/requests", headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        rows = data["requests"]
        assert [r["status"] for r in rows] == [503, 503, 200]
        assert [r["upstream_received"] for r in rows] == [False, False, True]
        assert [r["ordinal"] for r in rows] == [1, 2, 3]
        assert len(upstream[1]) == 1
        assert "secret-" not in json.dumps(data)
        expected = {"scope": "run", "count": 3, "statuses": [503, 503, 200]}
        result = await client.post(app.admin_url + "/verify", headers=AUTH, json=expected)
        assert result.status_code == 200 and result.json()["matched"] is True
        failed = await client.post(app.admin_url + "/verify", headers=AUTH, json={"count": 2})
        assert failed.status_code == 200 and failed.json()["matched"] is False
        for method, path in [
            ("GET", "/requests"),
            ("POST", "/requests/reset"),
            ("POST", "/verify"),
        ]:
            assert (await client.request(method, app.admin_url + path)).status_code == 401
        await client.post(app.admin_url + "/reset", headers=AUTH, json={"scope": "run"})
        assert len(app.journal.page()["requests"]) == 3
        cleared = await client.post(app.admin_url + "/requests/reset", headers=AUTH, json={})
        assert cleared.status_code == 200
        checkpoint = cleared.json()["checkpoint"]
        await client.post(app.url("orders") + "/secret-path", headers={"X-Test-ID": "run"})
        rerun = await client.post(
            app.admin_url + "/verify",
            headers=AUTH,
            json={"after": checkpoint, "count": 1, "statuses": [503]},
        )
        assert rerun.json()["matched"] is True


@pytest.mark.parametrize(
    "action,seen",
    [
        ("reset", False),
        ("disconnect", False),
        ("timeout", False),
        ("reset_after", True),
        ("disconnect_after", True),
    ],
)
async def test_transport_outcomes_are_terminal_and_not_http_errors(proxy, action, seen):
    step = {"action": action}
    if action == "timeout":
        step["seconds"] = 0.02
    async with proxy([rule(step)]) as app, httpx.AsyncClient() as client:
        with pytest.raises(httpx.TransportError):
            await client.get(app.url("orders") + "/fault")
        rows = app.journal.page()["requests"]
        assert len(rows) == 1
        assert rows[0]["status"] is None
        assert rows[0]["outcome"] == action
        assert rows[0]["upstream_received"] is seen


async def test_pending_cancellation_unmatched_and_arrival_order(proxy):
    async with proxy([rule({"action": "delay_after", "seconds": 60.0})]) as app:
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        writer.write(b"GET /fault HTTP/1.1\r\nHost: test\r\n\r\n")
        await writer.drain()
        async with asyncio.timeout(2):
            while not app.engine.snapshot():
                await asyncio.sleep(0.005)
        assert app.journal.verify(count=1)["complete"] is False
        async with httpx.AsyncClient() as client:
            assert (await client.get(app.url("orders") + "/normal")).status_code == 200
        rows = app.journal.page()["requests"]
        assert rows[0]["outcome"] == "pending"
        assert rows[1]["rule"] is None and rows[1]["status"] == 200
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(2):
            while not app.journal.verify(count=2)["complete"]:
                await asyncio.sleep(0.005)
        assert app.journal.page()["requests"][0]["outcome"] == "client_disconnected"


async def test_real_upstream_failure_does_not_claim_respond_after_status(proxy):
    services = [
        {"id": "orders", "port": free_port(), "upstream": f"http://127.0.0.1:{free_port()}"}
    ]
    async with (
        proxy([rule({"action": "respond_after", "status": 201})], services=services) as app,
        httpx.AsyncClient() as client,
    ):
        assert (await client.get(app.url("orders") + "/fault")).status_code == 502
        row = app.journal.page()["requests"][0]
        assert row["outcome"] == "transport_error" and row["status"] is None
        assert row["upstream_received"] is False


async def test_eviction_or_disabled_journal_never_passes_http_verification(proxy):
    for capacity in [0, 2]:
        async with proxy(limits={"journal_capacity": capacity}) as app:
            async with httpx.AsyncClient() as client:
                for _ in range(3):
                    await client.get(app.url("orders") + "/normal")
                response = await client.post(
                    app.admin_url + "/verify", headers=AUTH, json={"count": capacity}
                )
                assert response.status_code == 200
                assert response.json()["complete"] is False
                assert response.json()["matched"] is False


@pytest.mark.parametrize(
    "path",
    [
        "/requests?scope=a&scope=b",
        "/requests?limit=0",
        "/requests?limit=1001",
        "/requests?limit=true",
        "/requests?unknown=1",
        "/requests?after=garbage",
    ],
)
async def test_request_query_validation(proxy, path):
    async with proxy() as app, httpx.AsyncClient() as client:
        assert (await client.get(app.admin_url + path, headers=AUTH)).status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"count": True},
        {"count": -1},
        {"count": "1"},
        {"count": 1, "statuses": [True]},
        {"count": 1, "statuses": []},
        {"count": 1, "statuses": [999]},
        {"count": 0, "min_interval_seconds": -1},
        {"count": 0, "unknown": 1},
        {"count": 0, "after": "garbage"},
        {"count": 0, "scope": 1},
    ],
)
async def test_verify_strict_validation(proxy, body):
    async with proxy() as app, httpx.AsyncClient() as client:
        response = await client.post(app.admin_url + "/verify", headers=AUTH, json=body)
        assert response.status_code == 400


async def test_clear_rejects_filters_and_body_limits_still_apply(proxy):
    async with proxy() as app, httpx.AsyncClient() as client:
        assert (
            await client.post(
                app.admin_url + "/requests/reset", headers=AUTH, json={"scope": "run"}
            )
        ).status_code == 400
        assert (
            await client.post(app.admin_url + "/verify", headers=AUTH, content="invalid")
        ).status_code == 400
        assert (
            await client.post(
                app.admin_url + "/verify", headers=AUTH, json={"count": 0, "scope": "a" * 5000}
            )
        ).status_code == 413


async def test_scenario_errors_and_slow_uploads_are_not_recorded(proxy):
    async with proxy([rule({"action": "reset"}, scope="X-Test-ID")]) as app:
        async with httpx.AsyncClient() as client:
            assert (await client.get(app.url("orders") + "/fault")).status_code == 400
        reader, writer = await asyncio.open_connection("127.0.0.1", app.config.services[0].port)
        writer.write(b"POST /normal HTTP/1.1\r\nHost: test\r\nContent-Length: 100\r\n\r\nx")
        await writer.drain()
        assert app.journal.page()["requests"] == []
        writer.close()
        await writer.wait_closed()
