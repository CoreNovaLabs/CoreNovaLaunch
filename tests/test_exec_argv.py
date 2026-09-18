import subprocess

import pytest

from corenova.runtime import assert_version
from tests.test_regression_fixes import _spec_with_assertion
from tests.test_schema_rules import errors


@pytest.mark.parametrize("command,tail", [
    ("app --version", ["sh", "-c", "app --version"]),
    (["/bin/app", "--version"], ["/bin/app", "--version"]),
    (["/bin/app", "literal;$(value)"], ["/bin/app", "literal;$(value)"]),
])
def test_execution_preserves_arguments(monkeypatch, command, tail):
    def fake_run(argv, **kwargs):
        assert argv == ["docker", "exec", "container", *tail]
        return subprocess.CompletedProcess(argv, 0, "app 1.2.3\n", "")

    monkeypatch.setattr("corenova.runtime.run", fake_run)
    spec = _spec_with_assertion({"kind": "exec_command", "command": command,
                                 "expected": "app {version_no_v}"})
    assert assert_version("container", spec, "v1.2.3").ok


@pytest.mark.parametrize("command", [[], [""], ["app", 3], {}, True, 42, " "])
def test_invalid_command_rejected(tmp_path, command):
    def mutate(data):
        data["health_check"]["version_assertion"]["command"] = command
    assert any("规则12" in error for error in errors(tmp_path, mutate))


def test_direct_command_failure_is_not_a_pass(monkeypatch):
    monkeypatch.setattr("corenova.runtime.run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, "app 1.2.3", "failed"))
    spec = _spec_with_assertion({"kind": "exec_command", "command": ["/bin/app"],
                                 "expected": "app 1.2.3"})
    assert not assert_version("container", spec, "v1.2.3").ok
