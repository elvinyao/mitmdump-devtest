"""Bounded, authenticated admin requests for the standalone CLI."""

import argparse
import asyncio
import json
import math
import os
import re
import sys
from urllib.parse import urlsplit

import aiohttp

REQUEST_TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# At most 100,000 status/null entries and 99,999 finite float intervals:
# under 3.4 MB including JSON separators, with room for fixed result fields.
MAX_VERIFY_RESPONSE_BYTES = 4 * 1024 * 1024
ADMIN_COMMANDS = frozenset({"requests", "verify", "reset", "journal-clear"})


class AdminClientError(Exception):
    """A fixed local message safe to display without remote exception details."""


def register_admin_commands(subparsers: argparse._SubParsersAction) -> None:
    """Register the admin commands without requiring a scenario configuration."""
    descriptions = {
        "requests": "Print one page of request records as JSON",
        "verify": "Check recorded requests; exit 0 for a match, 1 for failed/incomplete evidence",
        "reset": "Reset scenario sequence counters selected by service, rule, and scope",
        "journal-clear": "Clear all request records globally without resetting sequence counters",
    }
    for name, description in descriptions.items():
        command = subparsers.add_parser(
            name, help=description, description=description, allow_abbrev=False
        )
        command.add_argument(
            "--admin-url", default="http://127.0.0.1:9090", help="HTTP(S) admin origin"
        )
        command.add_argument(
            "--token-env",
            default="FAULT_ADMIN_TOKEN",
            help="Environment variable with bearer token",
        )
        if name != "journal-clear":
            for selector in ("service", "rule", "scope"):
                command.add_argument(f"--{selector}", help=f"Exact {selector} selection")
        if name in {"requests", "verify"}:
            command.add_argument(
                "--after", help="Checkpoint marking the start of the request window"
            )
        if name == "requests":
            command.add_argument("--limit", type=int, default=100, help="Page size, from 1 to 1000")
        if name == "verify":
            command.add_argument("--count", type=int, required=True, help="Exact request count")
            command.add_argument(
                "--statuses",
                type=int,
                nargs="*",
                help="Expected HTTP status codes in arrival order",
            )
            command.add_argument(
                "--min-interval-seconds", type=float, help="Minimum time between request arrivals"
            )


def _admin_origin(value: str) -> str:
    try:
        url = urlsplit(value)
        port = url.port
        valid = (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and url.path in {"", "/"}
            and not url.query
            and not url.fragment
            and not any(character in value for character in "?#@")
            and not any(character.isspace() or ord(character) < 32 for character in value)
            and port != 0
        )
    except ValueError:
        valid = False
    if not valid:
        raise AdminClientError("admin URL must be an HTTP(S) origin without credentials or a path")
    return value.rstrip("/")


def _request_options(
    args: argparse.Namespace,
) -> tuple[str, str, dict[str, str], dict[str, object]]:
    if args.command not in ADMIN_COMMANDS:
        raise AdminClientError("unknown admin command")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.token_env):
        raise AdminClientError("invalid admin token environment variable name")
    selection = {
        key: getattr(args, key)
        for key in ("service", "rule", "scope")
        if getattr(args, key, None) is not None
    }
    if args.command == "journal-clear":
        return "POST", "/requests/reset", {}, {}
    if args.command == "reset":
        return "POST", "/reset", {}, selection
    if args.after is not None:
        selection["after"] = args.after
    if args.command == "requests":
        if not 1 <= args.limit <= 1000:
            raise AdminClientError("request limit must be between 1 and 1000")
        return "GET", "/requests", {**selection, "limit": str(args.limit)}, {}
    if args.count < 0:
        raise AdminClientError("expected count must be nonnegative")
    selection["count"] = args.count
    if args.statuses is not None:
        if len(args.statuses) != args.count or any(
            not 100 <= item <= 599 for item in args.statuses
        ):
            raise AdminClientError("expected statuses must contain one HTTP status per request")
        selection["statuses"] = args.statuses
    if args.min_interval_seconds is not None:
        if not math.isfinite(args.min_interval_seconds) or args.min_interval_seconds < 0:
            raise AdminClientError("minimum request interval must be finite and nonnegative")
        selection["min_interval_seconds"] = args.min_interval_seconds
    return "POST", "/verify", {}, selection


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


def _validate_response(command: str, result: object) -> dict[str, object]:
    valid = isinstance(result, dict)
    if isinstance(result, dict):
        if command == "verify":
            valid = type(result.get("matched")) is bool and type(result.get("complete")) is bool
        elif command == "requests":
            records = result.get("requests")
            valid = (
                isinstance(records, list)
                and all(isinstance(record, dict) for record in records)
                and isinstance(result.get("checkpoint"), str)
                and bool(result["checkpoint"])
                and type(result.get("complete")) is bool
                and ("next_cursor" not in result or isinstance(result["next_cursor"], str))
            )
        elif command == "reset":
            valid = type(result.get("reset")) is int and result["reset"] >= 0
        elif command == "journal-clear":
            valid = isinstance(result.get("checkpoint"), str) and bool(result["checkpoint"])
        if valid:
            return result
    raise AdminClientError("admin server returned an invalid response")


async def _request(
    args: argparse.Namespace,
    origin: str,
    token: str,
    options: tuple[str, str, dict[str, str], dict[str, object]],
) -> dict[str, object]:
    method, path, query, body = options
    response_limit = MAX_VERIFY_RESPONSE_BYTES if args.command == "verify" else MAX_RESPONSE_BYTES
    try:
        async with aiohttp.ClientSession(
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        ) as session:
            async with session.request(
                method,
                origin + path,
                params=query,
                json=body if method == "POST" else None,
                headers={"Authorization": f"Bearer {token}"},
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise AdminClientError("admin authentication failed")
                if not 200 <= response.status < 300:
                    raise AdminClientError("admin HTTP request failed")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(data) + len(chunk) > response_limit:
                        raise AdminClientError("admin response exceeded the size limit")
                    data.extend(chunk)
                try:
                    result = json.loads(
                        data.decode("utf-8"),
                        object_pairs_hook=_unique_object,
                        parse_float=_finite_float,
                        parse_constant=_finite_float,
                    )
                except (UnicodeError, ValueError, RecursionError):
                    raise AdminClientError("admin server returned an invalid response") from None
                return _validate_response(args.command, result)
    except TimeoutError:
        raise AdminClientError("admin request timed out") from None
    except (aiohttp.ClientError, OSError, ValueError, UnicodeError):
        raise AdminClientError("could not connect to the admin server") from None


def run_admin_command(args: argparse.Namespace) -> int:
    """Print a validated JSON result, or a static error; return the CLI exit status."""
    try:
        origin = _admin_origin(args.admin_url)
        options = _request_options(args)
        token = os.environ.get(args.token_env, "")
        if not token or any(not 33 <= ord(character) <= 126 for character in token):
            raise AdminClientError("admin token must be set to nonempty visible ASCII characters")
        result = asyncio.run(_request(args, origin, token, options))
        try:
            output = json.dumps(result, indent=2, allow_nan=False)
        except (ValueError, RecursionError):
            raise AdminClientError("admin server returned an invalid response") from None
        print(output)
        if args.command == "verify" and (not result["matched"] or not result["complete"]):
            return 1
        return 0
    except AdminClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
