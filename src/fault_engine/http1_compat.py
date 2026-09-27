"""Reject unsupported trailers through mitmproxy 12's normal protocol-error path.

The pinned HTTP/1 parser raises NotImplementedError after h11 successfully parses
trailers, leaving buffered clients waiting forever. Convert that one unsupported
event into a protocol error while it is still inside the parser's error boundary.
Install only while this process's single Runtime owns mitmproxy, then restore.
"""

from importlib.metadata import PackageNotFoundError, version
from inspect import signature

import h11
from h11._readers import ChunkedReader
from h11._receivebuffer import ReceiveBuffer
from mitmproxy.proxy.layers.http import _http1

_original_make_body_reader = _http1.make_body_reader
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
    if current_factory is _make_body_reader:
        return
    if current_factory is not _original_make_body_reader:
        raise RuntimeError("HTTP body reader was replaced; refusing to overwrite another adapter")
    try:
        compatible = tuple(signature(current_factory).parameters) == ("expected_size",)
        compatible &= isinstance(current_factory(None), ChunkedReader)
        compatible &= callable(current_factory(0)) and callable(current_factory(-1))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("Pinned HTTP body reader API is incompatible") from exc
    if not compatible:
        raise RuntimeError("Pinned HTTP body reader API is incompatible")
    # Deliberate replacement of a dependency function with the same signature.
    _http1.make_body_reader = _make_body_reader  # ty: ignore[invalid-assignment]


def restore() -> None:
    """Restore the dependency factory after all proxy connections are closed."""
    if _http1.make_body_reader is _make_body_reader:
        _http1.make_body_reader = _original_make_body_reader
