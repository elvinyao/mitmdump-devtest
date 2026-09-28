import asyncio
import json
import time

import httpx
import pytest
from conftest import rule
from pydantic import ValidationError

from fault_engine.config import Config
from fault_engine.engine import Engine


def config(**overrides):
    return Config.model_validate(
        {
            "services": [{"id": "orders", "port": 8080, "upstream": "http://localhost"}],
            "rules": [
                rule(
                    {"action": "respond", "status": 503},
                    scope="X-Test-ID",
                    after_sequence="repeat_last",
                    **overrides,
                )
            ],
        }
    )


def hit(engine, scope="a"):
    result = engine.decide("orders", "GET", "/fault", {"X-Test-ID": scope}, [])
    assert result is not None
    return result


def test_sampling_replays_after_reset_and_ignores_other_scopes():
    first = Engine(config(probability=0.5, seed=42))
    second = Engine(config(probability=0.5, seed=42))
    expected = [hit(first).sampled for _ in range(100)]
    actual = []
    for _ in range(100):
        hit(second, "other")
        actual.append(hit(second).sampled)
    assert actual == expected
    assert any(expected) and not all(expected)
    first.reset(scope="a")
    assert [hit(first).sampled for _ in range(100)] == expected
    assert [hit(Engine(config(probability=0.5, seed=n))).sampled for n in range(100)] != expected


@pytest.mark.parametrize("probability,action", [(0, "passthrough"), (1, "respond")])
def test_probability_endpoints_consume_ordinals(probability, action):
    engine = Engine(config(probability=probability))
    results = [hit(engine) for _ in range(5)]
    assert [d.ordinal for d in results] == [1, 2, 3, 4, 5]
    assert all(d.action.action == action for d in results)


def test_sampling_preserves_sequence_positions_and_delay_channel():
    data = config(probability=1, seed=17, start_at=3).model_dump(exclude_unset=True)
    data["rules"][0]["sequence"] = [
        {"action": "respond", "status": 501, "jitter_seconds": 0.1},
        {"action": "respond", "status": 502, "jitter_seconds": 0.1},
    ]
    data["rules"][0]["after_sequence"] = "cycle"
    always = Engine(Config.model_validate(data))
    data["rules"][0]["probability"] = 0.5
    sampled = Engine(Config.model_validate(data))
    for ordinal in range(1, 31):
        expected, actual = hit(always), hit(sampled)
        assert actual.ordinal == expected.ordinal == ordinal
        if ordinal < 3:
            assert actual.action.action == "passthrough"
        elif actual.sampled:
            assert actual.action.status == 501 + ((ordinal - 3) % 2)
            assert actual.action.delay_seconds == expected.action.delay_seconds
        else:
            assert actual.action.action == "passthrough"


@pytest.mark.parametrize(
    "action", ["respond", "respond_after", "delay_before", "delay_after", "timeout"]
)
def test_jitter_is_bounded_replayable_and_does_not_mutate_plan(action):
    step = {"action": action, "jitter_seconds": 0.2}
    if action.startswith("respond"):
        step.update(status=200, delay_seconds=0.1)
    else:
        step["seconds"] = 0.1
    data = config().model_dump(exclude_unset=True)
    data["rules"][0]["sequence"] = [step]
    engine = Engine(Config.model_validate(data))

    def delays():
        values = [hit(engine).action for _ in range(20)]
        return [a.seconds if a.seconds is not None else a.delay_seconds for a in values]

    values = delays()
    assert all(0.1 <= value <= 0.3 for value in values)
    assert len(set(values)) > 1
    engine.reset()
    assert delays() == values
    assert engine.plan.rules[0].sequence[0].jitter_seconds == 0.2


@pytest.mark.parametrize(
    "change",
    [
        {"probability": -0.1},
        {"probability": 1.1},
        {"probability": True},
        {"probability": float("nan")},
        {"seed": -1},
        {"seed": True},
        {"seed": "1"},
        {"sequence": [{"action": "respond", "status": 200, "jitter_seconds": -1}]},
        {
            "sequence": [
                {"action": "respond", "status": 200, "delay_seconds": 3600, "jitter_seconds": 1}
            ]
        },
        {"sequence": [{"action": "delay_after", "seconds": 3600, "jitter_seconds": 1}]},
        {"sequence": [{"action": "reset", "jitter_seconds": 1}]},
    ],
)
def test_invalid_sampling_configuration(change):
    data = config().model_dump(exclude_unset=True)
    data["rules"][0].update(change)
    with pytest.raises(ValidationError):
        Config.model_validate(data)


async def test_probability_gate_keeps_first_match_and_replays_on_wire(proxy, upstream):
    scenario = rule(
        {"action": "respond", "status": 503},
        probability=0.5,
        seed=42,
        scope="X-Test-ID",
        after_sequence="repeat_last",
    )
    fallback = rule({"action": "respond", "status": 418}, id="fallback")
    async with proxy([scenario, fallback]) as app, httpx.AsyncClient() as client:

        async def run():
            return [
                (
                    await client.get(app.url("orders") + "/fault", headers={"X-Test-ID": "a"})
                ).status_code
                for _ in range(20)
            ]

        statuses = await run()
        assert set(statuses) == {200, 503}
        assert len(upstream[1]) == statuses.count(200)
        assert app.engine.snapshot()[0]["count"] == 20
        auth = {"Authorization": "Bearer test-token"}
        assert (
            await client.post(app.admin_url + "/reset", headers=auth, json={"scope": "a"})
        ).json() == {"reset": 1}
        assert await run() == statuses
        summary = (await client.get(app.admin_url + "/rules", headers=auth)).json()["rules"][0]
        assert summary["probability"] == 0.5 and summary["seed"] == 42


@pytest.mark.parametrize("action", ["respond", "respond_after"])
async def test_jitter_executes_selected_delay_and_logs_it(proxy, upstream, caplog, action):
    scenario = rule(
        {"action": action, "status": 202, "delay_seconds": 0.03, "jitter_seconds": 0.02}
    )
    caplog.set_level("INFO", logger="fault_engine.addon")
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        started = time.monotonic()
        assert (await client.get(app.url("orders") + "/fault")).status_code == 202
        events = [json.loads(r.message) for r in caplog.records if r.name == "fault_engine.addon"]
        selected = next(e for e in events if e["phase"] == "request")
        assert selected["sampled"] is True
        assert 0.03 <= selected["delay_seconds"] <= 0.05
        assert time.monotonic() - started >= selected["delay_seconds"]
        assert len(upstream[1]) == int(action == "respond_after")
        assert app.budget.requests.used == 0
        response = await client.get(
            app.admin_url + "/rules", headers={"Authorization": "Bearer test-token"}
        )
        summary = response.json()["rules"][0]["sequence"][0]
        assert summary["delay_seconds"] == 0.03
        assert summary["jitter_seconds"] == 0.02


async def test_concurrent_scopes_replay_their_own_sampling():
    engine = Engine(config(probability=0.4, seed=8))

    async def run(scope):
        results = []
        for _ in range(20):
            await asyncio.sleep(0)
            results.append(hit(engine, scope).sampled)
        return results

    actual = await asyncio.gather(run("a"), run("b"))
    for scope, values in zip(["a", "b"], actual, strict=True):
        isolated = Engine(config(probability=0.4, seed=8))
        assert [hit(isolated, scope).sampled for _ in range(20)] == values
