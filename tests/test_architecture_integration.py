"""Keep the data plane and control plane on one immutable execution plan."""

import asyncio

import httpx
from conftest import free_port

from fault_engine.config import Config, Respond
from fault_engine.runtime import Runtime


async def test_runtime_keeps_inflight_response_routing_and_admin_on_same_plan(upstream):
    source = Config.model_validate(
        {
            "services": [{"id": "orders", "port": free_port(), "upstream": upstream[0]}],
            "admin": {"port": free_port()},
            "rules": [
                {
                    "id": "stable",
                    "service": "orders",
                    "match": {"path": "/stable", "headers": {"X-Mode": "original"}},
                    "after_sequence": "repeat_last",
                    "sequence": [
                        {
                            "action": "respond",
                            "status": 201,
                            "delay_seconds": 0.1,
                            "headers": {"X-Version": "original"},
                            "json_body": {"version": "original"},
                        }
                    ],
                }
            ],
        }
    )
    runtime = Runtime(source, admin_token="test-token")
    await runtime.start()
    try:
        assert runtime.config is runtime.engine.plan
        assert runtime.config is runtime.addon.config
        async with httpx.AsyncClient(timeout=3) as client:
            pending = asyncio.create_task(
                client.get(runtime.url("orders") + "/stable", headers={"X-Mode": "original"})
            )
            try:
                async with asyncio.timeout(2):
                    while not runtime.addon.pending:
                        await asyncio.sleep(0.001)
                action = source.rules[0].sequence[0]
                assert isinstance(action, Respond) and isinstance(action.json_body, dict)
                action.json_body["version"] = "changed"
                action.headers["X-Version"] = "changed"
                source.rules[0].match.headers["X-Mode"] = "changed"
                source.rules[0].sequence.clear()
                source.rules.clear()
                source.services.clear()

                response = await pending
                assert response.status_code == 201
                assert response.json() == {"version": "original"}
                assert response.headers["x-version"] == "original"
                rules = await client.get(
                    runtime.admin_url + "/rules",
                    headers={"Authorization": "Bearer test-token"},
                )
                assert rules.status_code == 200
                assert [row["id"] for row in rules.json()["rules"]] == ["stable"]
                response = await client.get(
                    runtime.url("orders") + "/stable", headers={"X-Mode": "original"}
                )
                assert response.status_code == 201
                assert response.json() == {"version": "original"}
                assert not upstream[1]
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        await runtime.close()
