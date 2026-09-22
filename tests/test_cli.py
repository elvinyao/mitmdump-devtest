import os
import subprocess
import sys

import yaml


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
