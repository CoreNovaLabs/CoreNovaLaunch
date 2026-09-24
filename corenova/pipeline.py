"""Application Verification orchestration (verify-gate-design.md §4).

    RESOLVED  ->  VERIFYING  ->  VERIFIED  ->  PUBLISHING -> PUBLISHED
                    \\__ FAILED (classified, ledger issue created/updated)

No AWS resource is ever created here: platform correctness is *referenced* from a valid
Platform Contract, and the reference itself is proven stale-or-fresh before we proceed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import appspec, platformref, publish, report, resolver, runtime, screenshots
from . import manifest as mf
from .appspec import AppSpec
from .backend import make_backend
from .config import Config
from .failure import FailureRecord, classify, record_failure, resolve_failures
from .gh import github_headers
from .util import die, http_request, log, sanitize_for_id, utcnow
from .util import run as sh


class StageError(RuntimeError):
    def __init__(
        self, stage: str, check: str, err: BaseException, *,
        app_version: str = "", verification_id: str = "",
    ):
        super().__init__(f"{stage}/{check}: {type(err).__name__}: {err}")
        self.stage, self.check, self.err = stage, check, err
        self.app_version, self.verification_id = app_version, verification_id


def _transient_retry(fn, *, attempts: int = 3, stage: str = "", check: str = ""):
    """Only TRANSIENT errors retry, with exponential backoff (§4)."""
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            from .util import is_transitional_error

            last = exc
            if not is_transitional_error(exc) or i == attempts - 1:
                raise StageError(stage, check, exc) from exc
            wait = 2 ** i * 5
            log(f"瞬时错误（{type(exc).__name__}），{wait}s 后重试 {i + 1}/{attempts - 1}")
            time.sleep(wait)
    raise StageError(stage, check, last or RuntimeError("unreachable"))


def docker_available() -> bool:
    import subprocess

    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def run_verification(
    app: str,
    *,
    version: str | None = None,
    do_publish: bool = True,
    force: bool = False,
    skip_ami_drift: bool = False,
    host_port: int | None = None,
    skip_tests: bool = False,
) -> dict[str, Any]:
    cfg = Config.load()
    root = cfg.root
    started = time.time()

    spec = appspec.load(app, root)
    violations = appspec.validate(spec, root, cfg.region)
    if violations:
        raise StageError("RESOLVED", "app_schema", ValueError("\n".join(violations)))
    log(f"{app}: schema 校验通过（app-schema §5 十九条）")

    backend = make_backend(cfg)
    run_id = os.environ.get("GITHUB_RUN_ID") or time.strftime("local-%Y%m%d%H%M%S", time.gmtime())
    outcome = mf.VerifyOutcome(started_at=utcnow())

    # ---------------- RESOLVED
    contract = platformref.check(
        backend, cfg, spec, spec.g("deployment.regions") or [], check_drift=not skip_ami_drift
    )
    for r in contract.reasons:
        log(f"platform contract: {r}")
    outcome.checks["required_platform_contract_valid"] = contract.valid

    resolved = _transient_retry(
        lambda: resolver.pick_release(spec, wanted=version),
        stage="RESOLVED", check="resolve_version",
    )
    image_ref = appspec.render_image_ref(spec, resolved.app_version)
    try:
        image = _transient_retry(
            lambda: resolver.resolve_digest(image_ref, cfg.registry_mirror),
            stage="RESOLVED", check="resolve_digest",
        )
    except StageError as exc:
        exc.app_version = resolved.app_version
        exc.verification_id = f"pre-verification-{app}-{sanitize_for_id(resolved.app_version)}"
        raise
    vid = mf.verification_id(app, resolved.app_version)
    log(f"RESOLVED {app}@{resolved.app_version} image={image.image_ref} digest={image.digest[:19]}…")

    # ---------------- VERIFYING
    workdir = cfg.output_dir / "runs" / vid
    workdir.mkdir(parents=True, exist_ok=True)
    env = runtime.build_env(cfg, spec, image.pull_ref, image.image_ref, workdir, host_port)
    shots_dir = workdir / "screenshots"
    try:
        _verify(spec, root, env, image, shots_dir, resolved, outcome, cfg, skip_tests)
    except StageError as exc:
        exc.app_version = resolved.app_version
        exc.verification_id = vid
        raise
    finally:
        outcome.finished_at = utcnow()
        outcome.duration_s = round(time.time() - started, 1)
        if os.environ.get("CORENOVA_KEEP_RUNNING") != "1":
            runtime.down(env, spec, root)

    outcome.checks["screenshots_uploaded"] = False
    outcome.checks["report_uploaded"] = False
    outcome.checks["verification_manifest_uploaded"] = False
    outcome.verification = {
        "application": "passed"
        if outcome.checks.get("compose_started") and outcome.checks.get("container_healthy")
        and outcome.checks.get("health_check_passed") and outcome.assertion_ok
        else "failed",
        "platform": "referenced" if contract.valid else "failed",
        "tests": "passed" if outcome.checks.get("tests_passed") else "failed",
    }

    manifest = mf.build(
        spec, root, resolved, image,
        {
            "platform_verification_id": contract.contract.get("platform_verification_id", ""),
            "ami_id": contract.contract.get("ami_id", ""),
            "base_ami_source": cfg.base_ami_source,
            "source_ami_name": contract.contract.get("source_ami_name", ""),
            "region": cfg.region,
            "architecture": cfg.architecture,
        },
        outcome, cfg, run_id, vid=vid,
    )
    html = report.render(manifest, outcome.tests_detail, outcome.log_tail)
    report_path = cfg.output_dir / "reports" / f"{vid}.html"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(html, encoding="utf-8")

    summary: dict[str, Any] = {
        "verification_id": vid,
        "app": app,
        "app_version": resolved.app_version,
        "checks": manifest["checks"],
        "verification": manifest["verification"],
        "report_path": str(report_path),
        "local_checks_all_true": all(manifest["checks"][c] for c in publish.LOCAL_CHECKS),
    }

    # ---------------- FAILED path
    if not summary["local_checks_all_true"]:
        failed_check = next((c for c in publish.LOCAL_CHECKS if not manifest["checks"][c]), "unknown")
        stage = "RESOLVED" if failed_check == "required_platform_contract_valid" else "VERIFYING"
        rec = FailureRecord(
            app=app, app_version=resolved.app_version, verification_id=vid,
            classification=classify(stage, failed_check), failed_stage=stage, failed_check=failed_check,
            run_url=mf.workflow_run_url(run_id),
            platform_verification_id=str(contract.contract.get("platform_verification_id", "")),
            detail="\n".join([outcome.probe_detail, outcome.assertion_detail, outcome.tests_detail,
                              *contract.reasons, outcome.log_tail[-2000:]]),
        )
        record_failure(rec)
        summary.update({"status": "FAILED", "classification": rec.classification, "failed_check": failed_check})
        _write_state(cfg, vid, manifest, summary)
        return summary

    # ---------------- PUBLISHING (P1..P5)
    if not do_publish:
        summary["status"] = "VERIFIED"
        summary["notes"] = ["--no-publish：未写 current.json/index.json"]
        _write_state(cfg, vid, manifest, summary)
        return summary

    # PUBLISHING 的失败必须走与其它阶段相同的落账路径（state-machine §4/§7）：
    # publish() 的索引条件写连续冲突会 raise，若让异常裸逃，state.json 与失败台账
    # 都不产生，reverify-failed 也就看不见这次失败。
    try:
        result = publish.publish(backend, cfg, manifest, shots_dir, html, force=force)
    except Exception as exc:  # noqa: BLE001
        cls = classify("PUBLISHING", "publish_commit", exc)
        rec = FailureRecord(
            app=app, app_version=resolved.app_version, verification_id=manifest["verification_id"],
            classification=cls, failed_stage="PUBLISHING", failed_check="publish_commit",
            run_url=mf.workflow_run_url(run_id), detail=f"{type(exc).__name__}: {exc}",
        )
        record_failure(rec)
        summary.update({
            "verification_id": manifest["verification_id"],
            "status": "FAILED",
            "classification": cls,
            "failed_check": "publish_commit",
            "checks": manifest["checks"],
            "notes": [f"PUBLISHING 中断（{type(exc).__name__}: {exc}）→ 未提交 current.json，"
                      f"可经 scripts/verify/publish.py --vid {manifest['verification_id']} 补投"],
        })
        _write_state(cfg, vid, manifest, summary)
        return summary
    summary["checks"] = result.checks
    summary["notes"] = result.notes
    summary["verification_id"] = manifest["verification_id"]
    summary["candidate"] = result.candidate
    summary["status"] = ("CANDIDATE_READY" if result.candidate_ready else
                         "PUBLISHED" if result.current_written else "FAILED")
    if result.candidate_ready:
        _write_state(cfg, vid, manifest, summary)
        return summary
    if result.current_written:
        # state-machine §7：发布成功即关闭该版本的历史失败台账
        resolve_failures(app, resolved.app_version, manifest["verification_id"])
        dispatch_site(cfg, manifest)
    else:
        rec = FailureRecord(
            app=app, app_version=resolved.app_version, verification_id=manifest["verification_id"],
            classification="TRANSIENT", failed_stage="PUBLISHING", failed_check="publish_commit",
            run_url=mf.workflow_run_url(run_id), detail="\n".join(result.notes),
        )
        record_failure(rec)
        summary["classification"] = rec.classification
    _write_state(cfg, vid, manifest, summary)
    return summary


def _verify(
    spec: AppSpec, root: Path, env: runtime.Env, image, shots_dir: Path,
    resolved: resolver.ResolvedVersion, outcome: mf.VerifyOutcome, cfg: Config, skip_tests: bool,
) -> None:
    checks = outcome.checks
    if not docker_available():
        raise StageError("VERIFYING", "compose_started", RuntimeError(
            "Docker daemon 不可用。启动 Docker Desktop 后重试（本地端到端依赖它）"))

    up_out = _transient_retry(
        lambda: runtime.up(env, spec, root), stage="VERIFYING", check="compose_started")
    checks["compose_started"] = True
    outcome.log_tail += "\n--- compose up ---\n" + (up_out or "")[-2000:]

    cid = runtime.container_id(env, spec, root)
    state = runtime.container_state(cid)
    outcome.container_state = state
    checks["container_healthy"] = state == "running"

    probe = runtime.wait_ready(env.base_url, spec)
    outcome.probe_detail, outcome.probe_status = probe.detail, probe.status
    checks["health_check_passed"] = probe.ok
    log(f"就绪探测：{'ok' if probe.ok else 'failed'} status={probe.status} attempts={probe.attempts}")

    assertion = runtime.assert_version(
        cid, spec, resolved.app_version,
        base_url=env.base_url, probe_headers=probe.headers,
    ) if cid else runtime.Assertion(False, False, "", "", "容器不存在，无法断言版本")
    outcome.assertion_ok = assertion.ok
    outcome.assertion_actual, outcome.assertion_detail = assertion.actual, assertion.detail
    if assertion.configured and not assertion.ok:
        # "端口可达但版本不对"等于没验证过：断言失败直接击穿健康门禁
        checks["health_check_passed"] = False
        log(f"版本断言失败 → health_check_passed=false：{assertion.detail}")

    if skip_tests:
        checks["tests_passed"] = False
        outcome.tests_detail = "skipped by --skip-tests"
    else:
        rc, out = _run_pytest(spec, root, env, cfg)
        checks["tests_passed"] = rc == 0
        outcome.tests_detail = f"pytest exit={rc}\n" + out[-4000:]
        if rc not in (0, 1):  # 2=usage error, 3=internal, 4=usage -> not a test verdict
            raise StageError("VERIFYING", "tests_passed", RuntimeError(f"pytest 未能正常执行 exit={rc}"))

    try:
        shots = screenshots.capture(spec, root, env.base_url, shots_dir,
                                    int(cfg.run_opts.get("playwright_timeout_seconds", 120)) * 1000)
        outcome.screenshots = shots
        checks["screenshots_generated"] = len(shots) == len(spec.scenarios) and bool(shots)
    except Exception as exc:  # noqa: BLE001
        checks["screenshots_generated"] = False
        outcome.tests_detail += f"\n截图阶段异常：{type(exc).__name__}: {exc}"

    outcome.log_tail += "\n--- container logs ---\n" + runtime.logs(env, spec, root)


def _run_pytest(spec: AppSpec, root: Path, env: runtime.Env, cfg: Config) -> tuple[int, str]:
    tests_dir = root / spec.g("tests.predefined_dir")
    proc = sh(
        [sys.executable, "-m", "pytest", str(tests_dir), "-q", "--no-header", "-rA",
         f"--timeout={int(cfg.run_opts.get('tests_timeout_seconds', 600))}"],
        cwd=root, env=env.values,
        timeout=int(cfg.run_opts.get("tests_timeout_seconds", 600)) + 60,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def dispatch_site(cfg: Config, manifest: dict[str, Any]) -> None:
    """repository_dispatch(verified-update) — a hint only; Repo A reads the data itself."""
    token = os.environ.get("REPO_A_PAT") or ""
    if not (cfg.site_repo and token):
        log("未配置 SITE_REPO / REPO_A_PAT → 跳过 dispatch（引导期需手动重建 Repo A）")
        return
    payload = {
        "event_type": "verified-update",
        "client_payload": {
            "apps": [manifest["app"]],
            "verification_id": manifest["verification_id"],
            "app_version": manifest["app_version"],
        },
    }
    try:
        status, _, body = http_request(
            f"https://api.github.com/repos/{cfg.site_repo}/dispatches",
            method="POST",
            headers=github_headers(token),
            data=payload,
        )
        log(f"dispatch verified-update → {cfg.site_repo} (HTTP {status})"
            + ("" if 200 <= status < 300 else f" 失败：{body[:300]!r}"))
    except Exception as exc:  # noqa: BLE001
        log(f"dispatch 失败（不影响已提交的 current.json）：{exc}")


def _write_state(cfg: Config, vid: str, manifest: dict[str, Any], summary: dict[str, Any]) -> None:
    p = cfg.output_dir / "runs" / vid / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"summary": summary, "manifest": manifest}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    log(f"运行状态：{p}")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="CoreNova Application Verification")
    ap.add_argument("--app", required=True)
    ap.add_argument("--version", help="指定 app_version（默认取上游最新 release）")
    ap.add_argument("--no-publish", action="store_true", help="只验证不发布（VERIFIED 即止）")
    ap.add_argument("--force", action="store_true", help="绕过版本覆盖保护（人工授权）")
    ap.add_argument("--skip-ami-drift", action="store_true", help="跳过公开 AMI 漂移检测（无 AWS 凭据时）")
    ap.add_argument("--host-port", type=int)
    ap.add_argument("--skip-tests", action="store_true", help="调试用：跳过 pytest（门禁必然不过）")
    args = ap.parse_args()

    try:
        summary = run_verification(
            args.app, version=args.version, do_publish=not args.no_publish, force=args.force,
            skip_ami_drift=args.skip_ami_drift, host_port=args.host_port, skip_tests=args.skip_tests,
        )
    except StageError as exc:
        cls = classify(exc.stage, exc.check, exc.err)
        log(f"FAILED ({cls}) {exc}")
        # 只信已解析的上下文，不把 CLI --version 当作解析结果。
        fallback_id = (sanitize_for_id(exc.app_version) if exc.app_version
                       else f"unknown-{exc.check}")
        record_failure(FailureRecord(
            app=args.app, app_version=exc.app_version or "unknown",
            verification_id=exc.verification_id or f"pre-verification-{args.app}-{fallback_id}",
            classification=cls, failed_stage=exc.stage, failed_check=exc.check, detail=str(exc.err),
            run_url=mf.workflow_run_url(os.environ.get("GITHUB_RUN_ID", "")),
        ))
        print(json.dumps({"status": "FAILED", "classification": cls, "stage": exc.stage,
                          "check": exc.check, "detail": str(exc.err)[:2000]}, ensure_ascii=False))
        raise SystemExit(2) from exc
    except FileNotFoundError as exc:
        die(str(exc))

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # Exact current invocation output, never infer a dispatch target from mtime/latest.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"status={summary['status']}\n")
            fh.write("candidate=" + json.dumps(summary.get("candidate", {}), separators=(",", ":")) + "\n")
    if summary["status"] not in ("PUBLISHED", "VERIFIED", "CANDIDATE_READY"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
