"""Fail clearly before listening when the private HTTP adapter is unsupported."""

import pytest
from conftest import free_port

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
    try:
        http1_compat.install()
        http1_compat.install()
        assert http1_compat._http1.make_body_reader is http1_compat._make_body_reader
    finally:
        http1_compat.restore()
    assert http1_compat._http1.make_body_reader is original
