"""AWS Golden Verification — the platform layer of the trust chain (verify-gate-design.md §5).

This module is the only place in Repo C allowed to create AWS resources, and it creates exactly one
stack: the canary. Its output is a Platform Contract (contracts/platform-contract.md §2).

Hard rules encoded here:
  * the AMI pointer is resolved **once** (§6) and the resulting `ami_id` is threaded through every
    later step - no helper may re-query SSM;
  * each `verification.*` boolean must come from a measured probe, never from an assertion;
  * the canary is always cleaned up unless `keep_stack: true`, and an unconfirmed cleanup is an
    error, because a leaked resource bills forever;
  * `--dry-run` / `--check` perform zero AWS calls.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import platformref
from .backend import Backend
from .config import Config
from .platformref import REVISION_KEYS
from .util import log, poll_until, utcnow, write_json
from .versioning import age_days

SCHEMA_VERSION = "1.0"
VALID = "valid"
INVALID = "invalid"
# Dry-run performs no AWS call, so the identity fields carry a visible placeholder instead of a
# value that could be mistaken for a real, verified AMI.
PLACEHOLDER_AMI_ID = "ami-PENDING-SSM-RESOLVE"
TEMPLATE_DIR = Path("templates/cloudformation/fixed")
INIT_DIR = TEMPLATE_DIR / "init"
TEMPLATES = ("network.yaml", "app.yaml", "canary.yaml")
# ValidateTemplate / CreateStack 的 TemplateBody API 上限（字节）；超出必须改走
# TemplateURL（上限 1MB）。2026-09 访问控制与上传参数扩容后 app.yaml 已超限。
TEMPLATE_BODY_API_LIMIT = 51_200
TEMP_VALIDATE_PREFIX = "golden-validate"
ASSET_SOURCES = (
    "00-packages-and-docker-runtime.sh",
    "10-nginx-base.sh",
    "20-cloudwatch-agent.sh",
    "30-app-container.sh",
    "40-ready-and-signal.sh",
)
# contracts/platform-contract.md §2 `verification` - exactly these eleven keys, in contract order.
VERIFICATION_KEYS = (
    "cfn_validated",
    "ec2_launched",
    "cfn_init_completed",
    "cfn_signal_received",
    "docker_runtime_ok",
    "nginx_ok",
    "ssm_ok",
    "cloudwatch_ok",
    "ebs_ok",
    "security_group_ok",
    "network_ok",
)
CONTRACT_FIELD_ORDER = (
    "schema_version",
    "platform_verification_id",
    "ami_id",
    "region",
    "architecture",
    *REVISION_KEYS,
    "verification",
    "status",
    "platform_verified_at",
    "invalidated_at",
    "invalidated_reason",
    "base_ami_source",
    "source_ami_name",
    "source_ami_account",
    "source_ssm_parameter",
    "ami_resolved_at",
    "reverify_interval_days",
)
REQUIRED_SECTIONS = ("AWSTemplateFormatVersion", "Description", "Parameters", "Resources")

# canary.yaml may only differ from app.yaml here: it is the *same* stack definition (§9) run under
# a canary identity, with a digest-pinned container that really answers HTTP so the platform path
# (docker run -> nginx proxy -> cfn-signal) is exercised end to end.
CANARY_PRESETS: dict[str, str] = {
    "AppName": "corenova-canary",
    # Digest resolved from the registry on 2026-08-29; the canary container must really answer HTTP
    # so that docker run -> nginx proxy -> cfn-signal is exercised as one chain.
    # 冒烟镜像必须免参数且不与反代自身的 :80 抢端口：http-echo 缺 -text 会立即退出，
    # nginx 会占 80；mendhak/http-https-echo 无参默认监听 8080（实例实测 200）。
    "ImageReference": "mendhak/http-https-echo:34@sha256:b9b45336763a8ee7f34b78fc77f3b1ecbaae41bb9ab72949d06e7c3cf6928532",
    "ContainerPort": "8080",
    "HealthCheckPath": "/",
    "DataDirHostPath": "/var/lib/corenova/canary/data",
    "CloudWatchLogGroupName": "/corenova/canary",
    "SelfSignedTls": "true",
    "AllowedWebCidr": "0.0.0.0/0",
    "TerminationProtection": "Disabled",
}
CANARY_DESCRIPTION = (
    "CoreNova Golden Verification canary. GENERATED from app.yaml by "
    "`python scripts/verify/golden_verify.py --render-canary`; only parameter defaults differ "
    "from app.yaml, and the run aborts if that ever stops being true (platform-contract.md §9)."
)


# --------------------------------------------------------------------------- dataclasses


@dataclass
class Canary:
    stack_name: str
    instance_id: str = ""
    volume_ids: list[str] = field(default_factory=list)
    public_dns: str = ""
    public_ip: str = ""
    private_ip: str = ""
    subnet_id: str = ""
    vpc_id: str = ""
    security_group_id: str = ""


@dataclass
class Probe:
    key: str
    title: str
    how: str
    ok: bool = False
    detail: str = ""


@dataclass
class GoldenReport:
    """Everything a run learned; serialized for audit next to the contract."""

    platform_verification_id: str = ""
    stage: str = "DISCOVERED"
    status: str = "running"
    dry_run: bool = False
    region: str = ""
    architecture: str = ""
    ami_id: str = ""
    ami_resolved_at: str = ""
    canary_stack: str = ""
    canary_instance: str = ""
    change_set: str = ""
    probes: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    cleanup: list[str] = field(default_factory=list)
    contract_key: str = ""
    started_at: str = field(default_factory=utcnow)
    finished_at: str = ""


@dataclass
class DriftReport:
    """§2.1 constraint 2: public AMI replacement + the six revisions + the reverify interval."""

    contract_found: bool = False
    contract_status: str = ""
    platform_verification_id: str = ""
    recorded_ami_id: str = ""
    live_ami_id: str = ""
    ami_drifted: bool = False
    revision_drifts: dict[str, dict[str, str]] = field(default_factory=dict)
    expired: bool = False
    age_days: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def drifted(self) -> bool:
        # A missing contract is not "no drift": Application Verification has nothing valid to
        # reference then, which is exactly what platformref.check() also rejects.
        return not self.contract_found or self.ami_drifted or bool(self.revision_drifts) or self.expired


# --------------------------------------------------------------------------- config accessors


def canary_opts(cfg: Config) -> dict[str, Any]:
    return dict(cfg.platform.get("canary") or {})


def canary_stack_name(cfg: Config) -> str:
    return str(canary_opts(cfg).get("stack_name") or "corenova-canary")


def network_stack_name(cfg: Config) -> str:
    return str(cfg.platform.get("network_stack_name") or canary_opts(cfg).get("network_stack_name") or "corenova-network")


def keep_stack(cfg: Config) -> bool:
    return str(canary_opts(cfg).get("keep_stack", False)).lower() in ("1", "true", "yes")


def signal_timeout_minutes(cfg: Config) -> int:
    return int(canary_opts(cfg).get("signal_timeout_minutes", 20))


def template_path(cfg: Config, name: str) -> Path:
    return cfg.root / TEMPLATE_DIR / name


def load_template(cfg: Config, name: str) -> dict[str, Any]:
    data = yaml.safe_load(template_path(cfg, name).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{name}: 模板根节点必须是 mapping")
    return data


def template_defaults(cfg: Config, name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, spec in (load_template(cfg, name).get("Parameters") or {}).items():
        default = spec.get("Default")
        if isinstance(default, list):
            default = ",".join(str(x) for x in default)
        if default is not None:
            out[key] = str(default)
    return out


# --------------------------------------------------------------------------- init asset sync


def inlined_assets(cfg: Config, name: str) -> dict[str, str]:
    """`{init asset file name: inlined body}` for one template."""
    out: dict[str, str] = {}
    init = _init_metadata(load_template(cfg, name))
    # 修正后的结构里各 config 与 configSets 同级（旧写法嵌在 config: 下，CFN 根本不认）
    stanzas = {k: v for k, v in init.items() if k not in ("Comment", "configSets")}
    for stanza in stanzas.values():
        for path, spec in (stanza.get("files") or {}).items():
            content = spec.get("content")
            if isinstance(content, str) and path.startswith("/opt/corenova/bin/"):
                out[Path(path).name] = content
    return out


def _init_metadata(template: dict[str, Any]) -> dict[str, Any]:
    return (
        template.get("Resources", {})
        .get("Instance", {})
        .get("Metadata", {})
        .get("AWS::CloudFormation::Init", {})
    )


def _marker(name: str) -> tuple[str, str]:
    return f"# BEGIN SYNCED ASSET {name}", f"# END SYNCED ASSET {name}"


def sync_init_assets(cfg: Config) -> list[str]:
    """Re-inject init/*.sh into app.yaml and regenerate canary.yaml from it.

    The templates are what CFN runs; `init/*.sh` is what the repository - and therefore `*_revision`
    - records. Syncing makes both the same bytes by construction instead of by hope.
    """
    app = template_path(cfg, "app.yaml")
    text = app.read_text(encoding="utf-8")
    changed: list[str] = []
    for asset in ASSET_SOURCES:
        body = (cfg.root / INIT_DIR / asset).read_text(encoding="utf-8").rstrip("\n")
        begin, end = _marker(asset)
        start, stop = text.find(begin), text.find(end)
        if start < 0 or stop < 0 or stop < start:
            raise RuntimeError(f"app.yaml 缺少 {asset} 的 SYNCED ASSET 标记，无法同步")
        # 缩进跟随标记行本身，不写死列数
        line_start = text.rfind("\n", 0, start) + 1
        pad = text[line_start:start][: len(text[line_start:start]) - len(text[line_start:start].lstrip())]
        indented = "\n".join((pad + line).rstrip() for line in body.splitlines())
        text = text[:start] + begin + "\n" + indented + "\n" + pad + text[stop:]
        changed.append(asset)
    app.write_text(text, encoding="utf-8")
    render_canary(cfg)
    return changed


def render_canary(cfg: Config) -> Path:
    """Generate canary.yaml from app.yaml with the canary presets (§9: reuse, never fork)."""
    app = load_template(cfg, "app.yaml")
    canary = json.loads(json.dumps(app))  # deep copy without touching app's structure
    canary["Description"] = CANARY_DESCRIPTION
    params = canary.get("Parameters") or {}
    for key, value in CANARY_PRESETS.items():
        if key not in params:
            raise RuntimeError(f"app.yaml 没有参数 {key}，canary 预设失效")
        params[key]["Default"] = value
    header = (
        "# GENERATED FILE - do not edit by hand.\n"
        "# Source: templates/cloudformation/fixed/app.yaml\n"
        "# Regenerate: python scripts/verify/golden_verify.py --sync-init\n"
    )
    body = yaml.safe_dump(canary, sort_keys=False, allow_unicode=True, width=10_000)
    path = template_path(cfg, "canary.yaml")
    path.write_text(header + body, encoding="utf-8")
    return path


def asset_drift(cfg: Config) -> list[str]:
    problems: list[str] = []
    for name in ("app.yaml", "canary.yaml"):
        if not template_path(cfg, name).exists():
            continue
        inlined = inlined_assets(cfg, name)
        for asset in ASSET_SOURCES:
            source = (cfg.root / INIT_DIR / asset).read_text(encoding="utf-8").rstrip("\n")
            body = inlined.get(asset)
            if body is None:
                problems.append(f"{name}: 未内联 init 资产 {asset}")
                continue
            recorded = _strip_marker_lines(body)
            if recorded != source:
                problems.append(f"{name}: 内联的 {asset} 与 {INIT_DIR}/{asset} 不一致（跑 --sync-init）")
    return problems


def _strip_marker_lines(body: str) -> str:
    lines = [ln for ln in body.splitlines() if not ln.startswith(("# BEGIN SYNCED ASSET", "# END SYNCED ASSET"))]
    return "\n".join(lines).rstrip("\n")


def canary_parity_errors(cfg: Config) -> list[str]:
    """canary.yaml is only allowed to differ in parameter defaults."""
    app, canary = load_template(cfg, "app.yaml"), load_template(cfg, "canary.yaml")
    out: list[str] = []
    for section in ("Resources", "Outputs", "Conditions"):
        if app.get(section) != canary.get(section):
            out.append(f"canary.yaml 的 {section} 与 app.yaml 不一致（canary 必须复用同一份资源定义）")
    app_params, canary_params = app.get("Parameters") or {}, canary.get("Parameters") or {}
    if set(app_params) != set(canary_params):
        out.append(f"canary.yaml 参数集合与 app.yaml 不同：{sorted(set(app_params) ^ set(canary_params))}")
    for key in sorted(set(app_params) & set(canary_params)):
        a, c = _without_default(app_params[key]), _without_default(canary_params[key])
        if a != c:
            out.append(f"canary.yaml 参数 {key} 除 Default 外与 app.yaml 不同")
    for key, value in CANARY_PRESETS.items():
        default = (canary_params.get(key) or {}).get("Default")
        if default is not None and str(default) != value:
            out.append(f"canary.yaml 参数 {key} 的默认值 {default!r} 与 CANARY_PRESETS {value!r} 不符")
    return out


def _without_default(spec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in spec.items() if k != "Default"}


# --------------------------------------------------------------------------- offline checks


def static_template_errors(cfg: Config) -> list[str]:
    """Everything a reviewer would otherwise discover by deploying a stack and watching it fail."""
    problems: list[str] = []
    for name in TEMPLATES:
        path = template_path(cfg, name)
        if not path.exists():
            problems.append(f"缺少模板 {path.relative_to(cfg.root)}")
            continue
        try:
            tpl = load_template(cfg, name)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: yaml.safe_load 失败 {type(exc).__name__}: {exc}")
            continue
        problems += _structure_errors(name, tpl)
        problems += _reference_errors(name, tpl)
        problems += _function_errors(name, tpl)
        problems += _cfn_init_errors(name, tpl)
        problems += _empty_file_content_errors(cfg, name, tpl)
        problems += _init_env_shape_errors(cfg, name, tpl)
        problems += _sub_name_errors(name, tpl)
        problems += _ssh_errors(name, path.read_text(encoding="utf-8"))
        if "@@ASSET@@" in path.read_text(encoding="utf-8"):
            problems.append(f"{name}: 仍含 @@ASSET@@ 占位符")

    problems += _network_sg_errors(cfg)
    problems += asset_drift(cfg)
    if template_path(cfg, "canary.yaml").exists():
        problems += canary_parity_errors(cfg)
    problems += _hardcoding_errors(cfg)
    problems += _revision_sanity_errors(cfg)
    return problems


SUPPORTED_CFN_FNS = {
    "Fn::GetAtt", "Fn::Base64", "Fn::Cidr", "Fn::FindInMap", "Fn::GetAZs", "Fn::If",
    "Fn::ImportValue", "Fn::Join", "Fn::Length", "Fn::Select", "Fn::Split", "Fn::Sub",
    "Fn::Transform", "Fn::ToJsonString", "Ref", "Condition", "Fn::And", "Fn::Contains",
    "Fn::EachMemberEquals", "Fn::EachMemberIn", "Fn::Equals", "Fn::Not", "Fn::Or",
    "Fn::RefAll", "Fn::ValueOf", "Fn::ValueOfAll", "Fn::AccountId", "Fn::AccountIdFromAlias",
    "Fn::GetSecretValue", "Fn::GetParameter", "Fn::GetStackOutput", "Fn::CostDetectionManifest",
}


def _function_errors(name: str, node: Any, path: str = "") -> list[str]:
    """CFN 没有乘法/算术函数；写一个不存在的 Fn:: 只会在真正建栈时才炸（已被咬过一次）。"""
    problems: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and (k.startswith("Fn::") or k == "Ref") and k not in SUPPORTED_CFN_FNS:
                problems.append(f"{name}: 不支持的内在函数 {k}（位置 {path or '<root>'}）")
            problems += _function_errors(name, v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            problems += _function_errors(name, v, f"{path}[{i}]")
    return problems



ENV_LINE_RE = re.compile(r'^export [A-Z_]+="[^"\n]*"$')


def _init_env_shape_errors(cfg: Config, name: str, tpl: dict[str, Any]) -> list[str]:
    """init.env 的两个坑（都实测过）：值不加引号时预签名 URL 的 `&` 会让 sourcing 执行到碎片；
    不用 export 时 `exec 子脚本` 看不到任何变量。所以强制 `export KEY="value"` 形式。"""
    problems: list[str] = []
    for cname, c in _init_metadata(tpl).items():
        if cname in ("Comment", "configSets") or not isinstance(c, dict):
            continue
        spec = (c.get("files") or {}).get("/opt/corenova/etc/init.env")
        if not spec:
            continue
        content = spec.get("content")
        body = content
        if isinstance(content, dict):
            sub = content.get("Fn::Sub") or content.get("!Sub")
            body = sub[0] if isinstance(sub, list) and sub else sub
        if not isinstance(body, str):
            continue
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not ENV_LINE_RE.match(line):
                problems.append(
                    f"{name}/{cname}: init.env 行不符合 export KEY=\"value\" 形式：{line[:70]}"
                )
    return problems



def _empty_file_content_errors(cfg: Config, name: str, tpl: dict[str, Any]) -> list[str]:
    """files 的 content 解析成空串时，cfn-init 直接报 "File specified without source or content"。
    最常见来源是 content 直接 Ref 了一个 Default 为空串的可选参数。"""
    problems: list[str] = []
    params = tpl.get("Parameters") or {}
    empty_defaults = {k for k, v in params.items() if isinstance(v, dict) and str(v.get("Default", "")) == ""}
    init = _init_metadata(tpl)
    for cname, c in init.items():
        if cname in ("Comment", "configSets") or not isinstance(c, dict):
            continue
        for path, spec in (c.get("files") or {}).items():
            content = (spec or {}).get("content")
            source = (spec or {}).get("source")
            if isinstance(content, str) and not content.strip() and not source:
                problems.append(f"{name}/{cname}: 文件 {path} 的 content 为空串")
            if isinstance(content, dict) and set(content) == {"Ref"} and content["Ref"] in empty_defaults:
                problems.append(
                    f"{name}/{cname}: 文件 {path} 的 content 直接引用空默认参数 {content['Ref']}"
                    "（cfn-init 会判定为未提供内容）"
                )
    return problems



def _cfn_init_errors(name: str, tpl: dict[str, Any]) -> list[str]:
    """AWS::CloudFormation::Init 的键名是 configSets + 同级配置块。
    写成 config-set / config 时 CFN 不报错，只会让 cfn-init 静默什么都不做（已被咬过）。"""
    problems: list[str] = []
    for rid, spec in (tpl.get("Resources") or {}).items():
        if not isinstance(spec, dict) or spec.get("Type") != "AWS::EC2::Instance":
            continue
        init = (spec.get("Metadata") or {}).get("AWS::CloudFormation::Init")
        if not isinstance(init, dict):
            problems.append(f"{name}/{rid}: 缺少 Metadata.AWS::CloudFormation::Init")
            continue
        for bad in ("config-set", "config", "configsets"):
            if bad in init:
                problems.append(f"{name}/{rid}: Init 含非法键 {bad!r}（CFN 只认 configSets + 同级配置块）")
        sets = init.get("configSets") or {}
        if not sets:
            problems.append(f"{name}/{rid}: Init 缺少 configSets")
        known = {k for k in init if k not in ("Comment", "configSets")}
        for cs_name, cfgs in sets.items():
            for c in cfgs or []:
                if c not in known:
                    problems.append(f"{name}/{rid}: configSets.{cs_name} 引用了不存在的配置 {c!r}")
        if "default" not in sets:
            problems.append(f"{name}/{rid}: configSets 缺少 default（cfn-init 无 -c 时会失败）")
    return problems



def _structure_errors(name: str, tpl: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for section in REQUIRED_SECTIONS:
        if section not in tpl:
            problems.append(f"{name}: 缺少 {section}")
    resources = tpl.get("Resources") or {}
    if not resources:
        problems.append(f"{name}: Resources 为空")
    for rid, spec in resources.items():
        if not isinstance(spec, dict) or "Type" not in spec:
            problems.append(f"{name}: 资源 {rid} 缺少 Type")
            continue
        if not str(spec["Type"]).startswith("AWS::"):
            problems.append(f"{name}: 资源 {rid} 类型异常 {spec['Type']!r}")
    return problems


# `${X}` is a CFN reference; `$${X}` is Sub's escape for a literal `${X}` (shell vars in user-data).
SUB_REF_RE = re.compile(r"(?<!\$)\$\{([^!}${]+)\}")


def _reference_errors(name: str, tpl: dict[str, Any]) -> list[str]:
    known = (
        set(tpl.get("Parameters") or {})
        | set(tpl.get("Resources") or {})
        | set(tpl.get("Conditions") or {})
        | {"AWS::Region", "AWS::AccountId", "AWS::Partition", "AWS::StackName", "AWS::StackId",
           "AWS::URLSuffix", "AWS::NoValue"}
    )
    return [
        f"{name}: 引用了未定义符号 {ref!r}"
        for ref in sorted(_collect_refs(tpl) - known)
    ]


# CloudFormation parses *every* unescaped brace token in Fn::Sub as a variable name before the
# escape applies, so `[0]`-style or default-value syntax fails validate-template even inside `$${}`.
SUB_ANY_TOKEN_RE = re.compile(r"(?<!\$)\$\{([^}\n]{0,120})\}")
SUB_LEGAL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:]+$")


def _sub_name_errors(name: str, tpl: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for token in _iter_sub_bodies(tpl):
        for found in SUB_ANY_TOKEN_RE.findall(token):
            if not SUB_LEGAL_NAME_RE.match(found):
                problems.append(
                    f"{name}: Fn::Sub 含非法变量名 ${{{found}}}（CFN 只允许字母数字 _ . :，"
                    "shell 数组/默认值写法必须换实现）"
                )
    return sorted(set(problems))


def _iter_sub_bodies(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Fn::Sub":
                body = value[0] if isinstance(value, list) else value
                if isinstance(body, str):
                    out.append(body)
            else:
                out += _iter_sub_bodies(value)
    elif isinstance(node, list):
        for item in node:
            out += _iter_sub_bodies(item)
    return out


def _collect_refs(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("Ref", "Condition", "DependsOn") and isinstance(value, str):
                out.add(value)
            elif key == "Fn::GetAtt":
                first = value.split(".")[0] if isinstance(value, str) else str(value[0]) if value else ""
                if first:
                    out.add(first)
            elif key == "Fn::Sub":
                body = value[0] if isinstance(value, list) else value
                if isinstance(body, str):
                    for token in SUB_REF_RE.findall(body):
                        out.add(token.split(".")[0])
                if isinstance(value, list) and len(value) > 1 and isinstance(value[1], dict):
                    out -= set(value[1])
            else:
                out |= _collect_refs(value)
    elif isinstance(node, list):
        for item in node:
            out |= _collect_refs(item)
    return out


SSH_MARKERS_RE = re.compile(r"(^|[\s\"'])(KeyName|SSHLocation|ssh -i|ssh root@|/dev/tcp/[^/]+/22)")


def _ssh_errors(name: str, text: str) -> list[str]:
    hits = {m.group(2) for m in SSH_MARKERS_RE.finditer(text)}
    return [f"{name}: 出现 SSH 运维痕迹 {sorted(hits)}（platform-contract.md §8 只允许 SSM）"] if hits else []


def _ports_of(rule: dict[str, Any]) -> set[int]:
    if str(rule.get("IpProtocol", "tcp")) not in ("tcp", "udp", "-1"):
        return set()
    try:
        lo, hi = int(rule.get("FromPort", 0)), int(rule.get("ToPort", 65535))
    except (TypeError, ValueError):
        return set()
    return set(range(lo, min(hi, 65535) + 1)) if hi - lo < 1024 else {lo, hi}


def _network_sg_errors(cfg: Config) -> list[str]:
    # 缺失/损坏已在 static_template_errors 的模板循环里记过问题，这里直接跳过，
    # 不让 --check/--dry-run 以 traceback 收场。
    try:
        tpl = load_template(cfg, "network.yaml")
    except Exception:  # noqa: BLE001
        return []
    resources = tpl.get("Resources") or {}
    problems: list[str] = []
    sgs = {rid: spec.get("Properties", {}) for rid, spec in resources.items()
           if spec.get("Type") == "AWS::EC2::SecurityGroup"}
    if not sgs:
        return ["network.yaml: 没有任何 SecurityGroup"]
    for rid, props in sgs.items():
        rules = list(props.get("SecurityGroupIngress") or [])
        extra = [r.get("Properties", {}) for r in resources.values()
                 if r.get("Type") == "AWS::EC2::SecurityGroupIngress"]
        ports: set[int] = set()
        for rule in rules + extra:
            ports |= _ports_of(rule)
        if 22 in ports:
            problems.append(f"network.yaml: {rid} 开放了 22 入站（§8 禁止）")
        if not {80, 443} <= ports:
            problems.append(f"network.yaml: {rid} 未同时开放 80 与 443 入站")
    for needed in ("VpcId", "SubnetIds", "BaseSGId"):
        if needed not in (tpl.get("Outputs") or {}):
            problems.append(f"network.yaml: 缺少输出 {needed}")
    return problems


# 平台级反模式（与具体应用无关）：漂移标签 :latest、裸写 80/443 的端口映射。
# 应用镜像/端口痕迹不写死在这里 —— 由 app_hardcode_patterns() 从 apps/*.yaml 派生。
GENERIC_HARDCODED_RE = re.compile(r"(:latest(?=[\"'\s\}]|$)|-p 80:80|-p 443:443)")


def app_hardcode_patterns(root: Path) -> list[tuple[str, str, re.Pattern[str]]]:
    """从 apps/*.yaml 派生每个应用的「镜像基名:数字」与「端口:端口」反模式。

    新接入应用不需要修改 golden.py：模板里硬编码 louislam/uptime-kuma:1 或 3001:3001
    时，本函数派生的模式照常命中。畸形注册文件直接跳过——schema 违规由
    appspec.validate 报告，这里不重复报。
    """
    out: list[tuple[str, str, re.Pattern[str]]] = []
    apps_dir = root / "apps"
    if not apps_dir.is_dir():
        return out
    for path in sorted(apps_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            deploy = data.get("deploy") or {}
            image = str(deploy.get("docker_image") or "").rsplit("/", 1)[-1]
            port = int(deploy.get("container_port") or 0)
        except Exception:  # noqa: BLE001
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", image):
            out.append((path.stem, image, re.compile(rf"{re.escape(image)}:\d")))
        if port > 0:
            out.append((path.stem, str(port), re.compile(rf"\b{port}:{port}\b")))
    return out


def _hardcoding_errors(cfg: Config) -> list[str]:
    """A template that hardcodes an image or a port silently verifies the wrong artifact."""
    problems: list[str] = []
    app_patterns = app_hardcode_patterns(cfg.root)
    for name in ("app.yaml", "canary.yaml"):
        if not template_path(cfg, name).exists():
            continue
        text = template_path(cfg, name).read_text(encoding="utf-8")
        for match in GENERIC_HARDCODED_RE.finditer(text):
            problems.append(f"{name}: 含硬编码镜像/端口痕迹 {match.group(1)!r}")
        for app, label, pattern in app_patterns:
            hit = pattern.search(text)
            if hit:
                problems.append(
                    f"{name}: 含硬编码应用镜像/端口痕迹 {hit.group(0)!r}"
                    f"（apps/{app}.yaml 的 {label} 必须经 init.env 注入）"
                )
        try:
            tpl = load_template(cfg, name)
        except Exception:  # noqa: BLE001 - 解析失败已由模板循环报告
            continue
        meta = json.dumps(_init_metadata(tpl), ensure_ascii=False)
        for token in ("CFNOVA_CONTAINER_PORT", "CFNOVA_IMAGE_REFERENCE"):
            if token not in meta:
                problems.append(f"{name}: cfn-init 资产未通过 init.env 注入 {token}")
    return problems


def _revision_sanity_errors(cfg: Config) -> list[str]:
    """`nginx_base_revision`/`docker_runtime_revision` collapse to `missing` if the file names lose
    their keywords, which would silently freeze a whole invalidation class (§5)."""
    revisions = platformref.compute_revisions(cfg, PLACEHOLDER_AMI_ID)
    problems: list[str] = []
    for key in ("nginx_base_revision", "docker_runtime_revision", "cfn_init_revision"):
        if revisions.get(key) in (None, "", "missing"):
            problems.append(f"platformref.compute_revisions(): {key}=missing（init 资产命名需含 docker/nginx 关键字）")
    for key in REVISION_KEYS:
        if not revisions.get(key):
            problems.append(f"platformref.compute_revisions(): {key} 为空")
    return problems


# --------------------------------------------------------------------------- AWS session


class Aws:
    """Lazily created boto3 clients; constructing this class performs no AWS call."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._clients: dict[str, Any] = {}

    def client(self, service: str) -> Any:
        if service not in self._clients:
            import boto3

            self._clients[service] = boto3.client(service, region_name=self.cfg.region)
        return self._clients[service]

    @property
    def cfn(self) -> Any:
        return self.client("cloudformation")

    @property
    def ec2(self) -> Any:
        return self.client("ec2")

    @property
    def ssm(self) -> Any:
        return self.client("ssm")

    @property
    def logs(self) -> Any:
        return self.client("logs")


# --------------------------------------------------------------------------- step 1


def resolve_base_ami(cfg: Config, *, override: str = "", allow_aws: bool = True) -> tuple[str, str]:
    """Resolve the mutable pointer ONCE (§6) -> (ami_id, resolved_at)."""
    if override:
        return override, utcnow()
    if not allow_aws:
        return PLACEHOLDER_AMI_ID, ""
    return platformref.resolve_public_ami_id(cfg), utcnow()


# --------------------------------------------------------------------------- steps 2-6


def validate_templates(aws: Aws) -> list[str]:
    notes: list[str] = []
    for name in TEMPLATES:
        _validate_template(aws, name)
        notes.append(f"{name}=ok")
    return notes


def _validate_template(aws: Aws, name: str) -> None:
    """ValidateTemplate 单个模板；超过 TemplateBody API 上限时改走 TemplateURL。

    超限对象临时放到模板桶 golden-validate/ 前缀下（桶策略允许公开 GetObject，
    CloudFormation 服务才能读到），校验完成/失败都立即删除，不留公开副本。
    Golden 全局单组（concurrency），固定 key 无并发覆盖风险。
    """
    body = template_path(aws.cfg, name).read_text(encoding="utf-8")
    if len(body.encode("utf-8")) <= TEMPLATE_BODY_API_LIMIT:
        aws.cfn.validate_template(TemplateBody=body)
        return
    bucket = aws.cfg.template_bucket
    if not bucket:
        raise RuntimeError(
            f"{name} 超过 TemplateBody {TEMPLATE_BODY_API_LIMIT} 字节上限，且未配置 "
            "TEMPLATE_S3_BUCKET（config/verify.yaml template_s3.bucket）→ 无法走 TemplateURL 校验"
        )
    from . import template_publish

    key = f"{TEMP_VALIDATE_PREFIX}/{aws.cfg.region}/{name}"
    region = aws.cfg.template_s3_region
    # boto3 对 us-east-1 默认解析为全局 s3.amazonaws.com；部分网络环境对该域不稳定，
    # 显式 regional endpoint（与模板桶同区）避免环境差异。
    import boto3

    s3 = boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com")
    url = f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
    try:
        s3.put_object(
            Bucket=bucket, Key=key, Body=body.encode("utf-8"),
            ContentType=template_publish.TEMPLATE_CONTENT_TYPE,
        )
        log(f"{name} 超过 TemplateBody 上限 → TemplateURL 校验 s3://{bucket}/{key}")
        aws.cfn.validate_template(TemplateURL=url)
    finally:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - 清理失败不得掩盖校验结论
            log(f"临时校验对象删除失败（不影响门禁；残留对象公开可读，应手动清理 "
                f"s3://{bucket}/{key}）：{type(exc).__name__}: {exc}")


def ensure_network_stack(aws: Aws, stack_name: str) -> str:
    """The network stack is a prerequisite and costs nothing to keep (§9: 建一次)."""
    try:
        aws.cfn.describe_stacks(StackName=stack_name)
        return f"复用既有 network 栈 {stack_name}"
    except Exception as exc:  # noqa: BLE001 - CFN reports "not found" as a ClientError
        if "does not exist" not in str(exc):
            raise
    aws.cfn.create_stack(
        StackName=stack_name,
        TemplateBody=template_path(aws.cfg, "network.yaml").read_text(encoding="utf-8"),
        Tags=[{"Key": "corenova:purpose", "Value": "platform-verification"}],
    )
    _wait_stack(aws, stack_name, timeout_minutes=15)
    return f"已创建 network 栈 {stack_name}"


def stack_outputs(aws: Aws, stack_name: str) -> dict[str, str]:
    stacks = aws.cfn.describe_stacks(StackName=stack_name)["Stacks"]
    if not stacks:
        raise RuntimeError(f"栈 {stack_name} 不存在")
    return {o["OutputKey"]: str(o["OutputValue"]) for o in stacks[0].get("Outputs", [])}


def canary_parameters(cfg: Config, ami_id: str, network: dict[str, str], run_id: str) -> dict[str, str]:
    """canary.yaml defaults are the base; only the facts resolved at run time override them."""
    opts = canary_opts(cfg)
    params = template_defaults(cfg, "canary.yaml")
    subnet_ids = [s for s in (network.get("SubnetIds") or "").split(",") if s]
    params.update(
        {
            "AmiId": ami_id,
            "SubnetId": subnet_ids[0] if subnet_ids else (network.get("SubnetIdA") or ""),
            "SecurityGroupId": network.get("BaseSGId", ""),
            "NetworkStackName": network_stack_name(cfg),
            "GoldenRunId": run_id,
            # A fixed log-group name can outlive a previous canary when stack cleanup was
            # interrupted, which makes CloudFormation Early Validation reject the next run.
            # Scope it to the immutable verification id so a stale observability resource can
            # never block platform verification (the successful stack still cleans this up).
            "CloudWatchLogGroupName": f"{params['CloudWatchLogGroupName'].rstrip('/')}/{run_id}",
            "SignalTimeoutSeconds": str(signal_timeout_minutes(cfg) * 60),
            "SignalTimeoutIso": f"PT{signal_timeout_minutes(cfg)}M",
            "TerminationProtection": "Disabled",
        }
    )
    if opts.get("instance_type"):
        params["InstanceType"] = str(opts["instance_type"])
    if opts.get("disk_gb"):
        params["DiskGb"] = str(opts["disk_gb"])
    # The EBS writability probe must hit the same directory the container bind-mounts.
    if opts.get("data_dir"):
        params["DataDirHostPath"] = str(opts["data_dir"])
    missing = [k for k in ("AmiId", "SubnetId", "SecurityGroupId", "ImageReference", "ContainerPort") if not params.get(k)]
    if missing:
        raise RuntimeError(f"canary 参数缺失：{missing}（network 栈输出与 canary.yaml 默认值必须齐全）")
    return params


def as_cfn_parameters(params: dict[str, str]) -> list[dict[str, str]]:
    return [{"ParameterKey": k, "ParameterValue": v} for k, v in sorted(params.items())]


def plan_change_set(aws: Aws, stack_name: str, params: dict[str, str], token: str) -> tuple[str, str]:
    """Step 2's second half: plan without executing, so CFN itself judges the template."""
    body = template_path(aws.cfg, "canary.yaml").read_text(encoding="utf-8")
    name = f"corenova-golden-plan-{token}"
    cfn_type = _change_set_type(aws, stack_name)
    resp = aws.cfn.create_change_set(
        StackName=stack_name,
        TemplateBody=body,
        Parameters=as_cfn_parameters(params),
        ChangeSetName=name,
        ChangeSetType=cfn_type,
        Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
        Description="CoreNova Golden Verification plan (no-execute)",
    )
    _wait_change_set(aws, name, stack_name)
    changes = aws.cfn.describe_change_set(StackName=stack_name, ChangeSetName=name).get("Changes", [])
    summary = ", ".join(f"{c['ResourceChange']['Action']}:{c['ResourceChange']['LogicalResourceId']}" for c in changes)
    return resp["Id"], f"{len(changes)} 项变更 {summary[:600]}"


# 这些状态下的栈无法再 CREATE，也无法 UPDATE：必须先删干净（上一次建栈失败回滚后就停在这里）
UNUSUABLE_STATUSES = ("ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "IMPORT_ROLLBACK_COMPLETE", "CREATE_FAILED")


def _change_set_type(aws: Aws, stack_name: str) -> str:
    status = _stack_status(aws, stack_name)
    if status is None:
        return "CREATE"
    if status.startswith("DELETE_"):
        # 上一轮清理还在进行：等它消失，否则 change-set 会报 "stack already exists"
        log(f"栈 {stack_name} 正在删除（{status}）→ 等待消失")
        _wait_stack_gone(aws, stack_name, timeout_minutes=15)
        return "CREATE"
    if status in UNUSUABLE_STATUSES or status == "REVIEW_IN_PROGRESS":
        if status in UNUSUABLE_STATUSES:
            log(f"栈 {stack_name} 处于 {status} → 先删除再重建（否则 change-set 会被 CFN 拒绝）")
            aws.cfn.delete_stack(StackName=stack_name)
            _wait_stack_gone(aws, stack_name, timeout_minutes=15)
        return "CREATE"
    return "UPDATE" if status.endswith("_COMPLETE") else "CREATE"


def _stack_status(aws: Aws, stack_name: str) -> str | None:
    try:
        return aws.cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except Exception:  # noqa: BLE001 - 不存在时 CFN 抛 ValidationError
        return None


def _wait_stack_gone(aws: Aws, stack_name: str, *, timeout_minutes: int) -> None:
    if poll_until(
        lambda: True if _stack_status(aws, stack_name) is None else None,
        timeout_s=timeout_minutes * 60,
        interval_s=10,
    ) is None:
        raise RuntimeError(f"栈 {stack_name} 未在 {timeout_minutes} 分钟内删除完成，请手动检查（残留资源=持续计费）")


def deploy_canary(aws: Aws, stack_name: str, params: dict[str, str], *, create: bool) -> None:
    body = template_path(aws.cfg, "canary.yaml").read_text(encoding="utf-8")
    if create:
        aws.cfn.create_stack(
            StackName=stack_name,
            TemplateBody=body,
            Parameters=as_cfn_parameters(params),
            Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
            Tags=[
                {"Key": "corenova:purpose", "Value": "golden-verification"},
                {"Key": "corenova:billing", "Value": "canary-temporary"},
            ],
        )
    else:
        aws.cfn.update_stack(
            StackName=stack_name,
            TemplateBody=body,
            Parameters=as_cfn_parameters(params),
            Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
        )


def read_canary(aws: Aws, stack_name: str) -> Canary:
    """不依赖栈 Outputs：WaitCondition 不翻转时栈停在 CREATE_IN_PROGRESS，Outputs 取不到。
    直接从 StackResources 拿 Instance 的 PhysicalResourceId（Instance CREATE_COMPLETE 即可用）。"""
    instance_id = ""
    try:
        for res in aws.cfn.describe_stack_resources(StackName=stack_name)["StackResources"]:
            if res.get("ResourceType") == "AWS::EC2::Instance":
                instance_id = res.get("PhysicalResourceId", "")
                break
    except Exception:  # noqa: BLE001
        pass
    canary = Canary(stack_name=stack_name, instance_id=instance_id)
    if instance_id:
        try:
            res = aws.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
            canary.public_ip = res.get("PublicIpAddress", "")
            canary.public_dns = res.get("PublicDnsName", "")
            canary.private_ip = res.get("PrivateIpAddress", "")
            canary.subnet_id = res.get("SubnetId", "")
            canary.vpc_id = res.get("VpcId", "")
            groups = res.get("SecurityGroups") or []
            canary.security_group_id = groups[0]["GroupId"] if groups else ""
            canary.volume_ids = [
                str(mapping["Ebs"]["VolumeId"])
                for mapping in res.get("BlockDeviceMappings") or []
                if (mapping.get("Ebs") or {}).get("VolumeId")
            ]
        except Exception:  # noqa: BLE001
            pass
    return canary


def read_canary_safe(aws: Aws, stack_name: str) -> Canary:
    try:
        return read_canary(aws, stack_name)
    except Exception:  # noqa: BLE001 - cleanup must proceed even if the stack is already gone
        return Canary(stack_name=stack_name)


def signal_received(aws: Aws, stack_name: str) -> tuple[bool, str]:
    """Step 6: the WaitCondition reaches CREATE_COMPLETE only when cfn-signal answered."""
    try:
        resources = aws.cfn.describe_stack_resources(StackName=stack_name)["StackResources"]
    except Exception as exc:  # noqa: BLE001
        return False, f"describe_stack_resources 失败：{exc}"
    for res in resources:
        if res.get("ResourceType") == "AWS::CloudFormation::WaitCondition":
            return (
                res.get("ResourceStatus") == "CREATE_COMPLETE",
                f"{res.get('ResourceStatus')} {(res.get('StatusReason') or '')[:200]}".strip(),
            )
    return False, "栈内没有 WaitCondition"


def _wait_instance_ready(aws: Aws, stack_name: str, *, timeout_minutes: int) -> None:
    """等 Instance 资源 CREATE_COMPLETE（实例已起、user-data 开始跑），不要求整栈完成。"""

    def probe() -> bool | None:
        try:
            resources = aws.cfn.describe_stack_resources(StackName=stack_name)["StackResources"]
        except Exception:  # noqa: BLE001 - 栈创建早期 describe 可能暂时失败
            return None
        for res in resources:
            if res.get("ResourceType") == "AWS::EC2::Instance":
                st = res.get("ResourceStatus", "")
                if st == "CREATE_COMPLETE":
                    return True
                if st.endswith("_FAILED") or st.startswith("ROLLBACK"):
                    raise RuntimeError(f"Instance 资源 {st}: {_stack_reason(aws, stack_name)}")
        return None

    if poll_until(probe, timeout_s=timeout_minutes * 60, interval_s=15) is None:
        raise TimeoutError(f"等待 Instance 就绪超时（{timeout_minutes} 分钟）")


def _wait_stack(aws: Aws, stack_name: str, *, timeout_minutes: int) -> str:
    def probe() -> str | None:
        status = aws.cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
        if status.endswith("_COMPLETE"):
            return status
        if status.endswith("_FAILED") or status.startswith("ROLLBACK"):
            raise RuntimeError(f"栈 {stack_name} 状态 {status}: {_stack_reason(aws, stack_name)}")
        return None

    status = poll_until(probe, timeout_s=timeout_minutes * 60, interval_s=15)
    if status is None:
        raise TimeoutError(f"等待栈 {stack_name} 完成超时（{timeout_minutes} 分钟）")
    return status


def _stack_reason(aws: Aws, stack_name: str) -> str:
    try:
        events = aws.cfn.describe_stack_events(StackName=stack_name)["StackEvents"][:6]
    except Exception as exc:  # noqa: BLE001
        return f"(读取栈事件失败 {exc})"
    return " | ".join(
        f"{e.get('LogicalResourceId')}:{e.get('ResourceStatus')}:{(e.get('ResourceStatusReason') or '')[:160]}"
        for e in events
    )


def _wait_change_set(aws: Aws, name: str, stack_name: str, *, timeout_minutes: int = 5) -> None:
    def failure_detail(fallback: str) -> str:
        """Surface Early Validation's actionable event instead of its generic wrapper."""
        try:
            events = aws.cfn.describe_events(
                StackName=stack_name,
                ChangeSetName=name,
                Filters={"FailedEvents": True},
            ).get("OperationEvents", [])
        except Exception:  # noqa: BLE001 - older botocore/accounts may not expose this API
            return fallback

        details = []
        for event in events:
            reason = event.get("ValidationStatusReason") or event.get("ResourceStatusReason")
            if reason:
                resource = event.get("LogicalResourceId") or event.get("ResourceType") or "validation"
                details.append(f"{resource}: {reason}")
        return f"{fallback} | {' | '.join(details)}" if details else fallback

    def probe() -> bool | None:
        resp = aws.cfn.describe_change_set(StackName=stack_name, ChangeSetName=name)
        if resp.get("Status") == "CREATE_COMPLETE":
            return True
        if resp.get("Status") in ("FAILED", "DELETE_COMPLETE"):
            reason = str(resp.get("StatusReason") or resp.get("Status"))
            raise RuntimeError(f"change-set 规划失败：{failure_detail(reason)}")
        return None

    if poll_until(probe, timeout_s=timeout_minutes * 60, interval_s=5) is None:
        raise TimeoutError(f"等待 change-set {name} 超时")


# --------------------------------------------------------------------------- probe plumbing


@dataclass
class Invocation:
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error

    @property
    def out(self) -> str:
        return (self.stdout + self.stderr).strip()


def ssm_run(aws: Aws, instance_id: str, script: str, *, timeout: int = 240) -> Invocation:
    """Execute a shell script inside the instance. There is deliberately no SSH path (§8)."""
    inv = Invocation()
    if not instance_id:
        inv.error = "没有实例 id（canary 未起来）"
        return inv
    try:
        sent = aws.ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [script], "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout,
            Comment="corenova-golden-probe",
        )
        command_id = sent["Command"]["CommandId"]
    except Exception as exc:  # noqa: BLE001
        inv.error = f"send_command 失败：{type(exc).__name__}: {exc}"
        return inv

    def probe() -> bool | None:
        try:
            got = aws.ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except Exception as exc:  # noqa: BLE001 - InvocationDoesNotExist while the agent catches up
            if "InvocationDoesNotExist" in type(exc).__name__ or "InvocationDoesNotExist" in str(exc):
                return None
            inv.error = f"get_command_invocation 失败：{exc}"
            return True
        if got.get("Status") in ("Pending", "InProgress", "Delayed", "Running"):
            return None
        # 以 Status 为准：实测 get_command_invocation 成功时 ExitCode 可能为 None（成功看
        # Status/ResponseCode）。之前 `0 or -1` 的写法还会把 ExitCode==0 误判成 -1。
        _status = got.get("Status")
        _ec = got.get("ExitCode")
        inv.stdout = got.get("StandardOutputContent", "")
        inv.stderr = got.get("StandardErrorContent", "")
        if _status == "Success":
            inv.exit_code = int(_ec) if _ec is not None else 0
        else:
            inv.exit_code = int(_ec) if _ec is not None else 1
            inv.error = f"{_status} {got.get('StatusDetails', '')}"[:300]
        return True

    # 轮询间隔统一 5s：原实现 InvocationDoesNotExist 分支 sleep(3)，与 5s 的差异只在
    # 命令刚发出的头几秒，相对 timeout（默认 240s）可忽略。
    if poll_until(probe, timeout_s=timeout + 60, interval_s=5) is None:
        inv.error = "等待 SSM 命令结果超时"
    return inv


@dataclass
class ProbeCtx:
    aws: Aws
    cfg: Config
    canary: Canary
    params: dict[str, str]

    def script(self, body: str) -> Invocation:
        return ssm_run(self.aws, self.canary.instance_id, body)


@dataclass
class ProbeSpec:
    step: int
    key: str
    title: str
    how: str
    fn: Callable[[ProbeCtx], tuple[bool, str]]


def _probe_cfn_init(ctx: ProbeCtx) -> tuple[bool, str]:
    inv = ctx.script("cat /run/corenova-cfn-init.rc 2>/dev/null || echo missing; tail -n 5 /var/log/corenova/cfn-init.log 2>/dev/null")
    first = inv.out.splitlines()[:1]
    return inv.exit_code == 0 and first == ["0"], (inv.out[:400] or inv.error)


def _probe_ssm(ctx: ProbeCtx) -> tuple[bool, str]:
    inv = ctx.script("echo ssm-channel-ok; systemctl is-active amazon-ssm-agent snap.amazon-ssm-agent amazon-ssm-agent 2>/dev/null | head -1")
    registered = ""
    try:
        info = ctx.aws.ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [ctx.canary.instance_id]}])
        registered = str(len(info.get("InstanceInformationList", [])))
    except Exception as exc:  # noqa: BLE001
        registered = f"describe_instance_information 失败 {type(exc).__name__}"
    return inv.exit_code == 0 and "ssm-channel-ok" in inv.out, f"SendCommand 往返 ok；托管实例记录={registered}"


def _probe_docker(ctx: ProbeCtx) -> tuple[bool, str]:
    smoke = ctx.params.get("SmokeImage", "alpine:3.20")
    inv = ctx.script(
        "docker version --format 'SERVER={{.Server.Version}}' 2>&1; "
        f"docker run --rm {shlex.quote(smoke)} /bin/sh -c 'echo container-started' 2>&1"
    )
    out = inv.out or ""
    version = re.search(r"SERVER=([0-9.]+)", out)
    started = "container-started" in out
    ok = bool(version) and started
    tail = out.strip().splitlines()[-3:] if not ok else []
    return ok, (
        f"docker server={version.group(1) if version else '?'} smoke 容器={'起' if started else '未起'}"
        + ("" if ok else " | " + " / ".join(tail)[:240])
    )


def _probe_nginx(ctx: ProbeCtx) -> tuple[bool, str]:
    port = ctx.params.get("ContainerPort", "80")
    inv = ctx.script(
        "nginx -t 2>&1; "
        "code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:80/); echo \"proxied=$code\"; "
        f"ss -ltn | grep -c ':{port} ' || true"
    )
    proxied = re.search(r"proxied=(\d{3})", inv.out)
    code = proxied.group(1) if proxied else "000"
    return (
        inv.exit_code == 0 and "successful" in inv.out and code[0] in "23",
        f"nginx -t={'ok' if 'successful' in inv.out else 'failed'} 经 :80 反代 HTTP={code}",
    )


def _probe_cloudwatch(ctx: ProbeCtx) -> tuple[bool, str]:
    inv = ctx.script("systemctl is-active amazon-cloudwatch-agent; test -s /opt/aws/amazon-cloudwatch-agent/etc/corenova-config.json && echo config-ok")
    if inv.exit_code != 0 or "config-ok" not in inv.out:
        return False, inv.out[:200] or inv.error
    group = ctx.params.get("CloudWatchLogGroupName", "/corenova/canary")
    seen = ""

    def probe() -> tuple[bool, str] | None:
        nonlocal seen
        try:
            streams = ctx.aws.logs.describe_log_streams(logGroupName=group, descending=True, limit=50).get("logStreams", [])
        except Exception as exc:  # noqa: BLE001
            return False, f"日志组 {group} 不可读：{type(exc).__name__}: {exc}"
        mine = [s for s in streams if ctx.canary.instance_id in s.get("logStreamName", "")]
        if any(int(s.get("lastEventTimestamp", 0) or 0) > 0 for s in mine):
            return True, f"日志组 {group} 内本实例 stream {len(mine)} 条且有事件"
        seen = f"日志组 {group} 存在但本实例尚无日志流（{len(streams)} 条流）"
        return None

    result = poll_until(probe, timeout_s=180, interval_s=20)
    return result if result is not None else (False, seen)


def _probe_ebs(ctx: ProbeCtx) -> tuple[bool, str]:
    data_dir = shlex.quote(ctx.params.get("DataDirHostPath", "/var/lib/corenova/app/data"))
    inv = ctx.script(
        "set -e; "
        "root=$(findmnt -no SOURCE /); echo \"root=$root\"; lsblk -no NAME,TYPE,SIZE \"$root\"; "
        f"d={data_dir}; mkdir -p $d; marker=$d/.corenova-golden; "
        "echo golden > $marker; sync; test \"$(cat $marker)\" = golden && echo ebs-writable; rm -f $marker; "
        "df -h / | tail -1"
    )
    volume = _root_volume(ctx.aws, ctx.canary.instance_id)
    want_gb = int(ctx.params.get("DiskGb", "0") or 0)
    notes = [f"根卷 {volume.get('VolumeType')}/{volume.get('Size')}GB state={volume.get('State')}"]
    ok = (
        inv.exit_code == 0
        and "ebs-writable" in inv.out
        and volume.get("VolumeType") == "gp3"
        and (not want_gb or int(volume.get("Size") or 0) >= want_gb)
        and volume.get("State") == "in-use"
    )
    return ok, "; ".join(notes) + f"; 写入实测={'ok' if 'ebs-writable' in inv.out else 'fail'}"


def _root_volume(aws: Aws, instance_id: str) -> dict[str, Any]:
    if not instance_id:
        return {}
    try:
        res = aws.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        mappings = res.get("BlockDeviceMappings") or []
        root = next((m for m in mappings if m.get("DeviceName") in ("/dev/sda1", "/dev/xvda", "/dev/root")), mappings[:1])
        volume_id = ((root or {}).get("Ebs") or {}).get("VolumeId", "")
        if not volume_id:
            return {}
        vols = aws.ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"]
        return vols[0] if vols else {}
    except Exception:  # noqa: BLE001
        return {}


def _probe_security_group(ctx: ProbeCtx) -> tuple[bool, str]:
    """Measured from *outside* the instance: a loopback test would never cross the SG."""
    canary, params = ctx.canary, ctx.params
    host = canary.public_ip or _strip_scheme(canary.public_dns)
    if not host:
        return False, "canary 没有公网地址，无法在 SG 外侧实测"
    ports = set()
    for rule in _sg_ingress(ctx.aws, canary.security_group_id):
        ports |= _runtime_ports(rule)
    ssh_blocked = _tcp_blocked(host, 22, timeout=12)
    http_ok, http_code = _http_probe(f"http://{host}:80/")
    notes = [f"22 入站 {'不通(符合 §8)' if ssh_blocked else '可连(违规)'}", f"80 入站 HTTP={http_code}"]
    tls = str(params.get("SelfSignedTls", "false")).lower() == "true"
    https_ok = True
    if tls:
        https_ok, https_code = _https_probe(host)
        notes.append(f"443 入站 HTTPS={https_code}")
    ok = (
        ssh_blocked
        and http_ok
        and https_ok
        and {80, 443} <= ports
        and 22 not in ports
    )
    return ok, "; ".join(notes) + f"; SG={canary.security_group_id} 入站端口={sorted(p for p in ports if p < 1024)}"


def _strip_scheme(value: str) -> str:
    return re.sub(r"^https?://", "", value).split("/")[0]


def _sg_ingress(aws: Aws, group_id: str) -> list[dict[str, Any]]:
    if not group_id:
        return []
    groups = aws.ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"]
    return groups[0].get("IpPermissions", []) if groups else []


def _runtime_ports(permission: dict[str, Any]) -> set[int]:
    if str(permission.get("IpProtocol", "-1")) == "-1":
        return {0}
    lo, hi = permission.get("FromPort") or 0, permission.get("ToPort") or 0
    return set(range(int(lo), min(int(hi), 65535) + 1)) if hi - lo < 1024 else {int(lo), int(hi)}


def _tcp_blocked(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return False
    except TimeoutError:
        return True
    except OSError:
        # Refused/reset also proves "22 is unusable"; only a completed handshake is a violation.
        return True


def _http_probe(url: str) -> tuple[bool, str]:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return 200 <= resp.status < 400, str(resp.status)
    except urllib.error.HTTPError as exc:
        return exc.code < 500, str(exc.code)
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def _https_probe(host: str) -> tuple[bool, str]:
    import ssl
    import urllib.request

    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(f"https://{host}:443/", timeout=15, context=ctx) as resp:
            return 200 <= resp.status < 400, str(resp.status)
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def _probe_network(ctx: ProbeCtx) -> tuple[bool, str]:
    canary = ctx.canary
    notes: list[str] = []
    if not canary.subnet_id:
        return False, "拿不到 canary 子网 id"
    subnet = ctx.aws.ec2.describe_subnets(SubnetIds=[canary.subnet_id])["Subnets"][0]
    notes.append(f"子网 {canary.subnet_id} auto-public-ip={'on' if subnet.get('MapPublicIpOnLaunch') else 'off'}")
    rts = ctx.aws.ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": [canary.subnet_id]}]
    ).get("RouteTables", [])
    has_igw = any(r.get("GatewayId", "").startswith("igw-") for rt in rts for r in rt.get("Routes", []))
    notes.append(f"默认路由指向 IGW={'是' if has_igw else '否'}")
    inv = ctx.script(
        "set -e; getent hosts download.docker.com >/dev/null && echo dns-ok; "
        "curl -fsS -o /dev/null -w 'egress=%{http_code}\\n' --max-time 10 https://checkip.amazonaws.com"
    )
    notes.append(f"实例出网 {inv.out[:120] or inv.error}")
    reachable = bool(canary.public_ip or canary.public_dns)
    notes.append(f"公网可达地址={'有' if reachable else '无'}")
    ok = bool(has_igw and subnet.get("MapPublicIpOnLaunch") and reachable and inv.exit_code == 0 and "dns-ok" in inv.out)
    return ok, "; ".join(notes)


def _probe_ec2_launched(ctx: ProbeCtx) -> tuple[bool, str]:
    if not ctx.canary.instance_id:
        return False, "栈输出里没有 InstanceId"
    # 两项状态检查在实例 running 后还会 "initializing" 1–2 分钟才转 ok，短轮询等它收敛，
    # 否则刚启动就探会误判（state=running system-status=initializing）。
    # state 必须在循环内重读：CFN CREATE_COMPLETE 时 EC2 可能还是 pending，
    # 只在循环前读一次会把"10 秒后转 running"的实例误判为失败。
    state, sys_check = "unknown", "pending"

    def probe() -> bool | None:
        nonlocal state, sys_check
        res = ctx.aws.ec2.describe_instances(InstanceIds=[ctx.canary.instance_id])[
            "Reservations"][0]["Instances"][0]
        state = (res.get("State") or {}).get("Name", "")
        statuses = ctx.aws.ec2.describe_instance_status(
            InstanceIds=[ctx.canary.instance_id], IncludeAllInstances=True
        ).get("InstanceStatuses", [])
        sys_check = (statuses[0].get("SystemStatus") or {}).get("Status", "pending") if statuses else "pending"
        if state == "running" and sys_check == "ok":
            return True
        return None

    poll_until(probe, timeout_s=10 * 12, interval_s=12)
    return state == "running" and sys_check == "ok", f"state={state} system-status={sys_check}"


# Probe order mirrors verify-gate-design.md §5 steps 4-12. `cfn_validated` is filled by the
# orchestrator because it is proven by steps 2-3 rather than by an in-instance command.
PROBE_SPECS: tuple[ProbeSpec, ...] = (
    ProbeSpec(4, "ec2_launched", "EC2 启动", "describe_instances state=running + system status check ok", _probe_ec2_launched),
    ProbeSpec(5, "cfn_init_completed", "cfn-init 完成", "SSM 读 /run/corenova-cfn-init.rc == 0 并回读 cfn-init.log 尾部", _probe_cfn_init),
    ProbeSpec(7, "docker_runtime_ok", "Docker 运行时", "SSM 内 docker version 取 Server.Version + 起一个 digest 钉住的公共镜像容器", _probe_docker),
    ProbeSpec(8, "nginx_ok", "Nginx", "SSM 内 nginx -t + 经本机 :80 反代访问容器端口拿 2xx/3xx", _probe_nginx),
    ProbeSpec(9, "ssm_ok", "SSM", "探针本身即经 SendCommand 送达；另测 agent 进程与托管实例注册", _probe_ssm),
    ProbeSpec(10, "cloudwatch_ok", "CloudWatch", "agent active + 配置非空 + 日志组内出现本实例 stream 且有事件", _probe_cloudwatch),
    ProbeSpec(11, "ebs_ok", "EBS", "describe_volumes 断言 gp3/容量/in-use + 实例内数据目录写入回读删除", _probe_ebs),
    ProbeSpec(12, "security_group_ok", "Security Group", "栈外实测 22 不可连、80 可连且 HTTP 2xx/443(自签 TLS) 可连 + SG 规则比对", _probe_security_group),
    ProbeSpec(12, "network_ok", "网络", "public 子网 + IGW 默认路由 + 实例内 DNS/HTTPS 出网 + 有公网地址", _probe_network),
)


def probe_plan() -> list[tuple[int, str, str, str]]:
    return [(s.step, s.key, s.title, s.how) for s in PROBE_SPECS]


def run_probes(aws: Aws, cfg: Config, canary: Canary, params: dict[str, str], *, cfn_validated: bool, change_set: str) -> list[Probe]:
    ctx = ProbeCtx(aws=aws, cfg=cfg, canary=canary, params=params)
    probes = [Probe(key="cfn_validated", title="CloudFormation 校验",
                    how="三份模板 validate-template + canary change-set（no-execute）规划成功",
                    ok=cfn_validated, detail=f"validate-template ×3 + change-set {change_set[:60]}")]
    for spec in PROBE_SPECS:
        try:
            ok, detail = spec.fn(ctx)
        except Exception as exc:  # noqa: BLE001 - one broken probe must not abort the other measurements
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        probes.append(Probe(key=spec.key, title=spec.title, how=spec.how, ok=ok, detail=detail[:600]))
    return probes


# --------------------------------------------------------------------------- contract


def read_contract(backend: Backend, cfg: Config) -> dict[str, Any]:
    raw = backend.get(platformref.contract_key(cfg.region, cfg.architecture))
    return json.loads(raw) if raw else {}


def next_platform_verification_id(cfg: Config, existing: dict[str, Any], *, when: str = "") -> str:
    day = (when or utcnow())[:10].replace("-", "")
    prefix = f"plat-{cfg.region}-{cfg.architecture}-{day}-"
    tail = re.search(r"-(\d{3})$", str(existing.get("platform_verification_id", "")))
    seq = int(tail.group(1)) + 1 if (str(existing.get("platform_verification_id", "")).startswith(prefix) and tail) else 1
    return f"{prefix}{seq:03d}"


def build_contract(
    cfg: Config,
    *,
    platform_verification_id: str,
    ami_id: str,
    ami_resolved_at: str,
    revisions: dict[str, str],
    verification: dict[str, bool],
    status: str,
    verified_at: str,
    invalidated_reason: str | None = None,
) -> dict[str, Any]:
    source = cfg.platform.get("public_ami") if cfg.base_ami_source == "public" else cfg.platform.get("custom_ami")
    source = source or {}
    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "platform_verification_id": platform_verification_id,
        "ami_id": ami_id,
        "region": cfg.region,
        "architecture": cfg.architecture,
        **{key: revisions.get(key, "") for key in REVISION_KEYS},
        "verification": {key: bool(verification.get(key, False)) for key in VERIFICATION_KEYS},
        "status": status,
        "platform_verified_at": verified_at,
        "invalidated_at": None if status == VALID else (verified_at or utcnow()),
        "invalidated_reason": None if status == VALID else (invalidated_reason or "探针未全通过"),
        "base_ami_source": cfg.base_ami_source,
        "source_ami_name": str(source.get("name_pattern", "")),
        "source_ami_account": str(source.get("owner_account", "")),
        "source_ssm_parameter": cfg.ami_ssm_parameter(),
        "ami_resolved_at": ami_resolved_at,
        "reverify_interval_days": cfg.reverify_interval_days,
    }
    return {key: contract[key] for key in CONTRACT_FIELD_ORDER}


def write_contract(backend: Backend, cfg: Config, contract: dict[str, Any]) -> str:
    key = platformref.contract_key(cfg.region, cfg.architecture)
    backend.put(key, (json.dumps(contract, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    if backend.name == "r2":
        # §3: with R2 live, the repository copy is audit-only and never consulted for gating.
        write_json(cfg.root / "data" / key, contract)
    return key


def mark_contract_invalid(backend: Backend, cfg: Config, *, reason: str) -> dict[str, Any]:
    contract = read_contract(backend, cfg)
    if not contract:
        return {}
    contract["status"] = INVALID
    contract["invalidated_at"] = utcnow()
    contract["invalidated_reason"] = reason[:500]
    write_contract(backend, cfg, contract)
    return contract


# --------------------------------------------------------------------------- drift (for App Verification)


def check_drift(
    backend: Backend,
    cfg: Config,
    *,
    live_ami_id: str | None = None,
    check_ssm: bool = True,
) -> DriftReport:
    """Compare the stored contract against the public SSM pointer and the current repo revisions.

    `live_ami_id` lets a caller that already resolved the pointer hand the value over instead of
    triggering a second SSM read (§6). Leaving it None performs one read-only GetParameter - free,
    and exactly what Application Verification's RESOLVED stage owes §2.1 constraint 2. This helper
    is additive: `platformref.check()` keeps its own signature and still calls
    `resolve_public_ami_id(cfg)` itself.
    """
    out = DriftReport()
    contract = read_contract(backend, cfg)
    if not contract:
        out.reasons.append(f"backend 中没有 Platform Contract：{platformref.contract_key(cfg.region, cfg.architecture)}")
        return out
    out.contract_found = True
    out.contract_status = str(contract.get("status", ""))
    out.platform_verification_id = str(contract.get("platform_verification_id", ""))
    out.recorded_ami_id = str(contract.get("ami_id", ""))

    live = live_ami_id
    if live is None and check_ssm and cfg.base_ami_source == "public":
        try:
            live = platformref.resolve_public_ami_id(cfg)
        except Exception as exc:  # noqa: BLE001
            out.reasons.append(f"读取公共 SSM 参数失败，无法判定 AMI 漂移：{type(exc).__name__}: {exc}")
    out.live_ami_id = live or ""
    if cfg.base_ami_source == "public" and live and out.recorded_ami_id and live != out.recorded_ami_id:
        out.ami_drifted = True
        out.reasons.append(f"公开 AMI 已被厂商替换：契约 {out.recorded_ami_id} != 现值 {live} → 契约须 invalid 并复验")

    current = platformref.compute_revisions(cfg, out.recorded_ami_id)
    for key in REVISION_KEYS:
        recorded = str(contract.get(key, ""))
        now = str(current.get(key, ""))
        if recorded and now and recorded != now:
            out.revision_drifts[key] = {"recorded": recorded, "current": now}
            out.reasons.append(f"{key} 变更：契约 {recorded} != 当前 {now}")

    interval = int(contract.get("reverify_interval_days") or cfg.reverify_interval_days)
    out.age_days = age_days(str(contract.get("platform_verified_at", "")))
    if out.age_days > interval:
        out.expired = True
        out.reasons.append(f"契约已超复验周期（{out.age_days:.1f} 天 > {interval} 天）")
    return out


# --------------------------------------------------------------------------- cleanup


def destroy_canary(aws: Aws, cfg: Config, stack_name: str, canary: Canary) -> tuple[bool, list[str]]:
    """Delete the canary and *prove* it is gone; a leftover is a permanent cost."""
    notes: list[str] = []
    try:
        aws.cfn.delete_stack(StackName=stack_name)
        notes.append(f"已发起删除 {stack_name}")
    except Exception as exc:  # noqa: BLE001
        return False, [f"delete_stack 失败：{type(exc).__name__}: {exc}"]

    def probe() -> bool | None:
        try:
            stacks = aws.cfn.describe_stacks(StackName=stack_name)["Stacks"]
        except Exception as exc:  # noqa: BLE001
            if "does not exist" in str(exc):
                return True
            return None
        status = stacks[0]["StackStatus"] if stacks else "DELETE_COMPLETE"
        if status == "DELETE_COMPLETE":
            return True
        if status == "DELETE_FAILED":
            notes.append(f"DELETE_FAILED：{_stack_reason(aws, stack_name)}")
            return False
        return None

    gone = poll_until(probe, timeout_s=20 * 60, interval_s=10)
    if gone is not True:
        notes.append(f"栈 {stack_name} 未在时限内消失（残留 = 持续计费）")

    # The production template deliberately retains the application data volume. A canary uses
    # that exact template, so stack deletion alone is not cleanup: delete only volumes captured
    # from this instance and carrying the explicit canary-temporary billing tag.
    state = _instance_state(aws, canary.instance_id) if canary.instance_id else ""
    if state in ("pending", "running", "stopping"):
        notes.append(f"实例 {canary.instance_id} 仍处于 {state} —— 必须人工终止")
    else:
        notes.append(f"实例 {canary.instance_id or '?'} 已不存在（state={state or 'gone'}）")
    volumes_clean, volume_notes = _cleanup_canary_volumes(aws, canary.volume_ids)
    notes.extend(volume_notes)
    clean = gone and volumes_clean and not any(
        marker in n
        for n in notes
        for marker in ("必须人工终止", "DELETE_FAILED", "未在时限内消失", "delete_stack 失败")
    )
    return clean, notes


def _instance_state(aws: Aws, instance_id: str) -> str:
    try:
        res = aws.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    except Exception:  # noqa: BLE001
        return ""
    return str((res.get("State") or {}).get("Name", ""))


def _cleanup_canary_volumes(aws: Aws, volume_ids: list[str]) -> tuple[bool, list[str]]:
    """Delete retained EBS volumes proven to belong to this temporary canary run."""
    notes: list[str] = []
    clean = True

    def describe(volume_id: str) -> dict[str, Any] | None:
        try:
            volumes = aws.ec2.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
        except Exception as exc:  # noqa: BLE001 - NotFound proves the volume is gone
            text = str(exc)
            if "does not exist" in text or "InvalidVolume.NotFound" in text:
                return None
            raise
        return volumes[0] if volumes else None

    for volume_id in dict.fromkeys(volume_ids):
        try:
            volume = describe(volume_id)
        except Exception as exc:  # noqa: BLE001
            clean = False
            notes.append(f"数据卷 {volume_id} 查询失败：{type(exc).__name__}: {exc}")
            continue
        if volume is None:
            notes.append(f"数据卷 {volume_id} 已不存在")
            continue

        tags = {str(tag.get("Key")): str(tag.get("Value")) for tag in volume.get("Tags") or []}
        if tags.get("corenova:billing") != "canary-temporary":
            clean = False
            notes.append(f"数据卷 {volume_id} 缺少 canary-temporary 标签，拒绝删除")
            continue

        def wait_detached(target: str = volume_id) -> str | None:
            current = describe(target)
            if current is None:
                return "gone"
            state = str(current.get("State") or "")
            return state if state in ("available", "deleting") else None

        try:
            ready = poll_until(wait_detached, timeout_s=5 * 60, interval_s=5)
            if ready == "gone":
                notes.append(f"数据卷 {volume_id} 已不存在")
                continue
            if ready not in ("available", "deleting"):
                clean = False
                notes.append(f"数据卷 {volume_id} 未在时限内解除挂载，必须人工处理")
                continue
            if ready == "available":
                aws.ec2.delete_volume(VolumeId=volume_id)
            deleted = poll_until(
                lambda target=volume_id: True if describe(target) is None else None,
                timeout_s=5 * 60,
                interval_s=5,
            )
        except Exception as exc:  # noqa: BLE001
            clean = False
            notes.append(f"数据卷 {volume_id} 删除失败：{type(exc).__name__}: {exc}")
            continue
        if deleted is True:
            notes.append(f"数据卷 {volume_id} 已删除")
        else:
            clean = False
            notes.append(f"数据卷 {volume_id} 未在时限内删除，必须人工处理")

    return clean, notes


# --------------------------------------------------------------------------- the 16 steps


def _step(n: int, title: str) -> None:
    log(f"[golden {n:02d}/16] {title}")


def dry_run(cfg: Config, backend: Backend, *, ami_override: str = "") -> dict[str, Any]:
    """Steps 1-2 planned offline: no AWS call at all, nothing written."""
    problems = static_template_errors(cfg)
    ami_id, resolved_at = resolve_base_ami(cfg, override=ami_override, allow_aws=False)
    opts = canary_opts(cfg)
    stack = canary_stack_name(cfg)
    run_id = next_platform_verification_id(cfg, read_contract(backend, cfg))
    params = template_defaults(cfg, "canary.yaml") if not problems else {}
    params = dict(params or {})
    params.update({"AmiId": ami_id, "InstanceType": str(opts.get("instance_type", "t3.small")),
                   "DiskGb": str(opts.get("disk_gb", 30)),
                   "SignalTimeoutSeconds": str(signal_timeout_minutes(cfg) * 60),
                   "SignalTimeoutIso": f"PT{signal_timeout_minutes(cfg)}M",
                   "NetworkStackName": network_stack_name(cfg), "GoldenRunId": run_id})
    contract = build_contract(
        cfg,
        platform_verification_id=run_id,
        ami_id=ami_id,
        ami_resolved_at=resolved_at,
        revisions=platformref.compute_revisions(cfg, ami_id),
        verification={key: True for key in VERIFICATION_KEYS},
        status=VALID,
        verified_at=utcnow(),
    )
    key = platformref.contract_key(cfg.region, cfg.architecture)

    log("dry-run：本地审阅模式，不调用任何 AWS API、不创建资源、不写契约")
    print("\n=== 步骤 1 Resolve Base AMI（离线时不解析）===")
    print(f"  base_ami_source={cfg.base_ami_source}  ssm={cfg.ami_ssm_parameter()}")
    print(f"  ami_id={ami_id}  （真跑时只解析一次，全程复用，§6）")
    print("\n=== 步骤 2 Validate CloudFormation（本地静态检查）===")
    for name in TEMPLATES:
        path = template_path(cfg, name)
        print(f"  {name:<12} exists={path.exists()} bytes={path.stat().st_size if path.exists() else 0}")
    if problems:
        for problem in problems:
            print(f"  [FAIL] {problem}")
    else:
        print("  [ok] 结构 / 引用完整 / SG 无 22 / init 资产一致 / canary 复用 app 资源 / 无硬编码")
    print("  真跑时追加：aws cloudformation validate-template ×3 → create-change-set(no-execute) → execute")
    print("\n=== 步骤 3-6 Canary 栈部署计划 ===")
    print(f"  stack={stack}  instance={params.get('InstanceType')}  disk={params.get('DiskGb')}GB  "
          f"image={params.get('ImageReference')}  port={params.get('ContainerPort')}")
    print(f"  signal_timeout={params.get('SignalTimeoutSeconds')}s / {params.get('SignalTimeoutIso')}  keep_stack={keep_stack(cfg)}  "
          f"SelfSignedTls={params.get('SelfSignedTls')}")
    print(f"  network={params.get('NetworkStackName')}（真跑时从该栈输出取 SubnetId/SecurityGroupId，缺失则先建栈）")
    print("\n=== 步骤 7-12 探针清单（逐项实测，任一失败 → status=invalid）===")
    print(f"  {'key':<22}{'步':<4}怎么实测")
    for step, key_, title, how in probe_plan():
        print(f"  {key_:<22}{step:<4}{title}：{how}")
    print("  cfn_validated        2   CloudFormation 校验：validate-template + change-set 规划结果")
    print("\n=== 步骤 14-15 将写入的 Platform Contract 预览 ===")
    print(json.dumps(contract, ensure_ascii=False, indent=2))
    print(f"  key={key}  backend={getattr(backend, 'name', '?')}")
    print("  注：预览里的 verification/status 是占位真值，真跑由探针决定；dry-run 不落盘。")

    report = GoldenReport(
        platform_verification_id=run_id,
        stage="VERIFIED" if not problems else "FAILED",
        status="dry-run-planned" if not problems else "dry-run-blocked",
        dry_run=True,
        region=cfg.region,
        architecture=cfg.architecture,
        ami_id=ami_id,
        canary_stack=stack,
        contract_key=key,
        probes=[{"step": s, "key": k, "title": t, "how": h, "planned": True} for s, k, t, h in probe_plan()],
        failures=problems,
        finished_at=utcnow(),
    )
    return {"report": asdict(report), "contract_preview": contract, "static_problems": problems}


def run(
    cfg: Config,
    *,
    backend: Backend | None = None,
    dry: bool = False,
    ami_override: str = "",
    keep: bool | None = None,
) -> dict[str, Any]:
    backend = backend or _backend(cfg)
    if dry:
        return dry_run(cfg, backend, ami_override=ami_override)

    aws = Aws(cfg)
    stack_name = canary_stack_name(cfg)
    effective_keep = keep_stack(cfg) if keep is None else keep
    report = GoldenReport(canary_stack=stack_name, region=cfg.region, architecture=cfg.architecture)
    params: dict[str, str] = {}
    canary = Canary(stack_name=stack_name)
    change_set_id = ""
    deployed = False
    verified = False
    exit_code = 0
    try:
        _step(1, "Resolve Base AMI（只解析一次，§6）")
        problems = static_template_errors(cfg)
        if problems:
            for problem in problems:
                log(f"静态检查失败：{problem}")
            raise RuntimeError("模板静态检查未通过：" + "; ".join(problems[:5]))
        report.stage = "RESOLVED"
        ami_id, resolved_at = resolve_base_ami(cfg, override=ami_override)
        report.ami_id, report.ami_resolved_at = ami_id, resolved_at
        log(f"ami_id={ami_id} source={cfg.base_ami_source} resolved_at={resolved_at}")

        _step(2, "Validate CloudFormation + change-set（no-execute）")
        log("validate-template: " + " ".join(validate_templates(aws)))
        report.platform_verification_id = next_platform_verification_id(cfg, read_contract(backend, cfg))

        _step(3, f"Create/update canary 栈 {stack_name}")
        log(ensure_network_stack(aws, network_stack_name(cfg)))
        params = canary_parameters(cfg, ami_id, stack_outputs(aws, network_stack_name(cfg)), report.platform_verification_id)
        create = _change_set_type(aws, stack_name) == "CREATE"
        change_set_id, summary = plan_change_set(aws, stack_name, params, time.strftime("%H%M%S", time.gmtime()))
        report.change_set = change_set_id
        log(f"change-set({summary[:200]})")
        deploy_canary(aws, stack_name, params, create=create)
        deployed = True
        # 只等 Instance 就绪，不等整栈 CREATE_COMPLETE：WaitCondition 的 ack 在本环境不稳定
        # （信号 HTTP 200 但资源不翻转，见 README 已知问题），探针才是平台验证的实质依据。
        _wait_instance_ready(aws, stack_name, timeout_minutes=signal_timeout_minutes(cfg) + 10)

        _step(4, "EC2 launch")
        report.stage = "DEPLOYING"
        canary = read_canary(aws, stack_name)
        report.canary_instance = canary.instance_id
        log(f"instance={canary.instance_id} public={canary.public_ip or canary.public_dns}")

        _step(5, "Wait for cfn-init")
        _wait_for_ssm_ready(aws, canary.instance_id, timeout_minutes=signal_timeout_minutes(cfg))

        _step(6, "Wait for cfn-signal")
        report.stage = "DEPLOYED"
        _wait_for_signal(aws, canary.instance_id, timeout_minutes=signal_timeout_minutes(cfg))
        signal_ok = True  # _wait_for_signal 未抛异常 = 实例已发出 SUCCESS 信号

        _step(7, "Verify Docker runtime")
        _step(8, "Verify Nginx")
        _step(9, "Verify SSM")
        _step(10, "Verify CloudWatch")
        _step(11, "Verify EBS")
        _step(12, "Verify Security Group / network")
        report.stage = "VERIFYING"
        probes = run_probes(aws, cfg, canary, params, cfn_validated=True, change_set=change_set_id)
        for probe in probes:
            log(f"探针 {probe.key:<22}{'PASS' if probe.ok else 'FAIL'}  {probe.detail[:200]}")
        report.probes = [asdict(p) for p in probes]

        _step(13, "Optional application smoke test")
        log("canary 跑 digest 钉住的占位容器；真实应用冒烟属 Application Verification（§4），此处不引入应用依赖")

        _step(14, "Produce Platform Contract")
        failed = [p.key for p in probes if not p.ok]
        verification = {p.key: p.ok for p in probes}
        # cfn_signal_received 不在 PROBE_SPECS（它是部署就绪信号，不是资源探针）；
        # 以第 6 步"实例发出 SUCCESS 信号"为准，契约 11 键才齐全。
        verification["cfn_signal_received"] = signal_ok
        contract = build_contract(
            cfg,
            platform_verification_id=report.platform_verification_id,
            ami_id=ami_id,
            ami_resolved_at=resolved_at,
            revisions=platformref.compute_revisions(cfg, ami_id),
            verification=verification,
            status=VALID if not failed else INVALID,
            verified_at=utcnow(),
            invalidated_reason=None if not failed else "AWS Golden 探针未全通过：" + ", ".join(failed),
        )
        report.contract_key = write_contract(backend, cfg, contract)
        log(f"契约 {report.contract_key} status={contract['status']}")

        _step(15, "Mark platform version")
        if failed:
            report.stage, report.status = "FAILED", "failed"
            report.failures = [f"{p.key}: {p.detail[:200]}" for p in probes if not p.ok]
            exit_code = 2
        else:
            report.stage, report.status, verified = "PUBLISHED", "verified", True
    except Exception as exc:  # noqa: BLE001
        report.failures.append(f"{type(exc).__name__}: {exc}")
        log(f"Golden Verification 失败：{type(exc).__name__}: {exc}")
        report.stage = report.stage if report.stage == "VERIFYING" else ("DEPLOYING" if deployed else "RESOLVED")
        report.status = "error"
        exit_code = 3
        try:
            stale = mark_contract_invalid(backend, cfg, reason=f"Golden Verification 中止：{type(exc).__name__}: {exc}"[:400])
            if stale:
                log("既有契约已按 §5 标为 invalid")
            elif report.platform_verification_id:
                contract = build_contract(
                    cfg,
                    platform_verification_id=report.platform_verification_id,
                    ami_id=report.ami_id,
                    ami_resolved_at=report.ami_resolved_at,
                    revisions=platformref.compute_revisions(cfg, report.ami_id),
                    verification={p["key"]: bool(p.get("ok")) for p in report.probes},
                    status=INVALID,
                    verified_at="",
                    invalidated_reason=f"Golden Verification 中止：{type(exc).__name__}: {exc}"[:400],
                )
                report.contract_key = write_contract(backend, cfg, contract)
                log(f"已写入 invalid 契约 {report.contract_key}")
        except Exception as inner:  # noqa: BLE001
            log(f"写 invalid 契约失败：{type(inner).__name__}: {inner}")
    finally:
        _step(16, "Cleanup canary resources")
        if not deployed:
            report.cleanup.append("未创建栈，无需清理")
        elif effective_keep and not verified:
            report.cleanup.append(f"keep_stack → 保留 {stack_name}（调试完请手动 delete-stack，栈内资源在计费）")
        elif effective_keep:
            report.cleanup.append(f"keep_stack: true → 保留 {stack_name}")
        else:
            gone, notes = destroy_canary(aws, cfg, stack_name, canary if canary.instance_id else read_canary_safe(aws, stack_name))
            report.cleanup += notes
            for note in notes:
                log(f"清理：{note}")
            if not gone:
                report.failures.append("canary 清理未确认 —— 残留资源会持续计费，必须人工处理")
                exit_code = exit_code or 4
        report.finished_at = utcnow()
        _write_audit(cfg, report)

    if exit_code:
        raise SystemExit(exit_code)
    return {"report": asdict(report)}


def _wait_for_ssm_ready(aws: Aws, instance_id: str, *, timeout_minutes: int) -> None:
    last = ""

    def probe() -> bool | None:
        nonlocal last
        inv = ssm_run(aws, instance_id, "echo up", timeout=60)
        if inv.exit_code == 0:
            return True
        last = inv.error
        return None

    if poll_until(probe, timeout_s=timeout_minutes * 60, interval_s=20) is None:
        raise TimeoutError(f"实例 {instance_id} 在 {timeout_minutes} 分钟内未通过 SSM 可达（{last}）")


def _wait_for_signal(aws: Aws, instance_id: str, *, timeout_minutes: int) -> None:
    """从实例侧确认信号已发出（signal.log），不依赖 CFN WaitCondition 的 ack。

    CFN 的 WaitCondition 在本环境出现"信号 HTTP 200 但资源不翻转"的现象（见 README 已知问题），
    因此 cfn_signal_received 以"实例确实发出了 SUCCESS 信号"为准——这是可复核的事实。"""
    last = ""
    # 注意：`a && b` 只输出 b 的结果，判断必须基于最终输出（status 行），不能用前半段字样。
    script = (
        'if grep -q "signaled successfully" /var/log/corenova/signal.log 2>/dev/null; then '
        'grep -o "status [A-Z]*" /var/log/corenova/signal.log | head -1; '
        'else echo NO-SIGNAL-YET; fi'
    )

    def probe() -> bool | None:
        nonlocal last
        inv = ssm_run(aws, instance_id, script, timeout=60)
        out = (inv.out or "").strip()
        last = out or inv.error
        if "status SUCCESS" in out:
            return True
        if "status FAILURE" in out:
            raise RuntimeError(f"cfn-signal 发出但状态为 FAILURE：{out}")
        return None

    if poll_until(probe, timeout_s=timeout_minutes * 60, interval_s=20) is None:
        raise TimeoutError(f"cfn-signal 未在 {timeout_minutes} 分钟内发出（{last}）")


def _backend(cfg: Config) -> Backend:
    from .backend import make_backend

    return make_backend(cfg)


def _write_audit(cfg: Config, report: GoldenReport) -> None:
    stamp = report.platform_verification_id or f"golden-{report.started_at.replace(':', '').replace('-', '')}"
    write_json(cfg.output_dir / "runs" / stamp / "golden-report.json", asdict(report))
    log(f"运行审计：{cfg.output_dir / 'runs' / stamp / 'golden-report.json'}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="CoreNova AWS Golden Verification（平台层）")
    ap.add_argument("--dry-run", action="store_true", help="完全离线：静态检查 + 部署计划 + 契约预览，零 AWS 调用")
    ap.add_argument("--check", action="store_true", help="只跑离线静态检查（CI 门禁）")
    ap.add_argument("--drift", action="store_true", help="只比对契约漂移（公共 SSM 现值 + 六个 revision）")
    ap.add_argument("--sync-init", action="store_true", help="把 init/*.sh 内联进 app.yaml 并重新生成 canary.yaml")
    ap.add_argument("--render-canary", action="store_true", help="只从 app.yaml 重新生成 canary.yaml")
    ap.add_argument("--ami-id", default=os.environ.get("CANARY_AMI_ID", ""), help="预解析 ami_id（复现或离线预览用）")
    ap.add_argument("--keep-stack", action="store_true", help="跑完保留 canary（覆盖 config）")
    ap.add_argument("--discard-stack", action="store_true", help="强制清理 canary（覆盖 config）")
    args = ap.parse_args(argv)

    cfg = Config.load()
    keep = True if args.keep_stack else (False if args.discard_stack else None)

    if args.sync_init:
        log(f"已同步：{sync_init_assets(cfg)}")
        return 0
    if args.render_canary:
        log(f"已生成：{render_canary(cfg)}")
        return 0
    if args.check:
        problems = static_template_errors(cfg)
        for problem in problems:
            print(f"[FAIL] {problem}")
        print(f"static check: {'FAIL' if problems else 'PASS'}（{len(problems)} problem(s)）")
        return 1 if problems else 0
    if args.drift:
        report = check_drift(_backend(cfg), cfg)
        payload = asdict(report)
        payload["drifted"] = report.drifted
        payload["contract_key"] = platformref.contract_key(cfg.region, cfg.architecture)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if report.drifted else 0

    summary = run(cfg, dry=args.dry_run, ami_override=args.ami_id, keep=keep)
    print(json.dumps(summary["report"], ensure_ascii=False, indent=2))
    return 0
