import json
import os
import subprocess
import sys

import pytest
import yaml

from fault_engine.config import Config, load_config
from fault_engine.engine import Engine


def invoke(*args):
    env = os.environ.copy()
    env.pop("FAULT_ADMIN_TOKEN", None)
    return subprocess.run(
        [sys.executable, "-m", "fault_engine", *args],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


@pytest.mark.parametrize("preset", ["retry", "timeout", "reset", "jitter"])
def test_init_creates_valid_complete_preset_without_token(tmp_path, preset):
    path = tmp_path / "scenario.yaml"
    result = invoke("init", str(path), "--upstream", "http://localhost:9000", "--preset", preset)
    assert result.returncode == 0, result.stderr
    config = load_config(path)
    assert config.services[0].id == "backend"
    assert config.services[0].host == config.admin.host == "0.0.0.0"
    assert config.services[0].port == 8080
    assert config.admin.port == 9090
    assert config.admin.token_env == "FAULT_ADMIN_TOKEN"
    assert config.rules[0].id == preset
    assert config.rules[0].match.path == "/retry"
    assert Config.model_validate(yaml.safe_load(path.read_text())) == config
    assert invoke("validate", str(path)).returncode == 0
    engine = Engine(config)
    decisions = []
    for _ in range(3):
        decision = engine.decide("backend", "GET", "/retry", {"X-Test-Run-ID": "run"}, [])
        assert decision is not None
        decisions.append(decision)
    if preset == "retry":
        assert config.rules[0].scope == "X-Test-Run-ID"
        assert [d.action.status for d in decisions] == [503, 503, None]
        assert decisions[-1].action.action == "passthrough"
    else:
        assert config.rules[0].after_sequence == "repeat_last"
        assert all(
            d.action.action == {"jitter": "delay_after"}.get(preset, preset) for d in decisions
        )
        if preset == "timeout":
            assert all(d.action.seconds == 10 for d in decisions)
        if preset == "jitter":
            for decision in decisions:
                assert decision.action.seconds is not None
                assert 0.05 <= decision.action.seconds < 0.2


def test_make_config_supports_custom_settings():
    from fault_engine.local_commands import make_config

    config = make_config(
        upstream="https://example.com/",
        preset="retry",
        service="orders",
        path="/orders",
        port=8000,
        admin_port=9000,
    )
    assert isinstance(config, Config)
    assert config.services[0].id == config.rules[0].service == "orders"
    assert config.services[0].upstream == "https://example.com"
    assert config.rules[0].match.path == "/orders"
    assert config.services[0].port == 8000
    assert config.admin.port == 9000


def test_init_custom_options_and_existing_file_is_never_overwritten(tmp_path):
    path = tmp_path / "scenario.yaml"
    args = [
        "init",
        str(path),
        "--upstream",
        "https://example.com",
        "--preset",
        "reset",
        "--service",
        "orders",
        "--path",
        "/orders",
        "--port",
        "8000",
        "--admin-port",
        "9000",
    ]
    assert invoke(*args).returncode == 0
    config = load_config(path)
    assert config.services[0].id == "orders"
    assert config.services[0].port == 8000
    assert config.admin.port == 9000
    assert config.rules[0].match.path == "/orders"
    original = path.read_bytes()
    result = invoke(*args)
    assert result.returncode == 2
    assert "exist" in result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "upstream", ["http://user:SECRET@localhost", "http://localhost/SECRET", "ftp://localhost"]
)
def test_init_invalid_origin_does_not_create_file_or_echo_input(tmp_path, upstream):
    path = tmp_path / "scenario.yaml"
    result = invoke("init", str(path), "--upstream", upstream, "--preset", "retry")
    assert result.returncode == 2
    assert "config services.0.upstream:" in result.stderr
    assert not path.exists()
    assert "SECRET" not in result.stderr
    assert "Traceback" not in result.stderr


def diagnostic_config(**rule_changes):
    rule = {
        "id": "selected",
        "service": "s",
        "match": {
            "methods": ["GET"],
            "path_regex": r"/orders/\d+",
            "headers": {"X-Client": "SECRET_HEADER"},
            "query": {"tag": "SECRET_QUERY"},
        },
        "scope": "X-Test-Run-ID",
        "probability": 0.5,
        "seed": 27,
        "start_at": 3,
        "sequence": [
            {
                "action": "respond_after",
                "status": 503,
                "delay_seconds": 0.1,
                "jitter_seconds": 0.2,
                "headers": {"X-Secret": "SECRET_RESPONSE"},
                "body": "SECRET_BODY",
            },
            {"action": "delay_after", "seconds": 0.05, "jitter_seconds": 0.15},
        ],
        "after_sequence": "cycle",
    }
    rule.update(rule_changes)
    return Config.model_validate(
        {
            "services": [{"id": "s", "port": 8080, "upstream": "http://localhost:9000"}],
            "rules": [
                {
                    "id": "miss",
                    "service": "s",
                    "match": {"path": "/SECRET_PATH"},
                    "sequence": [{"action": "reset"}],
                },
                rule,
                {"id": "fallback", "service": "s", "sequence": [{"action": "reset"}]},
            ],
            "state": {"ttl_seconds": 1.0},
        }
    )


def request_inputs():
    return (
        "s",
        "GET",
        "/orders/12",
        {"x-client": "SECRET_HEADER", "X-Test-Run-ID": "test-run"},
        [("tag", "other"), ("tag", "SECRET_QUERY")],
    )


def test_explain_matches_real_selection_sampling_and_jitter_without_state_changes():
    engine = Engine(diagnostic_config())
    before = engine.snapshot()
    for ordinal in range(1, 41):
        result = engine.explain(*request_inputs(), ordinal=ordinal)
        assert engine.snapshot() == before
        decision = engine.decide(*request_inputs())
        assert decision is not None
        assert result["matched_rule"] == decision.rule_id == "selected"
        assert result["scope"] == {"valid": True}
        assert result["decision"] == {
            "service": decision.service,
            "rule": decision.rule_id,
            "ordinal": ordinal,
            "action": decision.action.action,
            "status": decision.action.status,
            "seconds": decision.action.seconds,
            "delay_seconds": decision.action.delay_seconds,
            "sampled": decision.sampled,
        }
        assert "SECRET" not in json.dumps(result)
        before = engine.snapshot()


def test_explain_never_serializes_a_valid_scope_header_value():
    service, method, path, headers, query = request_inputs()
    headers["X-Test-Run-ID"] = "SECRET_TEST_SCOPE"
    result = Engine(diagnostic_config()).explain(service, method, path, headers, query)
    assert result["scope"] == {"valid": True}
    assert result["decision"] is not None
    assert "SECRET" not in json.dumps(result)
    assert "scope" not in result["decision"]


def test_explain_does_not_expire_or_touch_existing_counter(monkeypatch):
    now = [0.0]
    engine = Engine(diagnostic_config(), clock=lambda: now[0])
    engine.decide(*request_inputs())
    counters = {key: (c.count, c.touched, c.identifier) for key, c in engine._counters.items()}
    now[0] = 10.0
    engine.explain(*request_inputs(), ordinal=5)
    assert {
        key: (c.count, c.touched, c.identifier) for key, c in engine._counters.items()
    } == counters
    assert engine._next_counter_id == 2

    def unexpected_clock():
        raise AssertionError("explain must not access mutable state or clock")

    monkeypatch.setattr(engine, "_clock", unexpected_clock)
    engine.explain(*request_inputs(), ordinal=5)


def test_explain_lists_failure_dimensions_without_request_or_config_values():
    result = Engine(diagnostic_config()).explain("s", "POST", "/unknown", {}, [])
    assert result["matched_rule"] == "fallback"
    assert result["candidates"] == [
        {"rule": "miss", "matches": False, "failures": ["path"]},
        {"rule": "selected", "matches": False, "failures": ["method", "path", "header", "query"]},
        {"rule": "fallback", "matches": True, "failures": []},
    ]
    assert "SECRET" not in json.dumps(result)


def test_explain_no_match_returns_null_decision():
    config = diagnostic_config().model_copy(update={"rules": diagnostic_config().rules[:2]})
    result = Engine(config).explain("s", "GET", "/unknown", {}, [])
    assert result["matched_rule"] is result["decision"] is result["scope"] is None


@pytest.mark.parametrize("scope", ["", "a" * 257, "SECRET\n"])
def test_explain_reports_invalid_scope_without_falling_through_or_echoing_it(scope):
    service, method, path, headers, query = request_inputs()
    headers["X-Test-Run-ID"] = scope
    result = Engine(diagnostic_config()).explain(service, method, path, headers, query)
    assert result["matched_rule"] == "selected"
    assert result["scope"]["valid"] is False
    assert result["decision"] is None
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("ordinal", [0, -1, True, 1.5])
def test_explain_rejects_invalid_ordinal(ordinal):
    with pytest.raises(ValueError, match="ordinal"):
        Engine(diagnostic_config()).explain(*request_inputs(), ordinal=ordinal)


def test_explain_rejects_unknown_service_without_echoing_it():
    with pytest.raises(ValueError, match="unknown service") as error:
        Engine(diagnostic_config()).explain("SECRET", "GET", "/", {}, [])
    assert "SECRET" not in str(error.value)


def test_cli_explain_parses_query_and_headers_and_needs_no_token(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(diagnostic_config().model_dump(exclude_unset=True)))
    result = invoke(
        "explain",
        str(path),
        "--service",
        "s",
        "--path",
        "/orders/12?tag=other&tag=SECRET_QUERY",
        "--header",
        "X-Client: SECRET_HEADER",
        "--header",
        "X-Test-Run-ID: test-run",
        "--ordinal",
        "5",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == Engine(diagnostic_config()).explain(
        *request_inputs(), ordinal=5
    )
    assert "SECRET" not in result.stdout + result.stderr


@pytest.mark.parametrize("query", ["q=%FF", "%FF=value"])
def test_explain_query_decoding_matches_live_request_with_invalid_utf8(query):
    from mitmproxy import http

    from fault_engine.local_commands import explain_request

    match = {"q": "\ufffd"} if query.startswith("q=") else {"\ufffd": "value"}
    config = Config.model_validate(
        {
            "services": [{"id": "s", "port": 8080, "upstream": "http://localhost:9000"}],
            "rules": [
                {
                    "id": "replacement-character",
                    "service": "s",
                    "match": {"query": match},
                    "sequence": [{"action": "respond", "status": 503}],
                }
            ],
        }
    )
    request = http.Request.make("GET", f"http://localhost/?{query}")
    decision = Engine(config).decide(
        "s",
        request.method,
        request.path.split("?", 1)[0],
        dict(request.headers),
        list(request.query.items(multi=True)),
    )
    assert decision is None
    result = explain_request(
        config, service="s", method=request.method, path=request.path, headers=[], ordinal=1
    )
    assert result["matched_rule"] is None
    assert result["decision"] is None
    assert result["candidates"][0]["failures"] == ["query"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--header", "SECRET"],
        ["--header", "X-Test-Run-ID: a", "--header", "x-test-run-id: SECRET"],
        ["--header", "Bad Name: SECRET"],
        ["--header", "X-Test-Run-ID: SECRET\n"],
        ["--ordinal", "0"],
    ],
)
def test_cli_explain_invalid_inputs_are_redacted(tmp_path, extra):
    path = tmp_path / "scenario.yaml"
    assert (
        invoke("init", str(path), "--upstream", "http://localhost", "--preset", "retry").returncode
        == 0
    )
    result = invoke("explain", str(path), "--service", "backend", "--path", "/retry", *extra)
    assert result.returncode == 2
    assert "SECRET" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_cli_explain_missing_scope_returns_diagnostic_json_and_exit_two(tmp_path):
    path = tmp_path / "scenario.yaml"
    assert (
        invoke("init", str(path), "--upstream", "http://localhost", "--preset", "retry").returncode
        == 0
    )
    result = invoke("explain", str(path), "--service", "backend", "--path", "/retry")
    assert result.returncode == 2
    assert json.loads(result.stdout)["scope"]["valid"] is False
