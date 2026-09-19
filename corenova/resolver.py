"""RESOLVED stage: upstream version + release.type + exact image tag + immutable digest.

Registry access is implemented over the OCI distribution HTTP API rather than the Docker
CLI, because digest resolution must work *before* a daemon exists and must honour a
registry mirror (app-schema §0 / app-schema §3.1).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .appspec import AppSpec, render_image_ref
from .gh import github_headers, github_token
from .profiles import RELEASE_TYPES
from .util import HttpError, http_json, http_request, parse_semver

MANIFEST_ACCEPT = ",".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

SECURITY_RE = re.compile(r"CVE-\d{4}-\d{4,}|\bsecurity advis?ory\b|\bsecurity patch|\bvulnerab", re.I)

SEMVER_SEARCH = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def extract_semver(tag: str) -> tuple[int, int, int] | None:
    """宽容版 semver 提取：允许 tag 带任意前缀/后缀（如 "n8n@2.38.6"、"release-v1.2.3"），
    取 tag 中首个 X.Y.Z 片段；找不到返回 None。严格版见 util.parse_semver。"""
    m = SEMVER_SEARCH.search(tag.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# --------------------------------------------------------------------------- GitHub


class GitHub:
    def __init__(self, token: str | None = None):
        self.headers = github_headers(token or github_token())

    def latest_release(self, repo: str) -> dict[str, Any]:
        return http_json(f"https://api.github.com/repos/{repo}/releases/latest", headers=self.headers)

    def releases(self, repo: str, per_page: int = 30) -> list[dict[str, Any]]:
        url = f"https://api.github.com/repos/{repo}/releases?per_page={per_page}"
        data = http_json(url, headers=self.headers)
        return data if isinstance(data, list) else []

    def tag_sha(self, repo: str, ref: str) -> str:
        url = f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}"
        return http_json(url, headers=self.headers)["sha"]


@dataclass
class ResolvedVersion:
    app_version: str
    release_tag: str
    source_revision: str
    release_type: str
    type_evidence: str
    release_body: str
    published_at: str
    previous_version: str | None = None


def pick_release(spec: AppSpec, gh: GitHub | None = None, wanted: str | None = None) -> ResolvedVersion:
    """Resolve app_version per `source.version_strategy` (app-schema §4)."""
    gh = gh or GitHub()
    strategy = spec.version_strategy

    if strategy == "pinned":
        ref = wanted or render_image_ref(spec, spec.g("deploy.docker_image")).split(":")[-1]
        return ResolvedVersion(ref, ref, _safe_sha(gh, spec.source_repo, ref), "new_version",
                               "strategy=pinned: version taken from the pinned image tag", "", "")
    if strategy == "git_branch":
        branch = wanted or spec.g("source.branch", "main")
        return ResolvedVersion(branch, branch, _safe_sha(gh, spec.source_repo, branch), "new_version",
                               f"strategy=git_branch: {branch} HEAD", "", "")

    # semver_latest（app-schema §4）："从 tag 中取满足 semver 的最新稳定版"。
    # 上游可能发布非版本号的最新 release（如 n8n 的 "stable"），因此不能只看
    # /releases/latest —— 必须扫描 release 列表，在各 tag 中提取 semver 后取最高。
    if strategy == "semver_latest" and not wanted:
        scored: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
        for rel in _candidates(spec, gh):
            tag = str(rel.get("tag_name") or "")
            semver = extract_semver(tag)
            if semver:
                scored.append((semver, rel))
        if not scored:
            raise ValueError(f"上游 {spec.source_repo} 最近 30 个 release 中没有任何可解析 semver 的 tag")
        best = max(scored, key=lambda pair: pair[0])[1]
        return _to_resolved(spec, gh, best)

    if wanted:
        releases = [r for r in gh.releases(spec.source_repo, per_page=50) if r.get("tag_name") == wanted]
        if not releases:
            # semver_latest 的 app_version 是规范化形式（v2.39.8），而上游 tag 带自有
            # 前缀（如 n8n 的 "n8n@2.39.8"）：精确匹配失败时退回 semver 匹配，否则
            # 重验/重试永远找不到原 release（issue #16，n8n 连锁 34 次失败的根因）。
            semver = extract_semver(wanted)
            if semver:
                releases = [r for r in gh.releases(spec.source_repo, per_page=50)
                            if extract_semver(str(r.get("tag_name") or "")) == semver]
            if not releases:
                raise ValueError(f"上游 {spec.source_repo} 不存在 release tag {wanted!r}")
        rel = releases[0]
    else:
        try:
            rel = gh.latest_release(spec.source_repo)
        except HttpError as exc:
            if exc.status not in (403, 404):
                raise
            rel = next((r for r in _candidates(spec, gh) if r.get("tag_name")), None)
            if rel is None:
                raise
    if not _accepts(spec, rel):
        raise ValueError(f"release {rel.get('tag_name')} 被 source.release_filter 排除")
    return _to_resolved(spec, gh, rel)


def _candidates(spec: AppSpec, gh: GitHub) -> list[dict[str, Any]]:
    return [r for r in gh.releases(spec.source_repo) if _accepts(spec, r)]


def _accepts(spec: AppSpec, rel: dict[str, Any]) -> bool:
    filt = spec.g("source.release_filter") or {}
    if rel.get("draft") and not filt.get("draft", False):
        return False
    if rel.get("prerelease") and not filt.get("prerelease", False):
        return False
    return bool(rel.get("tag_name"))


def _safe_sha(gh: GitHub, repo: str, ref: str) -> str:
    try:
        return gh.tag_sha(repo, ref)
    except Exception:  # noqa: BLE001 - revision is evidence, not a gate
        return f"unresolved:{ref}"


def _to_resolved(spec: AppSpec, gh: GitHub, rel: dict[str, Any]) -> ResolvedVersion:
    tag = str(rel["tag_name"])
    body = f"{rel.get('name') or ''}\n{rel.get('body') or ''}"
    # 宽容提取：release_tag 策略下 tag 可能带前缀（如 n8n 的 "n8n@2.38.6"），
    # 提取失败不阻塞 release_tag（app_version 原样使用 tag），只影响 semver 可比性。
    semver = extract_semver(tag)
    app_version = tag if spec.version_strategy == "release_tag" else (
        f"v{semver[0]}.{semver[1]}.{semver[2]}" if semver else tag
    )
    previous = _previous_published_version(spec)
    rtype, evidence = classify_release(spec, rel, body, semver, previous)
    return ResolvedVersion(
        app_version=app_version,
        release_tag=tag,
        source_revision=_safe_sha(gh, spec.source_repo, tag),
        release_type=rtype,
        type_evidence=evidence,
        release_body=body.strip(),
        published_at=rel.get("published_at") or "",
        previous_version=previous,
    )


def classify_release(
    spec: AppSpec,
    rel: dict[str, Any],
    body: str,
    semver: tuple[int, int, int] | None,
    previous: str | None = None,
) -> tuple[str, str]:
    """deployment-contract.md §4.1 — first match wins, evidence recorded."""
    override = spec.g("release_type_override")
    if override:
        if override not in RELEASE_TYPES:
            raise ValueError(f"release_type_override={override!r} 不在允许的类型 {RELEASE_TYPES} 中")
        return override, f"manual: release_type_override={override} ({_reason_comment(spec)})"

    hit = SECURITY_RE.search(body)
    if hit:
        return "security_update", f"rule2: keyword {hit.group(0)!r} in upstream release notes"

    if previous is None:
        return "initial", "rule1: no published versions recorded for this app"
    prev_semver = parse_semver(previous)
    if semver and prev_semver:
        if semver[:2] == prev_semver[:2] and semver[2] != prev_semver[2]:
            return "bug_fix", f"rule3: patch-only bump {previous} -> {semver}"
        return "new_version", f"rule4: minor/major change {previous} -> {semver}"
    return "new_version", f"rule4: not semver-comparable to previous {previous!r}"


def _reason_comment(spec: AppSpec) -> str:
    m = re.search(r"#\s*reason:\s*(.+)", spec.raw)
    return m.group(1).strip() if m else "unspecified"


def _previous_published_version(spec: AppSpec) -> str | None:
    """Already-published version of this app, read from the active backend (once per resolve)."""
    from .publish import current_version  # local import: avoids cycle

    return current_version(spec.name)


# --------------------------------------------------------------------------- image / digest


@dataclass
class ResolvedImage:
    image_ref: str            # 上游精确 tag 引用（写进 Manifest / 给用户部署用）
    pull_ref: str             # 实际拉取引用（可能带镜像站前缀）+ @digest
    digest: str               # linux/amd64 单平台 image digest
    manifest_digest: str      # 多平台索引 digest
    repo: str
    tag: str
    host: str                 # 拉取时的 registry host（证据用）
    upstream_host: str        # 上游 registry host


def split_image(ref: str, mirror: str = "") -> tuple[str, str, str, str]:
    """-> (upstream_host, repo, tag, pull_host)。mirror 只改写拉取路径，不改身份。"""
    registry = "docker.io"
    name = ref
    if "/" in ref:
        first, _, rest = ref.partition("/")
        if "." in first or ":" in first:
            registry, name = first, rest
    if ":" in name.split("/")[-1]:
        repo, _, tag = name.rpartition(":")
    else:
        repo, tag = name, "latest"
    if registry == "docker.io" and "/" not in repo:
        repo = f"library/{repo}"
    upstream = "registry-1.docker.io" if registry == "docker.io" else registry
    # 公共镜像站只代理 Docker Hub
    pull_host = mirror or upstream
    if mirror and registry != "docker.io":
        pull_host = upstream
    return upstream, repo, tag, pull_host


def resolve_digest(image_ref: str, mirror: str = "", os_name: str = "linux", arch: str = "amd64") -> ResolvedImage:
    host, repo, tag, pull_host = split_image(image_ref, mirror)
    token = _registry_token(pull_host, repo)
    base = f"https://{pull_host}/v2/{repo}/manifests/{tag}"
    headers = {"Accept": MANIFEST_ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, hdrs, raw = http_request(base, headers=headers)
    if status >= 400:
        raise HttpError(status, base, raw.decode("utf-8", "replace"))
    import json as _json

    doc = _json.loads(raw)
    manifest_digest = hdrs.get("docker-content-digest") or _digest_of(raw)
    if isinstance(doc, dict) and doc.get("manifests"):
        entry = next(
            (
                m for m in doc["manifests"]
                if (m.get("platform") or {}).get("os") == os_name
                and (m.get("platform") or {}).get("architecture") == arch
            ),
            None,
        )
        if entry is None:
            raise ValueError(f"{image_ref}: 平台 {os_name}/{arch} 不存在（v1 仅支持 x86_64）")
        digest = entry["digest"]
    else:
        digest = manifest_digest  # single-platform image
    upstream_display = (
        f"{canonical_name(repo)}:{tag}" if host == "registry-1.docker.io" else f"{host}/{repo}:{tag}"
    )
    pull_name = (
        f"{canonical_name(repo)}" if pull_host == "registry-1.docker.io" else f"{pull_host}/{repo}"
    )
    return ResolvedImage(
        image_ref=upstream_display,
        pull_ref=f"{pull_name}:{tag}@{digest}",
        digest=digest,
        manifest_digest=manifest_digest,
        repo=repo,
        tag=tag,
        host=pull_host,
        upstream_host=host,
    )


def canonical_name(repo: str) -> str:
    """Docker Hub 官方镜像：library/ghost 对外就是 ghost。"""
    return repo[len("library/"):] if repo.startswith("library/") else repo


def _digest_of(raw: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _registry_token(host: str, repo: str) -> str | None:
    """Bearer-token dance per the OCI auth spec; works for Docker Hub, mirrors and GHCR."""
    if host == "registry-1.docker.io":
        url = "https://auth.docker.io/token?" + urllib.parse.urlencode(
            {"service": "registry.docker.io", "scope": f"repository:{repo}:pull"}
        )
        return http_json(url).get("token")
    status, hdrs, _ = http_request(f"https://{host}/v2/")
    if status != 401:
        return None
    www = hdrs.get("www-authenticate", "")
    m = re.search(r'realm="([^"]+)"', www)
    if not m:
        return None
    params = dict(re.findall(r'(\w+)="([^"]*)"', www))
    query = {"scope": f"repository:{repo}:pull"}
    if params.get("service"):
        query["service"] = params["service"]
    resp = http_json(m.group(1) + "?" + urllib.parse.urlencode(query))
    return resp.get("token") or resp.get("access_token")
