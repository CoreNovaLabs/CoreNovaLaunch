# Contract · Platform Contract（平台黄金契约）

> 优先级：**最高**。
> 术语：本文沿用 Repo A / Repo B / Repo C 代号，分别指 `CoreNovaLaunchWebsite`（官网，本地目录 `website/`）、`CoreNovaLaunchAmi`（AMI 构建，引导期未落地）、`CoreNovaLaunch`（验证枢纽）。
> 适用：Repo B `CoreNovaLaunchAmi` + Repo C `CoreNovaLaunch` 的 AWS Golden Verification 产物。
> 本文规定"平台（AMI/CFN/运行时）何时算验证过、何时失效"。任何设计文档与之冲突，以本文为准。

## 1. 目的

Application Verification 默认**不付 AWS 费用**，靠复用一份"已验证平台"契约。Platform Contract 就是这份契约：证明某一 `(ami_id, region, architecture)` 组合下的整套 AWS 部署链路曾经真实跑通。

## 2. Platform Contract 完整 Schema（v1.0）

```json
{
  "schema_version": "1.0",

  "platform_verification_id": "plat-us-east-1-x86_64-20260827-001",

  "ami_id": "ami-0abc123def4567890",
  "region": "us-east-1",
  "architecture": "x86_64",

  "cloudformation_revision": "git-sha-of-templates/cloudformation",
  "cfn_init_revision": "git-sha-of-cfn-init-scripts",
  "infrastructure_revision": "git-sha-of-iam-sg-network",
  "base_ami_revision": "git-sha-of-packer",
  "nginx_base_revision": "git-sha-of-nginx-base-config",
  "docker_runtime_revision": "git-sha-of-docker-setup",

  "verification": {
    "cfn_validated": true,
    "ec2_launched": true,
    "cfn_init_completed": true,
    "cfn_signal_received": true,
    "docker_runtime_ok": true,
    "nginx_ok": true,
    "ssm_ok": true,
    "cloudwatch_ok": true,
    "ebs_ok": true,
    "security_group_ok": true,
    "network_ok": true
  },

  "status": "valid",
  "platform_verified_at": "2026-08-27T10:00:00Z",
  "invalidated_at": null,
  "invalidated_reason": null,

  "base_ami_source": "public",
  "source_ami_name": "ubuntu/images/hvm-ssg/ubuntu-noble-24.04-amd64-server-*",
  "source_ami_account": "0996389817",
  "source_ssm_parameter": "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
  "ami_resolved_at": "2026-08-27T09:41:00Z",
  "reverify_interval_days": 30
}
```

### 2.1 两种基础镜像来源：`base_ami_source`

| 值 | 含义 | 何时用 |
|----|------|--------|
| `public` | 引用**社区/厂商公开 AMI**（Ubuntu LTS、Amazon Linux 2023 等），无镜像软件费；Docker / Nginx / SSM / CloudWatch 由 **cfn-init 在开机时安装** | 引导期（当前阶段）：先验证平台链路，不投入自建镜像成本 |
| `custom` | 引用 Repo B Packer 产出的自建 base AMI（后续含收费/上架形态） | 自建镜像就绪后切换 |

**公开模式（`public`）下的字段语义映射（否则 `*_revision` 会填不进去）：**

| 契约字段 | custom 模式来源 | public 模式来源 |
|---------|----------------|----------------|
| `base_ami_revision` | `packer/**` 的 git SHA（Repo B） | `public:<source_ssm_parameter>@<ami_id>`（解析值即身份，无 packer 资产） |
| `docker_runtime_revision` | Repo B `scripts/setup-docker.sh` SHA | Repo C `templates/cloudformation/init/**` 中安装 Docker 的资产 SHA |
| `nginx_base_revision` | Repo B Nginx 基础配置 SHA | Repo C `templates/cloudformation/init/**` 中 Nginx 配置模板 SHA |
| `cfn_init_revision` / `cloudformation_revision` / `infrastructure_revision` | Repo B/C 相应目录 SHA | 同（本就属 Repo C CFN 资产） |

**公开 AMI 的三个特有约束（补齐"厂商会滚动替换 AMI 内容"带来的静默失效风险）：**

1. **解析即钉死**：每次 Golden Verification 从 `source_ssm_parameter` 解析一次 `ami_id` 并写入契约，此后该契约的身份就是这个 `ami_id`，**不随 latest 漂移**。
2. **漂移检测**：Application Verification 的 `RESOLVED` 阶段除比对 `*_revision` 外，还须比对"SSM 公共参数现值 vs 有效契约记录的 `ami_id`"；不等 → 公开镜像已被替换 → 契约判 `invalid`，触发 Golden 复验（此比对为只读 SSM 调用，无 AWS 资源费用）。
3. **强制复验周期 ≤ 30 天**（`reverify_interval_days`）：公开 AMI 内部包版本会在 `ami_id` 不变的历史版本上随厂商更新滚动（新发布同名 AMI），故公开模式下 30 天复验为**硬性**要求，不同于 custom 模式的"建议"。

**切换到 `custom`（收费 AMI）时的改动面**：仅 `base_ami_source`、解析入口（SSM 参数名换为 `/corenova/ami/base/latest`）与 §6 的解析规则；契约字段集合、Application Verification 引用方式、`required_platform_contract_valid` 门禁语义**全部不变**——这是引导期选择公开 AMI 不产生返工的前提。收费 AMI 上线时需额外记录 `billing_product_codes`（证明用户订阅路径可用），届时作为新增可选字段引入。

## 3. 存储与解析

- 存于 R2：`platform/platform-contract-{region}-{architecture}.json`（如 `platform/platform-contract-us-east-1-x86_64.json`）。
- `status == "valid"` 的契约才能被 Application Verification 引用（`verification.platform = referenced`）。
- **解析入口按 `base_ami_source` 分岔**（§2.1）：
  - `public`（引导期）→ AWS 公共 SSM 参数，如 `/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id`；
  - `custom`（自建/收费 AMI）→ SSM `/corenova/ami/base/latest`。
  两条路径**都只作解析入口**（mutable pointer）；解析后固定为 `ami_id`，整个 workflow 不再二次查 SSM（见 §6）。
- **引导期未接入 R2 时的落盘位置**：Repo C 工作区 `data/platform/platform-contract-{region}-{arch}.json`（本地/自托管 runner 验证用）。该文件与 R2 同名对象是**同一份契约的两种存放**，任一时刻只能有一个生效后端（由 `PLATFORM_CONTRACT_BACKEND` 决定），禁止两处各存一份并让脚本择优读取——那会制造第二个事实源。接入 R2 后本地副本仅作审计，不再参与门禁。

## 4. AWS Golden Verification 触发条件（仅这些）

1. Base AMI 变更（`base_ami_revision` 变）
2. CloudFormation 基础设施变更（`cloudformation_revision` 变）
3. cfn-init 变更（`cfn_init_revision` 变）
4. Nginx 基础配置变更（`nginx_base_revision` 变）
5. IAM / Security Group / EBS / 网络基础设施变更（`infrastructure_revision` 变）
6. Docker 宿主机运行时变更（`docker_runtime_revision` 变）
7. OS / 基础平台变更（含镜像内 OS 包升级）
8. 手动 `workflow_dispatch`
9. 计划安全复验（如每 30 天）

**常规不触发 AWS：** 新应用版本、新 Docker 镜像、新应用接入、测试更新、应用配置更新——默认走 GitHub Application Verification，引用既有 Platform Contract。

## 5. 失效条件（status → invalid）

当出现以下任一变更，既有 Platform Contract 必须标 `status=invalid`，并要求重新跑 AWS Golden Verification：

- AMI 变更
- CloudFormation 变更
- cfn-init 变更
- Nginx 基础配置变更
- IAM 变更
- Security Group 变更
- Docker 宿主机变更
- OS 变更

**不导致失效：** Ghost 版本变、n8n 版本变、新增应用、Playwright 测试变——这些只动应用层，平台链路未变。

失效由 Repo C 在 `RESOLVED` 阶段比对 git revision 自动判定：任一 `*_revision` 与有效契约记录不符 → 触发 Golden Verification（或拒绝 Application Verification 并报警）。

## 6. SSM latest 仅作入口（不可变输入规则）

```
SSM /corenova/ami/base/latest  = mutable pointer（Repo B 每次构建覆盖）
        │
        │ resolve once（在 workflow 开头）
        ▼
AMI_ID=ami-0abc123def4567890   = immutable verification input
        │
        │ 本次 workflow 全程使用 ami-0abc...，禁止中途再查 latest
        ▼
写入 Verification Manifest / Platform Contract 的 ami_id
```

- 禁止 workflow 中途重新查询 `latest`（避免"验证到一半 AMI 被覆盖"导致结果不可复现）。
- Manifest / Platform Contract 必须记录最终的 `ami_id`（不可变），不记录 `latest`。

## 7. Region / Architecture（v1 写死）

- v1 仅支持 `x86_64`，单区域 `us-east-1`。
- 不允许 AI 自动扩展到 ARM；如需双架构，定义：
  - `/corenova/ami/base/x86_64/latest`
  - `/corenova/ami/base/arm64/latest`
  - 并各生成一份 `platform-contract-{region}-{arch}.json`。
- AMI 按区域构建（AMI 不跨 region），每个 region 独立 Platform Contract。

## 8. SSH Key Pair 歧义（明确删除）

- 基础 AMI 使用 **SSM Session Manager** 运维，关闭 `22` 入站。
- `KEY_PAIR_NAME` **不是默认必填项**；仅在显式 `debug mode` 设计下才允许，且默认关闭。
- 普通验证/部署流程中删除 `KEY_PAIR_NAME` 依赖。

## 9. CloudFormation Stack 模型（明确关系）

保持简单，第一版不引入多余 nested stack：

```
Network Stack      (corenova-network)      — VPC/Subnet/IGW/Route，建一次
Application Stack  (corenova-app-{app})    — 每应用一个，引用 Network Stack 输出 + SSM AMI
Canary Stack       (corenova-canary)       — Golden Verification 用，复用 Application Stack 模板，跑完保留或清理
```

- `DEPLOYING`/`DEPLOYED` 仅 Canary Stack 在 Platform Verification 中使用。
- 常规 Application Verification **不创建任何 Stack**。

## 10. 反模式

- ❌ 把 SSM `latest` 当不可变输入、中途反复查询。
- ❌ Manifest 只记 `latest` 不记 `ami_id`。
- ❌ 应用版本变化触发重新 Golden Verification。
- ❌ AI 自动扩展 ARM 架构。
- ❌ 默认依赖 `KEY_PAIR_NAME`。
- ❌ 常规应用验证创建 CFN Stack（产生 AWS 费用）。
