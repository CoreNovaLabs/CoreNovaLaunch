"""Shared helpers: subprocess, HTTP (proxy-aware), hashing, semver, git revisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


def log(msg: str) -> None:
    # 诊断日志走 stderr：stdout 是脚本的数据通道（--json / summary JSON），
    # 混入 [corenova] 前缀会让 json.loads 在 char 1 处炸掉（8/30 reverify-failed 事故）
    print(f"[corenova] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"[corenova] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def utcnow() -> str:
    return time.strftime(UTC_FMT, time.gmtime())


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
    return proc


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:40]


def git_revision(path: Path, repo_root: Path) -> str:
    """SHA of the last commit touching `path`; falls back to a content hash when the
    tree is not committed yet (local fixtures stage)."""
    rel = os.path.relpath(path, repo_root)
    try:
        proc = run(["git", "rev-list", "-1", "HEAD", "--", rel], cwd=repo_root, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except OSError:
        pass
    target = Path(path)
    if target.is_dir():
        digest = hashlib.sha256()
        for f in sorted(p for p in target.rglob("*") if p.is_file()):
            digest.update(str(rel).encode() + f.read_bytes())
        return "content-sha:" + digest.hexdigest()[:40]
    if target.exists():
        return "content-sha:" + file_sha(target)
    return "missing"


# --------------------------------------------------------------------------- HTTP


class HttpError(RuntimeError):
    def __init__(self, status: int | None, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status = status
        self.url = url
        self.body = body


def _opener() -> urllib.request.OpenerDirector:
    # Honour *_proxy / all_proxy from the environment (urllib only reads scheme-specific
    # vars, so mirror it explicitly for `all_proxy`).
    proxies: dict[str, str] = {}
    all_proxy = os.environ.get("all_proxy") or os.environ.get("ALL_PROXY")
    if all_proxy:
        proxies = {"http": all_proxy, "https": all_proxy}
    if not proxies:
        proxies = urllib.request.getproxies()
    handler = urllib.request.ProxyHandler(proxies)
    return urllib.request.build_opener(handler, urllib.request.HTTPRedirectHandler())


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: Any = None,
    timeout: int = 30,
    retries: int = 2,
) -> tuple[int, dict[str, str], bytes]:
    body = data
    if isinstance(data, (dict, list)):
        body = json.dumps(data).encode()
    elif isinstance(body, str):
        body = body.encode()
    hdrs = {"User-Agent": "corenovalaunch-verify/1.0"}
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with _opener().open(req, timeout=timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:  # 4xx/5xx
            payload = e.read() if e.fp else b""
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                last = e
                continue
            return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, payload
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise HttpError(getattr(last, "code", None), url, str(last))


def http_json(url: str, **kw: Any) -> Any:
    status, _, raw = http_request(url, **kw)
    if status >= 400:
        raise HttpError(status, url, raw.decode("utf-8", "replace"))
    return json.loads(raw or b"null")


# --------------------------------------------------------------------------- versions

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def parse_semver(version: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match(version.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def strip_v(version: str) -> str:
    return version[1:] if version[:1] in ("v", "V") else version


def sanitize_for_id(value: str) -> str:
    """对象键/verification_id 拼接用：先转小写，非 [a-z0-9._-] 一律替换为 _
    （契约 §2 解析约束）。上游 release tag 允许 `/`、大写等字符，进键前必须清洗，
    否则 dir 后端存在路径穿越面。"""
    return re.sub(r"[^a-z0-9._-]", "_", str(value).lower())


def is_transitional_error(err: BaseException) -> bool:
    """瞬时错误：网络抖动 / 超时 / 限流 —— 唯一允许自动重试的分类来源。"""
    text = f"{type(err).__name__}: {err}".lower()
    return any(
        k in text
        for k in (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection reset",
            "connection refused",
            "name or service not known",
            "toomanyrequests",
            "rate limit",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "eof occurred",
            "bad gateway",
            "unauthorized: authentication required",  # registry 限流常以 401 表现
        )
    ) or isinstance(err, (urllib.error.URLError, TimeoutError, ConnectionError))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- polling


def poll_until(
    probe: Callable[[], Any],
    *,
    timeout_s: float,
    interval_s: float,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """轮询直到 probe() 返回非 None（终值）或超时（返回 None）。

    约定：probe 返回 None = 未就绪继续等；其他任何返回值（含 False）= 终值原样透传；
    异常直接上抛——哪些异常算瞬时可吞、哪些必须立刻失败，由 probe 自己 try/except 决定。
    超时不抛异常：各调用方的超时语义不同（RuntimeError / TimeoutError / 业务返回值），
    由调用方检查 None 后自行包装。clock/sleep 可注入，测试用假时钟零等待复跑。
    """
    deadline = clock() + timeout_s
    while clock() < deadline:
        value = probe()
        if value is not None:
            return value
        sleep(interval_s)
    return None
