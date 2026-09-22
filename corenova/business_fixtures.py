"""Destructive business checks, restricted to the runner's exact compose project/mount.

Pipeline already passes Env.values to pytest. Required interface: CORENOVA_COMPOSE_FILE,
CORENOVA_COMPOSE_PROJECT and the existing CORENOVA_{DATA_DIR,APP_URL,APP_IMAGE,
HOST_PORT,CONTAINER_PORT}. Missing ownership metadata fails, never skips.
Cold backups are docker-cp tar archives of the ENTIRE stopped data mount, retaining
numeric owners (including n8n's encryption config). Archives stay beside content.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import requests

from corenova.util import run


def eventually(check, timeout=120, interval=1):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (requests.RequestException, AssertionError) as exc:
            last = str(exc)[:500]
        time.sleep(interval)
    raise AssertionError(f"Condition not satisfied within {timeout}s; last={last}")


class DockerApp:
    def __init__(self, values):
        self.values = dict(values)
        for key in ("COMPOSE_FILE", "COMPOSE_PROJECT", "DATA_DIR", "APP_URL", "APP_IMAGE",
                    "HOST_PORT", "CONTAINER_PORT"):
            assert self.values.get("CORENOVA_" + key), f"Runner must pass CORENOVA_{key} via Env.values"
        self.project = self.values["CORENOVA_COMPOSE_PROJECT"]
        self.data_dir = Path(self.values["CORENOVA_DATA_DIR"]).resolve()
        self.base_url = self.values["CORENOVA_APP_URL"].rstrip("/")

    def compose(self, *args):
        return run(["docker", "compose", "-p", self.project, "-f",
                    self.values["CORENOVA_COMPOSE_FILE"], *args], env=self.values, timeout=180).stdout

    def owned_container(self):
        ids = self.compose("ps", "-aq").split()
        assert len(ids) == 1, f"Expected exactly one owned app container, found {len(ids)}"
        info = json.loads(run(["docker", "inspect", ids[0]], timeout=30).stdout)[0]
        assert info["Config"]["Labels"].get("com.docker.compose.project") == self.project
        mounts = [m for m in info["Mounts"] if m["Type"] == "bind"
                  and Path(m["Source"]).resolve() == self.data_dir]
        assert len(mounts) == 1, "Refusing lifecycle operation: data mount ownership mismatch"
        return info, mounts[0]["Destination"]

    @property
    def cid(self):
        return self.owned_container()[0]["Id"]

    def ready(self):
        eventually(lambda: requests.get(self.base_url + "/", timeout=5).status_code == 200,
                   timeout=180)

    def restart(self):
        before, _ = self.owned_container()
        run(["docker", "restart", "--time", "30", before["Id"]], timeout=90)
        self.ready()
        after, _ = self.owned_container()
        assert after["Id"] == before["Id"], "Restart must not recreate the container"
        assert after["State"]["StartedAt"] != before["State"]["StartedAt"]
        assert after["State"]["Running"]

    @contextmanager
    def restored(self):
        info, destination = self.owned_container()
        cid = info["Id"]
        evidence = self.data_dir.parent / ("cold-backup-" + uuid4().hex[:10])
        evidence.mkdir(mode=0o700)
        archive = evidence / "data.tar"
        run(["docker", "stop", "--time", "30", cid], timeout=90)
        try:
            stopped, _ = self.owned_container()
            assert not stopped["State"]["Running"], "Never copy a live SQLite data directory"
            with archive.open("wb") as out:
                archive.chmod(0o600)
                subprocess.run(["docker", "cp", cid + ":" + destination + "/.", "-"],
                               stdout=out, stderr=subprocess.PIPE, check=True, timeout=120)
        finally:
            run(["docker", "start", cid], timeout=90)
            self.ready()
        assert archive.stat().st_size > 0
        clone_dir = evidence / "restored-content"
        clone_dir.mkdir(mode=0o777)
        clone_dir.chmod(0o777)
        with socket.socket() as sock:
            sock.bind(("", 0))
            port = sock.getsockname()[1]
        host = urlsplit(self.base_url).hostname
        values = {**self.values, "CORENOVA_COMPOSE_PROJECT": self.project + "-r-" + uuid4().hex[:6],
                  "CORENOVA_DATA_DIR": str(clone_dir), "CORENOVA_HOST_PORT": str(port),
                  "CORENOVA_APP_URL": f"http://{host}:{port}", "CORENOVA_APP_IMAGE": info["Image"]}
        replica = DockerApp(values)
        try:
            replica.compose("create", "--pull", "never")
            replica_info, clone_destination = replica.owned_container()
            assert replica_info["Id"] != cid and replica_info["Image"] == info["Image"]
            with archive.open("rb") as source:
                subprocess.run(["docker", "cp", "-a", "-", replica_info["Id"] + ":" + clone_destination],
                               stdin=source, capture_output=True, check=True, timeout=120)
            replica.compose("start")
            replica.ready()
            replica.archive = archive
            yield replica
        finally:
            replica.compose("down", "-v")


@pytest.fixture(scope="session")
def docker_app():
    app = DockerApp({k: v for k, v in os.environ.items() if k.startswith("CORENOVA_")})
    app.owned_container()
    return app


# This real HTTP target/webhook receiver binds ONLY container loopback. No host
# gateway, external URLs, production systems or extra downloaded images are used.
_SINK = r"""
const http = require('http'); let up = true; const events = [];
http.createServer((req,res) => {
  if(req.url === '/target') {res.writeHead(up ? 200 : 503); return res.end(up?'healthy':'outage');}
  if(req.url === '/down') up = false;
  if(req.url === '/up') up = true;
  if(req.url === '/hook' && req.method === 'POST') {
    let body=''; req.on('data',c=>body+=c); req.on('end',()=>{
      events.push(JSON.parse(body)); res.end('delivered');
    }); return;
  }
  if(req.url === '/events') {res.setHeader('Content-Type','application/json');return res.end(JSON.stringify(events));}
  res.end('ok');
  if(req.url === '/shutdown') setImmediate(()=>process.exit(0));
}).listen(19090, '127.0.0.1');
"""


class LocalWebhook:
    url = "http://127.0.0.1:19090"

    def __init__(self, app, keep_running=False):
        self.cid = app.cid
        self.keep_running = keep_running

    def request(self, path):
        result = run(["docker", "exec", self.cid, "node", "-e",
                      "fetch(process.argv[1]).then(r=>r.text()).then(console.log).catch(()=>process.exit(1))",
                      self.url + path], timeout=15, check=False)
        assert result.returncode == 0, "Local target/webhook receiver not ready"
        return result.stdout.strip()

    def events(self):
        return json.loads(self.request("/events"))

    def __enter__(self):
        if self.keep_running:
            # Remains local and healthy for the subsequent screenshot stage; the
            # runner's compose down removes it with this disposable container.
            run(["docker", "exec", "-d", self.cid, "node", "-e", _SINK], timeout=15)
        else:
            self.process = subprocess.Popen(["docker", "exec", self.cid, "node", "-e", _SINK],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        eventually(lambda: self.request("/up") == "ok", timeout=30)
        return self

    def __exit__(self, *args):
        if self.keep_running:
            return
        # The app restart may already have killed this exec; do not kill any other process.
        if self.process.poll() is None:
            self.request("/shutdown")
        self.process.wait(timeout=15)


def _local_main():
    """Local-only reproduction through the existing runner; never publish or call AWS."""
    import argparse
    import sys
    from types import SimpleNamespace

    from corenova import appspec, runtime, screenshots
    from corenova.pipeline import _run_pytest

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", choices=["ghost", "uptime-kuma", "n8n"])
    parser.add_argument("--image", required=True, help="Locally cached exact tag@digest")
    parser.add_argument("--check-seeded-order", action="store_true",
                        help="Run all tests again in reverse order against the seeded instance")
    args = parser.parse_args()
    assert "@sha256:" in args.image, "Use an immutable cached image reference"
    run(["docker", "image", "inspect", args.image], timeout=30)
    root = Path(__file__).resolve().parents[1]
    cfg = SimpleNamespace(root=root, run_opts={"tests_timeout_seconds": 600})
    spec = appspec.load(args.app, root)
    workdir = root / "data" / ("p03-" + args.app + "-" + uuid4().hex[:10])
    workdir.mkdir()
    with socket.socket() as sock:
        sock.bind(("", 0))
        port = sock.getsockname()[1]
    env = runtime.build_env(cfg, spec, args.image, args.image, workdir, port)
    # Preserve the exact non-secret runtime interface for reproduction/inspection.
    (workdir / "env.json").write_text(json.dumps(env.values, indent=2))
    code = 1
    try:
        runtime.compose(env, spec, root, "up", "-d", "--pull", "never")
        DockerApp(env.values).ready()
        code, output = _run_pytest(spec, root, env, cfg)
        (workdir / "pytest.log").write_text(output)
        print(output[-4000:], flush=True)
        if code == 0 and args.check_seeded_order:
            collected = run([sys.executable, "-m", "pytest", str(root / spec.g("tests.predefined_dir")),
                             "--collect-only", "-q"], cwd=root, env=env.values)
            nodeids = [line for line in collected.stdout.splitlines() if "::test_" in line]
            assert nodeids, "No business tests collected"
            second = run([sys.executable, "-m", "pytest", *reversed(nodeids), "-q", "--no-header",
                          "-rA", "--timeout=600"], cwd=root, env=env.values, timeout=660, check=False)
            code = second.returncode
            output = second.stdout + second.stderr
            (workdir / "pytest-seeded-reverse.log").write_text(output)
            print(output[-4000:], flush=True)
        if code == 0:
            screenshots.capture(spec, root, env.base_url, workdir / "screenshots")
    finally:
        (workdir / "container.log").write_text(runtime.logs(env, spec, root, tail=80))
        runtime.down(env, spec, root)
        print(f"P03 evidence: {workdir}", flush=True)
    raise SystemExit(code)


if __name__ == "__main__":
    _local_main()
