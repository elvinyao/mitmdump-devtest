import copy

import pytest
from pydantic import ValidationError

from fault_engine.config import Config, Respond, load_config


def config_data():
    return {
        "services": [{"id": "orders", "port": 8080, "upstream": "http://localhost:9000"}],
        "rules": [
            {
                "id": "retry",
                "service": "orders",
                "sequence": [
                    {"action": "respond", "status": 429, "repeat": 2},
                    {"action": "passthrough"},
                ],
            }
        ],
    }


def test_valid_defaults_and_actions():
    data = config_data()
    data["rules"][0]["sequence"] += [
        {"action": "respond", "status": 200, "json_body": {"ok": True}},
        {"action": "respond", "status": 503, "body_base64": "AP8="},
        {"action": "delay_before", "seconds": 0.1},
        {"action": "delay_after", "seconds": 0.1},
        {"action": "timeout", "seconds": 1.0},
        {"action": "disconnect"},
        {"action": "reset"},
    ]
    config = Config.model_validate(data)
    assert config.services[0].host == "127.0.0.1"
    assert config.rules[0].start_at == 1
    assert config.rules[0].scope == "global"
    binary = config.rules[0].sequence[3]
    assert isinstance(binary, Respond)
    assert binary.body_bytes() == b"\x00\xff"


@pytest.mark.parametrize(
    "action",
    [
        {"action": "unknown"},
        {"action": "respond", "status": 104},
        {"action": "respond", "status": 600},
        {"action": "respond", "status": True},
        {"action": "respond", "status": 200, "body": "a", "json_body": {}},
        {"action": "respond", "status": 204, "body": "a"},
        {"action": "respond", "status": 200, "body_base64": "!"},
        {"action": "respond", "status": 400, "headers": {"X-Bad": "a\r\nb"}},
        {"action": "respond", "status": 200, "headers": {"Content-Length": "90"}},
        {"action": "passthrough", "seconds": 2},
        {"action": "reset", "repeat": 0},
        {"action": "timeout", "seconds": -1},
        {"action": "delay_before", "seconds": float("nan")},
        {"action": "delay_after", "seconds": "1"},
    ],
)
def test_invalid_actions(action):
    data = config_data()
    data["rules"][0]["sequence"] = [action]
    with pytest.raises(ValidationError):
        Config.model_validate(data)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "retry", "service": "missing"},
        {"start_at": 0},
        {"sequence": []},
        {"scope": ""},
        {"after_sequence": "unknown"},
        {"unknown": True},
        {"match": {"path": "/a", "path_regex": ".*"}},
        {"match": {"path_regex": "["}},
        {"match": {"methods": ["BAD METHOD"]}},
        {"match": {"methods": ["CONNECT"]}},
        {"match": {"methods": []}},
    ],
)
def test_invalid_rules(change):
    data = config_data()
    data["rules"][0].update(change)
    with pytest.raises(ValidationError):
        Config.model_validate(data)


@pytest.mark.parametrize(
    "upstream",
    [
        "ftp://localhost",
        "http://localhost/path",
        "http://u:p@localhost",
        "http://localhost?x=1",
        "http://localhost#f",
        "http://localhost:99999",
        "http://",
        "http://bad host",
    ],
)
def test_invalid_upstream(upstream):
    data = config_data()
    data["services"][0]["upstream"] = upstream
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_duplicate_ids_and_ports():
    for kind in ["services", "rules"]:
        data = config_data()
        data[kind].append(copy.deepcopy(data[kind][0]))
        with pytest.raises(ValidationError):
            Config.model_validate(data)
    data = config_data()
    data["services"].append({"id": "other", "port": 8080, "upstream": "http://localhost"})
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_yaml_load_and_duplicate_keys(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("services:\n- id: orders\n  port: 8080\n  upstream: http://localhost\n")
    assert load_config(path).services[0].id == "orders"
    path.write_text("services: []\nservices: []\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_config(path)
