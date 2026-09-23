"""CLI entry point: validate configuration or run until SIGINT/SIGTERM."""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys

import yaml
from pydantic import ValidationError

from fault_engine.config import Config, load_config


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
    for name in ("validate", "serve"):
        command = subparsers.add_parser(name)
        command.add_argument("config", help="YAML scenario configuration")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "validate":
            print(f"valid: {len(config.services)} services, {len(config.rules)} rules")
            return 0
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
            message = {
                "union_tag_invalid": "unknown action; choose a supported action",
                "union_tag_not_found": "action is required",
            }.get(error["type"], error["msg"])
            print(f"config {location}: {message}", file=sys.stderr)
    except yaml.YAMLError:
        print("config: invalid YAML syntax", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    return 2
