"""Narrow HTTP/1 compatibility fixes for the pinned mitmproxy 12 parser.

The pinned HTTP/1 parser raises NotImplementedError after h11 successfully parses
trailers, leaving buffered clients waiting forever. Convert that one unsupported
event into a protocol error while it is still inside the parser's error boundary.
It also treats custom case variants of HEAD as bodyless; use ordinary response
framing for those methods without changing the request sent to the backend.
Install only while this process's single Runtime owns mitmproxy, then restore.
"""

from copy import copy
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from inspect import signature

import h11
from h11._readers import ChunkedReader
from h11._receivebuffer import ReceiveBuffer
from mitmproxy import http
from mitmproxy.net.http import http1
from mitmproxy.proxy.layers.http import _http1

_original_make_body_reader = _http1.make_body_reader
_original_expected_body_size = http1.expected_http_body_size
SUPPORTED_VERSIONS = {"mitmproxy": "12.2.3", "h11": "0.16.0"}


class _RejectTrailersReader(ChunkedReader):
    def __call__(self, buf: ReceiveBuffer) -> h11.Data | h11.EndOfMessage | None:
        event = super().__call__(buf)
        if isinstance(event, h11.EndOfMessage) and event.headers:
            raise h11.RemoteProtocolError("HTTP trailers are not supported")
        return event


def _make_body_reader(expected_size: int | None) -> _http1.TBodyReader:
    if expected_size is None:
        return _RejectTrailersReader()
    return _original_make_body_reader(expected_size)


def _expected_body_size(request: http.Request, response: http.Response | None = None) -> int | None:
    method = request.data.method
    if response is not None and method != b"HEAD" and method.upper() == b"HEAD":
        # Only the parser's framing view changes. Sharing read-only fields avoids
        # copying a potentially large upload; the original request stays intact.
        request = copy(request)
        request.data = replace(request.data, method=b"GET")
        expected_size = _original_expected_body_size(request, response)
        if (
            expected_size is None
            and response.headers.get("transfer-encoding", "").strip().lower() != "chunked"
        ):
            # Addon converts plain chunked output to Content-Length for these
            # custom methods. Additional transfer codings would be lost by that
            # conversion; reject through the normal upstream 502 error path.
            raise ValueError(
                "combined transfer codings are unsupported for custom HEAD-case methods"
            )
        return expected_size
    return _original_expected_body_size(request, response)


def install() -> None:
    """Activate the compatibility adapter after acquiring Runtime ownership."""
    for package, required in SUPPORTED_VERSIONS.items():
        try:
            installed = version(package)
        except PackageNotFoundError as exc:
            raise RuntimeError(f"HTTP compatibility requires {package}=={required}") from exc
        if installed != required:
            raise RuntimeError(
                f"HTTP compatibility requires {package}=={required}; found {installed}. "
                "Install the project's pinned dependencies before starting the proxy."
            )
    current_factory = getattr(_http1, "make_body_reader", None)
    current_size = getattr(http1, "expected_http_body_size", None)
    if current_factory is _make_body_reader and current_size is _expected_body_size:
        return
    if (
        current_factory is not _original_make_body_reader
        and current_factory is not _make_body_reader
    ):
        raise RuntimeError("HTTP body reader was replaced; refusing to overwrite another adapter")
    if current_size is not _original_expected_body_size and current_size is not _expected_body_size:
        raise RuntimeError("HTTP body sizing was replaced; refusing to overwrite another adapter")
    try:
        compatible = tuple(signature(current_factory).parameters) == ("expected_size",)
        compatible &= isinstance(current_factory(None), ChunkedReader)
        compatible &= callable(current_factory(0)) and callable(current_factory(-1))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("Pinned HTTP body reader API is incompatible") from exc
    if not compatible:
        raise RuntimeError("Pinned HTTP body reader API is incompatible")
    try:
        compatible_size = tuple(signature(current_size).parameters) == ("request", "response")
        probe = http.Request.make("GET", "http://localhost/")
        response = http.Response.make(200, b"x")
        compatible_size &= current_size(probe, response) == 1
        probe.method = "HEAD"
        compatible_size &= current_size(probe, response) == 0
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("Pinned HTTP body sizing API is incompatible") from exc
    if not compatible_size:
        raise RuntimeError("Pinned HTTP body sizing API is incompatible")
    # Deliberate replacement of a dependency function with the same signature.
    _http1.make_body_reader = _make_body_reader  # ty: ignore[invalid-assignment]
    http1.expected_http_body_size = _expected_body_size  # ty: ignore[invalid-assignment]


def restore() -> None:
    """Restore the dependency factory after all proxy connections are closed."""
    if _http1.make_body_reader is _make_body_reader:
        _http1.make_body_reader = _original_make_body_reader
    if http1.expected_http_body_size is _expected_body_size:
        http1.expected_http_body_size = _original_expected_body_size
