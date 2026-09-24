#!/usr/bin/env python3
"""版本监控：对每个 apps/*.yaml 解析上游 release，与"当前已发布版本"比较，输出待验证清单。

    python scripts/monitor/check_versions.py --all --json
    python scripts/monitor/check_versions.py --app ghost --json

真实逻辑放在这里而不是 workflow 的 shell 里，是为了能在本地/CI 外单独复跑与断言；
workflow（monitor-versions.yml）只负责定时 + 拿 token + 逐 app 扇出 dispatch。

判定口径（verify-gate-design.md §3 / workflow-state-machine.md §5）：
- “已发布版本”唯一事实源 = 当前生效后端的 verified/{app}/current.json。后端由
  VERIFIED_BACKEND 显式指定，禁止“读不到 R2 就回退本地”（repo-structure.md §4.2.1）。
- current.json 缺失 → 该应用从未发布过 → 判 initial（待首次验证），而不是"无变化"。
- 但如果后端在本环境**没有持久 current.json**（runner 上的 dir 后端每次都是空目录），
  “缺失”就证明不了“从未发布” → 判 hold：照实报告，不扇出。否则每 6h 会把同一版本
  反复推去验证（引导期接 R2 后自动恢复；也可用 --allow-non-durable 手工放行）。
- 扇出必须**逐 app 独立 run**：并发组按 app 划分，一个 run 跑多 app 会让队列互相阻塞。
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import pathlib
import subprocess
import time
from typing import Any

from corenova import appspec, publish, resolver
from corenova.config import Config
from corenova.failure import classify
from corenova.util import HttpError, log, utcnow
from corenova.versioning import relation as version_relation
from corenova.versioning import semver_relation


def ensure_gh_token() -> str:
    """本地开发没有 GITHUB_TOKEN 时借用 gh CLI 凭据。

    corenova.resolver.GitHub 只读 GITHUB_TOKEN/GH_TOKEN；匿名调用会被 GitHub 按 IP 限流
    （60 req/h），监控每轮要打好几个 release 接口，一旦限流整轮就退化成"解析失败"。
    CI 上 GITHUB_TOKEN 已存在 → 本函数直接返回，不改任何行为。
    """
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(key):
            return key
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    token = (proc.stdout or "").strip()
    if proc.returncode == 0 and token:
        os.environ["GH_TOKEN"] = token
        log("未设置 GITHUB_TOKEN → 已从 `gh auth token` 取本地凭据（仅本轮进程）")
        return "GH_TOKEN"
    log("无 GitHub 凭据 → 匿名访问 API（可能被限流，监控会报解析失败）")
    return ""


def compare(candidate: str, current: str | None, strategy: str) -> dict[str, Any]:
    """版本覆盖保护用的同一套 semver 口径（state-machine §5）：只认新于当前的 release。

    关系值由 corenova.versioning.relation 统一裁决（与 publish.may_update_current 同源），
    这里只负责把关系映射成扇出决策。
    """
    rel = version_relation(candidate, current or "")
    if rel == "initial":
        return {"decision": "dispatch", "reason": "尚无 current.json → 需首次验证", "relation": "initial"}
    if rel == "newer":
        return {"decision": "dispatch", "reason": f"{candidate} 新于已发布 {current}", "relation": "newer"}
    if rel == "same":
        return {"decision": "skip", "reason": f"{candidate} 已是当前发布版本", "relation": "same"}
    if rel == "older":
        return {
            "decision": "skip",
            "reason": f"{candidate} 旧于已发布 {current}（即使验证通过也不得覆盖 current.json）",
            "relation": "older",
        }
    # commit SHA / 日期标签无法用版本号裁决：交给 application-verify 的 run_id 覆盖保护，
    # 这里只做节流（见 up_to_date_within_window），不做"看起来不同就重跑"。
    return {
        "decision": "dispatch",
        "reason": f"strategy={strategy} 不可 semver 比较（{candidate} vs {current}）→ 交由版本覆盖保护裁决",
        "relation": "unknown",
    }


def up_to_date_within_window(release: dict[str, Any], hours: int) -> bool:
    """非 semver 应用的节流阀：窗口内没有新 release 就不必再 dispatch。

    git_branch/pinned 产出的 app_version 恒定（分支名/固定 tag），每次都比对必然得到
    "unknown → dispatch"，会天天扇出重复验证。
    """
    published_at = str(release.get("published_at") or "")
    if not published_at or hours <= 0:
        return False
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            ts = time.strptime(published_at, fmt)
        except ValueError:
            continue
        # GitHub 的 published_at 是 UTC，必须用 timegm：time.mktime 会按本地时区偏移。
        # 语义：最近一次 release 距今已超过窗口 → 窗口内没有新东西 → 可以跳过。
        return (time.time() - calendar.timegm(ts)) > hours * 3600
    return False


def _both_semver(candidate: str, current: str) -> bool:
    return semver_relation(candidate, current) is not None


def is_durable(cfg: Config) -> bool:
    """当前后端的 current.json 是否能跨 run 存活。

    runner 上的 `dir` 后端每次都是空目录 → "无 current.json" 不代表"从未发布"，
    只代表"这里看不到发布事实源"。不区分这两者会让每 6h 的监控重复扇出同一版本。
    """
    if cfg.verified_backend == "r2":
        return True
    return (cfg.output_dir / "verified").is_dir()


def image_preflight(image_ref: str, mirror: str) -> dict[str, str]:
    _, repo, tag, host = resolver.split_image(image_ref, mirror)
    manifest_url = f"https://{host}/v2/{repo}/manifests/{tag}"
    for attempt in range(3):
        try:
            resolver.resolve_digest(image_ref, mirror)
            return {}
        except Exception as exc:  # noqa: BLE001
            missing = False
            if isinstance(exc, HttpError) and exc.status == 404 and exc.url == manifest_url:
                try:
                    body = json.loads(exc.body)
                except ValueError:
                    body = None
                errors = body.get("errors") if isinstance(body, dict) else None
                missing = isinstance(errors, list) and any(
                    isinstance(error, dict) and error.get("code") == "MANIFEST_UNKNOWN"
                    for error in errors
                )
            if not missing:
                return {
                    "decision": "error",
                    "classification": classify("RESOLVED", "resolve_digest", exc),
                    "reason": f"精确镜像预检失败（不扇出）：{type(exc).__name__}: {exc}",
                }
            if attempt == 2:
                return {
                    "decision": "hold",
                    "classification": "MANUAL_REQUIRED",
                    "reason": f"精确镜像 {image_ref} 尚未就绪（MANIFEST_UNKNOWN，已查询 3 次）；"
                              "本轮不扇出，下轮监控重新检查。持续缺失需人工核对上游镜像发布。",
                }
            delay = 5 * 2 ** attempt
            log(f"精确镜像 {image_ref} 尚未就绪，{delay}s 后再次查询（不启动验证）")
            time.sleep(delay)
    raise AssertionError("unreachable")


def inspect_app(
    name: str, cfg: Config, gh: resolver.GitHub, min_age_hours: int,
    *, durable: bool | None = None, allow_non_durable: bool = False,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "app": name,
        "decision": "error",
        "dispatch": False,
        "upstream_repo": "",
        "version_strategy": "",
    }
    try:
        spec = appspec.load(name, cfg.root)
    except Exception as exc:  # noqa: BLE001 - 一个坏注册表项不该拖垮整轮监控
        entry["reason"] = f"注册文件不可用：{type(exc).__name__}: {exc}"
        entry["classification"] = classify("RESOLVED", "app_schema", exc)
        return entry

    entry["upstream_repo"] = spec.source_repo
    entry["version_strategy"] = spec.version_strategy
    try:
        current = publish.current_version(name, cfg)
        resolved = resolver.pick_release(spec, gh=gh)
        raw = gh.latest_release(spec.source_repo)
    except Exception as exc:  # noqa: BLE001
        entry["reason"] = f"上游解析失败：{type(exc).__name__}: {exc}"
        # RESOLVED 阶段的异常只有带瞬时特征（超时/限流/5xx）才值得自动重试
        entry["classification"] = classify("RESOLVED", "resolve_version", exc)
        return entry

    verdict = compare(resolved.app_version, current, spec.version_strategy)
    durable_ok = is_durable(cfg) if durable is None else durable
    if verdict["decision"] == "dispatch" and not current and not durable_ok and not allow_non_durable:
        verdict = {
            "decision": "hold",
            "relation": "initial",
            "reason": (
                f"后端 {cfg.verified_backend} 在本环境无持久 current.json（{cfg.output_dir}/verified 不存在）"
                "→ 无法证明这是新版本还是“这里看不到发布状态”，故不扇出。"
                "接入 R2（VERIFIED_BACKEND=r2）或手工 dispatch application-verify.yml 做首次验证。"
            ),
        }
    if verdict["decision"] == "dispatch" and not _both_semver(resolved.app_version, current or ""):
        if up_to_date_within_window(raw if isinstance(raw, dict) else {}, min_age_hours):
            verdict = {
                "decision": "skip",
                "reason": f"不可 semver 比较且 {min_age_hours}h 内无新 release，本轮跳过（节流）",
                "relation": verdict["relation"],
            }

    entry.update(
        {
            "app_version": resolved.app_version,
            "release_tag": resolved.release_tag,
            "release_type": resolved.release_type,
            "type_evidence": resolved.type_evidence,
            "published_at": resolved.published_at,
            "source_revision": resolved.source_revision,
            "current_published_version": current,
            "decision": verdict["decision"],
            "relation": verdict["relation"],
            "reason": verdict["reason"],
            "dispatch": verdict["decision"] == "dispatch",
            "classification": "",
        }
    )
    if entry["dispatch"]:
        try:
            image_ref = appspec.render_image_ref(spec, resolved.app_version)
            entry["image_ref"] = image_ref
            entry.update(image_preflight(image_ref, cfg.registry_mirror))
        except Exception as exc:  # noqa: BLE001
            entry.update(decision="error", classification=classify("RESOLVED", "resolve_digest", exc),
                         reason=f"精确镜像预检失败：{type(exc).__name__}: {exc}")
        entry["dispatch"] = entry["decision"] == "dispatch"
    return entry


def render_markdown(payload: dict[str, Any]) -> str:
    pending = [e for e in payload["entries"] if e["dispatch"]]
    held = [e for e in payload["entries"] if e.get("decision") == "hold"]
    lines = [
        "## 版本监控结论",
        "",
        f"- 生效后端：`{payload['backend']}`（事实源 `verified/<app>/current.json`）"
        f"，跨 run 可用：**{'是' if payload.get('durable_current_source') else '否'}**",
        f"- 检查应用：{payload['apps_checked']}，待验证扇出：**{len(pending)}**，"
        f"暂缓（后端或镜像未就绪）：{len(held)}，解析/预检失败：{payload['errors']}",
        "",
        "| app | upstream | strategy | 上游版本 | 已发布 | 关系 | 决策 | 依据 |",
        "|-----|----------|----------|---------|--------|------|------|------|",
    ]
    for e in payload["entries"]:
        lines.append(
            "| {app} | {repo} | {strategy} | `{ver}` | `{cur}` | {rel} | **{dec}** | {reason} |".format(
                app=e["app"],
                repo=e.get("upstream_repo") or "—",
                strategy=e.get("version_strategy") or "—",
                ver=e.get("app_version") or "—",
                cur=e.get("current_published_version") or "—（未发布）",
                rel=e.get("relation") or e.get("classification") or "—",
                dec="dispatch" if e["dispatch"] else e.get("decision", "skip"),
                reason=str(e.get("reason", "")).replace("|", "\\|"),
            )
        )
    errors = [e for e in payload["entries"] if e.get("decision") == "error"]
    if errors:
        lines += ["", "### 本轮解析失败（不扇出）", ""]
        for e in errors:
            lines.append(f"- `{e['app']}` [{e.get('classification') or '?'}] {e.get('reason')}")
    if held:
        lines += ["", "### 本轮暂缓扇出（不代表验证通过）", ""]
        lines += [f"- `{e['app']}` → `{e.get('app_version')}`：{e.get('reason')}" for e in held]
    if payload.get("dispatches"):
        lines += ["", "### 扇出结果", ""]
        for d in payload["dispatches"]:
            lines.append(f"- `{d['app']}`: {'ok' if d['ok'] else 'FAILED ' + (d.get('stderr') or '')}")
    lines += [
        "",
        "> 扇出为逐 app 独立 `workflow_dispatch`（state-machine §5：一个 run 跑多 app 会让并发组互相串行）。",
    ]
    return "\n".join(lines)


def emit_outputs(payload: dict[str, Any], args: argparse.Namespace) -> None:
    """把结果写给 $GITHUB_OUTPUT / job summary（本地跑时两者都不存在，静默跳过）。"""
    out_file = os.environ.get("GITHUB_OUTPUT")
    if out_file:
        with open(out_file, "a", encoding="utf-8") as fh:
            fh.write(
                "has_updates={}\napps_to_verify={}\nheld={}\nerrors={}\ndurable={}\n".format(
                    "true" if any(e["dispatch"] for e in payload["entries"]) else "false",
                    json.dumps([e["app"] for e in payload["entries"] if e["dispatch"]]),
                    payload.get("held", 0),
                    payload.get("errors", 0),
                    "true" if payload.get("durable_current_source") else "false",
                )
            )
    if args.summary_file:
        md = render_markdown(payload)
        pathlib.Path(args.summary_file).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.summary_file).write_text(md, encoding="utf-8")
        gh_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if gh_summary:
            with open(gh_summary, "a", encoding="utf-8") as fh:
                fh.write(md + "\n")


def fan_out(entries: list[dict[str, Any]], workflow: str, ref: str, dry_run: bool) -> list[dict[str, Any]]:
    """`gh workflow run` 扇出。失败只记录不抛出：单个 dispatch 出错不该让整轮监控误报新版本。"""
    results: list[dict[str, Any]] = []
    for e in entries:
        cmd = [
            "gh", "workflow", "run", workflow, "--ref", ref,
            "-f", f"app_name={e['app']}",
            "-f", f"app_version={e.get('app_version', '')}",
        ]
        if dry_run:
            log(f"[dry-run] {' '.join(cmd)}")
            results.append({"app": e["app"], "ok": True, "dry_run": True, "cmd": " ".join(cmd)})
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True)
        results.append(
            {
                "app": e["app"],
                "ok": proc.returncode == 0,
                "stderr": ((proc.stderr or "") + (proc.stdout or "")).strip()[-400:],
            }
        )
        log(f"dispatch {e['app']}@{e.get('app_version')} -> {'ok' if proc.returncode == 0 else 'failed'}")
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CoreNova 版本监控：对比上游 release 与当前已发布版本")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--app", action="append", help="只检查指定应用（可重复）")
    group.add_argument("--all", action="store_true", help="检查 apps/*.yaml 全部应用")
    ap.add_argument("--json", action="store_true", help="JSON 摘要打到 stdout（默认人类可读）")
    ap.add_argument("--trigger", action="store_true", help="对新版本执行 gh workflow run 扇出")
    ap.add_argument("--workflow", default="application-verify.yml", help="扇出目标工作流文件名")
    ap.add_argument("--ref", default=os.environ.get("GITHUB_REF_NAME", "main"), help="扇出目标分支")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的 dispatch 命令")
    ap.add_argument("--min-age-hours", type=int, default=6, help="不可 semver 比较应用的节流窗口（小时）")
    ap.add_argument(
        "--allow-non-durable", action="store_true",
        help="后端无持久 current.json 时仍按 initial 扇出（仅手工引导期用；定时任务不要用，会每轮重复验证）",
    )
    ap.add_argument("--summary-file", help="把 markdown 摘要写到该文件（供 job summary 使用）")
    args = ap.parse_args(argv)

    ensure_gh_token()
    cfg = Config.load()
    durable = is_durable(cfg)
    names = appspec.all_apps(cfg.root) if args.all or not args.app else list(dict.fromkeys(args.app))
    if not names:
        log("apps/ 下没有应用注册文件")

    gh = resolver.GitHub()
    entries = [
        inspect_app(
            n, cfg, gh, args.min_age_hours, durable=durable, allow_non_durable=args.allow_non_durable
        )
        for n in names
    ]
    payload: dict[str, Any] = {
        "generated_at": utcnow(),
        "backend": cfg.verified_backend,
        "output_dir": str(cfg.output_dir),
        "durable_current_source": durable,
        "apps_checked": len(entries),
        "needs_verification": sum(1 for e in entries if e["dispatch"]),
        "held": sum(1 for e in entries if e.get("decision") == "hold"),
        "errors": sum(1 for e in entries if e.get("decision") == "error"),
        "entries": entries,
        "dispatches": [],
    }
    if args.trigger:
        payload["dispatches"] = fan_out(
            [e for e in entries if e["dispatch"]], args.workflow, args.ref, args.dry_run
        )

    emit_outputs(payload, args)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for e in entries:
            print(
                f"{e['app']:<12} 上游={e.get('app_version') or '?':<12} "
                f"已发布={(e.get('current_published_version') or '—'):<12} "
                f"{e.get('relation') or e.get('classification') or e.get('decision'):<9} -> {e.get('reason')}"
            )
        for d in payload["dispatches"]:
            print(f"dispatch {d['app']}: {'ok' if d['ok'] else 'FAILED ' + d.get('stderr', '')}")

    # 解析失败不是"没新版本"，必须让 cron 可见；但绝不因失败而扇出
    return 1 if payload["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
