"""Verification Manifest construction + the `website` projection (verification-manifest.md §3/§4).

The generator is the only place allowed to materialise these fields; the projection parity
rules (§4) are asserted here *and* re-checked in tests, because `current.json` is literally
`manifest["website"]` and any drift becomes a second source of truth on the website.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import usertemplate
from .appspec import AppSpec
from .resolver import ResolvedImage, ResolvedVersion
from .template_publish import public_template_url
from .util import file_sha, git_revision, sanitize_for_id, utcnow

IDENTITY_IN_WEBSITE = (
    "app",
    "app_version",
    "verification_id",
    "verified_at",
    "platform_verification_id",
    "ami_id",
    "region",
    "architecture",
)

CHECKS = (
    "compose_started",
    "container_healthy",
    "health_check_passed",
    "tests_passed",
    "screenshots_generated",
    "screenshots_uploaded",
    "report_uploaded",
    "verification_manifest_uploaded",
    "required_platform_contract_valid",
)


@dataclass
class VerifyOutcome:
    """Everything VERIFYING learned, in one place — the Manifest is rendered from this."""

    checks: dict[str, bool] = field(default_factory=dict)
    verification: dict[str, str] = field(default_factory=dict)
    probe_detail: str = ""
    probe_status: int | None = None
    assertion_ok: bool = True
    assertion_actual: str = ""
    assertion_detail: str = ""
    tests_detail: str = ""
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    container_state: str = ""
    log_tail: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0


def verification_id(app: str, app_version: str, seq: int = 1, when: str | None = None) -> str:
    day = (when or utcnow())[:10].replace("-", "")
    # 契约 §2：vid 只含 [a-z0-9._-]。isalnum() 会放行大写与 Unicode 字母，必须用统一清洗。
    return f"{app}-{sanitize_for_id(app_version)}-{day}-{seq:03d}"


def build(
    spec: AppSpec,
    root: Path,
    resolved: ResolvedVersion,
    image: ResolvedImage,
    platform: dict[str, Any],
    outcome: VerifyOutcome,
    cfg,
    run_id: str,
    verified_at: str | None = None,
    vid: str | None = None,
) -> dict[str, Any]:
    verified_at = verified_at or utcnow()
    vid = vid or verification_id(spec.name, resolved.app_version)
    instance_type, data_volume_gb = spec.resources()
    persistence = spec.g("deployment.persistence")
    if persistence == "none":
        data_volume_gb = 0  # 无数据卷不改变实例尺寸阶梯或资源地板校验
    region = str(platform.get("region") or cfg.region)

    # 模板-证据绑定（deployment-contract.md §2.4）：记录验证时用户模板的内容 SHA。
    # 模板由 publish-template.yml 单对象覆盖发布（无版本号对象键），时间戳无法
    # 回答“这份证据对应的模板是哪版”，内容哈希可以；current.json 随证据发布后，
    # 模板与证据是否匹配可查，新验证自动绑定新 revision。模板源缺失时省略键
    # （与其他可选投影同规则；真实流水线的仓库 root 必有 templates/）。
    tpl_revision = usertemplate.revision(root) if usertemplate.fixed_dir(root).is_dir() else ""
    tpl_bucket = getattr(cfg, "template_bucket", "")
    tpl_url = (
        public_template_url(tpl_bucket, cfg.template_s3_region)
        if tpl_bucket and tpl_revision else ""
    )

    shots = [
        {
            "scenario": s["slug"],
            "file": s["file"],
            "url": screenshot_url(cfg, spec.name, resolved.app_version, s["file"]),
            "caption": s["caption"],
        }
        for s in outcome.screenshots
    ]
    order = [s["slug"] for s in outcome.screenshots]

    website: dict[str, Any] = {
        "app": spec.name,
        "app_version": resolved.app_version,
        "verification_id": vid,
        "verification_run_id": str(run_id),
        "platform_verification_id": platform.get("platform_verification_id", ""),
        "ami_id": platform.get("ami_id", ""),
        "architecture": platform.get("architecture", cfg.architecture),
        "region": region,
        "display_name": {
            "en": spec.g("app.i18n.en.display_name"),
            "zh": spec.g("app.i18n.zh.display_name"),
        },
        "description": {
            "en": spec.g("app.i18n.en.description"),
            "zh": spec.g("app.i18n.zh.description"),
        },
        "category": spec.g("app.category"),
        "icon": spec.g("app.icon"),
        "featured": bool(spec.g("website.featured")),
        "tags": spec.g("website.tags") or [],
        "features": spec.g("website.features") or [],
        "health": outcome.verification.get("application", "failed"),
        "status": "verified" if all(outcome.checks.get(c) for c in CHECKS) else "pending",
        "verified_at": verified_at,
        "report_url": "",  # filled by publish once the object exists
        "workflow_run_url": workflow_run_url(run_id),
        "screenshots_order": order,
        "deploy": {
            "documentation_url": spec.g("deployment.documentation_url", ""),
            "regions": spec.g("deployment.regions") or [region],
            "instance_type": instance_type,
            "data_volume_gb": data_volume_gb,
            "container_port": spec.container_port,
            "health_check_path": spec.g("health_check.endpoint", "/"),
            "docker_image": image.image_ref,
            "extra_environment": spec.g("deploy.extra_environment") or [],
            # 深链模板与证据的绑定：官网展示“验证所用模板”，用户可核对深链
            # templateURL 内容与验证证据的对应关系（部署不猜，对得上）。
            **({"template": {"url": tpl_url, "revision": tpl_revision}} if tpl_revision else {}),
        },
        "release": {
            "type": resolved.release_type,
            "previous_version": resolved.previous_version or "",
            "type_evidence": resolved.type_evidence,
        },
        "screenshots": shots,
    }

    # 部署后指引（app-schema.md 规则17）：应用注册了才投影；无后台/旧记录省略键，前端按可选渲染
    post_deploy = spec.g("deployment.post_deploy")
    if isinstance(post_deploy, dict) and post_deploy:
        website["deploy"]["post_deploy"] = post_deploy

    # 月成本估算（app-schema.md 规则18）：同为可选展示字段，注册了才投影
    cost = spec.g("deployment.cost_estimate")
    if isinstance(cost, dict) and cost:
        website["deploy"]["cost_estimate"] = cost

    # 只投影显式声明；旧应用缺少 data_path 不等于无状态。
    if persistence in ("none", "volume"):
        website["deploy"]["persistence"] = persistence

    # 容器内数据路径（app-schema.md 规则19）：none 不投影挂载目录。
    dp = spec.g("deployment.data_path")
    if persistence != "none" and isinstance(dp, str) and dp:
        website["deploy"]["data_path"] = dp

    # CloudFormation 在实例启动后才知道 PublicDnsName；应用只声明接收该地址的
    # 原生环境变量名（例如 Ghost 的 `url`），通用模板不猜应用私有配置。
    app_url_env_name = spec.g("deployment.app_url_env_name")
    if isinstance(app_url_env_name, str) and app_url_env_name:
        website["deploy"]["app_url_env_name"] = app_url_env_name

    # 运维性部署暂停（app-schema.md 规则21）：注册了才投影。hold 独立于验证结果，
    # 官网以 deploy.hold 渲染暂停原因并拦截部署入口（“已验证”≠“当前可部署”）。
    hold = spec.g("deployment.hold")
    if isinstance(hold, dict) and isinstance(hold.get("reason"), dict) and hold["reason"].get("en"):
        website["deploy"]["hold"] = {"reason": hold["reason"]}

    # L1.5 生产核对声明（app-schema.md 规则22）：注册了才投影，无声明时省略键
    # （同 template_revision 的省略规则）。官网据此解释“何时解除暂停”，核对闭环
    # （production-verify.yml）也以这份清单为门禁输入。
    contract = spec.g("deployment.production_contract")
    if isinstance(contract, dict) and isinstance(contract.get("checks"), list) and contract["checks"]:
        website["deploy"]["production_contract"] = {"checks": [str(c) for c in contract["checks"]]}

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "verification_id": vid,
        "app": spec.name,
        "app_version": resolved.app_version,
        "release": {
            "source_repo": spec.source_repo,
            "source_revision": resolved.source_revision,
            "release_tag": resolved.release_tag,
            "upstream_tag": resolved.release_tag,
            "image_reference": image.image_ref,
        },
        "container": {
            "image": image.image_ref,
            "digest": image.digest,
            "manifest_digest": image.manifest_digest,
            "platform": f"linux/{cfg_run_arch()}",
        },
        "platform": {
            "platform_verification_id": platform.get("platform_verification_id", ""),
            "ami_id": platform.get("ami_id", ""),
            "base_ami_source": platform.get("base_ami_source", cfg.base_ami_source),
            "source_ami_name": platform.get("source_ami_name", ""),
            "region": region,
            "architecture": platform.get("architecture", cfg.architecture),
        },
        "config": {
            "app_config_revision": file_sha(spec.path),
            "compose_revision": file_sha(root / spec.g("deploy.compose_file")),
            "tests_revision": git_revision(root / spec.g("tests.predefined_dir"), root),
            **({"template_revision": tpl_revision} if tpl_revision else {}),
        },
        "verification": {
            "application": outcome.verification.get("application", "failed"),
            "platform": outcome.verification.get("platform", "referenced"),
            "tests": outcome.verification.get("tests", "failed"),
        },
        "checks": {c: bool(outcome.checks.get(c)) for c in CHECKS},
        "artifacts": {
            "screenshots": shots,
            "report_url": "",
            "workflow_run_url": website["workflow_run_url"],
        },
        "website": website,
        "verification_run_id": str(run_id),
        "verified_at": verified_at,
        # non-contract scratch, stripped before upload
        "_strategy": spec.version_strategy,
        "_evidence": {
            "health_probe": {"status": outcome.probe_status, "detail": outcome.probe_detail},
            "version_assertion": {
                "ok": outcome.assertion_ok,
                "actual": outcome.assertion_actual,
                "detail": outcome.assertion_detail,
            },
            "tests": outcome.tests_detail,
            "container_state": outcome.container_state,
            "started_at": outcome.started_at,
            "finished_at": outcome.finished_at,
            "duration_s": outcome.duration_s,
            "registry_host": image.host,
            "pull_ref": image.pull_ref,
        },
    }
    assert_projection(manifest)
    return manifest


def cfg_run_arch() -> str:
    return "amd64"  # v1: x86_64 only (platform-contract.md §7)


def workflow_run_url(run_id: str) -> str:
    import os

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not (repo and run_id and run_id.isdigit()):
        return ""
    return f"https://github.com/{repo}/actions/runs/{run_id}"


def assert_projection(manifest: dict[str, Any]) -> None:
    """§4: website identity fields must equal their source section — no independent copies."""
    w, top = manifest["website"], manifest
    for k in ("app", "app_version", "verification_id", "verified_at", "verification_run_id"):
        if w.get(k) != top.get(k):
            raise AssertionError(f"投影漂移：website.{k}={w.get(k)!r} != 顶层 {top.get(k)!r}")
    plat = manifest.get("platform") or {}
    for k in ("platform_verification_id", "ami_id", "region", "architecture"):
        if w.get(k) != plat.get(k):
            raise AssertionError(f"投影漂移：website.{k}={w.get(k)!r} != platform.{k}={plat.get(k)!r}")
    if w["region"] not in (w["deploy"].get("regions") or []):
        raise AssertionError("website.deploy.regions 必须包含本次验证所用 region")
    if [s["scenario"] for s in manifest["artifacts"]["screenshots"]] != w["screenshots_order"]:
        raise AssertionError("screenshots_order 与 artifacts.screenshots 顺序不一致（app-schema §5 规则8）")
    if {s["scenario"] for s in w["screenshots"]} != {s["scenario"] for s in manifest["artifacts"]["screenshots"]}:
        raise AssertionError("website.screenshots 与 artifacts.screenshots 场景集合不一致")
    if w["health"] != manifest["verification"]["application"]:
        raise AssertionError("website.health 必须等于 verification.application 的投影")
    if w["deploy"]["docker_image"] != manifest["container"]["image"]:
        raise AssertionError("website.deploy.docker_image 必须等于 container.image")
    if (w["deploy"].get("template") or {}).get("revision") != manifest["config"].get("template_revision"):
        raise AssertionError("website.deploy.template.revision 必须等于 config.template_revision（或同时省略）")
    # production_contract 只允许这一种形状（或省略键）：current.json 是展示与
    # 核对门禁的共同事实源，出现第二种形状即说明有人绕过了投影器。
    pc = w["deploy"].get("production_contract")
    if pc is not None and (
        not isinstance(pc, dict) or set(pc) != {"checks"}
        or not isinstance(pc["checks"], list) or not pc["checks"]
        or not all(isinstance(c, str) and c for c in pc["checks"])
    ):
        raise AssertionError("website.deploy.production_contract 必须为 {checks: [非空字符串…]}（未声明时省略键）")


def screenshot_key(app: str, app_version: str, filename: str) -> str:
    """版本隔离的对象键（repo-structure.md §4.2）：同名场景跨版本不串图。
    每个片段都经统一清洗：app_version 来自上游 tag，不得原样进键。"""
    return f"screenshots/{sanitize_for_id(app)}/{sanitize_for_id(app_version)}/{sanitize_for_id(filename)}"


def screenshot_url(cfg, app: str, app_version: str, filename: str) -> str:
    key = screenshot_key(app, app_version, filename)
    base = (cfg.r2_public_base_url or "").rstrip("/")
    if cfg.verified_backend == "r2" and not base:
        raise RuntimeError("r2 后端必须配置 R2_PUBLIC_BASE_URL，否则网站截图无法访问")
    return f"{base}/{key}" if base else f"/{key}"


def report_url(cfg, verification_id: str) -> str:
    base = (cfg.r2_public_base_url or "").rstrip("/")
    key = f"reports/{verification_id}.html"
    return f"{base}/{key}" if base else f"/{key}"
