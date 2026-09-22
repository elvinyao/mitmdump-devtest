import asyncio
import json
import os
import signal
import subprocess
import sys

import httpx
import yaml
from conftest import free_port


def invoke(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "fault_engine", *args],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


def valid_file(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text(
        yaml.safe_dump(
            {"services": [{"id": "orders", "port": 8080, "upstream": "http://localhost:9000"}]}
        )
    )
    return str(path)


def test_cli_validate_and_help(tmp_path):
    result = invoke("validate", valid_file(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "valid" in result.stdout.lower()
    assert invoke("--help").returncode == 0


def test_cli_validation_is_nonzero_and_redacts_input(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text(
        "services:\n- id: orders\n  port: 8080\n  upstream: http://user:SECRET@localhost\n"
    )
    result = invoke("validate", str(path))
    assert result.returncode != 0
    assert "services.0.upstream" in result.stderr
    assert "SECRET" not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_missing_file_and_malformed_yaml(tmp_path):
    assert invoke("validate", str(tmp_path / "missing")).returncode != 0
    path = tmp_path / "invalid.yaml"
    path.write_text("services: [SECRET")
    result = invoke("validate", str(path))
    assert result.returncode != 0
    assert "SECRET" not in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_requires_admin_token(tmp_path):
    env = os.environ.copy()
    env.pop("FAULT_ADMIN_TOKEN", None)
    result = invoke("serve", valid_file(tmp_path), env=env)
    assert result.returncode != 0
    assert "FAULT_ADMIN_TOKEN" in result.stderr


def test_cross_field_errors_include_specific_location(tmp_path):
    path = tmp_path / "invalid-ref.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "services": [{"id": "orders", "port": 8080, "upstream": "http://localhost"}],
                "rules": [{"id": "retry", "service": "missing", "sequence": [{"action": "reset"}]}],
            }
        )
    )
    result = invoke("validate", str(path))
    assert result.returncode == 2
    assert "config rules.0.service:" in result.stderr


async def test_cli_serve_ready_mock_and_clean_signal_exit(tmp_path):
    port = free_port()
    path = tmp_path / "serve.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "services": [{"id": "s", "port": port, "upstream": "http://127.0.0.1:9"}],
                "admin": {"port": free_port()},
                "rules": [
                    {
                        "id": "r",
                        "service": "s",
                        "sequence": [
                            {"action": "respond", "status": 200, "json_body": {"ok": True}},
                        ],
                    }
                ],
            }
        )
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fault_engine",
        "serve",
        str(path),
        env={**os.environ, "FAULT_ADMIN_TOKEN": "test-token"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None and process.stderr is not None
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line, (await process.stderr.read()).decode()
        assert json.loads(line)["event"] == "ready"
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{port}/")
            assert response.json() == {"ok": True}
        process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(process.wait(), 5) == 0
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
