"""L1.5 生产核对（Production Check）— 验证通过后在真实 AWS 一次性栈上实证"可部署"。

填补"验证通过 ≠ 生产可部署"的缺口（deployment-contract.md §2.6）：新版本发布后，
在与官网深链同源的平台 AMI + 公开模板上为该应用建一次性用户栈，按 apps yaml 声明的
checks 逐项实测；全部通过即满足规则21 的解除条件，由 production-verify.yml 清 hold。

与 golden.py 同一纪律：实例侧探针只走 SSM、没有 SSH 路径；一次性栈必须删除且清理未
确认即报错（残留 = 持续计费）；plan() 完全离线可跑（--dry-run 零 AWS 调用）。
ImageReference 必须 `image@digest` 钉扎——核对的必须恰是刚发布并验证过的那份镜像，
否则绿灯证明的是另一个字节序列（深链同源性的根）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import golden
from .appspec import AppSpec
from .config import Config
from .template_publish import public_template_url
from .util import http_request, log, poll_until, sanitize_for_id, utcnow, write_json

# apps yaml 可声明的核对项（admin_auth/host_metrics 同时是模板参数开关；
# admin_auth 的实证落在 health_external 的 401/200 双探测里，不单独成项）。
DECLARED_CHECKS = ("admin_auth", "host_metrics", "data_dir_write", "url_injection")
# 固定执行序：health_external 恒跑，其余按声明追加。stack_created 由建栈结果填入。
EXECUTION_ORDER = ("health_external", "host_metrics", "data_dir_write", "url_injection")
STACK_PREFIX = "corenova-prodcheck"
STACK_TAG = {"Key": "corenova:prodcheck", "Value": "true"}
BILLING_TAG = {"Key": "corenova:billing", "Value": "prodcheck-temporary"}
# 栈创建超时：EC2 启动 + cfn-init 装 Docker/Nginx + 应用容器就绪 + WaitCondition ack。
CREATE_TIMEOUT_MINUTES = 30
SSM_READY_TIMEOUT_MINUTES = 15
# 健康探针短轮询预算：应用首启（建库/迁移）可慢，但一次性核对不值得无限等。
HEALTH_POLL_TIMEOUT_S = 300
HEALTH_POLL_INTERVAL_S = 15
CRED_FILE = "/opt/corenova/credentials/admin.txt"
ADMIN_USER = "corenova"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


# --------------------------------------------------------------------------- dataclasses


@dataclass
class ProdPlan:
    """一次性栈的完整计划；plan() 纯函数产出，dry-run 只打印它，不碰 AWS。"""

    stack_name: str
    parameters: dict[str, str]
    checks: list[str]                 # 实际执行的探针名（含恒跑的 health_external）
    declared: list[str]               # apps yaml 声明的原样列表（证据用）
    template_url: str
    tags: list[dict[str, str]] = field(default_factory=list)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CheckCtx:
    """探针执行上下文：AWS 交互全部经 run_script/http_get 注入，测试喂假响应。"""

    plan: ProdPlan
    instance_id: str
    public_dns: str
    container: str            # systemd unit 里的容器名即 app name（30-app-container.sh）
    image_ref: str
    data_path: str
    url_env_names: list[str] = field(default_factory=list)
    secret: str = ""          # 运行期取回的 admin 密码；只在此内存对象中，落盘前脱敏
    run_script: Callable[[str], golden.Invocation] = lambda _s: golden.Invocation()
    http_get: Callable[[str, dict[str, str]], int] = lambda _u, _h: 0


@dataclass
class ProdCheckReport:
    """一次核对的全部事实，序列化为证据 JSON。"""

    app: str = ""
    app_version: str = ""
    run_id: str = ""
    stack_name: str = ""
    template_url: str = ""
    region: str = ""
    instance_id: str = ""
    public_dns: str = ""
    declared_checks: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    all_passed: bool = False
    cleanup: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=utcnow)
    finished_at: str = ""


# --------------------------------------------------------------------------- plan


def stack_name_for(app: str, version: str, run_id: str) -> str:
    """corenova-prodcheck-<app>-<version去点>-<run_id尾6位>：CFN 栈名只允许 [A-Za-z0-9-]。"""
    ver = re.sub(r"[^a-z0-9-]", "", sanitize_for_id(version).replace(".", ""))
    tail = re.sub(r"[^A-Za-z0-9-]", "", str(run_id))[-6:] or "local"
    return f"{STACK_PREFIX}-{sanitize_for_id(app).strip('-')}-{ver}-{tail}".lower()


def pinned_image(manifest: dict[str, Any]) -> str:
    """ImageReference = container.image + '@' + digest：钉扎到被验证的那份镜像。"""
    image = str(manifest.get("container", {}).get("image") or "")
    digest = str(manifest.get("container", {}).get("digest") or "")
    if not image:
        raise ValueError("manifest.container.image 缺失，无法构造钉扎引用")
    if "@" in image:
        raise ValueError(f"manifest.container.image 不应已含 digest：{image!r}")
    if not DIGEST_RE.match(digest):
        raise ValueError(f"manifest.container.digest 不是 sha256 钉扎值：{digest!r}（生产核对拒绝浮动 tag）")
    return f"{image}@{digest}"


def url_env_names_from(manifest: dict[str, Any]) -> list[str]:
    """声明接收实例公网 URL 的环境变量名：app_url_env_name + extra_environment 里
    引用了 ${CORENOVA_APP_URL} 的键（vikunja 走后者；模板在实例内展开占位符）。"""
    deploy = manifest.get("website", {}).get("deploy", {}) or {}
    names: list[str] = []
    native = deploy.get("app_url_env_name")
    if isinstance(native, str) and native:
        names.append(native)
    for item in deploy.get("extra_environment") or []:
        key, _, value = str(item).partition("=")
        if "CORENOVA_APP_URL" in value:
            names.append(key)
    return names


def plan(
    cfg: Config,
    spec: AppSpec,
    manifest: dict[str, Any],
    *,
    run_id: str = "",
    template_url: str | None = None,
) -> ProdPlan:
    """apps yaml + Manifest → 一次性栈参数。与官网深链同源：同一份公开模板、
    同一钉扎 AMI、同一 digest 镜像，只差 AdminAuth/HostMetrics 两个核对开关。

    用公开 one-click 合并模板建栈：网络（VPC/子网/SG）由模板自包含，其
    SubnetId/SecurityGroupId/NetworkStackName 参数已被合并器 DROP（usertemplate.
    DROP_PARAMS）——传这三个会触发 CFN "unused parameters" 校验失败，故此处不发。"""
    declared = list(spec.g("deployment.production_contract.checks") or [])
    unknown = [c for c in declared if c not in DECLARED_CHECKS]
    if unknown:
        raise ValueError(f"production_contract.checks 含未知项 {unknown}（允许 {list(DECLARED_CHECKS)}）")
    if not declared:
        raise ValueError("deployment.production_contract.checks 为空：无声明的应用不该进入生产核对")

    deploy = manifest.get("website", {}).get("deploy") or {}
    platform = manifest.get("platform") or {}
    ami_id = str(platform.get("ami_id") or "")
    if not ami_id.startswith("ami-"):
        raise ValueError(f"manifest.platform.ami_id 非法：{ami_id!r}（必须复用 Platform Contract 钉扎值）")

    app = str(manifest.get("app") or spec.name)
    version = str(manifest.get("app_version") or "")
    stack = stack_name_for(app, version, run_id)
    admin_auth = "admin_auth" in declared
    host_metrics = "host_metrics" in declared

    params: dict[str, str] = {
        "AppName": app,
        "ImageReference": pinned_image(manifest),
        "ContainerPort": str(deploy["container_port"]),
        "AmiId": ami_id,
        "InstanceType": str(deploy["instance_type"]),
        "DataVolumeSize": str(deploy["data_volume_gb"]),
        "DataContainerPath": str(deploy.get("data_path") or "/data"),
        "HealthCheckPath": str(deploy.get("health_check_path") or "/"),
        "CloudWatchLogGroupName": f"/corenova/prodcheck/{stack}",
        # 一次性核对栈绝不能停在"删不掉"的状态：TerminationProtection=Enabled 会让
        # delete-stack 失败——显式 Disable 保证清理步任何时候都能终止实例。
        "TerminationProtection": "Disabled",
        # AllowedWebCidr 两分支（简单方案）：声明 admin_auth 的应用开 0.0.0.0/0，
        # Basic auth 是第二道门——health_external 的"无凭据 401/带凭据 200"正是这道门
        # 的实证；未声明 admin_auth 的应用（code-server、vikunja）以 127.0.0.1/32 对
        # 公网关闭，健康探测改经 SSM 在实例内 curl 127.0.0.1——docker run→nginx 反代→
        # 应用应答全链路仍被覆盖，缺的只是"公网→SG→nginx"一跳，不为核对引入隧道编排。
        "AllowedWebCidr": "0.0.0.0/0" if admin_auth else "127.0.0.1/32",
        "AdminAuthEnabled": "true" if admin_auth else "false",
        "HostMetricsAccess": "true" if host_metrics else "false",
    }
    if deploy.get("app_url_env_name"):
        params["AppUrlEnvironmentName"] = str(deploy["app_url_env_name"])
    extra_env = deploy.get("extra_environment") or []
    if extra_env:
        # 模板要求换行分隔的 KEY=VALUE（ExtraEnvironment 参数契约）
        params["ExtraEnvironment"] = "\n".join(str(item) for item in extra_env)

    checks = ["health_external"] + [c for c in EXECUTION_ORDER if c != "health_external" and c in declared]
    return ProdPlan(
        stack_name=stack,
        parameters=params,
        checks=checks,
        declared=declared,
        template_url=template_url if template_url is not None
        else public_template_url(cfg.template_bucket, cfg.template_s3_region),
        tags=[STACK_TAG, BILLING_TAG, {"Key": "corenova:purpose", "Value": "production-check"}],
    )


# --------------------------------------------------------------------------- probes


def _redact(ctx: CheckCtx, text: str) -> str:
    """密码只允许存在于内存：任何 detail/日志落笔前统一脱敏。"""
    if ctx.secret:
        text = text.replace(ctx.secret, "***")
    return text[:600]


def _http_status(ctx: CheckCtx, url: str, headers: dict[str, str]) -> int:
    try:
        status, _, _ = ctx.http_get(url, headers)
        return int(status)
    except Exception as exc:  # noqa: BLE001 - 探测异常按未就绪计，交给轮询收敛
        log(f"http 探测异常（重试中）：{type(exc).__name__}: {exc}")
        return 0


def _fetch_admin_credentials(ctx: CheckCtx) -> bool:
    """经 SSM 取回 Basic auth 密码（root:root 0600，只有 root shell 读得到）。
    失败不抛——detail 里不能出现文件内容的任何片段。"""
    inv = ctx.run_script(f"cat {shlex.quote(CRED_FILE)}")
    if not inv.ok or ":" not in inv.out:
        return False
    _, _, password = inv.out.strip().partition(":")
    ctx.secret = password
    return bool(password)


def _check_health_external(ctx: CheckCtx) -> CheckResult:
    if ctx.plan.parameters.get("AdminAuthEnabled") == "true":
        return _health_with_admin_auth(ctx)
    return _health_via_loopback(ctx)


def _health_with_admin_auth(ctx: CheckCtx) -> CheckResult:
    """外部真实路径：无凭据必须 401（门有效），带凭据必须 2xx/3xx（应用活着）。
    任一条不满足都不能解除 hold——401 消失意味着保护失效。"""
    if not _fetch_admin_credentials(ctx):
        return CheckResult("health_external", False, "SSM 读取管理凭据失败（admin.txt 不存在或为空？）")
    token = base64.b64encode(f"{ADMIN_USER}:{ctx.secret}".encode()).decode()
    url = f"http://{ctx.public_dns}{ctx.plan.parameters.get('HealthCheckPath', '/')}"
    seen = {"anon": 0, "authed": 0}

    def probe() -> bool | None:
        seen["anon"] = _http_status(ctx, url, {})
        seen["authed"] = _http_status(ctx, url, {"Authorization": f"Basic {token}"})
        return True if (seen["anon"] == 401 and 200 <= seen["authed"] < 400) else None

    ok = poll_until(probe, timeout_s=HEALTH_POLL_TIMEOUT_S, interval_s=HEALTH_POLL_INTERVAL_S) is True
    detail = (
        f"外部无凭据={seen['anon']} 带凭据={seen['authed']}"
        f"（预期 401/2xx；本机 127.0.0.1 经 geo 空 realm 免认证不在此路径）"
    )
    return CheckResult("health_external", bool(ok), _redact(ctx, detail))


def _health_via_loopback(ctx: CheckCtx) -> CheckResult:
    """AllowedWebCidr=127.0.0.1/32 的分支：公网打不进来是设计使然，探针经 SSM 在
    实例内 curl 127.0.0.1——同一 nginx 反代路径，只差公网一跳（原因见 plan() 注释）。"""
    path = ctx.plan.parameters.get("HealthCheckPath", "/")
    seen = {"code": "000"}

    def probe() -> str | None:
        inv = ctx.run_script(
            "curl -s -o /dev/null -w 'code=%{http_code}\\n' --max-time 15 "
            f"http://127.0.0.1:80{shlex.quote(path)}"
        )
        m = re.search(r"code=(\d{3})", inv.out)
        if m and 200 <= int(m.group(1)) < 400:
            return m.group(1)
        seen["code"] = m.group(1) if m else "000"
        return None

    code = poll_until(probe, timeout_s=HEALTH_POLL_TIMEOUT_S, interval_s=HEALTH_POLL_INTERVAL_S)
    ok = code is not None
    return CheckResult("health_external", bool(ok), _redact(
        ctx, f"实例内经 :80 反代 HTTP={code or seen['code']}（入口对公网关闭，非缺陷）"))


def _check_data_dir_write(ctx: CheckCtx) -> CheckResult:
    """以镜像自身用户实测数据目录可写：init 的 chown 是否把 bind mount 交给了
    正确 uid，只有 docker exec -u <image user> 能证明（root 写永远成功，测不出权限）。"""
    image = shlex.quote(ctx.image_ref)
    container = shlex.quote(ctx.container)
    marker = f"{ctx.data_path.rstrip('/')}/.corenova-probe"
    inner = shlex.quote(f"touch {shlex.quote(marker)} && rm -f {shlex.quote(marker)}")
    inv = ctx.run_script(
        "set -e\n"
        f"uid_gid=$(docker image inspect --format '{{{{.Config.User}}}}' {image})\n"
        # 空 / root / 0 都是 root：docker exec -u "" 会报 valid specification 错。
        'case "$uid_gid" in ""|root|0) uid_gid=0 ;; esac\n'
        f"docker exec -u \"$uid_gid\" {container} sh -c {inner}\n"
        "echo write-ok"
    )
    ok = inv.exit_code == 0 and "write-ok" in inv.out
    detail = ("数据目录以镜像用户写入回读成功" if ok
              else f"写入探测失败：{(inv.out or inv.error)[:300]}")
    return CheckResult("data_dir_write", ok, _redact(ctx, detail))


def _check_url_injection(ctx: CheckCtx) -> CheckResult:
    """URL 注入是"部署后应用自报地址对不对"的根（Ghost/vikunja 类应用配错公网
    URL 会把所有绝对链接指向 localhost），必须在容器 env 里以实例公网 DNS 实证。"""
    if not ctx.url_env_names:
        return CheckResult("url_injection", False, "应用未声明任何接收 URL 的环境变量，无从核对")
    if not ctx.public_dns:
        return CheckResult("url_injection", False, "实例没有公网 DNS，无法断言注入值")
    inv = ctx.run_script(f"docker exec {shlex.quote(ctx.container)} env")
    if inv.exit_code != 0:
        return CheckResult("url_injection", False, _redact(ctx, f"docker exec env 失败：{inv.out[:200]} {inv.error[:200]}"))
    actual: dict[str, str] = {}
    for line in inv.out.splitlines():
        key, _, value = line.partition("=")
        if key:
            actual[key] = value
    want = f"http://{ctx.public_dns}"
    bad: list[str] = []
    for name in ctx.url_env_names:
        value = actual.get(name, "")
        # 注入的根 URL 不带斜杠；应用配置可能约定带斜杠或在其后接路径
        # （如 PUBLIC_HOOK=${CORENOVA_APP_URL}/hook）。故以"根地址为前缀"实证，
        # 尾斜杠归一，但绝不允许指向 localhost/别的 host。
        root = value.rstrip("/")
        if not (root == want or root.startswith(want + "/")):
            bad.append(f"{name}={value[:120]!r}")
    ok = not bad
    detail = "全部 URL 变量以实例公网地址为值：" + ",".join(ctx.url_env_names) if ok \
        else "不符：" + "; ".join(bad)
    return CheckResult("url_injection", ok, _redact(ctx, detail))


def _check_host_metrics(ctx: CheckCtx) -> CheckResult:
    """HostMetricsAccess 参数翻到 docker run 命令行上的挂载，只有容器内部看得见才算生效。"""
    inv = ctx.run_script(
        f"docker exec {shlex.quote(ctx.container)} sh -c 'test -d /host/proc/1 && echo metrics-ok'"
    )
    ok = inv.exit_code == 0 and "metrics-ok" in inv.out
    return CheckResult("host_metrics", ok, _redact(
        ctx, "容器内 /host/proc/1 可见" if ok else f"不可见：{(inv.out or inv.error)[:200]}"))


CHECK_FNS: dict[str, Callable[[CheckCtx], CheckResult]] = {
    "health_external": _check_health_external,
    "data_dir_write": _check_data_dir_write,
    "url_injection": _check_url_injection,
    "host_metrics": _check_host_metrics,
}


def run_checks(ctx: CheckCtx) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name in ctx.plan.checks:
        try:
            result = CHECK_FNS[name](ctx)
        except Exception as exc:  # noqa: BLE001 - 单项探针异常不中断其余测量（golden 同规则）
            result = CheckResult(name, False, _redact(ctx, f"{type(exc).__name__}: {exc}"))
        log(f"核对 {name:<16}{'PASS' if result.passed else 'FAIL'}  {result.detail[:200]}")
        results.append(result)
    return results


# --------------------------------------------------------------------------- stack lifecycle


def _stack_status(aws: golden.Aws, stack_name: str) -> str:
    try:
        return aws.cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except Exception:  # noqa: BLE001 - CFN 对不存在的栈抛 ValidationError
        return ""


def _wait_create(aws: golden.Aws, stack_name: str) -> tuple[bool, str]:
    """CREATE_COMPLETE 才算数：模板末尾的 WaitCondition 要求实例内 cfn-signal 成功，
    所以这一项同时证明"平台链路 + 应用容器就绪"（golden 因 ack 不稳改读 signal.log，
    一次性核对栈没有这个历史包袱，信整栈状态更严格）。"""

    def probe() -> str | None:
        status = _stack_status(aws, stack_name)
        if status == "CREATE_COMPLETE":
            return status
        if status.endswith("_FAILED") or status.startswith("ROLLBACK"):
            return status
        return None

    status = poll_until(probe, timeout_s=CREATE_TIMEOUT_MINUTES * 60, interval_s=15)
    if status == "CREATE_COMPLETE":
        return True, "CREATE_COMPLETE"
    if status is None:
        return False, f"创建超时（{CREATE_TIMEOUT_MINUTES} 分钟），最后状态={_stack_status(aws, stack_name) or 'IN_PROGRESS'}"
    return False, f"{status}: {golden._stack_reason(aws, stack_name)}"


def _create_stack(aws: golden.Aws, p: ProdPlan) -> None:
    """栈名带 run_id 后缀天然唯一；同名残留只可能是上次中断的失败回滚，删净再建。"""
    status = _stack_status(aws, p.stack_name)
    if status:
        if not status.startswith("DELETE_"):
            aws.cfn.delete_stack(StackName=p.stack_name)
        if poll_until(
            lambda: True if not _stack_status(aws, p.stack_name) else None,
            timeout_s=20 * 60, interval_s=10,
        ) is None:
            raise RuntimeError(f"残留栈 {p.stack_name} 未能在 20 分钟内删净（残留=计费）")
    if not p.template_url:
        raise RuntimeError(
            "TEMPLATE_S3_BUCKET 未配置：生产核对必须用公开模板对象建栈（TemplateURL），"
            "与官网深链逐字节同源；不提供 TemplateBody 兜底，避免核对到未发布的模板副本。"
        )
    aws.cfn.create_stack(
        StackName=p.stack_name,
        TemplateURL=p.template_url,
        Parameters=golden.as_cfn_parameters(p.parameters),
        Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
        Tags=p.tags,
    )


def _cleanup_volumes(aws: golden.Aws, captured: list[str], notes: list[str]) -> bool:
    """删栈后按 tag 扫残留 EBS 并删除。

    模板数据卷 DeleteOnTermination=false（对用户是特性，对一次性栈是计费泄漏），
    栈级 tag 经 PropagateTagsToVolumeOnCreation 落在卷上；卷在实例存活期间恒为
    in-use，所以 available+prodcheck 标签即残留（本方或并发核对方泄漏的都收）。
    """
    clean = True

    def describe(volume_id: str) -> dict[str, Any] | None:
        try:
            volumes = aws.ec2.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
        except Exception as exc:  # noqa: BLE001 - NotFound 即已消失
            if "does not exist" in str(exc) or "InvalidVolume.NotFound" in str(exc):
                return None
            raise
        return volumes[0] if volumes else None

    targets = list(dict.fromkeys(captured))
    try:
        listed = aws.ec2.describe_volumes(Filters=[
            {"Name": "tag:corenova:prodcheck", "Values": ["true"]},
            {"Name": "status", "Values": ["available"]},
        ]).get("Volumes", [])
        targets += [v["VolumeId"] for v in listed if v.get("VolumeId")]
    except Exception as exc:  # noqa: BLE001 - 扫描是保险，失败不推翻逐项删除
        clean = False
        notes.append(f"prodcheck 标签卷扫描失败（请手动核对残留卷）：{type(exc).__name__}: {exc}")

    for volume_id in dict.fromkeys(targets):
        try:
            volume = describe(volume_id)
            if volume is None:
                continue
            tags = {str(t.get("Key")): str(t.get("Value")) for t in volume.get("Tags") or []}
            # 只删自己创建的卷：captured 里出现无标签卷说明传播假设破了，宁可留人工。
            if tags.get("corenova:prodcheck") != "true":
                clean = False
                notes.append(f"数据卷 {volume_id} 缺少 prodcheck 标签，拒绝删除")
                continue
            if volume.get("State") != "available":
                clean = False
                notes.append(f"数据卷 {volume_id} 状态 {volume.get('State')}，无法删除，必须人工处理")
                continue
            aws.ec2.delete_volume(VolumeId=volume_id)
            notes.append(f"数据卷 {volume_id} 已删除")
        except Exception as exc:  # noqa: BLE001
            clean = False
            notes.append(f"数据卷 {volume_id} 删除失败：{type(exc).__name__}: {exc}")
    return clean


def destroy_stack(aws: golden.Aws, p: ProdPlan, instance) -> tuple[bool, list[str]]:
    """删栈并等 DELETE_COMPLETE，再收残留卷——未确认清理必须报错（golden 同规则）。"""
    notes: list[str] = []
    try:
        aws.cfn.delete_stack(StackName=p.stack_name)
        notes.append(f"已发起删除 {p.stack_name}")
    except Exception as exc:  # noqa: BLE001
        return False, [f"delete_stack 失败：{type(exc).__name__}: {exc}"]

    def probe() -> bool | None:
        status = _stack_status(aws, p.stack_name)
        if not status or status == "DELETE_COMPLETE":
            return True
        if status == "DELETE_FAILED":
            notes.append(f"DELETE_FAILED：{golden._stack_reason(aws, p.stack_name)}")
            return False
        return None

    gone = poll_until(probe, timeout_s=20 * 60, interval_s=10)
    if gone is not True:
        notes.append(f"栈 {p.stack_name} 未在时限内删净（残留=持续计费）")
    instance_id = getattr(instance, "instance_id", "")
    volume_ids = list(getattr(instance, "volume_ids", []) or [])
    volumes_clean = _cleanup_volumes(aws, volume_ids, notes)
    if instance_id and _instance_running(aws, instance_id):
        notes.append(f"实例 {instance_id} 仍在运行 —— 必须人工终止")
        return False, notes
    return gone is True and volumes_clean, notes


def _instance_running(aws: golden.Aws, instance_id: str) -> bool:
    try:
        res = aws.ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    except Exception:  # noqa: BLE001 - 栈删净后实例必然消失，查询失败按已消失处理，卷扫描兜底
        return False
    return (res.get("State") or {}).get("Name", "") in ("pending", "running", "stopping")


# --------------------------------------------------------------------------- evidence


def evidence_path(cfg: Config, report: ProdCheckReport) -> Path:
    ts = report.started_at.replace(":", "").replace("-", "")
    return Path(cfg.output_dir) / "runs" / f"prodcheck-{sanitize_for_id(report.app)}-{sanitize_for_id(report.app_version)}-{ts}.json"


def summary_markdown(report: ProdCheckReport) -> str:
    lines = [
        "## L1.5 生产核对（production check）",
        "",
        f"- 应用/版本：`{report.app}` @ `{report.app_version}`",
        f"- 一次性栈：`{report.stack_name}`（region={report.region}）",
        f"- 结论：**{'全部通过，可解除部署暂停' if report.all_passed else '未通过，hold 保持不变'}**",
        "",
        "| 核对项 | 结果 | 实测 |",
        "| --- | --- | --- |",
    ]
    for check in report.checks:
        mark = "PASS" if check.get("passed") else "FAIL"
        detail = str(check.get("detail", "")).replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {check.get('name')} | {mark} | {detail} |")
    if report.cleanup:
        lines += ["", "清理：", *[f"- {n}" for n in report.cleanup]]
    if report.notes:
        lines += ["", "备注：", *[f"- {n}" for n in report.notes]]
    return "\n".join(lines) + "\n"


def _emit_report(cfg: Config, report: ProdCheckReport) -> None:
    path = evidence_path(cfg, report)
    write_json(path, asdict(report))
    log(f"核对证据：{path}")
    # golden 不落 step summary（由 workflow 打印）；这里若身处 Actions 就顺手追加，
    # 失败/中断的 run 也能在摘要里看到已测到哪一项。
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write("\n" + summary_markdown(report))


# --------------------------------------------------------------------------- manifest 读取 / 编排


def load_manifest(backend, app: str, version: str = "") -> dict[str, Any]:
    """读已发布 Manifest（versions/<ver>.json）——核对的对象必须是发布过的那份证据。
    不给 version 时取 current.json 指向的版本（发布后立即核对的自然选择）。"""
    app_key = sanitize_for_id(app)
    if not version:
        raw = backend.get(f"verified/{app_key}/current.json")
        if not raw:
            raise RuntimeError(f"{app} 尚无已发布的 current.json，无法确定核对版本")
        version = str(json.loads(raw).get("app_version") or "")
    key = f"verified/{app_key}/versions/{sanitize_for_id(version)}.json"
    raw = backend.get(key)
    if not raw:
        raise RuntimeError(f"读取发布 Manifest 失败（未发布过该版本？）：{key}")
    return json.loads(raw)


def run(
    backend,
    cfg: Config,
    spec: AppSpec,
    manifest: dict[str, Any],
    aws: golden.Aws,
    *,
    keep: bool = False,
) -> dict[str, Any]:
    """建一次性栈 → 逐项核对 → finally 删栈收卷。返回可序列化的核对报告。"""
    # 一次性栈用公开 one-click 合并模板：网络自包含，无前置 network 栈依赖（plan() 注释）。
    p = plan(
        cfg, spec, manifest,
        run_id=os.environ.get("GITHUB_RUN_ID") or time.strftime("%H%M%S", time.gmtime()),
    )
    report = ProdCheckReport(
        app=p.parameters["AppName"],
        app_version=str(manifest.get("app_version") or ""),
        run_id=p.stack_name.rsplit("-", 1)[-1],
        stack_name=p.stack_name,
        template_url=p.template_url,
        region=cfg.region,
        declared_checks=list(p.declared),
    )
    current_raw = backend.get(f"verified/{sanitize_for_id(report.app)}/current.json")
    if current_raw:
        published = str(json.loads(current_raw).get("app_version") or "")
        if published and published != report.app_version:
            report.notes.append(f"核对版本 {report.app_version} 不是当前发布版本 {published}（结果只对该版本生效）")

    instance = golden.Canary(stack_name=p.stack_name)
    deployed = False
    try:
        try:
            _create_stack(aws, p)
            deployed = True
        except Exception as exc:  # noqa: BLE001 - 建栈请求本身失败也要留下 stack_created 证据
            report.checks.append(
                asdict(CheckResult("stack_created", False, f"{type(exc).__name__}: {exc}"[:600]))
            )
            report.cleanup.append("建栈请求未成功，无栈可清理")
            report.all_passed = False
            return asdict(report) | {"plan": {"parameters": p.parameters, "checks": p.checks}}
        ok, detail = _wait_create(aws, p.stack_name)
        report.checks.append(asdict(CheckResult("stack_created", ok, detail)))
        if ok:
            # CREATE_COMPLETE 已含 cfn-signal（WaitCondition），实例与容器就绪；
            # 探针通道仍需单独确认可达（SSM agent 晚于 signal 注册实测发生过）。
            instance = golden.read_canary(aws, p.stack_name)
            report.instance_id, report.public_dns = instance.instance_id, instance.public_dns
            try:
                golden._wait_for_ssm_ready(aws, instance.instance_id, timeout_minutes=SSM_READY_TIMEOUT_MINUTES)
            except Exception as exc:  # noqa: BLE001
                report.checks.extend(
                    asdict(CheckResult(name, False, f"SSM 通道不可达：{type(exc).__name__}"))
                    for name in p.checks
                )
            else:
                ctx = CheckCtx(
                    plan=p,
                    instance_id=instance.instance_id,
                    public_dns=instance.public_dns,
                    container=p.parameters["AppName"],
                    image_ref=p.parameters["ImageReference"],
                    data_path=p.parameters["DataContainerPath"],
                    url_env_names=url_env_names_from(manifest),
                    run_script=lambda script: golden.ssm_run(aws, instance.instance_id, script),
                    http_get=lambda url, headers: http_request(url, headers=headers, timeout=20, retries=0),
                )
                report.checks.extend(asdict(r) for r in run_checks(ctx))
    finally:
        if deployed and not keep:
            gone, notes = destroy_stack(aws, p, instance)
            report.cleanup += notes
            for note in notes:
                log(f"清理：{note}")
            if not gone:
                report.notes.append("核对栈清理未确认 —— 残留资源持续计费，必须人工处理")
        elif deployed:
            report.cleanup.append(f"keep-stack → 保留 {p.stack_name}（栈内资源在计费，调试完手动 delete-stack 并删数据卷）")
        report.all_passed = bool(report.checks) and all(c["passed"] for c in report.checks)
        report.finished_at = utcnow()
        _emit_report(cfg, report)

    return asdict(report) | {"plan": {"parameters": p.parameters, "checks": p.checks}}

# hold 的解除（deployment.hold 行级手术）在 corenova/holds.py：strip_hold /
# strip_hold_file 是 hold 生命周期唯一归属。production-verify.yml 核对全绿后调用，
# 保留注释与键序、结构异常拒绝盲删。
