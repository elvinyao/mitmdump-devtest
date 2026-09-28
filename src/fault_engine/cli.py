"""CLI entry point for local scenario tools and running the proxy."""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys

import yaml
from pydantic import TypeAdapter, ValidationError

from fault_engine.admin_client import ADMIN_COMMANDS, register_admin_commands, run_admin_command
from fault_engine.config import Action, Config, load_config
from fault_engine.local_commands import PRESETS, explain_request, make_config, write_config


def config_error_location(location: tuple[str | int, ...]) -> str:
    """Keep schema field names and indices; never print user-supplied mapping keys."""
    schema = Config.model_json_schema()
    definitions = schema.get("$defs", {})
    node = schema
    safe = []
    for part in location:
        while "$ref" in node:
            node = definitions[node["$ref"].rsplit("/", 1)[-1]]
        if isinstance(part, int) and node.get("type") == "array":
            safe.append(str(part))
            node = node["items"]
        elif isinstance(part, str) and part in node.get("properties", {}):
            safe.append(part)
            node = node["properties"][part]
        elif part in node.get("discriminator", {}).get("mapping", {}):
            safe.append(str(part))
            node = {"$ref": node["discriminator"]["mapping"][part]}
        else:
            break
    return ".".join(safe) or "<root>"


async def serve(config: Config, token: str) -> None:
    from fault_engine.runtime import Runtime

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    runtime = Runtime(config, admin_token=token)
    try:
        await runtime.start()
        print(
            json.dumps(
                {
                    "event": "ready",
                    "services": {s.id: runtime.url(s.id) for s in config.services},
                    "admin": runtime.admin_url,
                }
            ),
            flush=True,
        )
        await stop.wait()
    finally:
        await runtime.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic mitmproxy HTTP fault scenarios")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("validate", "Check configuration without starting listeners or requiring a token"),
        ("serve", "Start proxy and admin listeners; requires the configured admin token"),
    ):
        command = subparsers.add_parser(name, help=description, description=description)
        command.add_argument("config", help="YAML scenario configuration")
    subparsers.add_parser("schema", help="Print the configuration JSON Schema for editor tooling")
    init = subparsers.add_parser(
        "init", help="Create a validated scenario file without overwriting"
    )
    init.add_argument("config", help="New YAML scenario configuration")
    init.add_argument("--upstream", required=True, help="HTTP(S) upstream origin")
    init.add_argument("--preset", required=True, choices=PRESETS)
    init.add_argument("--service", default="backend")
    init.add_argument("--path", default="/retry")
    init.add_argument("--port", type=int, default=8080)
    init.add_argument("--admin-port", type=int, default=9090)
    explain = subparsers.add_parser("explain", help="Explain rule matching and sampling offline")
    explain.add_argument("config", help="YAML scenario configuration")
    explain.add_argument("--service", required=True)
    explain.add_argument("--method", default="GET")
    explain.add_argument("--path", required=True, help="Request path, optionally including query")
    explain.add_argument(
        "--header",
        action="append",
        default=[],
        help="Request header: Name: value; duplicate names are rejected, supply merged values",
    )
    explain.add_argument("--ordinal", type=int, default=1, help="Sequence position (default: 1)")
    register_admin_commands(subparsers)
    args = parser.parse_args(argv)
    if args.command in ADMIN_COMMANDS:
        return run_admin_command(args)
    if args.command == "schema":
        print(json.dumps(Config.model_json_schema(), indent=2))
        return 0
    try:
        if args.command == "init":
            config = make_config(
                upstream=args.upstream,
                preset=args.preset,
                service=args.service,
                path=args.path,
                port=args.port,
                admin_port=args.admin_port,
            )
            write_config(args.config, config)
            print("created: validated scenario configuration")
            return 0
        config = load_config(args.config)
        if args.command == "validate":
            print(f"valid: {len(config.services)} services, {len(config.rules)} rules")
            return 0
        if args.command == "explain":
            result = explain_request(
                config,
                service=args.service,
                method=args.method,
                path=args.path,
                headers=args.header,
                ordinal=args.ordinal,
            )
            print(json.dumps(result, indent=2))
            return 2 if result["scope"] is not None and not result["scope"]["valid"] else 0
        token = os.environ.get(config.admin.token_env, "")
        if not token:
            raise ValueError(f"set {config.admin.token_env} to a nonempty admin bearer token")
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        logging.getLogger("fault_engine").setLevel(logging.INFO)
        asyncio.run(serve(config, token))
        return 0
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            location = config_error_location(error["loc"])
            message = error["msg"]
            if error["type"] in {"union_tag_invalid", "union_tag_not_found"}:
                actions = ", ".join(TypeAdapter(Action).json_schema()["discriminator"]["mapping"])
                reason = (
                    "unknown action"
                    if error["type"] == "union_tag_invalid"
                    else "action is required"
                )
                message = f"{reason}; choose one of: {actions}"
            print(f"config {location}: {message}", file=sys.stderr)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        print(f"config: invalid YAML syntax{location}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    return 2
