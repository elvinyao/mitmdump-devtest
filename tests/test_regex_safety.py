import subprocess
import sys

import pytest
from pydantic import ValidationError

from fault_engine.cli import main
from fault_engine.config import Match
from fault_engine.matching import REGEX_LENGTH_LIMIT, compile_path_pattern


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        (r"/orders/\d+", "/orders/12", True),
        (r"/orders/\d+", "/orders/12/extra", False),
        (r"/orders/\d+", "prefix/orders/12", False),
        (r"/a|/b", "/b", True),
        (r"/a|/b", "/abc", False),
        (r"(?i)/hello", "/HELLO", True),
        (r"(?m)^/hello$", "/hello\n", False),
        (r"/(?P<name>[a-z]+)", "/hello", True),
    ],
)
def test_linear_regex_preserves_fullmatch_contract(pattern, path, expected):
    assert compile_path_pattern(pattern).fullmatch(path) is expected


@pytest.mark.parametrize(
    "pattern",
    [r"/a(?=secret)", r"/(?<=secret)a", r"/(secret)\1", "[", ")|.*(?:", "(?x)/a # tail"],
)
def test_unsupported_regex_rejected_during_configuration(pattern):
    with pytest.raises(ValidationError) as raised:
        Match(path_regex=pattern)
    errors = raised.value.errors(include_input=False, include_context=False)
    assert "unsupported path_regex" in errors[0]["msg"]
    assert "secret" not in errors[0]["msg"]


def test_regex_compilation_length_is_bounded():
    with pytest.raises(ValueError, match="exceeds"):
        compile_path_pattern("a" * (REGEX_LENGTH_LIMIT + 1))


def test_nested_repetition_does_not_block_request_matching():
    # A subprocess timeout makes this fail safely if backtracking is reintroduced.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fault_engine.config import Config; from fault_engine.engine import Engine; "
            "config=Config.model_validate({"
            "'services':[{'id':'s','port':8080,'upstream':'http://localhost:9000'}],"
            "'rules':[{'id':'r','service':'s','match':{'path_regex':'/(a+)+$'},"
            "'sequence':[{'action':'passthrough'}]}]}); "
            "engine=Engine(config); "
            "assert engine.decide('s','GET','/'+'a'*10000+'!',{},[]) is None; "
            "assert engine.snapshot()==[]; print('completed')",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "completed"


def test_cli_regex_errors_do_not_echo_pattern(tmp_path, capsys):
    config = tmp_path / "scenario.yaml"
    config.write_text(
        "services:\n- id: s\n  port: 8080\n  upstream: http://localhost\n"
        "rules:\n- id: r\n  service: s\n  match:\n    path_regex: '/(?=secret)'\n"
        "  sequence:\n  - action: passthrough\n"
    )
    assert main(["validate", str(config)]) == 2
    error = capsys.readouterr().err
    assert "unsupported path_regex" in error
    assert "secret" not in error
