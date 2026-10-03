"""Fail clearly before listening when the private HTTP adapter is unsupported."""

import pytest
from conftest import free_port
from mitmproxy import http

from fault_engine import http1_compat
from fault_engine.config import Config
from fault_engine.runtime import Runtime


@pytest.mark.parametrize("package", ["mitmproxy", "h11"])
async def test_incompatible_dependency_fails_before_binding(package, monkeypatch):
    original_version = http1_compat.version
    original_factory = http1_compat._http1.make_body_reader
    monkeypatch.setattr(
        http1_compat,
        "version",
        lambda name: "99.0.0" if name == package else original_version(name),
    )
    runtime = Runtime(
        Config.model_validate(
            {
                "services": [{"id": "test", "port": free_port(), "upstream": "http://127.0.0.1:9"}],
                "admin": {"port": free_port()},
            }
        ),
        admin_token="test-token",
    )
    try:
        with pytest.raises(RuntimeError, match=rf"requires {package}==.*found 99.0.0"):
            await runtime.start()
        assert runtime.master is None
        assert not runtime.bridges
        assert Runtime._active is None
        assert http1_compat._http1.make_body_reader is original_factory
    finally:
        await runtime.close()


def test_install_preserves_an_existing_foreign_adapter(monkeypatch):
    def foreign_adapter(expected_size):
        return None

    monkeypatch.setattr(http1_compat._http1, "make_body_reader", foreign_adapter)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        http1_compat.install()
    http1_compat.restore()
    assert http1_compat._http1.make_body_reader is foreign_adapter


def test_install_detects_body_reader_contract_drift(monkeypatch):
    def changed_factory(size):
        return None

    monkeypatch.setattr(http1_compat, "_original_make_body_reader", changed_factory)
    monkeypatch.setattr(http1_compat._http1, "make_body_reader", changed_factory)
    with pytest.raises(RuntimeError, match="body reader API is incompatible"):
        http1_compat.install()
    assert http1_compat._http1.make_body_reader is changed_factory


def test_install_is_idempotent_and_restores_exact_original():
    original = http1_compat._http1.make_body_reader
    original_size = http1_compat.http1.expected_http_body_size
    try:
        http1_compat.install()
        http1_compat.install()
        assert http1_compat._http1.make_body_reader is http1_compat._make_body_reader
        assert http1_compat.http1.expected_http_body_size is http1_compat._expected_body_size
    finally:
        http1_compat.restore()
    assert http1_compat._http1.make_body_reader is original
    assert http1_compat.http1.expected_http_body_size is original_size


def test_install_preserves_foreign_body_sizing_without_partially_installing(monkeypatch):
    original_factory = http1_compat._http1.make_body_reader

    def foreign_size(request, response=None):
        return 0

    monkeypatch.setattr(http1_compat.http1, "expected_http_body_size", foreign_size)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        http1_compat.install()
    http1_compat.restore()
    assert http1_compat.http1.expected_http_body_size is foreign_size
    assert http1_compat._http1.make_body_reader is original_factory


def test_install_detects_body_sizing_contract_drift_without_partial_install(monkeypatch):
    original_factory = http1_compat._http1.make_body_reader

    def changed_size(message):
        return None

    monkeypatch.setattr(http1_compat, "_original_expected_body_size", changed_size)
    monkeypatch.setattr(http1_compat.http1, "expected_http_body_size", changed_size)
    with pytest.raises(RuntimeError, match="body sizing API is incompatible"):
        http1_compat.install()
    assert http1_compat.http1.expected_http_body_size is changed_size
    assert http1_compat._http1.make_body_reader is original_factory


@pytest.mark.parametrize("method,expected_size", [("head", 4), ("hEaD", 4), ("HEAD", 0)])
def test_body_sizing_preserves_request_and_upload_length(method, expected_size):
    request = http.Request.make(method, "http://localhost/", b"upload")
    original_data = request.data
    response = http.Response.make(200, b"body")
    assert http1_compat._expected_body_size(request, response) == expected_size
    assert http1_compat._expected_body_size(request) == len(b"upload")
    assert request.data is original_data
    assert request.data.method == method.encode()
    assert request.raw_content == b"upload"


@pytest.mark.parametrize("coding", ["gzip,chunked", "deflate, chunked", "compress, CHUNKED"])
def test_custom_head_combined_transfer_coding_boundary(coding):
    response = http.Response.make(200, b"opaque", {"Transfer-Encoding": coding})
    for method in ["head", "hEaD"]:
        request = http.Request.make(method, "http://localhost/")
        with pytest.raises(ValueError, match="combined transfer codings are unsupported"):
            http1_compat._expected_body_size(request, response)
        bodyless = http.Response.make(304, b"", {"Transfer-Encoding": coding})
        assert http1_compat._expected_body_size(request, bodyless) == 0
    assert (
        http1_compat._expected_body_size(http.Request.make("HEAD", "http://localhost/"), response)
        == 0
    )
    assert (
        http1_compat._expected_body_size(http.Request.make("GET", "http://localhost/"), response)
        is None
    )
