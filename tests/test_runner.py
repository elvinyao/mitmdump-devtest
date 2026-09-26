"""Check the runner interface inside Docker without starting nested containers."""

import os
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / ".agent" / "run.sh"


def run_runner(*args, path, cwd=None):
    return subprocess.run(
        ["/bin/bash", str(RUNNER), *args],
        env={**os.environ, "PATH": path},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize("args", [(), ("--publish",)])
def test_missing_runner_command_shows_usage_without_docker(tmp_path, args):
    result = run_runner(*args, path=str(tmp_path))
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not result.stdout


def test_runner_help_needs_no_docker(tmp_path):
    result = run_runner("--help", path=str(tmp_path))
    assert result.returncode == 0
    assert "--publish" in result.stdout
    assert "bash .agent/check.sh" in result.stdout
    assert not result.stderr


def test_missing_docker_explains_required_setup(tmp_path):
    result = run_runner("uv", "run", "pytest", path=str(tmp_path))
    assert result.returncode == 127
    assert "Docker CLI not found" in result.stderr


@pytest.mark.parametrize("publish", [False, True])
def test_runner_preserves_arguments_working_directory_and_docker_exit(tmp_path, publish):
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\nexit 37\n")
    docker.chmod(0o755)
    command = ["sh", "-lc", "printf '%s' 'two words; $HOME'", ""]
    result = run_runner(
        *(["--publish"] if publish else []),
        *command,
        path=f"{tmp_path}:{os.environ['PATH']}",
        cwd=tmp_path,
    )
    assert result.returncode == 37
    forwarded = result.stdout.splitlines()
    assert forwarded[:4] == ["run", "--rm", "-i", "--init"]
    assert f"{RUNNER.parent.parent}:/workspace" in forwarded
    assert forwarded[forwarded.index("-w") + 1] == "/workspace"
    assert forwarded[-7:] == [
        "python:3.12-bookworm",
        "bash",
        "/workspace/.agent/container.sh",
        *command,
    ]
    bindings = [forwarded[i + 1] for i, arg in enumerate(forwarded) if arg == "-p"]
    assert bindings == (
        ["127.0.0.1:18080:8080", "127.0.0.1:18081:8081", "127.0.0.1:19090:9090"] if publish else []
    )
