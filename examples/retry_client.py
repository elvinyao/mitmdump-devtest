"""A small GET client whose explicit retry policy can be checked against Fault Engine.

This is an executable teaching example, not a general retry SDK. It retries only
429, 503 and read timeouts, with a fixed wait and bounded attempts. It does not
interpret Retry-After, parse application payloads or retry writes. Replace the
request loop with your application's client to test its actual policy.
"""

import argparse
import asyncio
import json
import math
import sys
import uuid
from urllib.parse import urlsplit

import httpx

RETRY_STATUSES = {429, 503}


async def get_with_retry(
    url: str, *, run_id: str, max_attempts: int, operation_timeout: float, retry_delay: float
) -> dict:
    attempts = []
    result = {"run_id": run_id, "attempts": attempts, "outcome": "retry_exhausted"}
    async with httpx.AsyncClient(
        timeout=operation_timeout, trust_env=False, follow_redirects=False
    ) as client:
        for number in range(1, max_attempts + 1):
            try:
                response = await client.get(url, headers={"X-Test-Run-ID": run_id})
            except httpx.ReadTimeout:
                attempts.append({"number": number, "status": None, "outcome": "read_timeout"})
            except httpx.DecodingError:
                attempts.append({"number": number, "status": None, "outcome": "decode_error"})
                result["outcome"] = "decode_error"
                break
            except httpx.TransportError:
                attempts.append({"number": number, "status": None, "outcome": "transport_error"})
                result["outcome"] = "transport_error"
                break
            else:
                status = response.status_code
                attempts.append({"number": number, "status": status, "outcome": "http"})
                if 200 <= status < 300:
                    result["outcome"] = "success"
                    break
                if status not in RETRY_STATUSES:
                    result["outcome"] = "http_error"
                    break
            if number < max_attempts:
                await asyncio.sleep(retry_delay)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Proxy URL for a GET request")
    parser.add_argument("--run-id", default=None, help="Non-sensitive test ID; defaults to a UUID")
    parser.add_argument(
        "--max-attempts", type=int, default=3, help="Total attempts, including first"
    )
    parser.add_argument("--timeout", type=float, default=1, help="Per-operation timeout in seconds")
    parser.add_argument("--retry-delay", type=float, default=1, help="Fixed wait between attempts")
    args = parser.parse_args()
    if not 1 <= args.max_attempts <= 100:
        parser.error("max-attempts must be between 1 and 100")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
        parser.error("timeout must be finite and between 0 (exclusive) and 3600")
    if not math.isfinite(args.retry_delay) or not 0 <= args.retry_delay <= 3600:
        parser.error("retry-delay must be finite and between 0 and 3600")
    run_id = args.run_id if args.run_id is not None else uuid.uuid4().hex
    if not run_id or len(run_id) > 256 or any(not 33 <= ord(ch) <= 126 for ch in run_id):
        parser.error("run-id must contain 1 to 256 visible ASCII characters")
    try:
        url = urlsplit(args.url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.fragment
        ):
            raise ValueError("invalid URL")
        result = asyncio.run(
            get_with_retry(
                args.url,
                run_id=run_id,
                max_attempts=args.max_attempts,
                operation_timeout=args.timeout,
                retry_delay=args.retry_delay,
            )
        )
    except (ValueError, httpx.InvalidURL):
        print(
            "error: expected a valid HTTP(S) URL without credentials or fragment", file=sys.stderr
        )
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["outcome"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
