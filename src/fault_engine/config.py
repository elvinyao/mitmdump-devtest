"""Validated, versioned scenario configuration. No network I/O."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import yaml
import yaml.resolver
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
IDENTIFIER = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$"
Port = Annotated[int, Field(ge=1, le=65535)]
CONFIG_BYTE_LIMIT = 1_048_576


def header_name(value: str) -> str:
    if not TOKEN.fullmatch(value):
        raise ValueError("invalid HTTP header name")
    return value


def listen_host(value: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("host must be an IPv4 or IPv6 address") from exc
    return value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class Service(Model):
    id: str = Field(pattern=IDENTIFIER)
    host: str = "127.0.0.1"
    port: Port
    upstream: str

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        return listen_host(value)

    @field_validator("upstream")
    @classmethod
    def valid_upstream(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError as exc:
            raise ValueError("upstream must have a valid host and port") from exc
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or "?" in value
            or "#" in value
            or "@" in value
            or any(ch.isspace() or ord(ch) < 32 for ch in value)
        ):
            raise ValueError(
                "upstream must be an http(s) origin without credentials, path or query"
            )
        if port == 0:
            raise ValueError("upstream port must be positive")
        return value.rstrip("/")


class Match(Model):
    methods: list[str] | None = None
    path: str | None = None
    path_regex: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)

    @field_validator("methods")
    @classmethod
    def valid_methods(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (
            not value or any(not TOKEN.fullmatch(m) or m.upper() == "CONNECT" for m in value)
        ):
            raise ValueError("methods must be nonempty HTTP tokens excluding CONNECT")
        return value

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {header_name(k).lower(): v for k, v in value.items()}
        if len(normalized) != len(value):
            raise ValueError("duplicate case-insensitive header names")
        return normalized

    @model_validator(mode="after")
    def paths(self) -> Self:
        if self.path is not None and self.path_regex is not None:
            raise ValueError("path and path_regex are mutually exclusive")
        if self.path is not None and (not self.path.startswith("/") or "?" in self.path):
            raise ValueError("path must start with / and exclude query")
        if self.path_regex is not None:
            try:
                re.compile(self.path_regex)
            except re.error as exc:
                raise ValueError("invalid path_regex") from exc
        return self


class Step(Model):
    repeat: int = Field(default=1, ge=1, le=1_000_000)


class Pass(Step):
    action: Literal["passthrough"] = "passthrough"


class Respond(Step):
    action: Literal["respond", "respond_after"]
    status: int = Field(ge=200, le=599)
    delay_seconds: float = Field(default=0, ge=0, le=3600)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    json_body: JsonValue = None
    body_base64: str | None = None

    @model_validator(mode="after")
    def valid_body(self) -> Self:
        body_fields = {"body", "json_body", "body_base64"} & self.model_fields_set
        if len(body_fields) > 1:
            raise ValueError("choose only one body encoding")
        try:
            content = self.body_bytes()
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid response body encoding") from exc
        if self.status in {204, 205, 304} and content:
            raise ValueError("this response status cannot have a body")
        seen: set[str] = set()
        for name, value in self.headers.items():
            key = header_name(name).lower()
            if key in seen:
                raise ValueError("duplicate case-insensitive header names")
            seen.add(key)
            if key in {"content-length", "transfer-encoding", "connection", "trailer", "upgrade"}:
                raise ValueError("response framing headers are managed by the proxy")
            if any(ord(ch) < 32 and ch != "\t" or ord(ch) == 127 for ch in value):
                raise ValueError("invalid HTTP header value")
            try:
                value.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise ValueError("HTTP header values must be latin-1") from exc
        return self

    def body_bytes(self) -> bytes:
        if self.body_base64 is not None:
            return base64.b64decode(self.body_base64, validate=True)
        if "json_body" in self.model_fields_set:
            return json.dumps(self.json_body, ensure_ascii=False, allow_nan=False).encode()
        return (self.body or "").encode()


class Delay(Step):
    action: Literal["delay_before", "delay_after", "timeout"]
    seconds: float = Field(gt=0, le=3600)


class Disconnect(Step):
    action: Literal["disconnect", "reset", "disconnect_after", "reset_after"]


Action = Annotated[Pass | Respond | Delay | Disconnect, Field(discriminator="action")]


class Rule(Model):
    id: str = Field(pattern=IDENTIFIER)
    service: str
    match: Match = Field(default_factory=Match)
    scope: str = "global"
    start_at: int = Field(default=1, ge=1)
    sequence: list[Action] = Field(min_length=1)
    after_sequence: Literal["passthrough", "repeat_last", "cycle"] = "passthrough"

    @field_validator("scope")
    @classmethod
    def valid_scope(cls, value: str) -> str:
        if value != "global":
            header_name(value)
            if value.lower() in {
                "authorization",
                "cookie",
                "host",
                "content-length",
                "transfer-encoding",
                "connection",
                "proxy-authorization",
                "proxy-authenticate",
                "proxy-connection",
                "keep-alive",
                "te",
                "trailer",
                "upgrade",
                "expect",
                "content-type",
                "content-encoding",
            }:
                raise ValueError("use a dedicated non-sensitive test scope header")
        return value


class StateConfig(Model):
    capacity: int = Field(default=10000, ge=1, le=1_000_000)
    ttl_seconds: float = Field(default=3600.0, gt=0)


class AdminConfig(Model):
    host: str = "127.0.0.1"
    port: Port = 9090
    token_env: str = Field(default="FAULT_ADMIN_TOKEN", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @field_validator("host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        return listen_host(value)


class Config(Model):
    version: Literal[1] = 1
    services: list[Service] = Field(min_length=1)
    rules: list[Rule] = Field(default_factory=list)
    state: StateConfig = Field(default_factory=StateConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    body_limit: int = Field(default=10_485_760, ge=1, le=1_073_741_824)
    upstream_ca: str | None = None

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be the integer 1")
        return value

    @model_validator(mode="after")
    def references(self) -> Self:
        def invalid(location: tuple[str | int, ...], message: str) -> None:
            raise ValidationError.from_exception_data(
                "Config",
                [
                    {
                        "type": "value_error",
                        "loc": location,
                        "input": None,
                        "ctx": {"error": ValueError(message)},
                    }
                ],
            )

        service_ids: set[str] = set()
        ports: set[int] = set()
        for index, service in enumerate(self.services):
            if service.id in service_ids:
                invalid(("services", index, "id"), "duplicate service id")
            if service.port in ports:
                invalid(("services", index, "port"), "service ports must be distinct")
            service_ids.add(service.id)
            ports.add(service.port)
        rule_ids: set[str] = set()
        for index, rule in enumerate(self.rules):
            if rule.id in rule_ids:
                invalid(("rules", index, "id"), "duplicate rule id")
            if rule.service not in service_ids:
                invalid(("rules", index, "service"), "rule references unknown service")
            rule_ids.add(rule.id)
        if self.admin.port in ports:
            invalid(("admin", "port"), "admin port must differ from service ports")
        return self


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate keys rather than silently replacing scenario rules."""


def _mapping(loader: UniqueKeyLoader, node: yaml.MappingNode) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        location = f"line {key_node.start_mark.line + 1}, column {key_node.start_mark.column + 1}"
        if not isinstance(key, str):
            raise ValueError(f"YAML mapping keys must be strings at {location}")
        if key in result:
            raise ValueError(f"duplicate YAML mapping key at {location}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_config(path: str | Path) -> Config:
    with Path(path).open("rb") as stream:
        source = stream.read(CONFIG_BYTE_LIMIT + 1)
    if len(source) > CONFIG_BYTE_LIMIT:
        raise ValueError("configuration exceeds 1 MiB")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("configuration must use UTF-8 encoding") from exc
    return Config.model_validate(yaml.load(text, Loader=UniqueKeyLoader))
