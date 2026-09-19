"""one-click 用户模板的合并构造（build_user_template.py 的逻辑层）。

模板内容 **app 无关**，只在 templates/cloudformation/fixed/*.yaml 变化时变化，
由 publish-template.yml 合并后单对象覆盖发布到公开桶（无版本号对象键）。
因此验证证据必须显式记录"验证时用户模板"的内容 SHA（template_revision，
deployment-contract.md §2.4）：current.json 随证据一起发布，模板再次发布不改变
既有证据，但新的验证将绑定新 revision——模板与证据是否匹配由此可查，
而不是靠发布时间戳去猜。
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import yaml

# app.yaml 里这三个参数表达"挂到已有网络栈"；单栈模板由本模板自己的网络资源取代。
DROP_PARAMS = ("SubnetId", "SecurityGroupId", "NetworkStackName")


def fixed_dir(root: Path) -> Path:
    return root / "templates" / "cloudformation" / "fixed"


def build(root: Path) -> dict:
    """把 network.yaml + app.yaml 合并成单栈自包含模板（与发布物同一份输出）。"""
    fixed = fixed_dir(root)
    net = yaml.safe_load((fixed / "network.yaml").read_text(encoding="utf-8"))
    app = yaml.safe_load((fixed / "app.yaml").read_text(encoding="utf-8"))

    conditions: dict = {}
    for src in (net, app):
        for k, v in (src.get("Conditions") or {}).items():
            conditions[k] = copy.deepcopy(v)
    resources: dict = {}
    for name, spec in net["Resources"].items():
        resources[name] = copy.deepcopy(spec)
    for name, spec in app["Resources"].items():
        spec = copy.deepcopy(spec)
        text = yaml.safe_dump(spec, sort_keys=False)
        text = (
            text.replace("Ref: SubnetId\n", "Ref: PublicSubnetA\n")
            .replace("Ref: SecurityGroupId\n", "Ref: BaseSG\n")
            .replace("Ref: NetworkStackName\n", "Ref: AWS::StackName\n")
        )
        resources[name] = yaml.safe_load(text)

    parameters: dict = {}
    for src in (net, app):
        for k, v in src["Parameters"].items():
            if k in DROP_PARAMS:
                continue
            v = copy.deepcopy(v)
            if k == "TerminationProtection":
                # 一键评估默认不锁定实例（用户可显式选 Enabled）；三栈生产模板保持 Enabled
                v["Default"] = "Disabled"
            parameters[k] = v

    # 一键部署用户只需要入口地址和定位实例的 ID，其余是三栈内部落地细节（噪声）。
    keep_outputs = {"InstanceId", "PublicIp", "PublicDnsName", "PrivateIp", "ResolvedLaunchUrl"}
    outputs: dict = {}
    for src in (net, app):
        for k, v in (src.get("Outputs") or {}).items():
            if k not in keep_outputs:
                continue
            v = copy.deepcopy(v)
            # 单栈模板自包含：Export 面向三栈架构（network 被其他栈消费），
            # 保留会在用户账号里与既有 corenova-network 栈的导出名同名冲突
            # （"Export with name corenova-network-VpcId is already exported"
            #   -> CREATE 即回滚，2026-08-31 线上事故）。Outputs 的 Value 照留。
            v.pop("Export", None)
            outputs[k] = v

    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "CoreNova Launch — one-click deploy of a CoreNova-verified application "
            "into your own AWS account. Single stack: VPC + SSM-only EC2 host "
            "(Docker via cfn-init, port 22 closed) running the exact image that "
            "passed verification. Docs: https://corenova-website.pages.dev/docs/verification"
        ),
        "Parameters": parameters,
        "Conditions": conditions,
        "Resources": resources,
        "Outputs": outputs,
        "Metadata": {
            "AWS::CloudFormation::Interface": {
                "ParameterGroups": [
                    {"Label": {"default": "Application"}, "Parameters": [
                        "AppName", "ImageReference", "ContainerPort", "HealthCheckPath",
                        "DataContainerPath", "AppUrlEnvironmentName", "ExtraEnvironment",
                    ]},
                    {"Label": {"default": "Host"}, "Parameters": [
                        "InstanceType", "DiskGb", "DataVolumeSize", "AmiId",
                    ]},
                    {"Label": {"default": "访问控制与上传"}, "Parameters": [
                        "AllowedWebCidr", "HttpIngressCidr", "MaxUploadSizeMb",
                        "LaunchUrl", "Hostnames", "TlsPemPath", "SelfSignedTls",
                    ]},
                    {"Label": {"default": "显式启用的能力（非默认验证配置）"}, "Parameters": [
                        "DockerSocketAccess", "ExtraTcpPort", "ExtraUdpPort", "ExtraPortIngressCidr",
                    ]},
                ],
                "ParameterLabels": {
                    "ImageReference": {
                        "default": "Image (exact tag) — keep the digest-pinned value"
                    },
                    "AppName": {"default": "Application name"},
                },
            }
        },
    }


def merged_text(root: Path) -> str:
    """与 build_user_template.py 落盘/发布完全一致的单栈模板文本。"""
    return yaml.safe_dump(build(root), sort_keys=False, allow_unicode=True, width=10_000)


def revision(root: Path) -> str:
    """合并输出的内容 SHA（截断到 40 hex，与 file_sha 同一口径）。"""
    digest = hashlib.sha256(merged_text(root).encode("utf-8")).hexdigest()
    return digest[:40]
