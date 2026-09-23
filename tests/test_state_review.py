"""Additional configuration, control-plane and state-contract regressions."""

import asyncio
import json

import httpx
import pytest
import yaml
from conftest import rule
from pydantic import ValidationError

from fault_engine.cli import main
from fault_engine.config import Config, Respond, load_config
from fault_engine.engine import Engine, ScenarioError


def scenario_data():
    return {
        "services": [{"id": "orders", "port": 8080, "upstream": "http://localhost:9000"}],
        "rules": [
            rule(
                {"action": "respond", "status": 429},
                scope="X-Test-ID",
                after_sequence="repeat_last",
            )
        ],
    }


def decide(engine, scope, *, path="/fault"):
    return engine.decide("orders", "GET", path, {"X-Test-ID": scope}, [])


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 2])
def test_schema_version_requires_integer_one(version):
    data = scenario_data()
    data["version"] = version
    with pytest.raises(ValidationError):
        Config.model_validate(data)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("services", "host", "SECRET_HOST"),
        ("services", "upstream", "http://localhost:SECRET_PORT"),
        ("rules", "sequence", [{"action": "SECRET_ACTION"}]),
        ("rules", "SECRET_EXTRA_FIELD", True),
        (
            "rules",
            "sequence",
            [{"action": "respond", "status": 200, "json_body": {"SECRET_KEY": float("nan")}}],
        ),
    ],
)
def test_cli_redacts_values_and_arbitrary_mapping_keys(tmp_path, capsys, section, field, value):
    data = scenario_data()
    data[section][0][field] = value
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["validate", str(path)]) == 2
    captured = capsys.readouterr()
    assert f"config {section}.0" in captured.err
    assert "SECRET" not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_config_size_limit_counts_utf8_bytes(tmp_path):
    source = yaml.safe_dump(scenario_data()) + "#" + "界" * 350_000 + "\n"
    assert len(source) < 1_048_576 < len(source.encode("utf-8"))
    path = tmp_path / "oversized.yaml"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="1 MiB"):
        load_config(path)


def test_config_accepts_exact_size_boundary(tmp_path):
    source = yaml.safe_dump(scenario_data())
    source += "#" + "x" * (1_048_576 - len(source.encode()) - 2) + "\n"
    assert len(source.encode()) == 1_048_576
    path = tmp_path / "boundary.yaml"
    path.write_text(source, encoding="utf-8")
    assert load_config(path).rules[0].id == "r"


@pytest.mark.parametrize(
    "scope",
    [
        "Proxy-Authorization",
        "pRoXy-AuThOrIzAtIoN",
        "Proxy-Authenticate",
        "TE",
        "Trailer",
        "Upgrade",
        "Content-Type",
        "Content-Encoding",
    ],
)
def test_scope_cannot_strip_protocol_headers_or_proxy_credentials(scope):
    data = scenario_data()
    data["rules"][0]["scope"] = scope
    with pytest.raises(ValidationError, match="scope"):
        Config.model_validate(data)


def test_yaml_recursive_alias_and_deep_input_have_clean_cli_error(tmp_path, capsys):
    path = tmp_path / "invalid.yaml"
    for source in ["services: &loop [*loop]\n", "services: " + "[" * 1200 + "]" * 1200]:
        path.write_text(source, encoding="utf-8")
        assert main(["validate", str(path)]) == 2
        assert "Traceback" not in capsys.readouterr().err


def test_yaml_aliases_and_explicit_json_null_preserve_body_encoding(tmp_path):
    data = scenario_data()
    body = {"nested": [1, "two", None]}
    data["rules"][0]["sequence"] = [
        {"action": "respond", "status": 200, "json_body": body},
        {"action": "respond", "status": 201, "json_body": body},
        {"action": "respond", "status": 202, "json_body": None},
    ]
    source = yaml.safe_dump(data)
    assert "&id" in source and "*id" in source
    path = tmp_path / "aliases.yaml"
    path.write_text(source, encoding="utf-8")
    config = load_config(path)
    rebuilt = Config.model_validate(config.model_dump(exclude_unset=True))
    for first, second in zip(config.rules[0].sequence, rebuilt.rules[0].sequence, strict=True):
        assert isinstance(first, Respond) and isinstance(second, Respond)
        assert first.body_bytes() == second.body_bytes()
    final = rebuilt.rules[0].sequence[-1]
    assert isinstance(final, Respond)
    assert final.body_bytes() == b"null"


def test_ttl_expiration_follows_last_hit_order_and_exact_boundary():
    now = [0.0]
    data = scenario_data()
    data["state"] = {"capacity": 2, "ttl_seconds": 10.0}
    engine = Engine(Config.model_validate(data), clock=lambda: now[0])
    assert decide(engine, "a").ordinal == 1
    now[0] = 1.0
    assert decide(engine, "b").ordinal == 1
    now[0] = 9.0
    assert decide(engine, "a").ordinal == 2
    now[0] = 10.999
    with pytest.raises(ScenarioError, match="capacity"):
        decide(engine, "c")
    assert [row["scope"] for row in engine.snapshot()] == ["b", "a"]
    now[0] = 11.0
    assert decide(engine, "c").ordinal == 1
    assert [row["scope"] for row in engine.snapshot()] == ["a", "c"]
    now[0] = 19.0
    assert [row["scope"] for row in engine.snapshot()] == ["c"]
    assert decide(engine, "a").ordinal == 1


def test_invalid_and_unmatched_requests_do_not_consume_scope_capacity():
    data = scenario_data()
    data["state"] = {"capacity": 1}
    engine = Engine(Config.model_validate(data))
    assert decide(engine, "unused", path="/other") is None
    with pytest.raises(ScenarioError, match="scope"):
        decide(engine, "")
    first = decide(engine, "valid")
    with pytest.raises(ScenarioError, match="capacity"):
        decide(engine, "full")
    assert decide(engine, "valid").ordinal == 2
    assert engine.reset(scope="valid") == 1
    assert first.ordinal == 1 and first.action.action == "respond"
    assert decide(engine, "full").ordinal == 1


async def test_concurrent_scopes_allocate_independent_contiguous_ordinals():
    engine = Engine(Config.model_validate(scenario_data()))

    async def request(number):
        await asyncio.sleep(0)
        decision = decide(engine, f"scope-{number % 7}")
        await asyncio.sleep(0)
        return decision

    decisions = await asyncio.gather(*(request(i) for i in range(700)))
    for scope in range(7):
        actual = sorted(d.ordinal for d in decisions if d.scope == f"scope-{scope}")
        assert actual == list(range(1, 101))
    assert engine.reset() == 7
    assert all(d.action.action == "respond" for d in decisions)
    assert engine.snapshot() == []


async def test_admin_invalid_reset_preserves_state_and_rules_redact_response_body(proxy):
    scenario = rule(
        {"action": "respond", "status": 200, "json_body": {"password": "SECRET_BODY"}},
        scope="X-Test-ID",
    )
    async with proxy([scenario]) as app, httpx.AsyncClient() as client:
        await client.get(app.url("orders") + "/fault", headers={"X-Test-ID": "a"})
        baseline = app.engine.snapshot()
        auth = {"Authorization": "Bearer test-token"}
        for body in [[], None, {"scope": 1}, {"service": False}, {"rule": []}]:
            response = await client.post(
                app.admin_url + "/reset", headers=auth, content=json.dumps(body)
            )
            assert response.status_code == 400
            assert app.engine.snapshot() == baseline
        response = await client.get(app.admin_url + "/rules", headers=auth)
        assert response.status_code == 200
        assert "SECRET_BODY" not in response.text
        response = await client.post(
            app.admin_url + "/reset",
            headers=auth,
            json={"service": "orders", "rule": "r", "scope": "a"},
        )
        assert response.json() == {"reset": 1}
        assert app.engine.snapshot() == []
