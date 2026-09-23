"""Reject unsupported trailers through mitmproxy 12's normal protocol-error path.

The pinned HTTP/1 parser raises NotImplementedError after h11 successfully parses
trailers, leaving buffered clients waiting forever. Convert that one unsupported
event into a protocol error while it is still inside the parser's error boundary.
Install only while this process's single Runtime owns mitmproxy, then restore.
"""

import h11
from h11._readers import ChunkedReader
from h11._receivebuffer import ReceiveBuffer
from mitmproxy.proxy.layers.http import _http1

_original_make_body_reader = _http1.make_body_reader


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


def install() -> None:
    """Activate the compatibility adapter after acquiring Runtime ownership."""
    # Deliberate replacement of a dependency function with the same signature.
    _http1.make_body_reader = _make_body_reader  # ty: ignore[invalid-assignment]


def restore() -> None:
    """Restore the dependency factory after all proxy connections are closed."""
    if _http1.make_body_reader is _make_body_reader:
        _http1.make_body_reader = _original_make_body_reader
