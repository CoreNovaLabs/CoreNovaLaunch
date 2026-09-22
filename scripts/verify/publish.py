#!/usr/bin/env python3
"""独立发布 CLI：从 data/runs/{vid}/state.json 重新走两阶段提交（P3/P4 中断后的补投）。

    python scripts/verify/publish.py --vid ghost-v6.61.0-20260829-001
    python scripts/verify/publish.py --latest-failed-publish ghost --dry-run

与 pipeline 的关系：`run_verification` 现已把 PUBLISHING 的异常也落 state.json +
失败台账（TRANSIENT），本脚本提供**不重跑验证**的补投路径——直接复用当时落盘的
Manifest 与产物重走 P1..P5，并在任何写入前先跑 P0 前置门禁——绝不因为
"有人在补投"就绕过门禁。

退出码：0=PUBLISHED；1=未提交（版本覆盖保护/待人工）；2=P0 门禁不过或输入不可用；3=需要重建现场。

幂等性来自 `corenova.publish.publish()`：versions/{app_version}.json 以版本为键，
同一 run 重试沿用同一 verification_id（§7 规则 2），P5 是唯一提交点。
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

from corenova import publish
from corenova.backend import make_backend
from corenova.config import Config
from corenova.manifest import CHECKS
from corenova.util import log, utcnow


def load_manifest(cfg: Config, vid: str) -> dict[str, Any]:
    path = cfg.output_dir / "runs" / vid / "state.json"
    if not path.exists():
        raise FileNotFoundError(f"缺少 {path} —— 该次运行没有可补投的 Manifest")
    doc = json.loads(path.read_text(encoding="utf-8"))
    manifest = doc.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError(f"{path} 里没有 manifest 字段")
    # P5 写的就是 manifest["website"]：结构缺件必须在看不到写入之前就停手
    for required in ("website", "checks", "verification_id", "app_version", "app"):
        if required not in manifest:
            raise ValueError(f"manifest 缺字段 {required!r}（{path}）——不是完整 Manifest，拒绝补投")
    if not isinstance(manifest.get("artifacts"), dict) or "screenshots" not in manifest["artifacts"]:
        raise ValueError(f"manifest.artifacts.screenshots 缺失（{path}）——P2/P3 无从探测")
    return manifest


def latest_publishable_vid(cfg: Config, app: str | None) -> str | None:
    """找最近一次“本地 checks 全过但未提交”的运行（P3/P4 中断的典型现场）。"""
    runs = sorted((cfg.output_dir / "runs").glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in runs:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        m = doc.get("manifest") or {}
        if app and m.get("app") != app:
            continue
        local_ok = all((m.get("checks") or {}).get(c) for c in publish.LOCAL_CHECKS)
        committed = bool((m.get("checks") or {}).get("report_uploaded"))
        if local_ok and not committed:
            return p.parent.name
    return None


def rebuild_report(cfg: Config, manifest: dict[str, Any], report_html: str | None) -> str:
    """P2 要上传报告文本。优先用磁盘上的报告，其次用 --report-file，都没有则拒绝补投。

    不用 `report.render()` 现生成：那会引入当前仓库 revision，破坏“验证当时的证据”。
    """
    if report_html:
        return report_html
    vid = str(manifest["verification_id"])
    on_disk = cfg.output_dir / "reports" / f"{vid}.html"
    if on_disk.exists():
        return on_disk.read_text(encoding="utf-8")
    raise FileNotFoundError(f"找不到报告 {on_disk}，请用 --report-file 指定，否则无法补投 P2")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 state.json 重新执行两阶段提交（P1..P5）")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--vid", help="data/runs/<vid>/state.json 的 verification_id")
    src.add_argument("--run-dir", help="data/runs/<vid> 目录（与 --vid 等价）")
    src.add_argument("--latest-failed-publish", metavar="APP", nargs="?", const="", default=None,
                     help="自动选取最近一次未提交的运行（可选限定 app）")
    ap.add_argument("--report-file", help="补投用的报告 HTML 路径（默认取 data/reports/<vid>.html）")
    ap.add_argument("--force", action="store_true", help="绕过版本覆盖保护（人工授权，需在台账留痕）")
    ap.add_argument("--dry-run", action="store_true", help="只跑 P0 门禁并打印将做的事，零写入")
    ap.add_argument("--dispatch", action="store_true", help="提交成功后额外发 repository_dispatch → Repo A")
    args = ap.parse_args(argv)

    cfg = Config.load()
    vid = args.vid or (pathlib.Path(args.run_dir).name if args.run_dir else "")
    if args.latest_failed_publish is not None:
        vid = latest_publishable_vid(cfg, args.latest_failed_publish or None) or ""
    if not vid:
        log("没有可补投的运行（--latest-failed-publish 未命中）→ 需要先重跑 application-verify")
        return 3

    try:
        manifest = load_manifest(cfg, vid)
    except FileNotFoundError as exc:
        log(str(exc))
        return 3
    except (json.JSONDecodeError, ValueError) as exc:
        log(f"state.json 不可用：{type(exc).__name__}: {exc}")
        return 2

    app = str(manifest.get("app") or "")
    shots_dir = cfg.output_dir / "runs" / vid / "screenshots"

    # -------- P0 前置门禁：六项本地 checks 必须已在这次 Manifest 里为真
    local_failed = [c for c in publish.LOCAL_CHECKS if not (manifest.get("checks") or {}).get(c)]
    if local_failed:
        log(f"P0 门禁未过 → 拒绝发布（R2/磁盘零写入）：{local_failed}")
        log("正确出路：修 apps/ 内的应用注册或预写测试后重跑 application-verify.yml，不是补投。")
        print(json.dumps({"status": "REFUSED", "reason": "p0_gate", "local_failed": local_failed},
                         ensure_ascii=False))
        return 2

    try:
        report_html = rebuild_report(
            cfg, manifest,
            pathlib.Path(args.report_file).read_text(encoding="utf-8") if args.report_file else None,
        )
    except FileNotFoundError as exc:
        log(str(exc))
        return 3

    if args.dry_run:
        out = {
            "status": "DRY_RUN", "app": app, "app_version": manifest["app_version"],
            "verification_id": manifest["verification_id"],
            "would_commit_current": False if manifest["website"].get("deploy", {}).get("production_contract") else None,
            "notes": ["offline only; no backend/current lookup; freshness is checked at actual publish"],
            "missing_local_artifacts": [] if shots_dir.is_dir() else [str(shots_dir)],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    backend = make_backend(cfg)
    current_before = publish.current_version(app, cfg)
    result = publish.publish(backend, cfg, manifest, shots_dir, report_html, force=args.force)
    status = ("CANDIDATE_READY" if result.candidate_ready else "PUBLISHED" if result.current_written
              else "NOT_COMMITTED" if result.committed_ready else "FAILED")
    out = {
        "status": status,
        "backend": backend.name,
        "app": app,
        "app_version": manifest["app_version"],
        "verification_id": manifest["verification_id"],
        "current_published_before": current_before,
        "current_published_after": publish.current_version(app, cfg),
        "committed_ready": result.committed_ready,
        "checks_all_true": all(result.checks.get(c) for c in CHECKS),
        "checks": result.checks,
        "candidate": result.candidate,
        "notes": result.notes,
        "published_at": utcnow() if result.current_written else "",
    }
    if result.candidate_ready:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0  # no site dispatch until exact production promotion
    if result.current_written:
        if args.dispatch:
            # 复用 pipeline 的实现，避免 dispatch payload 出现第二套形状
            from corenova import pipeline

            pipeline.dispatch_site(cfg, manifest)
            out["dispatch"] = "attempted"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    log("未提交 current.json → 官网仍显示旧版本（无损）。按 notes 判断是 TRANSIENT 补投还是需人工。")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
