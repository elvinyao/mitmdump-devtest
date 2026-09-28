"""Offline scenario templates and request diagnostics, usable from an installed wheel."""

from pathlib import Path
from urllib.parse import parse_qsl

import yaml

from fault_engine.config import TOKEN, Config, header_name
from fault_engine.engine import Engine

PRESETS = ("retry", "timeout", "reset", "jitter")


def make_config(
    *,
    upstream: str,
    preset: str,
    service: str = "backend",
    path: str = "/retry",
    port: int = 8080,
    admin_port: int = 9090,
) -> Config:
    """Build and strictly validate a complete, independently usable configuration."""
    steps = {
        "retry": {"action": "respond", "status": 503, "repeat": 2},
        "timeout": {"action": "timeout", "seconds": 10.0},
        "reset": {"action": "reset"},
        "jitter": {"action": "delay_after", "seconds": 0.05, "jitter_seconds": 0.15},
    }
    if preset not in steps:
        raise ValueError("unknown preset; choose retry, timeout, reset, or jitter")
    return Config.model_validate(
        {
            "version": 1,
            "services": [{"id": service, "host": "0.0.0.0", "port": port, "upstream": upstream}],
            "admin": {"host": "0.0.0.0", "port": admin_port, "token_env": "FAULT_ADMIN_TOKEN"},
            "rules": [
                {
                    "id": preset,
                    "service": service,
                    "match": {"path": path},
                    "scope": "X-Test-Run-ID" if preset == "retry" else "global",
                    "sequence": [steps[preset]],
                    "after_sequence": "passthrough" if preset == "retry" else "repeat_last",
                }
            ],
        }
    )


def write_config(path: str | Path, config: Config) -> None:
    """Exclusively create a YAML file after configuration and serialization succeed."""
    source = yaml.safe_dump(config.model_dump(mode="json", exclude_unset=True), sort_keys=False)
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(source)


def explain_request(
    config: Config,
    *,
    service: str,
    method: str,
    path: str,
    headers: list[str],
    ordinal: int,
) -> dict:
    """Parse command-line HTTP fields without echoing their potentially sensitive values.

    This CLI accepts one value per case-insensitive header name. Live requests may
    merge ordinary duplicate headers; callers must supply that merged value here.
    """
    if not TOKEN.fullmatch(method) or method.upper() == "CONNECT":
        raise ValueError("method must be an HTTP token excluding CONNECT")
    if not path.startswith("/") or "#" in path or any(ord(ch) < 32 for ch in path):
        raise ValueError("path must be an HTTP request path with an optional query")
    parsed_headers: dict[str, str] = {}
    for entry in headers:
        name, separator, value = entry.partition(":")
        if not separator:
            raise ValueError("header must use 'Name: value' format")
        key = header_name(name).lower()
        if key in parsed_headers:
            raise ValueError("duplicate case-insensitive header names")
        if any(ord(ch) < 32 and ch != "\t" or ord(ch) == 127 for ch in value):
            raise ValueError("invalid HTTP header value")
        parsed_headers[key] = value.strip(" \t")
    request_path, _, query = path.partition("?")
    return Engine(config).explain(
        service,
        method,
        request_path,
        parsed_headers,
        parse_qsl(query, keep_blank_values=True, errors="surrogateescape"),
        ordinal=ordinal,
    )
