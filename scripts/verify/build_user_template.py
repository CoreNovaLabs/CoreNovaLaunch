#!/usr/bin/env python3
"""把 network.yaml + app.yaml 合并成**单栈**的一键部署模板（用户自有 AWS 账号用）。

产出自包含模板：VPC/子网/SG + EC2 + cfn-init 装机 + 应用容器（镜像引用参数化）。
- contracts/app-schema.md §7（单容器）、architecture.md §9（自助部署=用户自己账号）
- platform-contract.md §8（SSM 运维，22 入站关闭，无 KeyPair）
- AMI：AmiId 必填，网站必须传入 Application Verification 所引用 Platform Contract 的
  不可变 AMI id。one-click 模板不再回退到“部署时最新”的公共 SSM 参数，避免验证对象
  与用户真正启动的主机漂移。

    python scripts/verify/build_user_template.py --out data/templates/corenova-one-click.template.yaml
    python scripts/verify/build_user_template.py --publish-s3   # 追加：发布到公开读桶（深链 URL 源）

输出与 app.yaml 的注释版不同属预期（yaml.safe_dump，注释不保留；canary.yaml 同理）。
"""

from __future__ import annotations

import argparse
import copy
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXED = ROOT / "templates" / "cloudformation" / "fixed"

# app.yaml 里这三个参数表达"挂到已有网络栈"；单栈模板由本模板自己的网络资源取代。
DROP_PARAMS = ("SubnetId", "SecurityGroupId", "NetworkStackName")

def build() -> dict:
    net = yaml.safe_load((FIXED / "network.yaml").read_text(encoding="utf-8"))
    app = yaml.safe_load((FIXED / "app.yaml").read_text(encoding="utf-8"))

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
    KEEP_OUTPUTS = {"InstanceId", "PublicIp", "PublicDnsName", "PrivateIp", "ResolvedLaunchUrl"}
    outputs: dict = {}
    for src in (net, app):
        for k, v in (src.get("Outputs") or {}).items():
            if k not in KEEP_OUTPUTS:
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out",
        default=str(ROOT / "data" / "templates" / "corenova-one-click.template.yaml"),
    )
    ap.add_argument(
        "--publish-s3",
        action="store_true",
        help="发布到公开读 S3 桶（TEMPLATE_S3_BUCKET；put 后匿名 GET 探测，不可读即失败）",
    )
    args = ap.parse_args()

    tpl = build()
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(tpl, sort_keys=False, allow_unicode=True, width=10_000),
        encoding="utf-8",
    )
    print(f"written: {out_path}")
    print(
        f"  params: {len(tpl['Parameters'])}  resources: {len(tpl['Resources'])}"
        f"  outputs: {len(tpl['Outputs'])}  conditions: {len(tpl['Conditions'])}"
    )

    text = out_path.read_text(encoding="utf-8")
    # 自包含校验：旧网络栈的参数引用必须重接；跨栈导出/导入一律不允许
    # （Export 与用户账号里既有的三栈导出同名冲突，ImportValue 在单栈里无源可导）。
    for bad in (
        "Ref: SubnetId\n",
        "Ref: SecurityGroupId\n",
        "Ref: NetworkStackName\n",
        "Export:\n",
        "Fn::ImportValue:",
    ):
        if bad in text:
            print(f"FAIL: merged template still references {bad.strip()}")
            return 1
    # 正向自检：用户模板必须要求不可变 AmiId，并原样接到 EC2 ImageId。
    if tpl["Parameters"].get("AmiId", {}).get("Type") != "AWS::EC2::Image::Id":
        print("FAIL: AmiId 必须保持 AWS::EC2::Image::Id 必填参数")
        return 1
    if tpl["Resources"].get("Instance", {}).get("Properties", {}).get("ImageId") != {"Ref": "AmiId"}:
        print("FAIL: EC2 ImageId 未直接引用已验证 AmiId")
        return 1
    print("rewire check: OK")

    if args.publish_s3:
        # 深链 templateURL 的唯一事实源（deployment-contract.md §2.4）：
        # 模板活在公开桶里，站点不再自托管副本。发布失败必须让本脚本退出非零。
        from corenova import template_publish
        from corenova.config import Config

        try:
            info = template_publish.publish(Config.load(), text.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 发布失败要红给 CI 看，不吞
            print(f"FAIL: S3 发布失败：{type(exc).__name__}: {exc}")
            return 1
        print(f"published: {info['url']} ({info['bytes']} bytes, {info['readable_via']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
