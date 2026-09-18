import json
import os
import urllib.error
import urllib.request

import pytest

from corenova.stack_runtime import LocalStack, resolve_images
from tests.test_stack import stack  # noqa: F401


def test_credentials_reused_and_permissions_private(stack, tmp_path):  # noqa: F811
    runtime = LocalStack(stack, tmp_path / "run", 18764)
    path = runtime.root / "secrets/db-auth"
    original = path.read_bytes()
    runtime.prepare()
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o600
    assert original.decode() not in (runtime.root / "compose.yaml").read_text()
    assert original.decode() not in (runtime.root / "stack-lock.json").read_text()
    path.chmod(0o644)
    with pytest.raises(ValueError):
        runtime.prepare()


def test_existing_workspace_rejected(stack, tmp_path):  # noqa: F811
    with pytest.raises(ValueError):
        LocalStack(stack, tmp_path, 18764)


def test_symbolic_credential_rejected(stack, tmp_path):  # noqa: F811
    runtime = LocalStack(stack, tmp_path / "run", 18764)
    credential = runtime.root / "secrets/db-auth"
    credential.unlink()
    target = tmp_path / "external"
    target.write_text("must remain unchanged")
    credential.symlink_to(target)
    with pytest.raises(ValueError):
        runtime.prepare()
    assert target.read_text() == "must remain unchanged"


def test_resolver_locks_requested_repository(stack, monkeypatch):  # noqa: F811
    for service in stack["services"].values():
        service["image"] = "example/app:1.2.3"
    pinned = "example/app@sha256:" + "a" * 64
    def fake_docker(args, **kwargs):
        if args[0] == "pull":
            assert args[1:3] == ["--platform", "linux/amd64"]
            return ""
        return json.dumps([{"Os": "linux", "Architecture": "amd64", "Id": "sha256:config",
                            "RepoDigests": [pinned]}])
    monkeypatch.setattr("corenova.stack_runtime.docker", fake_docker)
    locked, evidence = resolve_images(stack)
    assert locked["services"]["web"]["image"] == pinned
    assert evidence["web"]["source"] == "example/app:1.2.3"
    assert stack["services"]["web"]["image"] == "example/app:1.2.3"


def test_resolver_rejects_wrong_architecture(stack, monkeypatch):  # noqa: F811
    monkeypatch.setattr("corenova.stack_runtime.docker", lambda args, **kw:
                        "" if args[0] == "pull" else json.dumps([{"Os": "linux", "Architecture": "arm64"}]))
    with pytest.raises(ValueError, match="linux/amd64"):
        resolve_images(stack)


@pytest.mark.skipif(os.environ.get("CORENOVA_STACK_INTEGRATION") != "1", reason="opt-in real Docker test")
def test_two_service_persistence_and_failure(tmp_path):
    # A real authenticated HTTP backend and proxy, deliberately not a production app.
    backend = '''import http.server, os, pathlib
key=pathlib.Path(os.environ['AUTH_FILE']).read_text()
data=pathlib.Path('/data/value')
if not data.exists(): data.write_text('persisted-stack-marker')
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  if self.headers.get('Authorization') != key: self.send_error(403); return
  self.send_response(200); self.end_headers(); self.wfile.write(data.read_bytes())
 def log_message(self,*args): pass
http.server.HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''
    frontend = '''import http.server, os, pathlib, urllib.request
key=pathlib.Path(os.environ['AUTH_FILE']).read_text()
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  try:
   req=urllib.request.Request('http://db:8080',headers={'Authorization':key})
   body=urllib.request.urlopen(req,timeout=2).read()
  except Exception: self.send_error(503); return
  self.send_response(200); self.end_headers(); self.wfile.write(body)
 def log_message(self,*args): pass
http.server.HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''
    probe = "import urllib.request; urllib.request.urlopen('http://localhost:8080',timeout=2)"
    backend_probe = "import os,pathlib,urllib.request; urllib.request.urlopen(urllib.request.Request('http://localhost:8080',headers={'Authorization':pathlib.Path(os.environ['AUTH_FILE']).read_text()}),timeout=2)"
    doc = {"schema_version": 2, "name": "integration-stack", "entrypoint": {"service": "web", "port": 8080},
           "volumes": ["db-data"], "secrets": ["shared-auth"], "services": {
               "db": {"image": "python:3.12.10-alpine", "command": ["python", "-c", backend],
                      "healthcheck": ["python", "-c", backend_probe], "volumes": {"db-data": "/data"},
                      "secret_files": {"AUTH_FILE": "shared-auth"}},
               "web": {"image": "python:3.12.10-alpine", "command": ["python", "-c", frontend],
                       "healthcheck": ["python", "-c", probe], "depends_on": ["db"],
                       "secret_files": {"AUTH_FILE": "shared-auth"}}}}
    locked, evidence = resolve_images(doc)
    assert all(e["platform"] == "linux/amd64" for e in evidence.values())
    runtime = LocalStack(locked, tmp_path / "integration", 18764)
    before = (runtime.root / "secrets/shared-auth").read_bytes()
    try:
        runtime.up()
        assert urllib.request.urlopen('http://127.0.0.1:18764', timeout=5).read() == b'persisted-stack-marker'
        (runtime.root / "data/db-data/value").write_text("changed-after-initialization")
        cid = runtime.compose("ps", "-q", "db").strip()
        from corenova.stack_runtime import docker
        ports = json.loads(docker(["inspect", cid]))[0]["HostConfig"]["PortBindings"]
        assert not ports
        runtime.compose("stop", "db")
        assert not runtime.healthy()
        with pytest.raises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen('http://127.0.0.1:18764', timeout=5)
        assert failure.value.code == 503
        runtime.down()
        runtime.prepare()
        runtime.up()
        assert urllib.request.urlopen('http://127.0.0.1:18764', timeout=5).read() == b'changed-after-initialization'
        assert (runtime.root / "secrets/shared-auth").read_bytes() == before
    finally:
        runtime.down()
