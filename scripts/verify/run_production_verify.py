#!/usr/bin/env python3
"""Production candidate check. --dry-run requires a local manifest and is offline."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from corenova import appspec, candidates, golden, prodcheck
from corenova.backend import make_backend
from corenova.config import Config


def main() -> int:
    ap = argparse.ArgumentParser(description="CoreNova production candidate gate")
    ap.add_argument("--app", required=True)
    ap.add_argument("--candidate", help="exact candidate reference JSON, never latest")
    ap.add_argument("--manifest-file", type=Path, help="local manifest for offline dry-run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-stack", action="store_true")
    ap.add_argument("--promote", action="store_true", help="promote using this run's evidence file")
    ap.add_argument("--evidence-file", type=Path)
    args = ap.parse_args()
    cfg = Config.load()
    spec = appspec.load(candidates.component(args.app), cfg.root)
    if args.dry_run:
        if not args.manifest_file or args.promote:
            ap.error("--dry-run requires --manifest-file; no backend or AWS lookup is allowed")
        m = json.loads(args.manifest_file.read_text())
        if m.get("app") != args.app:
            ap.error("manifest app mismatch")
        p = prodcheck.plan(cfg, spec, m, run_id="dryrun")
        print(json.dumps({"stack_name": p.stack_name, "template_url": p.template_url,
                          "parameters": p.parameters, "checks": p.checks,
                          "note": "offline plan only; no deployment or business verification"}, indent=2))
        return 0
    if not args.candidate or not args.evidence_file:
        ap.error("live check/promotion requires --candidate and --evidence-file")
    ref = json.loads(args.candidate)
    if ref.get("app") != args.app:
        ap.error("candidate app mismatch")
    backend = make_backend(cfg)
    _, manifest = candidates.load(backend, ref)
    if args.promote:
        evidence = json.loads(args.evidence_file.read_text())
        candidates.promote(backend, cfg, ref, evidence,
                           production_run_id=os.environ.get("GITHUB_RUN_ID", ""), spec=spec,
                           production_run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
                           check_template=prodcheck.verify_public_template)
        print("PROMOTED: exact candidate; manual holds preserved")
        return 0
    report = prodcheck.run(backend, cfg, spec, manifest, golden.Aws(cfg),
                           keep=args.keep_stack, candidate_ref=ref)
    args.evidence_file.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_file.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("all_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
