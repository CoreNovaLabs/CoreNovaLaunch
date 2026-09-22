# Contract · Verification Manifest（验证清单）

> 优先级：**最高**。
> 术语：本文沿用 Repo A / Repo B / Repo C 代号，分别指 `CoreNovaLaunchWebsite`（官网，本地目录 `website/`）、`CoreNovaLaunchAmi`（AMI 构建，引导期未落地）、`CoreNovaLaunch`（验证枢纽）。
> 适用：Repo C `CoreNovaLaunch` 产出的验证记录，上传至 R2 `verified/{app}/current.json` 与 `verified/{app}/versions/{app_version}.json`。
> 本文定义"验证了什么"的不可变记录格式。任何设计文档与之冲突，以本文为准。

## 1. 为什么需要 Verification Identity

旧方案只记录 `app` + `version`，无法回答："这次验证跑的是哪个镜像 digest？哪个 AMI？哪个 compose 提交？"——一旦出问题无法追溯。

因此引入 **`verification_id`** 作为一次验证的唯一键，并强制绑定所有不可变输入。

## 2. `verification_id` 组成规则

```
格式：  {app}-{app_version}-{YYYYMMDD}-{seq}
示例：  ghost-v5.75.0-20260827-001

- app            = app.name
- app_version    = source 解析出的应用版本
- YYYYMMDD       = verified_at 的 UTC 日期
- seq            = 当日该 app+version 的第 N 次验证（从 001 起，padding 3 位）
```

`verification_id` 在验证开始时生成，全程不变；失败的重试用同一 `verification_id` 更新记录，不新建。

**解析约束（消除歧义）：**
- `verification_id` 是 **opaque 唯一键**，仅供人类可读与去重；程序**不得**按 `-` 分隔符反向解析其组成字段——结构化绑定关系（app/app_version/digest/ami_id…）一律以 Manifest 顶层字段为准。
- 生成规则要求：`app_version` 在拼入 `verification_id` 前，若含 `-` 或空格等非常规字符，统一替换为 `_`（例如上游 `v5.75.0-rc1` → `ghost-v5.75.0_rc1-20260827-001`）。
- `app` 与 `app_version` 本身必须满足 `^[a-z0-9._-]+$` 且不含路径分隔符。
- **`seq` 分配规则（GitHub Actions 无中心协调者，故不依赖"原子分配器"）**：
  1. `RESOLVED` 阶段在本 run 内分配 `seq`（初值 `001`）；同一 run 内的自动重试沿用该 `verification_id`，不改号。
  2. `versions/{app_version}.json` **以 app_version 为对象键**，因此同一版本的重复验证是**覆盖同一条记录**（"该版本当前有效结论"语义），不能靠改 `seq` 另存一条。`seq` 用于区分不同次验证的 `verification_id`，判定依据是**记录归属的 run**：
     - 目标键已有记录，且其 `verification_run_id` 与本次不同 → 本次是另一次验证 → `seq = 旧记录 seq + 1`；
     - `verification_run_id` 相同 → 同一 run 内的自动重试 → 沿用原 `verification_id` 覆盖；
     - **与"opaque 规则"的边界**：这里只读旧记录的 `seq` 后缀用于生成新 id，不构成业务判定；版本、平台、镜像等判定一律读 Manifest 结构化字段（§2 首条仍然成立）。
  3. 同一 `(app, app_version)` 的串行性由 app 级 `concurrency` 保证（见 workflow-state-machine.md §5），故无需分布式锁。
  4. 需要保留历史验证过程的场景由 `reports/{verification_id}.html` 承载（一次验证一份报告，不覆盖）；`versions/` 只保存该版本的当前结论。

## 3. Manifest 完整 Schema（v1.0）

> **字段命名对齐（消除命名歧义）：** 本方案与你最初提出的 Verification Identity 字段清单一一对应，仅做了必要的分组与拆分：
>
> | 你清单中的字段        | 本方案落点                                | 说明 |
> |----------------------|-------------------------------------------|------|
> | `app`               | 顶层 `app`                               | — |
> | `app_version`       | 顶层 `app_version`                       | — |
> | `source_revision`   | `release.source_revision`                | 上游 git 提交 |
> | `docker_image`      | `container.image`                        | tag 形式，可变 |
> | `docker_digest`     | `container.digest`                       | sha256，不可变 |
> | `ami_id`            | `platform.ami_id`                        | 不可变 |
> | `ami_region`        | `platform.region`                        | 已统一命名为 `region`（与 Platform Contract / current.json 一致） |
> | `architecture`      | `platform.architecture`                  | — |
> | `config_revision`   | `config.app_config_revision`             | 应用配置 `apps/{app}.yaml` 的 SHA |
> | `compose_revision`  | `config.compose_revision`                | compose 文件的 SHA |
> | `tests_revision`    | `config.tests_revision`                  | 测试脚本 `apps/{app}/tests/**` 的 SHA（§4.3） |
> | `template_revision` | `config.template_revision`               | one-click 模板合并输出的内容 SHA（deployment-contract.md §2.4） |
> | `verification_run_id` | 顶层 `verification_run_id`             | — |
> | `verified_at`       | 顶层 `verified_at`                       | — |
>
> 除此之外本方案新增：`verification_id`（唯一键）、`release.release_tag`、`checks.*`（Publish Gate 九项）、`artifacts.*`、`website.*`（前端投影段）。

```json
{
  "schema_version": "1.0",

  "verification_id": "ghost-v5.75.0-20260827-001",

  "app": "ghost",
  "app_version": "v5.75.0",

  "release": {
    "source_repo": "TryGhost/Ghost",
    "source_revision": "abc123def456",
    "release_tag": "v5.75.0",
    "upstream_tag": "v5.75.0",
    "image_reference": "ghost:5.75.0-alpine"
  },

  "container": {
    "image": "ghost:5.75.0-alpine",
    "digest": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "manifest_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    "platform": "linux/amd64"
  },

  "platform": {
    "platform_verification_id": "plat-us-east-1-x86_64-20260827-001",
    "ami_id": "ami-0abc123def4567890",
    "base_ami_source": "public",
    "source_ami_name": "ubuntu/images/hvm-ssd/ubuntu-noble-24.04-amd64-server-*",
    "region": "us-east-1",
    "architecture": "x86_64"
  },

  "config": {
    "app_config_revision": "git-sha-of-apps/ghost.yaml",
    "compose_revision": "git-sha-of-apps/ghost/docker-compose.yml",
    "tests_revision": "git-sha-of-apps/ghost/tests",
    "template_revision": "content-sha-of-merged-one-click-template"
  },

  "verification": {
    "application": "passed",
    "platform": "referenced",
    "tests": "passed"
  },

  "checks": {
    "compose_started": true,
    "container_healthy": true,
    "health_check_passed": true,
    "tests_passed": true,
    "screenshots_generated": true,
    "screenshots_uploaded": true,
    "report_uploaded": true,
    "verification_manifest_uploaded": true,
    "required_platform_contract_valid": true
  },

  "artifacts": {
    "screenshots": [
      { "scenario": "home", "file": "home.png",
        "url": "https://<r2-public>/screenshots/ghost/v5.75.0/home.png",
        "caption": { "en": "Home", "zh": "首页" } },
      { "scenario": "admin", "file": "admin.png",
        "url": "https://<r2-public>/screenshots/ghost/v5.75.0/admin.png",
        "caption": { "en": "Admin", "zh": "管理后台" } }
    ],
    "report_url": "https://<r2-public>/reports/ghost-v5.75.0-20260827-001.html",
    "workflow_run_url": "https://github.com/<org>/CoreNovaLaunch/actions/runs/123456"
  },

  "website": {
    "display_name": { "en": "Ghost Blog", "zh": "Ghost 博客" },
    "description": { "en": "...", "zh": "..." },
    "category": "cms",
    "icon": "/icons/ghost.svg",
    "featured": true,
    "tags": ["blog", "cms", "publishing"],
    "health": "passed",
    "status": "verified",
    "verified_at": "2026-08-27T10:00:00Z",
    "verification_id": "ghost-v5.75.0-20260827-001",
    "verification_run_id": "123456",
    "platform_verification_id": "plat-us-east-1-x86_64-20260827-001",
    "ami_id": "ami-0abc123def4567890",
    "architecture": "x86_64",
    "region": "us-east-1",
    "report_url": "https://<r2-public>/reports/ghost-v5.75.0-20260827-001.html",
    "workflow_run_url": "https://github.com/<org>/CoreNovaLaunch/actions/runs/123456",
    "features": [
      { "en": "Automated testing before each release", "zh": "每次发布前自动化测试" },
      { "en": "One-click CloudFormation deploy", "zh": "CloudFormation 一键部署" }
    ],
    "deploy": {
      "documentation_url": "https://docs.ghost.org",
      "regions": ["us-east-1"],
      "instance_type": "t3.small",
      "data_volume_gb": 30,
      "container_port": 2368,
      "health_check_path": "/",
      "docker_image": "ghost:5.75.0-alpine",
      "app_url_env_name": "url",
      "post_deploy": {
        "admin_path": "/ghost/",
        "admin_setup": {
          "en": "Open the admin path on first visit — the setup wizard walks you through creating the owner account. There are no preset credentials.",
          "zh": "首次打开后台地址会进入初始化向导，按步骤创建站长账户；没有预置账号密码。"
        },
        "notes": [
          { "en": "Content is stored on the instance's data volume (/var/lib/ghost/content); back it up before terminating the instance.",
            "zh": "内容保存在实例数据卷（/var/lib/ghost/content），终止实例前请先备份。" }
        ]
      },
      "cost_estimate": {
        "monthly_usd": 23,
        "note": {
          "en": "Verified default: ~$15.2 t3.small + $4 for 50 GB gp3 + $3.65 public IPv4 per month.",
          "zh": "已验证默认配置：t3.small 约 $15.2 + 50GB gp3 约 $4 + 公网 IPv4 约 $3.65/月。"
        }
      },
      "data_path": "/var/lib/ghost/content"
    },
    "release": {
      "type": "new_version",
      "previous_version": "v5.74.0",
      "type_evidence": "release notes contain no security/CVE keyword; version bump 5.74.0 -> 5.75.0"
    },
    "screenshots_order": ["home", "admin"],
    "screenshots": [
      { "scenario": "home", "file": "home.png",
        "url": "https://<r2-public>/screenshots/ghost/v5.75.0/home.png",
        "caption": { "en": "Home", "zh": "首页" } },
      { "scenario": "admin", "file": "admin.png",
        "url": "https://<r2-public>/screenshots/ghost/v5.75.0/admin.png",
        "caption": { "en": "Admin", "zh": "管理后台" } }
    ]
  },

  "verification_run_id": "123456",
  "verified_at": "2026-08-27T10:00:00Z"
}
```

## 4. 字段语义与不可变性

| 字段 | 来源 | 可变性 | 说明 |
|------|------|--------|------|
| `schema_version` | 固定 | 升级时变 | Manifest 格式版本 |
| `verification_id` | 验证开始时生成 | **不可变** | 一次验证唯一键 |
| `app` / `app_version` | source 解析 | 每次验证变 | — |
| `release.source_revision` | GitHub API | 每次验证变 | 上游提交的不可变引用 |
| `container.image` | app schema | 可变（tag） | 人类可读入口 |
| `container.digest` | 注册表解析 | **不可变** | 验证只信 digest |
| `platform.*` | Platform Contract | 引用既有契约 | 默认 `referenced`（见 §5） |
| `config.*_revision` | Git SHA | 每次验证变 | 证明跑的是哪份配置 |
| `verification.*` | 验证结果 | 每次验证变 | `passed`/`failed`/`referenced`/`skipped` |
| `checks.*` | Publish Gate | 每次验证变 | 全部 `true` 才 `PUBLISHED` |
| `verified_at` | 验证完成时间 | 每次验证变 | UTC ISO8601 |
| `health` | `verification.application` 投影 | 每次验证变 | 仅出现在 `website` 段；值 = `verification.application`（`passed`/`referenced`/`failed`），前端渲染徽章用，不得独立计算 |
| `container.platform` | 固定 | 不可变 | v1 恒为 `linux/amd64`（对齐 x86_64 平台契约） |
| `container.manifest_digest` | 注册表解析 | **不可变** | 多平台索引（manifest list）digest，仅作补充证据；门禁与部署以 `container.digest` 为准 |
| `release.image_reference` | 由 `deploy.image_tag_template` 渲染 | **不可变** | 本次 `app_version` 对应的**精确**镜像引用，见 §4.1 |
| `platform.base_ami_source` | Platform Contract | 每次验证变 | `public` \| `custom`（公开 AMI 引导期 vs 自建/收费 AMI 期），见 platform-contract.md §2.1 |
| `config.tests_revision` | `apps/{app}/tests/**` 的 git SHA（未提交回退内容哈希） | 测试变更时变 | 钉住产生本次 `tests_passed` 的测试版本，见 §4.3 |
| `config.template_revision` / `website.deploy.template.revision` | one-click 模板合并输出的内容 SHA（未提交回退内容哈希） | 模板源变更时变 | 钉住产生本次证据的部署模板版本，对账 `deploy.template`，见 deployment-contract.md §2.4 |
| `website.deploy.production_contract` | app schema `deployment.production_contract` 投影（`{"checks": [...]}`） | 声明变更时变 | 发布前候选生产门禁与深链参数声明，不自动解除人工 hold；见 deployment-contract.md §2.6，无声明省略键 |
| `verification_run_attempt` / `website.verification_run_attempt` | 候选 stage 的 Actions run attempt（字符串） | 每次 attempt 变 | 本轮候选新增投影；与 run_id 一起排序/隔离，旧路径可缺省（§6.4） |
| `website.deploy.hold` | app schema `deployment.hold` 投影 | 运维态，**非证据** | 只随 `current.json` 发布；发布器写 `versions/<version>.json` 时剥离（不可变证据不冻结运维态），消费口径见 deployment-contract.md §2.5 |
| `website.features` / `website.deploy.docker_image` / `website.release.type_evidence` / `website.workflow_run_url` | app schema + 运行时解析 | 投影 | 前端直接消费的字段，必须由生成器从顶层/artifacts 投影，禁止手写第二份 |

### 4.1 版本 ↔ 镜像绑定证明（强制）

> 旧方案允许 `deploy.docker_image: ghost:5-alpine` 这类**移动 tag** 与 `app_version: v5.75.0` 并存，二者之间没有任何证据链——`checks.container_healthy` 永远测不出"实际跑的是 5.74 而非 5.75"，`app_version` 于是成为未经证明的断言。本节堵住这个洞。

一次发布的 Manifest 必须同时具备三层绑定，缺一即 `required` 校验失败、不得 `PUBLISHED`：

```
app_version (v5.75.0)
   │  ① deploy.image_tag_template 渲染出精确 tag（无移动 tag）
   ▼
release.image_reference (ghost:5.75.0-alpine)
   │  ② 注册表解析成内容寻址 digest
   ▼
container.digest (sha256:…) —— 实际启动的镜像
   │  ③（app 支持时）运行期版本断言
   ▼
version_assertion 通过（应用自报版本 == app_version）
```

- **① 强制**：`deploy.image_tag_template` 必填（见 app-schema.md §1），渲染变量只允许 `{version}`（`app_version` 原样）与 `{version_no_v}`（去掉前导 `v`/`V`）。渲染结果**不得**等于仓库原始值（防止把 `ghost:5-alpine` 这类移动 tag 直接当精确 tag）。
- **② 强制**：`container.image` = 渲染出的精确 tag 引用；`container.digest` 必填。
- **③ 尽力**：`health_check.version_assertion` 可选；配置后必须在 `VERIFYING` 阶段实测通过（失败 → `TEST`/`APPLICATION` 分类，不得发布）。上游确实无版本可观测性的应用可不配 ③，但必须在 `apps/{app}.yaml` 注释说明理由。

### 4.2 digest 语义（消除"哪个 digest"歧义）

- `container.digest` = **单平台 image manifest digest**（`linux/amd64`），即 `docker buildx imagetools inspect <image> --format '{{json .Image}}'` 的 `Digest`。这是 `docker pull` 实际落地的内容指纹，也是本地 `docker images --digests` 对 `RepoDigests` 的匹配目标，故作为唯一门禁值。
- `container.manifest_digest` = 多平台索引 digest（`--format '{{json .Manifest}}'`）。仅审计用，**不得**写入 CFN 部署参数。
- CFN / 用户自助部署模板引用的镜像引用形式统一为 `<精确 tag>@<container.digest>`。

### 4.3 测试脚本 × app 版本的双轴管理（版本控制与漂移）

测试脚本与 app 版本是**两条独立的版本轴**。一个发布结果要被复跑/审计，必须同时钉住两者：
`app_version`（被测对象）+ `config.tests_revision`（测量工具）。

- **测试必须 git 版本控制**（在 `apps/{app}/tests/**`，随 Repo C 主干持续维护）。不做"每个 app 版本冻结一份测试"的矩阵——那会让测试失去"抓回归"的意义。
- **验证用主干当前测试**，并把其修订号写入 `config.tests_revision`（git SHA；未提交时回退内容哈希）。
- **漂移处理**：新 app 版本若因 UI/接口真实变化导致旧测试失败，分类 `TEST` → 走 §6 的离线人+AI 修复（更新测试）→ PR + review → 重验 → 以**新 tests_revision** 发布。这正是"测试与 app 版本不一致"的既定出口，不需要特殊机制。
- **历史可复跑**：已发布版本保留其当时的 `tests_revision`，任何时候可检出该修订复跑，得到与 Manifest 一致的结果。
- **防混淆**：`tests_passed` 的语义是"该 app 版本通过了 tests_revision 这一版测试"，不是"通过了任意最新测试"。

**版本稳健测试编写准则**（减少无谓漂移、又不放过真回归）：
- 断言**稳定语义**而非脆弱 DOM：优先 data-testid / role / 可见文案 / API 契约，少用层级选择器与像素。
- 把"会随版本变的值"（版本号、示例文章标题）参数化或从应用自身读取，不写死。
- 截图场景用 ASCII slug 与稳定路由（app-schema §5 规则 8），路由比 DOM 更稳。
- 区分"漂移"与"回归"：选择器找不到是漂移（修测试）；业务行为变坏是回归（修应用/拒发布）。review 时据此判断。

**`website` 段 = `current.json` 投影权威（消除重复字段漂移）：**

`website` 段是专门给前端消费的扁平投影。它与顶层存在同名冗余字段（`app`、`app_version`、`verification_id`、`verification_run_id`、`verified_at`、`platform_verification_id`、`ami_id`、`region`、`architecture`），这些**值必须与顶层严格相等**，由 Manifest 生成器写入、CI 校验，禁止各自独立维护。

- `current.json`（R2）的验证字段逐字段等于 `manifest.website`。`verification_run_id` 随投影进入 current；候选分支新增 `verification_run_attempt` 顶层/website 同值投影，二者共同参与候选新旧排序（§6.4）。**运维投影例外：`deploy.hold` 使用 current 实时值**，可由 sync_holds 条件更新；promote 保留人工 hold，不把候选创建时的旧状态覆盖到新 current。
  - `website` 段除内联 identity 字段外，还包含前端直接消费的内容字段：`display_name` / `description` / `category` / `icon` / `featured` / `tags` / `health` / `status` / `report_url` / `deploy{...}` / `release{...}` / `screenshots_order` / `screenshots[]`。其中 `report_url` 由 `artifacts.report_url` 投影、`screenshots[]` 与 `screenshots_order` 由 `artifacts.screenshots[]` 投影（顺序与 `screenshots_order` 一致，且 ≡ `tests.scenarios[].name`，见 app-schema §5 规则 8）。
- `versions/{app_version}.json`（R2）保留完整 Manifest 验证证据，但**剥离 `website.deploy.hold`**，不可变证据不得冻结实时运维态。这是上述 1:1 投影的明确例外，不允许借此增删其他证据字段；历史版本显示的暂停状态一律以 current 为准。
- 前端只读 `current.json`，不读完整 Manifest 嵌套；因此 `website` 段是唯一的前端字段事实源。

## 5. `verification.platform` 取值

- `passed` — 本次验证同时跑了 AWS Golden Verification（平台变更场景）。
- `referenced` — 本次仅 Application Verification，复用了既有的、有效的 Platform Contract（**绝大多数常规验证**）。
- `skipped` — 明确无平台依赖（极少）。

**关键区分（强制写入文档语气）：**
> GitHub CI 验证应用行为；AWS Golden Verification 验证平台与部署链路。两者组合形成 CoreNova Launch 的完整可信链。
> **GitHub Application Verification ≠ AWS Infrastructure Verification。** 不能声称"GitHub 验证可以保证 EC2 一定正常运行"。

**信任链残余风险（写清隐含假设）**：`referenced` 仅证明"该 `(ami_id, region, arch)` 组合下的 AMI + CFN + 运行时曾经在真实 AWS 跑通过"。但常规 Application Verification 实际在 **GitHub runner 的 Docker** 里跑容器，**并不在 AMI 上**。因此"引用即可信"成立的隐含前提是：**runner 的 Docker 运行时 ≈ AMI 内 Docker 运行时**（版本 / 行为一致）。一旦 Repo B 升级 AMI 内的 Docker 引擎，旧 Platform Contract 即失效（见 platform-contract §5 失效条件 6），必须重跑 Golden Verification 重置 `referenced` 的信任基础，否则会出现"runner 能跑、AMI 上行为不同"的裂缝。

## 6. Publish Gate 与 Manifest 的关系（两阶段提交）

### 6.1 为什么不能"先凑齐九项 true 再上传"

`checks.screenshots_uploaded`、`checks.report_uploaded`、`checks.verification_manifest_uploaded` 三项描述的是**上传动作的结果**。若要求它们在上传发生前就为 `true`，逻辑上不可能成立（Manifest 里写"我已上传"的那次上传正是待验证的上传本身）。旧文档在此处自相矛盾，本节给出可执行时序。

### 6.2 兼容时序（无 production_contract 的旧 P1–P5）

仅无 `deployment.production_contract` 声明的应用保留此路径；有声明的必须走 §6.4，不能先写 versions 再补生产核对。

```
P0  门禁前置：6 项本地 checks 必须已为 true
    compose_started / container_healthy / health_check_passed /
    tests_passed / screenshots_generated / required_platform_contract_valid
    任一为 false → 直接 FAILED，不进入 PUBLISHING，不产生任何 R2 写入。

P1  占位写 versions/{app_version}.json
    上传完整 Manifest，其中三项上传 check 记 false（probe 前不得声称 true）。
    写入成功 → checks.verification_manifest_uploaded = true

P2  上传 artifacts.screenshots[].file → screenshots/{app}/{app_version}/{file}
    上传 report                  → reports/{verification_id}.html

P3  逐项 HEAD/GET 探测 P1、P2 的对象是否可读
    全部截图可读 → screenshots_uploaded = true
    报告可读     → report_uploaded      = true
    任一不可读   → FAILED(TRANSIENT，最多 3 次退避)，current.json 保持不动

P4  以 P3 结果重写 versions/{app_version}.json（最终态：九项全 true）
    重写失败 → 删除已上传对象并 FAILED；current.json 未动，网站无损

P5  提交点（唯一）：写 current.json，再更新 verified/index.json
    写 current.json 前必须通过"版本覆盖保护"（workflow-state-machine.md §5）
```

### 6.3 语义与不变式

- **九项全真是发布必要条件，不总是充分条件**：旧路径按 §6.2 提交；有生产声明还须通过 §6.4 的精确候选生产门禁。current 中历史记录不证明本轮新增门禁曾执行，也不表示人工 hold 已解除。
- 任一 check 为 `false` → **绝不写 `current.json`、绝不更新 `index.json`、绝不发 `repository_dispatch`**（门禁落点）。
- 中断在 P1–P4 之间：官网仍展示旧 `current.json`；`versions/` 里可能残留一条三项上传 check 为 `false` 的记录——该记录**不得**出现在网站版本页（前端按 `checks` 全真过滤，见 deployment-contract.md §2.1）。
- 网站展示的版本记录，其九项 check 必须全部来自最终态 Manifest，禁止前端补写或推断。

### 6.4 候选分支（声明 production_contract 时强制）

由 `corenova/candidates.py` 执行，与旧 P1–P5 分支互斥：

```
C0  六项本地 checks 通过；校验 template revision 与 verified_at 新鲜度
C1  CAS candidates/{app}/latest.json，先登记数值 run/attempt 排序的候选意图
C2  上传到 candidates/{app}/{run}/{attempt}/{verification_id}/：
      screenshots/{file}、report.html、manifest.json、candidate.json
    资产回读核对哈希、Manifest 回读一致后才返回 CANDIDATE_READY
    不写 verified/、稳定截图路径、版本清单，也不触发官网发布
C3  production-verify 使用完整精确候选引用核对：
      同 manifest SHA / tag@digest / template revision
      公开模板 SHA + CFN 实际模板 + 私有 SSM 参数 + 真实会话
      必需及声明探针全通过 + cleanup_confirmed=true
C4  复核 24h 新鲜度、latest 意图、run/attempt、app config hash、current etag、
    资产哈希与公开模板内容；任一不符拒绝晋级
C5  CAS current（保留人工 hold）→ versions → 应用索引 → 版本索引 → 官网通知
```

- 完整引用含 `app`、`app_version`、`verification_id`、`verification_run_id`、
  `verification_run_attempt`、`key`、`manifest_sha256`；`key` 为上述候选目录下的 `candidate.json`。
  SHA-256 按确定性 JSON 编码计算，不能以应用名、版本号或旧 current 代替精确身份。
- 候选 Manifest 顶层新增字符串 `verification_run_attempt`（Actions attempt，默认 `"1"`），
  严格同值投影到 `website.verification_run_attempt`，晋级后进入 current 与版本证据。
  旧 P1–P5 记录可无此字段，比较旧 current 时按 attempt 1 兼容。
- 候选与验证时间均须在 24 小时内且非未来；生产证据时间必须位于候选创建后、本次核对期间，
  并绑定生产 run/attempt。`latest` 先记录意图：新 run 即使上传失败，旧候选也不再有晋级资格；
  重试须新 attempt。数值 `(run, attempt)` 必须更新，且不得 semver 回退。
- 所有候选对象不可变；晋级仍引用原 `candidates/...` 报告/截图 URL，官网按 URL 原路径镜像。
  已被 current/versions 引用的资产不得当临时文件 GC；隔离既防并发覆盖，也防旧报告串图。
- 候选内部 `website.status=verified` 与九项全真只表示证据已就绪，**不是已公开发布**。
  任一前置核对失败或 CAS 冲突均不 promote、不改变稳定命名空间。
  C5 的 CAS 与后续版本/索引写入并非跨对象事务，后续写入中断需核对并修复一致性。
- 当前配置 hash 与 current etag 防止旧 run 覆盖新配置或人工 hold；自动流程永不删人工 hold。
  hold 的实时投影例外见 §4，精确探针和人工解除流程见 deployment-contract.md §2.5–2.6。

## 7. current.json、versions/*.json 与 index.json

- `verified/{app}/current.json` = 当前官网应展示的最新已发布验证状态；验证字段等于 Manifest 的 `website` 段，`deploy.hold` 为实时投影例外（§4）。
- `verified/{app}/versions/{app_version}.json` = 该版本的最终态 Verification Record（完整验证证据，剥离 `website.deploy.hold`）。
- `verified/index.json` = 应用清单（网站可枚举的唯一入口，形状见 deployment-contract.md §2.1）。R2 公共端点不支持 ListObjects，Repo A **无法**自行发现 app 列表，故该文件是链路成立的必要条件，必须在每次 P5 与 current 同批更新。
- 新版本验证失败 → **旧 `current.json` 与旧 `index.json` 条目保留**，网站上稳定版本不消失。
- 版本覆盖保护：较旧验证结果不得覆盖较新已发布版本（见 workflow-state-machine.md §5）。

详见 deployment-contract.md（current.json / index.json 形状）与 repo-structure.md（R2 路径）。

## 8. 反模式（本轮整改新增）

- ❌ `app_version: v5.75.0` + `container.image: ghost:5-alpine`（移动 tag，版本未经证明；见 §4.1）。
- ❌ 把多平台索引 digest 写进 `container.digest` 或 CFN 参数（与实际启动的 amd64 image 不一致；见 §4.2）。
- ❌ 上传前先把 `*_uploaded` 写成 `true`（§6.1 循环依赖的复活）。
- ❌ 跳过 P3 探测、凭"put 返回 200"就认定对象可读（权限/公共端点配错时网站图 404）。
- ❌ `artifacts.screenshots[].scenario` 使用中文/空格（对象键非 ASCII、URL 编码不确定、跨语言环境易碎）。
- ❌ `current.json` 出现 `website` 段之外的字段，或前端把 `versions/*.json` 的字段混进 current（第二事实源）。
- ❌ 未过版本覆盖保护就更新 `current.json` / `index.json`（迟到旧版本覆盖新版本）。
