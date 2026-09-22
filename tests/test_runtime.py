"""Runtime project isolation and ownership-checked destructive-test interface."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from corenova import business_fixtures, runtime
from corenova.appspec import AppSpec
from corenova.business_fixtures import DockerApp


def make_env(tmp_path, monkeypatch, **opts):
    monkeypatch.delenv("CORENOVA_PROBE_HOST", raising=False)
    cfg = SimpleNamespace(root=tmp_path, run_opts=opts)
    spec = AppSpec("sample", tmp_path / "apps/sample.yaml", "", {
        "deploy": {"container_port": 1234, "compose_file": "apps/sample/docker-compose.yml"},
    })
    env = runtime.build_env(cfg, spec, "sample:1@sha256:abc", "sample:1", tmp_path / "run")
    return cfg, spec, env


def test_env_exports_exact_project_and_compose_path(tmp_path, monkeypatch):
    cfg, spec, env = make_env(tmp_path, monkeypatch, host_port=4321, probe_host="probe.test")
    assert env.base_url == "http://probe.test:4321"
    assert env.values["CORENOVA_COMPOSE_PROJECT"] == env.project
    assert env.values["CORENOVA_COMPOSE_FILE"] == str(tmp_path / "apps/sample/docker-compose.yml")
    assert env.values["CORENOVA_DATA_DIR"] == str(env.data_dir)
    assert env.data_dir.is_dir()
    assert env.project.startswith("cn-sample-")
    other = runtime.build_env(cfg, spec, "sample:1@sha256:abc", "sample:1", tmp_path / "run", 5555)
    assert other.project != env.project
    assert other.base_url == "http://probe.test:5555"
    monkeypatch.setenv("CORENOVA_PROBE_HOST", "override.test")
    third = runtime.build_env(cfg, spec, "sample:1@sha256:abc", "sample:1", tmp_path / "third")
    assert third.base_url == "http://override.test:4321"


def test_compose_receives_same_environment_as_tests(tmp_path, monkeypatch):
    _, spec, env = make_env(tmp_path, monkeypatch)
    command = Mock(return_value=SimpleNamespace(stdout="ok", stderr=""))
    monkeypatch.setattr(runtime, "run", command)
    assert runtime.compose(env, spec, tmp_path, "ps", "-q") == "ok"
    args, kwargs = command.call_args
    assert args[0] == ["docker", "compose", "-p", env.project, "-f",
                       env.values["CORENOVA_COMPOSE_FILE"], "ps", "-q"]
    assert kwargs["env"] == env.values


def test_missing_runtime_identity_fails_instead_of_skipping():
    with pytest.raises(AssertionError, match="Runner must pass CORENOVA_COMPOSE_FILE"):
        DockerApp({"CORENOVA_APP_URL": "http://localhost:1234"})


@pytest.mark.parametrize("bad_project,bad_mount", [(True, False), (False, True)])
def test_wrong_ownership_cannot_restart(tmp_path, monkeypatch, bad_project, bad_mount):
    _, _, env = make_env(tmp_path, monkeypatch)
    app = DockerApp(env.values)
    monkeypatch.setattr(app, "compose", lambda *args: "explicit-id")
    info = {"Id": "explicit-id", "Config": {"Labels": {"com.docker.compose.project":
            "someone-else" if bad_project else env.project}}, "Mounts": [{"Type": "bind",
            "Source": str(tmp_path / "unrelated" if bad_mount else env.data_dir), "Destination": "/data"}]}
    command = Mock(return_value=SimpleNamespace(stdout=json.dumps([info])))
    monkeypatch.setattr(business_fixtures, "run", command)
    with pytest.raises(AssertionError):
        app.restart()
    assert all(call.args[0][:2] == ["docker", "inspect"] for call in command.call_args_list)


@pytest.mark.parametrize("after_id", ["owned-id", "unexpected-replacement"])
def test_restart_targets_id_and_checks_no_recreation(tmp_path, monkeypatch, after_id):
    _, _, env = make_env(tmp_path, monkeypatch)
    app = DockerApp(env.values)
    before = {"Id": "owned-id", "State": {"StartedAt": "before", "Running": True}}
    after = {"Id": after_id, "State": {"StartedAt": "after", "Running": True}}
    monkeypatch.setattr(app, "owned_container", Mock(side_effect=[(before, "/data"), (after, "/data")]))
    monkeypatch.setattr(app, "ready", Mock())
    command = Mock()
    monkeypatch.setattr(business_fixtures, "run", command)
    if after_id != before["Id"]:
        with pytest.raises(AssertionError, match="must not recreate"):
            app.restart()
    else:
        app.restart()
    command.assert_called_once_with(["docker", "restart", "--time", "30", "owned-id"], timeout=90)


def test_backup_refuses_live_copy_and_restarts_source(tmp_path, monkeypatch):
    _, _, env = make_env(tmp_path, monkeypatch)
    app = DockerApp(env.values)
    info = {"Id": "owned-id", "State": {"Running": True}}
    monkeypatch.setattr(app, "owned_container", lambda: (info, "/data"))
    monkeypatch.setattr(app, "ready", Mock())
    command = Mock()
    copy = Mock()
    monkeypatch.setattr(business_fixtures, "run", command)
    monkeypatch.setattr(business_fixtures.subprocess, "run", copy)
    with pytest.raises(AssertionError, match="Never copy a live"):
        with app.restored():
            pytest.fail("A live database must never reach restoration")
    copy.assert_not_called()
    assert command.call_args_list[-1].args[0] == ["docker", "start", "owned-id"]
