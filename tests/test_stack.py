import copy
import hashlib

import pytest
import yaml

from corenova.stack import build_bundle, compile_stack, validate_stack
from tests.test_schema_rules import errors


@pytest.fixture
def stack():
    # Deliberately synthetic digests: unit fixture, NOT a deployable registration.
    return {
        "schema_version": 2, "name": "test-stack",
        "entrypoint": {"service": "web", "port": 8080},
        "volumes": ["db-data"], "secrets": ["db-auth"],
        "services": {
            "web": {"image": "example/web@sha256:" + "a" * 64,
                    "depends_on": ["db"], "healthcheck": ["/check"],
                    "environment": {"DB_HOST": "db"},
                    "secret_files": {"DB_PASSWORD_FILE": "db-auth"}},
            "db": {"image": "example/db@sha256:" + "b" * 64,
                   "healthcheck": ["/check"], "volumes": {"db-data": "/var/lib/db"},
                   "secret_files": {"DB_PASSWORD_FILE": "db-auth"}},
        },
    }


def test_compile_is_deterministic_and_private_by_default(stack):
    original = copy.deepcopy(stack)
    first, lock = build_bundle(stack)
    assert (first, lock) == build_bundle(stack)
    assert stack == original
    assert lock["status"] == "UNVERIFIED"
    assert lock["compose_sha256"] == hashlib.sha256(first.encode()).hexdigest()
    compose = yaml.safe_load(first)
    assert "ports" not in compose["services"]["db"]
    assert compose["services"]["web"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert compose["services"]["web"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert validate_stack(stack) == ["db", "web"]
    assert compose["services"]["db"]["volumes"][0]["bind"]["create_host_path"] is False


@pytest.mark.parametrize("field,value", [
    ("privileged", True), ("network_mode", "host"), ("build", "."),
    ("ports", ["5432:5432"]), ("env_file", "/tmp/creds"),
    ("image", "postgres:latest"), ("image", "postgres:17"),
    ("command", "sh -c anything"), ("healthcheck", []),
    ("depends_on", ["missing"]), ("depends_on", ["web"]),
    ("environment", {"DB_PASSWORD": "plaintext"}),
    ("secret_files", {"DB_PASSWORD_FILE": "unknown"}),
    ("volumes", {"db-data": "/run/secrets"}),
    ("volumes", {"db-data": "/a/../etc"}),
    ("volumes", {"/var/run/docker.sock": "/socket"}),
])
def test_unsafe_service_is_rejected(stack, field, value):
    stack["services"]["web"][field] = value
    with pytest.raises(ValueError):
        compile_stack(stack)


@pytest.mark.parametrize("bad", [None, [], True, {}, {"schema_version": 2}])
def test_malformed_top_level(bad):
    with pytest.raises(ValueError):
        validate_stack(bad)


def test_cycles_rejected(stack):
    stack["services"]["db"]["depends_on"] = ["web"]
    with pytest.raises(ValueError, match="cyclic"):
        validate_stack(stack)


def test_literal_interpolation_not_expanded(stack):
    stack["services"]["web"]["command"] = ["echo", "${HOME}"]
    stack["services"]["web"]["environment"]["TEXT"] = "$HOME"
    generated = compile_stack(stack)["services"]["web"]
    assert generated["command"] == ["echo", "$${HOME}"]
    assert generated["environment"]["TEXT"] == "$$HOME"


def test_v2_cannot_enter_legacy_publish_validation(tmp_path):
    assert errors(tmp_path, lambda d: d.update(schema_version=2))


def test_hidden_multiservice_cannot_enter_legacy_validation(tmp_path):
    def mutate(doc):
        doc["deploy"]["services"] = {}
    assert errors(tmp_path, mutate)
