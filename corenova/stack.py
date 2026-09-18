"""Experimental v2 stack compiler. No execution, secrets generation or publication.

Only reviewed, digest-locked definitions are accepted. Unknown fields fail closed;
this is deliberately not a general-purpose Compose passthrough.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

import yaml

NAME = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}\Z")
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
SENSITIVE = re.compile(r"password|secret|token|private_key", re.I)


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _object(value, allowed, required, where):
    _require(isinstance(value, dict), f"{where}: expected mapping")
    _require(not set(value) - set(allowed), f"{where}: unsupported fields")
    _require(set(required) <= set(value), f"{where}: missing required fields")


def _name(value):
    return isinstance(value, str) and NAME.fullmatch(value) is not None


def _argv(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(x, str) and bool(x.strip()) and "\x00" not in x for x in value))


def validate_stack(doc: dict) -> list[str]:
    """Validate and return a deterministic dependency-first service ordering."""
    _object(doc, {"schema_version", "name", "entrypoint", "services", "volumes", "secrets"},
            {"schema_version", "name", "entrypoint", "services"}, "stack")
    _require(type(doc["schema_version"]) is int and doc["schema_version"] == 2, "schema_version must be 2")
    _require(_name(doc["name"]), "invalid stack name")
    services = doc["services"]
    _require(isinstance(services, dict) and 1 <= len(services) <= 8, "services: require 1..8 services")
    _require(all(_name(n) for n in services), "invalid service name")
    entry = doc["entrypoint"]
    _object(entry, {"service", "port"}, {"service", "port"}, "entrypoint")
    _require(isinstance(entry["service"], str) and entry["service"] in services, "unknown entrypoint service")
    _require(type(entry["port"]) is int and 1 <= entry["port"] <= 65535, "invalid entrypoint port")
    for field in ("volumes", "secrets"):
        names = doc.get(field, [])
        _require(isinstance(names, list) and all(_name(n) for n in names), f"invalid {field}")
        _require(len(names) == len(set(names)), f"duplicate {field}")
    used_volumes, used_secrets = set(), set()
    for name, service in services.items():
        _object(service, {"image", "command", "environment", "secret_files", "volumes",
                          "depends_on", "healthcheck"}, {"image", "healthcheck"}, name)
        _require(isinstance(service["image"], str) and IMAGE.fullmatch(service["image"]),
                 f"{name}: image must be pinned by sha256 digest")
        if "command" in service:
            _require(_argv(service["command"]), f"{name}: command must be argv")
        _require(_argv(service["healthcheck"]), f"{name}: healthcheck must be argv")
        deps = service.get("depends_on", [])
        _require(isinstance(deps, list) and all(isinstance(d, str) and d in services and d != name for d in deps),
                 f"{name}: invalid dependency")
        _require(len(set(deps)) == len(deps), f"{name}: duplicate dependency")
        env = service.get("environment", {})
        _require(isinstance(env, dict), f"{name}: environment must be mapping")
        for key, value in env.items():
            _require(isinstance(key, str) and ENV.fullmatch(key), f"{name}: invalid environment name")
            _require(isinstance(value, str) and "\x00" not in value, f"{name}: environment values must be strings")
            _require(not SENSITIVE.search(key) and not SENSITIVE.search(value),
                     f"{name}: sensitive configuration must use secret_files")
        refs = service.get("secret_files", {})
        _require(isinstance(refs, dict), f"{name}: secret_files must be mapping")
        for key, ref in refs.items():
            _require(isinstance(key, str) and ENV.fullmatch(key) and key.endswith("_FILE"),
                     f"{name}: file credential variable must end in _FILE")
            _require(key not in env, f"{name}: duplicate environment binding")
            _require(isinstance(ref, str) and ref in doc.get("secrets", []), f"{name}: undeclared secret")
            used_secrets.add(ref)
        mounts = service.get("volumes", {})
        _require(isinstance(mounts, dict), f"{name}: volumes must map declared names to targets")
        targets = []
        for volume, target in mounts.items():
            _require(isinstance(volume, str) and volume in doc.get("volumes", []), f"{name}: undeclared volume")
            _require(isinstance(target, str) and re.fullmatch(r"/[A-Za-z0-9_./-]+", target),
                     f"{name}: invalid volume target")
            path = PurePosixPath(target)
            _require(".." not in path.parts and str(path) == target and target not in ("/", "/run", "/run/secrets"),
                     f"{name}: unsafe volume target")
            _require(not target.startswith("/run/secrets/"), f"{name}: secret path cannot be shadowed")
            _require(not any(path == p or path in p.parents or p in path.parents for p in targets),
                     f"{name}: overlapping mount targets")
            targets.append(path)
            used_volumes.add(volume)
    _require(used_volumes == set(doc.get("volumes", [])), "unused volume declarations")
    _require(used_secrets == set(doc.get("secrets", [])), "unused secret declarations")
    order, visiting, visited = [], set(), set()

    def visit(name):
        _require(name not in visiting, "cyclic service dependencies")
        if name in visited:
            return
        visiting.add(name)
        for dependency in sorted(services[name].get("depends_on", [])):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        order.append(name)

    for name in sorted(services):
        visit(name)
    return order


def _literal(value):
    """Prevent Compose from expanding environment references in reviewed literals."""
    return value.replace("$", "$$")


def compile_stack(doc: dict) -> dict:
    """Compile a portable definition; host path/port values remain required inputs.

    No secret values are read. Data paths are isolated per declared volume under
    a caller-owned absolute data root. Volume ownership initialization is deferred.
    """
    order = validate_stack(doc)
    result = {"services": {}}
    for name in order:
        spec = doc["services"][name]
        service = {
            "image": spec["image"], "platform": "linux/amd64", "restart": "unless-stopped",
            "healthcheck": {"test": ["CMD", *map(_literal, spec["healthcheck"])],
                            "interval": "5s", "timeout": "3s", "retries": 24, "start_period": "20s"},
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        }
        if "command" in spec:
            service["command"] = list(map(_literal, spec["command"]))
        env = {k: _literal(v) for k, v in spec.get("environment", {}).items()}
        refs = spec.get("secret_files", {})
        env.update({k: f"/run/secrets/{v}" for k, v in refs.items()})
        if env:
            service["environment"] = env
        if refs:
            service["secrets"] = sorted(set(refs.values()))
        if spec.get("depends_on"):
            service["depends_on"] = {n: {"condition": "service_healthy"} for n in sorted(spec["depends_on"])}
        if spec.get("volumes"):
            service["volumes"] = [
                {"type": "bind", "source": "${CORENOVA_STACK_DATA_DIR:?absolute data directory required}/" + v,
                 "target": target, "bind": {"create_host_path": False}}
                for v, target in sorted(spec["volumes"].items())]
        if name == doc["entrypoint"]["service"]:
            service["ports"] = [{"target": doc["entrypoint"]["port"],
                                 "published": "${CORENOVA_STACK_HOST_PORT:?host port required}",
                                 "host_ip": "127.0.0.1", "protocol": "tcp"}]
        result["services"][name] = service
    if doc.get("secrets"):
        result["secrets"] = {n: {"file": "${CORENOVA_STACK_SECRETS_DIR:?absolute secrets directory required}/" + n}
                             for n in sorted(doc["secrets"])}
    return result


def build_bundle(doc: dict) -> tuple[str, dict]:
    """Return deterministic public artifacts, NOT a publishable verification."""
    compose = yaml.safe_dump(compile_stack(doc), sort_keys=True, allow_unicode=True)
    spec_bytes = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    lock = {"schema_version": 2, "name": doc["name"], "status": "UNVERIFIED",
            "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
            "compose_sha256": hashlib.sha256(compose.encode()).hexdigest(),
            "services": {n: {"image": s["image"]} for n, s in sorted(doc["services"].items())}}
    return compose, lock
