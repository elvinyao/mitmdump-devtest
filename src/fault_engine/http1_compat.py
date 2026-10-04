"""Narrow HTTP/1 compatibility fixes for the pinned mitmproxy 12 parser.

The pinned HTTP/1 parser raises NotImplementedError after h11 successfully parses
trailers, leaving buffered clients waiting forever. Convert that one unsupported
event into a protocol error while it is still inside the parser's error boundary.
It also treats custom case variants of HEAD as bodyless; use ordinary response
framing for those methods without changing the request sent to the backend.
Consume non-101 informational responses before the final-response-only flow hooks.
Install only while this process's single Runtime owns mitmproxy, then restore.
"""

from copy import copy
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from inspect import signature
from weakref import WeakKeyDictionary

import h11
from h11._readers import ChunkedReader
from h11._receivebuffer import ReceiveBuffer
from mitmproxy import connection, http, options
from mitmproxy.net.http import http1
from mitmproxy.proxy import commands, events, layer
from mitmproxy.proxy.context import Context
from mitmproxy.proxy.layers.http import _http1

_original_make_body_reader = _http1.make_body_reader
_original_expected_body_size = http1.expected_http_body_size
_original_read_response_headers = _http1.Http1Client.read_headers
SUPPORTED_VERSIONS = {"mitmproxy": "12.2.3", "h11": "0.16.0"}
MAX_INFORMATIONAL_RESPONSES = 100
_interim_counts: WeakKeyDictionary[_http1.Http1Client, int] = WeakKeyDictionary()


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


def _read_response_headers(
    self: _http1.Http1Client, event: events.ConnectionEvent
) -> layer.CommandGenerator[None]:
    while True:
        parser = _original_read_response_headers(self, event)
        try:
            for command in parser:
                if isinstance(command, _http1.ReceiveHttp):
                    if isinstance(command.event, _http1.ResponseHeaders):
                        status = command.event.response.status_code
                        if 100 <= status < 200 and status != 101:
                            # The pinned generator has parsed only the headers;
                            # body_reader/state/mark_done have not run yet. Drop
                            # this event and close the generator at that boundary.
                            # This buffered proxy only exposes the final response.
                            self.response = None
                            count = _interim_counts.get(self, 0) + 1
                            if count > MAX_INFORMATIONAL_RESPONSES:
                                _interim_counts.pop(self, None)
                                yield commands.CloseConnection(self.conn)
                                yield _http1.ReceiveHttp(
                                    _http1.ResponseProtocolError(
                                        command.event.stream_id,
                                        "too many informational HTTP responses",
                                        _http1.ErrorCode.GENERIC_SERVER_ERROR,
                                    )
                                )
                                return
                            _interim_counts[self] = count
                            break
                        _interim_counts.pop(self, None)
                    elif isinstance(command.event, _http1.ResponseProtocolError):
                        _interim_counts.pop(self, None)
                yield command
            else:
                if isinstance(event, events.ConnectionClosed):
                    _interim_counts.pop(self, None)
                return
        finally:
            parser.close()


def _response_reader_compatible(reader) -> bool:
    """Check the private suspension point before installing any adapters."""
    if tuple(signature(reader).parameters) != ("self", "event"):
        return False
    client = connection.Client(peername=("127.0.0.1", 1), sockname=("127.0.0.1", 2))
    probe = _http1.Http1Client(Context(client, options.Options()))
    probe.request = http.Request.make("GET", "http://localhost/")
    probe.stream_id = 1
    final = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    probe.buf += b"HTTP/1.1 103 Early Hints\r\n\r\n" + final
    state = probe.state
    generator = reader(probe, events.DataReceived(probe.conn, b""))
    try:
        command = next(generator)
        return (
            isinstance(command, _http1.ReceiveHttp)
            and isinstance(command.event, _http1.ResponseHeaders)
            and command.event.response.status_code == 103
            and command.event.end_stream
            and probe.response is command.event.response
            and probe.state == state
            and not hasattr(probe, "body_reader")
            and not probe.response_done
            and bytes(probe.buf) == final
        )
    finally:
        generator.close()


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
    current_headers = getattr(_http1.Http1Client, "read_headers", None)
    if (
        current_factory is _make_body_reader
        and current_size is _expected_body_size
        and current_headers is _read_response_headers
    ):
        return
    if (
        current_factory is not _original_make_body_reader
        and current_factory is not _make_body_reader
    ):
        raise RuntimeError("HTTP body reader was replaced; refusing to overwrite another adapter")
    if current_size is not _original_expected_body_size and current_size is not _expected_body_size:
        raise RuntimeError("HTTP body sizing was replaced; refusing to overwrite another adapter")
    if current_headers not in (_original_read_response_headers, _read_response_headers):
        raise RuntimeError(
            "HTTP response reader was replaced; refusing to overwrite another adapter"
        )
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
    try:
        compatible_headers = _response_reader_compatible(_original_read_response_headers)
    except (TypeError, ValueError, AttributeError, StopIteration) as exc:
        raise RuntimeError("Pinned HTTP response reader API is incompatible") from exc
    if not compatible_headers:
        raise RuntimeError("Pinned HTTP response reader API is incompatible")
    # Deliberate replacement of a dependency function with the same signature.
    _http1.make_body_reader = _make_body_reader  # ty: ignore[invalid-assignment]
    http1.expected_http_body_size = _expected_body_size  # ty: ignore[invalid-assignment]
    _http1.Http1Client.read_headers = _read_response_headers


def restore() -> None:
    """Restore the dependency factory after all proxy connections are closed."""
    if _http1.make_body_reader is _make_body_reader:
        _http1.make_body_reader = _original_make_body_reader
    if http1.expected_http_body_size is _expected_body_size:
        http1.expected_http_body_size = _original_expected_body_size
    if _http1.Http1Client.read_headers is _read_response_headers:
        _http1.Http1Client.read_headers = _original_read_response_headers
    _interim_counts.clear()
