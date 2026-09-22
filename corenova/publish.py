"""Production-contract apps stage isolated candidates; only production promotes.

Apps without production_contract retain the legacy two-phase publishing path:

    P1 placeholder versions/{app_version}.json   (three upload checks = false)
    P2 upload screenshots + report
    P3 probe every object is readable           -> truthful values for the upload checks
    P4 rewrite versions/{app_version}.json      (final state, nine checks truthful)
    P5 commit point: current.json + verified/index.json

The legacy path keeps its historical same-version overwrite semantics. The
production candidate path never writes stable versions, indexes or screenshots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .manifest import CHECKS, report_url, screenshot_key
from .util import log, sanitize_for_id, utcnow
from .versioning import semver_relation

UPLOAD_CHECKS = ("screenshots_uploaded", "report_uploaded", "verification_manifest_uploaded")
LOCAL_CHECKS = tuple(c for c in CHECKS if c not in UPLOAD_CHECKS)

# 索引条件写（If-Match）失配后的重试次数：跨应用并发 P5 时重读-重合-重写。
_INDEX_WRITE_ATTEMPTS = 5


# --------------------------------------------------------------------------- current state


def _get_current(backend, app: str) -> dict[str, Any] | None:
    raw = backend.get(f"verified/{app}/current.json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def current_version(app: str, cfg=None) -> str | None:
    """RESOLVED 阶段查"当前已发布版本"（release.type 判定与覆盖保护共用同一事实源）。"""
    from .backend import make_backend
    from .config import Config

    cfg = cfg or Config.load()
    cur = _get_current(make_backend(cfg), app)
    return str(cur["app_version"]) if cur and cur.get("app_version") else None


def may_update_current(
    backend,
    app: str,
    candidate_version: str,
    candidate_run_id: str,
    strategy: str,
    force: bool = False,
) -> tuple[bool, str]:
    """版本覆盖保护（workflow-state-machine.md §5）。"""
    cur = _get_current(backend, app)
    if not cur:
        return True, "尚无 current.json"
    if force:
        return True, f"force=true（{cur.get('app_version')} -> {candidate_version}）"
    cur_v, cur_run = str(cur.get("app_version") or ""), str(cur.get("verification_run_id") or "0")
    rel = semver_relation(candidate_version, cur_v)
    if rel and strategy in ("release_tag", "semver_latest"):
        if rel in ("newer", "same"):
            return True, f"semver {candidate_version} >= 当前 {cur_v}"
        return False, f"拒绝回退：候选 {candidate_version} < 当前 {cur_v}"
    try:
        newer = int(candidate_run_id) > int(cur_run)
    except ValueError:
        newer = False
    if newer:
        return True, f"非 semver 版本 {candidate_version}，run {candidate_run_id} 较新于 {cur_run}"
    return False, f"拒绝覆盖：版本不可 semver 比较且 run {candidate_run_id} 不新于 {cur_run}"


@dataclass
class PublishResult:
    checks: dict[str, bool] = field(default_factory=dict)
    verification_id: str = ""
    committed_ready: bool = False
    current_written: bool = False
    committed: bool = False
    candidate_ready: bool = False
    candidate: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _strip_scratch(manifest: dict[str, Any]) -> dict[str, Any]:
    """Drop pipeline-internal keys (prefixed `_`) — versions/*.json must equal the contract."""
    return {k: v for k, v in manifest.items() if not k.startswith("_")}


def _put_json(backend, key: str, payload: dict[str, Any]) -> None:
    backend.put(key, json.dumps(payload, ensure_ascii=False, indent=2).encode() + b"\n")


def _put_json_if_match(backend, key: str, payload: dict[str, Any], etag: str | None) -> bool:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode() + b"\n"
    return backend.put_if_match(key, data, etag)


def publish(
    backend,
    cfg,
    manifest: dict[str, Any],
    screenshots_dir: Path,
    report_html: str,
    force: bool = False,
    retries: int = 3,
) -> PublishResult:
    deploy = manifest.get("website", {}).get("deploy", {})
    # A saved pre-gate manifest must not bypass a newly declared production gate.
    root = getattr(cfg, "root", None)
    if root and (Path(root) / "apps" / f"{sanitize_for_id(manifest['app'])}.yaml").is_file():
        from .appspec import load
        declared = load(manifest["app"], root).g("deployment.production_contract")
        if declared is not None and deploy.get("production_contract") != declared:
            raise ValueError("production contract changed/missing; application re-verification required")
    if "production_contract" in deploy:
        import os

        from .candidates import stage
        return stage(backend, cfg, manifest, screenshots_dir, report_html,
                     attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    app = manifest["app"]
    app_version = manifest["app_version"]
    strategy = str(manifest.get("_strategy") or "release_tag")
    # 键片段清洗：app_version 来自上游 tag（可能含 `/`、大写），不得原样进对象键。
    ver_key = f"verified/{sanitize_for_id(app)}/versions/{sanitize_for_id(app_version)}.json"
    result = PublishResult(verification_id=manifest["verification_id"])

    local_failed = [c for c in LOCAL_CHECKS if not manifest["checks"].get(c)]
    if local_failed:
        result.notes.append(f"P0 门禁未过，PUBLISHING 不开始（R2/磁盘零写入）：{local_failed}")
        result.checks = dict(manifest["checks"])
        return result

    def serialize() -> dict[str, Any]:
        out = _strip_scratch(manifest)
        out["website"] = _strip_scratch(manifest["website"])
        # deploy.hold 是运维态、只活在 current.json（§2.5）；漏进不可变的版本记录
        # 会把暂停冻结成历史证据：解除后该版本永远被旧快照拦，反向（hold 前验证的
        # 版本无快照 hold）则在暂停期间看起来可部署。两个方向都是错的。
        # 注意重建 deploy 层再 pop——_strip_scratch 只拷顶层，就地 pop 会污染
        # manifest["website"]，让稍后写 current.json 时 hold 已丢失。
        dep = out["website"].get("deploy")
        if isinstance(dep, dict) and "hold" in dep:
            out["website"]["deploy"] = {k: v for k, v in dep.items() if k != "hold"}
        return out

    def placeholder() -> dict[str, Any]:
        out = serialize()
        for c in UPLOAD_CHECKS:
            out["checks"][c] = False
        return out

    # ---------------- P1: placeholder record (never claim an upload we haven't done)
    # versions/{app_version}.json 以版本为键：同一版本重新验证 = 覆盖该记录，
    # seq 只用于区分不同次验证的 verification_id（契约 §2）。
    existing = backend.get(ver_key)
    notes: list[str] = []
    prev: dict[str, Any] | None = None
    if existing:
        try:
            prev = json.loads(existing)
        except json.JSONDecodeError:
            # 与 _get_current 对 current.json 的口径一致：损坏记录视为缺失，
            # 补投/重跑不该以 traceback 收场。
            notes.append(f"既有 {ver_key} 损坏（非 JSON）→ 按缺失处理并覆盖")
    if prev:
        recorded = prev.get("verification_id") or ""
        if str(prev.get("verification_run_id") or "") != str(manifest.get("verification_run_id") or ""):
            _rename_id(manifest, _seq_of(recorded) + 1)
            notes.append(
                f"该版本已由 run {prev.get('verification_run_id')} 验证过（{recorded}）"
                f"→ 本次 verification_id = {manifest['verification_id']}"
            )
        else:
            notes.append(f"同一 run 重试，沿用 {recorded}")
    _put_json(backend, ver_key, placeholder())
    manifest["checks"]["verification_manifest_uploaded"] = backend.exists(ver_key)

    # ---------------- P2: screenshots + report
    shots = manifest["artifacts"]["screenshots"]
    for s in shots:
        src = Path(screenshots_dir) / s["file"]
        if src.exists():
            backend.put(screenshot_key(app, app_version, s["file"]), src.read_bytes(), "image/png")
        else:
            notes.append(f"缺少本地截图 {src}")
    vid = manifest["verification_id"]
    report_key = f"reports/{vid}.html"
    backend.put(report_key, report_html.encode("utf-8"), "text/html; charset=utf-8")
    url = report_url(cfg, vid)
    manifest["artifacts"]["report_url"] = url
    manifest["website"]["report_url"] = url

    # ---------------- P3: probe — the only honest source for the upload checks
    probes: dict[str, bool] = {}
    for attempt in range(1, retries + 1):
        probes = {
            "screenshots": bool(shots) and all(
                backend.exists(screenshot_key(app, app_version, s["file"])) for s in shots
            ),
            "report": backend.exists(report_key),
            "manifest": backend.exists(ver_key),
        }
        if all(probes.values()):
            break
        notes.append(f"P3 探测未通过（第 {attempt} 次）：{probes}")
    manifest["checks"]["screenshots_uploaded"] = bool(probes.get("screenshots"))
    manifest["checks"]["report_uploaded"] = bool(probes.get("report"))
    manifest["checks"]["verification_manifest_uploaded"] = bool(probes.get("manifest"))

    if not all(probes.values()):
        _put_json(backend, ver_key, placeholder())
        result.notes.extend(notes)
        result.notes.append("P3 未通过 → 不写 current.json（按 TRANSIENT 重试，官网无损）")
        result.checks = dict(manifest["checks"])
        return result

    # ---------------- P4: final record, nine checks truthful
    result.committed_ready = all(manifest["checks"][c] for c in CHECKS)
    if result.committed_ready:
        # build 时上传类 check 尚为 false，status 被算成 pending；发布成立后必须是 verified
        manifest["website"]["status"] = "verified"
    _put_json(backend, ver_key, serialize())
    if not backend.exists(ver_key):
        result.notes.append("P4 重写失败 → 不提交")
        result.checks = dict(manifest["checks"])
        return result

    # ---------------- P5: commit point
    ok, why = may_update_current(
        backend, app, app_version, str(manifest.get("verification_run_id") or "0"), strategy, force
    )
    if not ok:
        result.notes.extend(notes + [f"版本覆盖保护：不更新 current.json —— {why}"])
        result.checks = dict(manifest["checks"])
        return result
    # current.json 是实时运维面：hold 由 Manifest 投影链在此写入（版本记录已剥离，
    # 之后由 sync_holds.py 单独条件维护）。
    _put_json(backend, f"verified/{app}/current.json", _strip_scratch(manifest["website"]))
    _update_index(backend, app, manifest)
    _update_versions_index(backend, app, manifest)
    result.current_written = True
    result.committed = True
    result.notes.extend(notes + [why])
    result.checks = dict(manifest["checks"])
    log(f"PUBLISHED {vid} -> current.json + index.json + versions/index.json（{why}）")
    return result


def _update_index(backend, app: str, manifest: dict[str, Any]) -> None:
    """verified/index.json — the website's only way to enumerate apps (§2.1)。

    跨应用读改写：CI 并发组只互斥同 app（verify-<app>），不同 app 的 P5 可能并行，
    无条件 last-writer-wins 会丢条目。因此用条件写（If-Match ETag）：
    失配即重读-重合-重写；连续冲突才让本次发布失败（宁可失败重触发，不可静默丢数据）。
    """
    key = "verified/index.json"
    w = manifest["website"]
    entry = {
        "app": app,
        "app_version": manifest["app_version"],
        "verification_id": manifest["verification_id"],
        "status": w["status"],
        "health": w["health"],
        "verified_at": manifest["verified_at"],
    }
    for attempt in range(1, _INDEX_WRITE_ATTEMPTS + 1):
        raw, etag = backend.get_with_etag(key)
        index: dict[str, Any] = {"schema_version": "1.0", "apps": []}
        if raw:
            try:
                index = json.loads(raw)
            except json.JSONDecodeError:
                log(f"index.json 损坏 → 以空索引重建（{key}）")
        index["apps"] = sorted(
            [a for a in index.get("apps", []) if a.get("app") != app] + [entry],
            key=lambda a: a["app"],
        )
        index["generated_at"] = utcnow()
        if _put_json_if_match(backend, key, index, etag):
            return
        log(f"index.json 条件写冲突（第 {attempt}/{_INDEX_WRITE_ATTEMPTS} 次）→ 重读重合")
    raise RuntimeError(f"index.json 条件写连续 {_INDEX_WRITE_ATTEMPTS} 次冲突：{key}")


def _update_versions_index(backend, app: str, manifest: dict[str, Any]) -> None:
    """verified/{app}/versions/index.json — 每应用版本清单（deployment-contract §2.2）。

    索引按应用分键：同应用发布已被 CI 并发组（application-verify.yml 的 verify-<app>）
    互斥，正常无竞争；条件写是纵深防御（本地并发 / 组保护失效时也不静默丢条目）。
    只在 P5 提交点写入——未过提交门禁的版本留在 versions/{app_version}.json，但不进清单。
    """
    key = f"verified/{sanitize_for_id(app)}/versions/index.json"
    entry = {
        "app_version": manifest["app_version"],
        "verification_id": manifest["verification_id"],
        "status": manifest["website"]["status"],
        "verified_at": manifest["verified_at"],
    }
    for attempt in range(1, _INDEX_WRITE_ATTEMPTS + 1):
        raw, etag = backend.get_with_etag(key)
        entries: list[dict[str, Any]] = []
        if raw:
            try:
                loaded = json.loads(raw)
                entries = list(loaded.get("versions", [])) if isinstance(loaded, dict) else []
            except json.JSONDecodeError:
                log(f"{key} 损坏 → 以空清单重建")
        entries = [
            e for e in entries
            if isinstance(e, dict) and e.get("app_version") != manifest["app_version"]
        ]
        entries.append(entry)
        entries.sort(key=lambda e: str(e.get("verified_at") or ""), reverse=True)
        payload = {
            "schema_version": "1.0",
            "app": app,
            "generated_at": utcnow(),
            "versions": entries,
        }
        if _put_json_if_match(backend, key, payload, etag):
            return
        log(f"{key} 条件写冲突（第 {attempt}/{_INDEX_WRITE_ATTEMPTS} 次）→ 重读重合")
    raise RuntimeError(f"{key} 条件写连续 {_INDEX_WRITE_ATTEMPTS} 次冲突")


def _seq_of(vid: str) -> int:
    try:
        return int(vid.rsplit("-", 1)[-1])
    except ValueError:
        return 1


def _rename_id(manifest: dict[str, Any], seq: int) -> None:
    new = manifest["verification_id"][: -3] + f"{seq:03d}"
    manifest["verification_id"] = new
    manifest["website"]["verification_id"] = new
