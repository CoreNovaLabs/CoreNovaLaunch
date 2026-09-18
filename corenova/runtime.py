"""VERIFYING runtime: compose lifecycle, readiness probe, version assertion.

The container that starts here is addressed as `<exact tag>@<digest>` (app-schema §0), so
what we verify is byte-for-byte what the Manifest records.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .appspec import AppSpec
from .util import log, run, strip_v


@dataclass
class Env:
    values: dict[str, str]
    base_url: str
    project: str
    data_dir: Path


def build_env(cfg, spec: AppSpec, image_pull_ref: str, image_display_ref: str, workdir: Path, host_port: int | None = None) -> Env:
    port = spec.container_port
    # 端口优先级：CLI/workflow 覆盖 > config run.host_port > 应用自身 container_port。
    # 不设全局默认端口（那会把某个应用的端口泄漏成所有应用的默认值）。
    hp = int(host_port or cfg.run_opts.get("host_port") or port)
    # 验证器与 Docker daemon 不一定同机（自托管 runner / 容器内跑验证 / 远程 daemon），
    # 因此"从哪里访问这个端口"必须可配置，不能写死 localhost。
    probe_host = (os.environ.get("CORENOVA_PROBE_HOST") or cfg.run_opts.get("probe_host") or "localhost")
    data_dir = workdir / "content"
    data_dir.mkdir(parents=True, exist_ok=True)
    # Ghost (and friends) run as a non-root uid inside the image; a bind mount must be
    # writable by it, and the uid is image-defined rather than ours.
    data_dir.chmod(0o777)
    values = {
        "CORENOVA_APP_IMAGE": image_pull_ref,
        "CORENOVA_APP_IMAGE_REF": image_display_ref,
        "CORENOVA_CONTAINER_PORT": str(port),
        "CORENOVA_HOST_PORT": str(hp),
        "CORENOVA_APP_URL": f"http://{probe_host}:{hp}",
        "CORENOVA_DATA_DIR": str(data_dir),
    }
    return Env(values=values, base_url=values["CORENOVA_APP_URL"], project=f"cn-{spec.name}", data_dir=data_dir)


def compose(env: Env, spec: AppSpec, root: Path, *args: str, timeout: int = 900) -> str:
    compose_file = root / spec.g("deploy.compose_file")
    proc = run(
        ["docker", "compose", "-p", env.project, "-f", str(compose_file), *args],
        cwd=compose_file.parent,
        env=env.values,
        timeout=timeout,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def up(env: Env, spec: AppSpec, root: Path) -> str:
    out = compose(env, spec, root, "up", "-d", "--quiet-pull", timeout=1800)
    log("compose up 完成")
    return out


def down(env: Env, spec: AppSpec, root: Path) -> None:
    try:
        compose(env, spec, root, "down", "-v", "--remove-orphans", timeout=180)
    except Exception as exc:  # noqa: BLE001 - teardown must never mask the result
        log(f"compose down 异常（忽略）：{exc}")


def container_id(env: Env, spec: AppSpec, root: Path, service: str | None = None) -> str:
    compose_file = root / spec.g("deploy.compose_file")
    service = service or _first_service(compose_file)
    proc = run(
        ["docker", "compose", "-p", env.project, "-f", str(compose_file), "ps", "-q", service],
        cwd=compose_file.parent,
        env=env.values,
        timeout=60,
    )
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""


def _first_service(compose_file: Path) -> str:
    import yaml

    doc = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    services = doc.get("services") or {}
    return next(iter(services), "")


def container_state(cid: str) -> str:
    if not cid:
        return "absent"
    proc = run(["docker", "inspect", "-f", "{{.State.Status}}", cid], check=False, timeout=60)
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def logs(env: Env, spec: AppSpec, root: Path, tail: int = 200) -> str:
    try:
        return compose(env, spec, root, "logs", "--tail", str(tail), timeout=120)
    except Exception as exc:  # noqa: BLE001
        return f"(logs unavailable: {exc})"


# --------------------------------------------------------------------------- readiness


@dataclass
class Probe:
    ok: bool
    status: int | None
    detail: str
    attempts: int
    elapsed_s: float
    headers: dict[str, str]
    body: str


def wait_ready(base_url: str, spec: AppSpec) -> Probe:
    hc = spec.g("health_check") or {}
    path = hc.get("endpoint", "/")
    expect = int(hc.get("expected_status", 200))
    method = (hc.get("method") or "GET").upper()
    timeout_s = float(hc.get("timeout_seconds", 5))
    retries = int(hc.get("retries", 10))
    interval = float(hc.get("interval_seconds", 3))
    startup = float(spec.startup_timeout)
    body = hc.get("body")
    ctype = hc.get("content_type") or "application/json"
    contains = hc.get("expected_body_contains")

    url = base_url.rstrip("/") + path
    started = time.time()
    last_status, last_detail, headers, text = None, "not attempted", {}, ""
    attempt = 0
    while time.time() - started < startup and attempt < max(retries, 1):
        attempt += 1
        try:
            data = body.encode() if body else None
            req = urllib.request.Request(url, data=data, method=method)
            if data:
                req.add_header("Content-Type", ctype)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - fixed localhost base
                last_status = resp.status
                headers = {k.lower(): v for k, v in resp.headers.items()}
                text = resp.read(4000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last_status, last_detail = e.code, f"HTTP {e.code} {e.reason}"
            headers = {k.lower(): v for k, v in (e.headers or {}).items()}
            text = (e.read(2000) if e.fp else b"").decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last_status, last_detail = None, f"{type(exc).__name__}: {exc}"
        ok_status = last_status is not None and (
            last_status == expect or (200 <= expect < 300 and 200 <= last_status < 300)
        )
        if ok_status and (not contains or contains in text):
            return Probe(True, last_status, "ready", attempt, time.time() - started, headers, text)
        if last_status == 404:
            # 路径不存在是确定的探针错误，耗满启动窗口没有意义
            last_detail = "HTTP 404：endpoint 不存在（检查 health_check.endpoint）"
            break
        if last_status is not None and 400 <= last_status < 500:
            last_detail = f"HTTP {last_status}：应用已应答但拒绝该请求（多为鉴权/路径问题）"
        time.sleep(interval)
    detail = last_detail if last_status is None else f"HTTP {last_status} ({last_detail})"
    if contains and last_status == expect and contains not in text:
        detail = f"响应体未包含 {contains!r}"
    return Probe(False, last_status, detail, attempt, time.time() - started, headers, text)


# --------------------------------------------------------------------------- version assertion


@dataclass
class Assertion:
    configured: bool
    ok: bool
    actual: str
    expected: str
    detail: str


def assert_version(
    cid: str,
    spec: AppSpec,
    app_version: str,
    *,
    base_url: str = "",
    probe_headers: dict[str, str] | None = None,
) -> Assertion:
    va = spec.g("health_check.version_assertion")
    if not va:
        return Assertion(False, True, "not_configured", "", "上游无版本可观测性：仅由精确 tag 证明版本")
    kind = va["kind"]
    expected = str(va.get("expected", "")).format(version=app_version, version_no_v=strip_v(app_version))
    actual = ""
    try:
        if kind == "exec_command":
            command = va["command"]
            argv = ["sh", "-c", command] if isinstance(command, str) else command
            if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x.strip() for x in argv):
                return Assertion(True, False, "", expected, "command 必须是命令字符串或非空字符串参数列表")
            proc = run(["docker", "exec", cid, *argv], check=False, timeout=90)
            actual = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            if proc.returncode != 0:
                return Assertion(True, False, "", expected, f"命令失败({proc.returncode}): {proc.stderr.strip()[:200]}")
        elif kind == "env":
            proc = run(["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", cid], check=False, timeout=60)
            pairs = dict(
                line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
            )
            actual = pairs.get(va["name"], "")
        elif kind == "label":
            proc = run(
                ["docker", "inspect", "-f", "{{index .Config.Labels " + repr(va["name"]) + "}}", cid],
                check=False, timeout=60,
            )
            actual = proc.stdout.strip()
        elif kind == "header":
            # app-schema §3.2：取"健康探测响应"的 HTTP 头 —— 复用就绪探测已拿到的
            # 响应头（wait_ready 的 Probe.headers，键已小写），不再另发请求。
            if probe_headers is None:
                return Assertion(True, False, "", expected,
                                 "kind=header 需要就绪探测的响应头，但调用方未提供（探测未执行或无响应）")
            actual = probe_headers.get(str(va["name"]).lower(), "")
        elif kind == "api_json_path":
            # app-schema §3.2：GET {baseURL}{path} —— 字段是 path（与校验器规则12同源），
            # 不存在独立的 url 字段。
            if not base_url:
                return Assertion(True, False, "", expected,
                                 "kind=api_json_path 需要 base_url，但调用方未提供")
            url = base_url.rstrip("/") + str(va["path"])
            with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - probe-host base
                import json as _json

                doc = _json.loads(resp.read())
            cur: Any = doc
            for part in str(va["json_pointer"]).strip("/").split("/"):
                cur = cur[int(part)] if part.isdigit() else cur[part]
            actual = str(cur)
        else:
            return Assertion(True, False, "", expected, f"未知 kind {kind!r}")
    except Exception as exc:  # noqa: BLE001
        return Assertion(True, False, actual, expected, f"{type(exc).__name__}: {exc}")

    match = va.get("match", "exact")
    ok = bool(actual) and (actual == expected if match == "exact" else actual.startswith(expected))
    return Assertion(
        True, ok, actual, expected,
        f"{'匹配' if ok else '不匹配'}（{match}）：kind={kind} 取到 {actual!r}，期望 {expected!r}",
    )
