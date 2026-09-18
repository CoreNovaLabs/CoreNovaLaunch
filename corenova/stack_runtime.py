"""Experimental local-only stack lifecycle. Never publishes or contacts AWS."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import stat
import subprocess
import uuid
from pathlib import Path

from .stack import IMAGE, build_bundle, validate_stack


def docker(args, *, env=None, timeout=180):
    # Container output can contain credentials. Never attach it to public errors.
    proc = subprocess.run(["docker", *args], env={**os.environ, **(env or {})},
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f"Docker operation {args[0]} failed (exit {proc.returncode}); output withheld")
    return proc.stdout


def resolve_images(doc):
    """Resolve explicit numeric tags or digests locally; inspect the pulled platform."""
    locked = copy.deepcopy(doc)
    evidence = {}
    # Validate structure before any pull, substituting only the image inputs.
    probe = copy.deepcopy(doc)
    for service in probe.get("services", {}).values():
        service["image"] = "validation/image@sha256:" + "0" * 64
    validate_stack(probe)
    for name, service in locked["services"].items():
        ref = service["image"]
        if not isinstance(ref, str) or not (IMAGE.fullmatch(ref) or re.fullmatch(
                r"[a-z0-9][a-z0-9./_-]*:v?\d+\.\d+\.\d+[A-Za-z0-9._-]*", ref)):
            raise ValueError("image must have a digest or an explicit numeric version tag")
        docker(["pull", "--platform", "linux/amd64", ref], timeout=600)
        info = json.loads(docker(["image", "inspect", ref]))[0]
        if (info["Os"], info["Architecture"]) != ("linux", "amd64"):
            raise ValueError("pulled image is not linux/amd64")
        refs = info.get("RepoDigests", [])
        repository = ref.split("@")[0] if "@" in ref else ref.rsplit(":", 1)[0]
        def canonical(value):
            value = value.removeprefix("docker.io/")
            return value if "/" in value else "library/" + value
        candidates = [r for r in refs if canonical(r.split("@")[0]) == canonical(repository)]
        if not candidates:
            raise ValueError("registry digest missing for requested repository")
        pinned = candidates[0]
        if "@" in ref and ref.split("@")[1] != pinned.split("@")[1]:
            raise ValueError("requested digest does not match pulled image")
        service["image"] = pinned
        evidence[name] = {"source": ref, "image": pinned, "image_id": info["Id"], "platform": "linux/amd64"}
    validate_stack(locked)
    return locked, evidence


class LocalStack:
    """Caller-owned workspace. Only this object's unique Compose project is stopped.

    Credentials are root-readable files: non-root secret consumers and ownership
    mapping are intentionally not supported yet. Data survives container recreation.
    """

    def __init__(self, doc, workspace: Path, port: int):
        validate_stack(doc)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("local host port must be 1024..65535")
        self.doc = copy.deepcopy(doc)
        self.root = workspace.absolute()
        if self.root.exists():
            raise ValueError("workspace must be a new directory")
        self.root.mkdir(mode=0o700)
        self.project = "cn-v2-" + uuid.uuid4().hex[:16]
        self.env = {"CORENOVA_STACK_DATA_DIR": str(self.root / "data"),
                    "CORENOVA_STACK_SECRETS_DIR": str(self.root / "secrets"),
                    "CORENOVA_STACK_HOST_PORT": str(port)}
        self.prepare()
        compose, lock = build_bundle(doc)
        (self.root / "compose.yaml").write_text(compose)
        (self.root / "stack-lock.json").write_text(json.dumps(lock, indent=2))

    def prepare(self):
        """Idempotent on this private workspace; never rotates existing credentials."""
        for subdir in ("data", "secrets"):
            path = self.root / subdir
            if path.is_symlink():
                raise ValueError("symlink directory rejected")
            path.mkdir(mode=0o700, exist_ok=True)
        for name in self.doc.get("volumes", []):
            path = self.root / "data" / name
            if path.is_symlink():
                raise ValueError("symlink volume rejected")
            path.mkdir(mode=0o750, exist_ok=True)
        for name in self.doc.get("secrets", []):
            path = self.root / "secrets" / name
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size < 32:
                    raise ValueError("existing credential has unsafe type, mode or size") from None
            else:
                with os.fdopen(fd, "w") as stream:
                    stream.write(secrets.token_hex(32))

    def compose(self, *args, timeout=180):
        return docker(["compose", "--project-name", self.project, "--file", str(self.root / "compose.yaml"),
                       *args], env=self.env, timeout=timeout)

    def up(self):
        self.compose("up", "-d", "--wait", "--wait-timeout", "120", timeout=180)
        if not self.healthy():
            raise RuntimeError("not all stack services are healthy")

    def healthy(self):
        for name in self.doc["services"]:
            cid = self.compose("ps", "--all", "-q", name).strip()
            if not cid:
                return False
            state = json.loads(docker(["inspect", cid]))[0]["State"]
            if state.get("Status") != "running" or state.get("Health", {}).get("Status") != "healthy":
                return False
        return True

    def down(self):
        # No --volumes, prune, image deletion or broad Docker cleanup.
        self.compose("down", "--timeout", "15")
