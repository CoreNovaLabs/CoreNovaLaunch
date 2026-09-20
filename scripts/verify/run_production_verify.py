#!/usr/bin/env python3
"""L1.5 生产核对入口（会在真实 AWS 建一次性栈，产生费用）。

    python scripts/verify/run_production_verify.py --app netdata --dry-run   # 零 AWS 调用，只打印计划
    python scripts/verify/run_production_verify.py --app netdata             # 核对当前已发布版本
    python scripts/verify/run_production_verify.py --app netdata --version v1.47.0

核对项来自 apps/<app>.yaml 的 deployment.production_contract.checks；Manifest 从
生效后端读取（核对的必须恰是发布过的那份证据，corenova/prodcheck.py）。
退出码：0=全部通过；1=任一核对项失败或流程错误（hold 保持不变）。
"""

from __future__ import annotations

import argparse
import json

from corenova import appspec, golden, prodcheck
from corenova.backend import make_backend
from corenova.config import Config


def main() -> int:
    ap = argparse.ArgumentParser(description="CoreNova 生产核对（L1.5）")
    ap.add_argument("--app", required=True, help="apps/<app>.yaml 的应用名")
    ap.add_argument("--version", default="", help="app_version（留空 = 当前已发布版本）")
    ap.add_argument("--dry-run", action="store_true", help="只打印一次性栈计划，零 AWS 调用")
    ap.add_argument("--keep-stack", action="store_true", help="调试用：核对完保留栈（栈内资源在计费）")
    args = ap.parse_args()

    cfg = Config.load()
    backend = make_backend(cfg)
    spec = appspec.load(args.app, cfg.root)
    manifest = prodcheck.load_manifest(backend, args.app, args.version)

    declared = spec.g("deployment.production_contract.checks") or []
    if not declared:
        print(f"[corenova] {args.app} 未声明 deployment.production_contract，无核对项 → 跳过（退出 0 不代表可部署）")
        return 0

    if args.dry_run:
        plan = prodcheck.plan(cfg, spec, manifest, run_id="dryrun")
        print(json.dumps({
            "stack_name": plan.stack_name,
            "template_url": plan.template_url,
            "checks": plan.checks,
            "declared": plan.declared,
            "parameters": plan.parameters,
            "tags": plan.tags,
            "note": "用公开 one-click 合并模板建栈：网络自包含，无 SubnetId/SG 参数",
        }, ensure_ascii=False, indent=2))
        return 0

    aws = golden.Aws(cfg)
    report = prodcheck.run(backend, cfg, spec, manifest, aws, keep=args.keep_stack)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("all_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
