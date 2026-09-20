# Contract · Deployment Contract（网站数据契约）

> 优先级：**最高**。
> 术语：本文沿用 Repo A / Repo B / Repo C 代号，分别指 `CoreNovaLaunchWebsite`（官网，本地目录 `website/`）、`CoreNovaLaunchAmi`（AMI 构建，引导期未落地）、`CoreNovaLaunch`（验证枢纽）。
> 适用：Repo A `CoreNovaLaunchWebsite` 构建时消费的运行时数据。
> 本文规定"网站只能从 Manifest 拿什么、不能自己猜什么"。任何设计文档与之冲突，以本文为准。

## 1. 唯一事实源声明

```
R2 = Website Runtime Source of Truth
```

- Repo A 构建时**只**从 R2 拉取 `verified/{app}/current.json` 与 `verified/{app}/versions/*.json`。
- Repo A **禁止**读取 `apps/`（ Repo C 工作区文件）。
- Repo A **禁止**把 Git 仓库里任何 `verified` 数据当作运行时事实源。
- Git 中可保留审计副本，但**不能形成第二套网站事实源**。

> **例外（非验证事实源）**：官网版本页展示的 GitHub Release Notes（`Repo A` 构建时缓存到 `data/{app}/releases.json`）属于**上游元数据同步**，从 GitHub API 拉取，**不进入** `current.json` / `versions/*.json`，也不属于"验证事实源"。它不构成第二套网站事实源——`verified/*/current.json` 仍是唯一验证事实源。Release Notes 缺失 / 限流时降级为空白或上次缓存，不影响验证状态展示。拉取所需的 `owner/repo` 读自 `verified/{app}/versions/*.json` 的 `release.source_repo`（验证事实源自带，verification-manifest.md §3），不读取 Repo C 工作区 `apps/`。

## 2. `current.json` 形状（= Manifest 的 website 投影）

`current.json` 逐字段等于 Verification Manifest 的 `website` 段（见 verification-manifest.md §4 投影权威）。`website` 段已内联扁平化后的 identity 字段（`app`、`app_version`、`verification_id`、`verified_at`、`platform_verification_id`、`ami_id`、`region`、`architecture`），因此 `current.json` 不额外携带、也不缺漏任何字段。前端只读 `current.json`：

```json
{
  "app": "ghost",
  "app_version": "v5.75.0",
  "verification_id": "ghost-v5.75.0-20260827-001",
  "verification_run_id": "123456",
  "platform_verification_id": "plat-us-east-1-x86_64-20260827-001",
  "ami_id": "ami-0abc123def4567890",
  "architecture": "x86_64",
  "region": "us-east-1",

  "display_name": { "en": "Ghost Blog", "zh": "Ghost 博客" },
  "description": { "en": "...", "zh": "..." },
  "category": "cms",
  "icon": "/icons/ghost.svg",
  "featured": true,
  "tags": ["blog", "cms", "publishing"],
  "features": [
    { "en": "Automated testing before each release", "zh": "每次发布前自动化测试" }
  ],

  "health": "passed",
  "status": "verified",
  "verified_at": "2026-08-27T10:00:00Z",
  "report_url": "https://<r2-public>/reports/ghost-v5.75.0-20260827-001.html",
  "workflow_run_url": "https://github.com/<org>/CoreNovaLaunch/actions/runs/123456",
  "screenshots_order": ["home", "admin"],

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
      "admin_setup": { "en": "…setup wizard…", "zh": "…初始化向导…" },
      "notes": [ { "en": "…", "zh": "…" } ]
    },
    "cost_estimate": {
      "monthly_usd": 23,
      "note": { "en": "Verified default: ~$15.2 t3.small + $4 for 50 GB gp3 + $3.65 public IPv4/mo.", "zh": "已验证默认配置：t3.small 约 $15.2 + 50GB gp3 约 $4 + 公网 IPv4 约 $3.65/月。" }
    },
    "data_path": "/var/lib/ghost/content",
    "hold": { "reason": { "en": "Deployment paused: …", "zh": "暂停部署：…" } }
  },

  "release": {
    "type": "new_version",
    "previous_version": "v5.74.0",
    "type_evidence": "release notes contain no security/CVE keyword; version bump 5.74.0 -> 5.75.0"
  },

  "screenshots": [
    { "scenario": "home", "file": "home.png",
      "url": "https://<r2-public>/screenshots/ghost/v5.75.0/home.png",
      "caption": { "en": "Home", "zh": "首页" } },
    { "scenario": "admin", "file": "admin.png",
      "url": "https://<r2-public>/screenshots/ghost/v5.75.0/admin.png",
      "caption": { "en": "Admin", "zh": "管理后台" } }
  ]
}
```

### 2.1 `verified/index.json` 形状（网站枚举应用的唯一入口）

R2 公共端点**不支持 ListObjects**，Repo A 无法自行发现有哪些 app。因此 Repo C 在 PUBLISHING 的 P5 步骤与 `current.json` 同批写一份清单，前端**必须先读它**：

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-08-27T10:00:05Z",
  "apps": [
    {
      "app": "ghost",
      "app_version": "v5.75.0",
      "verification_id": "ghost-v5.75.0-20260827-001",
      "status": "verified",
      "health": "passed",
      "verified_at": "2026-08-27T10:00:00Z"
    }
  ]
}
```

- `apps[]` 与 `verified/{app}/current.json` 一一对应；清单里有、current 缺失 → Repo A 构建**必须失败**（不得静默跳过，否则网站少应用无人察觉）。
- 移除应用：Repo C 删除其 `current.json` 并同步从 `index.json` 摘除。

### 2.2 版本记录过滤（`versions/*.json`）

- 版本页读取 `verified/{app}/versions/*.json`（完整 Manifest，见 verification-manifest.md §3）。
- **版本清单 `verified/{app}/versions/index.json`**（Repo C 在 P5 提交点写入）：`versions[]` 按 `verified_at` 倒序列出**曾通过提交门禁**的版本（`app_version` / `verification_id` / `status` / `verified_at`）。官网以该清单枚举版本记录；清单缺失或为空时回退到 `release.previous_version` 链回溯（旧数据兼容，链断裂即止，不猜测）。清单按应用分键，同应用发布由 CI 并发组互斥，不与全局 `index.json` 共享跨应用写竞争面。
- **只渲染 `checks.*` 九项全 `true`** 的最终态记录；两阶段提交中断留下的占位记录（三项上传 check 为 `false`）不得出现在网站上，也不得由前端补写成通过。
- 前端展示"测试报告"必须逐条映射 Manifest 的九个 check 名（见 §3.1），**禁止**自造 `Docker Build / API Test` 之类不存在的检查项。

### 2.3 截图投递方式（裁决：构建期镜像，不改键路径）

此前 `website-design.md` §8 说"构建时下载到 `static/screenshots/`"，而本文 §2 示例给的是 R2 绝对 URL —— 二者冲突。定为：

- **Repo A 构建期把每张截图镜像到站点自身的同名相对路径**，仅替换 origin，**不改键路径**：

```
Manifest: https://pub-xxxx.r2.dev/screenshots/ghost/v5.75.0/home.png
产物:     dist/screenshots/ghost/v5.75.0/home.png
渲染:     /screenshots/ghost/v5.75.0/home.png
```

- 理由：① R2 公共访问未开启/配错时不至于全站图裂；② 省 R2 egress 与跨域配置；③ 站点不依赖站外链接存活。
- 该镜像是**构建期搬运**，键路径完全由 Manifest 的 `url` 决定；**前端代码不得自行拼路径或猜文件名**（§6 反模式仍成立）。
- 镜像失败（对象 404）→ Repo A 构建**必须失败**，不得静默产出图裂的页面（这正是 verification-manifest.md §6.2 P3 探测要防的事故）。
- 引导期后端为本地 fixtures 时，同一逻辑从目录读取同名文件后镜像，路径规则不变。

### 2.4 one-click 模板分发（公开 S3 直链 = 深链 URL 源）

`Generate Template` 深链的 `templateURL` 指向 Repo C 发布的公开读 S3 直链
`https://<bucket>.s3.us-east-1.amazonaws.com/corenova-one-click.template.yaml`（CloudFormation 控制台原生支持，一点即进创建向导）：

- 模板由 Repo C `scripts/verify/build_user_template.py` 从 `templates/cloudformation/fixed/*.yaml` 合成
  （合并构造与内容 SHA 见 `corenova/usertemplate.py`），`publish-template.yml` 在 fixed 栈变化时发布；
  `corenova/template_publish.py` put 后以**匿名 GET 探测**，不可读即失败（与 §2.3 镜像失败即构建失败
  同一精神：深链绝不指向读不到的对象）。
- **模板-证据绑定**：模板以**单对象覆盖**发布（无版本号对象键），时间戳无法回答“这份证据对应的
  模板是哪版”。因此每次验证必须计算合并输出的内容 SHA（`usertemplate.revision`，前 40 hex）并写入
  `config.template_revision`，同值投影到 `current.json` 的 `deploy.template.revision`（附带发布 URL
  `deploy.template.url` 用于对账）。深链入口仍是 Repo A 构建期常量（见下条），`deploy.template` 仅作
  证据对账，不作为深链源。模板再次发布不使既有证据失效（镜像 digest 与 AMI 已分别钉扎）；
  新验证自动绑定新 revision，模板与证据是否匹配由此可查，而非靠发布时间猜。
- 默认深链必须把当前记录的 `ami_id`、`deploy.instance_type`、`deploy.data_volume_gb`、
  `deploy.data_path`、`deploy.health_check_path`、`deploy.app_url_env_name` 与 digest-pinned
  `deploy.docker_image` 完整映射到 CloudFormation 参数；深链的 `templateURL` 由 Repo A 构建期常量引用
  （`src/lib/deploy.ts`，`VITE_ONE_CLICK_TEMPLATE_URL` 可覆盖），默认值必须与 Repo C `TEMPLATE_S3_BUCKET`
  指向同一只桶（repo-structure.md §4.4）。缺任一强制值时 Deploy 按钮必须禁用并提示
  “需要重新验证”，不得退回模板默认值后仍称为已验证部署。
- 详情页允许用户在该基线上**主动上调** `InstanceType` 和 `DataVolumeSize`，但不得下调：实例仅显示
  当前 t3 档及更高档，数据卷仅显示当前值及其 2× / 4×（且不超过模板上限 4096 GB）。一旦修改，
  界面必须明确标为“自定义配置 / 未单独验证”，不得继续显示该组合已验证，也不得沿用默认配置的
  成本估算；镜像 digest、AMI、数据路径、端口与健康检查仍必须来自所选版本的 Manifest 并保持钉扎。

### 2.5 部署暂停（hold）："已验证"与"当前可部署"是两个状态

`status: verified` 证明的是**历史验证事实**（该版本曾通过全部门禁），不等于**当前允许部署**。
平台层模板/初始化脚本变更、生产部署核对发现问题等情况下，已发布的应用可能被运维性暂停；
两者必须分别建模，禁止互相覆盖：

- **事实源**：`apps/*.yaml` 的 `deployment.hold.reason`（app-schema.md §5 规则21，双语必填）。
- **发布数据**：`current.json` 的 `deploy.hold`（`{ "reason": { en, zh } }`）；由 Manifest 投影链在
  验证发布时携带，也可由 `scripts/verify/sync_holds.py` 独立条件写（If-Match）进已发布的
  `current.json` —— **不等下一次验证**，否则"hold 拦住验证 → hold 字段永远进不了发布数据"死锁。
- **消费规则**：官网任何部署入口（详情页配置器、版本页部署按钮、方案页就绪判定、预渲染 SEO）
  都必须先判定 hold：存在即拦截 `Deploy on AWS` 并展示双语原因，同时把该应用标注为
  "Deployment paused" 而非 "Verified"；拦截逻辑集中在 `deploymentHold()`，禁止各页面自行判断。
- **解除条件**：生产契约重新验证通过（含 AWS 真实部署核对），不是版本更新；解除 = 移除 yaml 里的
  `hold` 后重跑 `sync_holds.py --app <app>` 清除 `deploy.hold`。该重新核对已由 L1.5 生产核对门禁
  （§2.6）自动化：核对全绿即由 `production-verify.yml` 删 hold 并触发 sync-holds。
- **迁移窗口（已关闭，2026-09-20）**：R2 旧数据尚无 `deploy.hold` 字段时，官网构建期兜底表
  （`deploymentSafety.ts`）曾继续拦截暂停应用；兜底表只允许缩小。现所有已发布
  `current.json` 均携带该字段，兜底表清空为**永久禁止再添加条目**——L1.5（§2.6）从 Repo C
  自动解除 hold 时无法同步修改 Repo A 的构建期表，残留条目会在暂停已解除后继续静默拦截官网。

### 2.6 L1.5 生产核对门禁（production check）："验证通过"到"可部署"的自动化桥

本地应用验证（compose + Playwright）不接触 AWS，永远产不出"生产语义正确"的证据，
hold 因此无法靠版本更新解除。L1.5 把这条核对自动化：新版本发布后，在真实 AWS 建一个
**一次性用户栈**（与用户经深链部署的同一份公开模板、同一 digest 钉扎镜像），逐项实证后删栈。

- **声明**：`apps/*.yaml` 的 `deployment.production_contract.checks`（app-schema.md 规则22），
  取值子集：`admin_auth`（整站 Basic 保护：无凭据 401、带凭据 200）、`data_dir_write`
  （以镜像声明用户向数据卷写删文件）、`url_injection`（URL 环境变量注入生效）、
  `host_metrics`（宿主机指标只读挂载可用）。核对结论由 `corenova/prodcheck.py` 写入
  `data/runs/prodcheck-*.json` 证据。
- **解除闭环**：全部 checks 通过 → `production-verify.yml` 删除该应用 yaml 的 `hold`
  并 push（sync-holds.yml 自动清 R2 `deploy.hold`）；任一失败 → 不碰 yaml，hold 保持。
  人工解除路径仍然有效（演练实证，如 gitea/portainer 2026-09-19）。
- **深链联动**：核对项决定模板参数——`admin_auth` 对应 `AdminAuthEnabled=true`、
  `host_metrics` 对应 `HostMetricsAccess=true`。Manifest 把声明投影为
  `current.json` 的 `deploy.production_contract.checks`，官网深链必须按声明携带对应参数，
  否则部署形态退回核对前的未保护基线（§3 表）。
- **成本与卫生**：栈名 `corenova-prodcheck-<app>-<version>-<run>`，打 tag
  `corenova:prodcheck=true`；无论成败 finally 删栈并扫残留数据卷；密码只存在于核对进程内存，
  证据与日志一律脱敏。
- **边界**：L1.5 不改变九项验证 check 的语义，也不是发布门禁——发布照常，
  它只决定 hold 的解除；未声明 `production_contract` 的应用不受影响。

## 3. 字段来源约束（禁止前端猜测）

| 网站展示项 | 来源字段 | 是否允许前端推断 |
|-----------|---------|----------------|
| Deploy 按钮深链 | §2.4 构建期常量模板 URL + 已验证 `deploy.docker_image`（tag@digest 钉扎） | ❌ 镜像引用必须来自 Manifest；模板 URL 不得拼站点 origin /  自托管副本 |
| one-click 深链 templateURL | 构建期常量（§2.4 公开 S3 直链）；`deploy.template.url` 仅作证据对账 | ❌ 深链不得改读 `deploy.template`；证据 URL 不得拼站点 origin |
| 模板-证据对应关系 | `current.json` 的 `deploy.template.revision`（验证时合并输出内容 SHA，§2.4） | ❌ 不得以发布时间戳代替；旧记录无此键表示早于字段引入 |
| 深链保护参数（AdminAuthEnabled / HostMetricsAccess） | `current.json` 的 `deploy.production_contract.checks`（§2.6） | ❌ 前端不得按应用名猜；无该字段的旧记录不携带保护参数 |
| 文档链接 | `deploy.documentation_url` | ❌ 必须来自 Manifest |
| 支持区域 | `deploy.regions` | ❌ 必须来自 Manifest |
| 更新类型徽章（New Version / Security Update） | `release.type` | ❌ **必须来自数据**，前端不得按版本号猜 |
| 上一版本 | `release.previous_version` | ❌ 必须来自 Manifest |
| 验证状态 | `status` / `health` | ❌ 必须来自 Manifest |
| 验证时间 | `verified_at` | ❌ 必须来自 Manifest |
| 验证标识 | `verification_id` | ❌ 必须来自 Manifest |
| 架构 / 区域 | `architecture` / `region` | ❌ 必须来自 Manifest |

网站**不得**出现 `verified=true` 之外的"假阳性"状态推断。未验证应用根本不会出现在 R2，自然不上前端。

> 注：`health` 字段 = Verification Manifest 的 `verification.application`（值 `passed`/`referenced`/`failed`），是 `website` 段的投影，非前端独立计算。它描述"应用行为是否通过验证"，与 `status=verified`（已发布）语义不同：一个应用可 `status=verified` 但某次 `health=referenced`（仅引用平台契约、应用层未单独重跑 AWS）。

### 3.1 测试报告映射（版本页 / 详情页展开区）

网站"CoreNova Test Report"**必须逐条渲染 Manifest 的 `checks.*` 九项**，名称与语义一一对应。**禁止**出现契约中不存在的检查项（旧 `website-design.md` 版本页示例中的 `Docker Build` / `API Test` 属自造项，已废弃）。

| 网站展示行 | Manifest 字段 | 阶段 |
|-----------|--------------|------|
| Compose started | `checks.compose_started` | VERIFYING |
| Container healthy | `checks.container_healthy` | VERIFYING |
| Health check passed | `checks.health_check_passed` | VERIFYING |
| Application tests | `checks.tests_passed` | VERIFYING |
| Screenshots captured | `checks.screenshots_generated` | VERIFYING |
| Screenshots published | `checks.screenshots_uploaded` | PUBLISHING |
| Report published | `checks.report_uploaded` | PUBLISHING |
| Manifest published | `checks.verification_manifest_uploaded` | PUBLISHING |
| Platform contract valid | `checks.required_platform_contract_valid` | RESOLVED |

- 展示文案的 i18n 由前端负责；**check 名与条数不得增删**。
- 版本页另需渲染 `verification.application` / `verification.platform` / `verification.tests` 三值（`passed`/`referenced`/`failed`/`skipped`），与 `checks` 布尔值分列，不得互相推断。

### 3.2 新增字段的来源约束

| 网站展示项 | 来源字段 | 是否允许前端推断 |
|-----------|---------|----------------|
| 详情页能力要点 | `features[]`（双语） | ❌ 来自 app schema → Manifest 投影 |
| 详情页 Docker Image | `deploy.docker_image` | ❌ 必须来自 Manifest（= `container.image`） |
| 更新类型判定依据（tooltip/审计） | `release.type_evidence` | ❌ 必须来自 Manifest |
| 验证运行链接 | `workflow_run_url` | ❌ 必须来自 Manifest |
| 截图 | `screenshots[].url` + `screenshots_order` | ❌ URL 与顺序均来自 Manifest，前端不得自行拼接对象路径 |
| 部署后指引（后台入口/首次登录方式/注意事项） | `deploy.post_deploy`（源 = app schema `deployment.post_deploy`，app-schema.md 规则17） | ❌ 必须来自 Manifest；无该键的应用前端只渲染平台通用步骤，不得自造后台路径或凭据提示 |
| 部署区成本估算卡 | `deploy.cost_estimate`（源 = app schema `deployment.cost_estimate`，app-schema.md 规则18） | ❌ 数字与口径均来自 Manifest；前端不得按实例规格自行计算价格 |
| 容器内数据路径 | `deploy.data_path`（源 = app schema `deployment.data_path`，app-schema.md 规则19） | ❌ 必须来自 Manifest；无该键的应用不显示数据路径，前端不得从 compose 文件推断 |
| 数据卷容量 | `deploy.data_volume_gb`（源 = app profile / schema 资源解析） | ❌ 必须来自 Manifest；不得误传到系统盘 `DiskGb` |
| 就绪探针路径 | `deploy.health_check_path`（源 = app schema `health_check.endpoint`） | ❌ 必须来自 Manifest；不得依赖模板默认 `/` |
| 应用 URL 环境变量名 | `deploy.app_url_env_name`（源 = app schema `deployment.app_url_env_name`，规则20） | ❌ 必须来自 Manifest；无该键表示无需应用私有 URL 变量 |

## 4. `release.type` 枚举与数据化

更新历史/列表页的"类型"必须数据化，不得前端猜测：

| 值 | 含义 | 由谁写入 |
|----|------|---------|
| `initial` | 首次上架 | Repo C 解析到该 app 无历史版本时 |
| `new_version` | 普通新版本 | Repo C 对比 `app_version` 变化且非安全公告 |
| `security_update` | 安全更新 | Repo C 解析上游 release 含 `security`/`CVE` 关键词，或人工标注 |
| `bug_fix` | 缺陷修复版本 | Repo C 解析 release notes 判定 |

前端根据 `release.type` 渲染不同颜色徽章（New Version 蓝 / Security Update 红 / Bug Fix 灰）。

### 4.1 确定性判定顺序（Repo C `RESOLVED` 阶段执行）

自上而下取**首个命中项**，并把命中依据写入 `release.type_evidence`（字符串，说明命中了哪条规则、命中了什么关键词/版本差）：

| 序 | 规则 | 结果 |
|----|------|------|
| 1 | 该 app 在 R2 无任何已发布 `versions/*.json` | `initial` |
| 2 | 上游 release `name` + `body` + `tag` 命中 `CVE-\d{4}-\d+` 或 `security advis?ory` / `security patch` / `vulnerab` | `security_update` |
| 3 | semver 可比且**仅 patch 位**变化（major/minor 不变） | `bug_fix` |
| 4 | 其余（major/minor 变化、semver 不可比如日期串/分支名） | `new_version` |

- 人工覆盖：只允许在 `apps/{app}.yaml` 的 `release_type_override` 显式声明（值 ∈ 四枚举 + 必填 `# reason:` 注释），优先级高于上表，且必须同样写入 `type_evidence`（记 `manual:` 前缀）。
- semver 不可比较时**禁止**猜测 `bug_fix`，一律落 `new_version`。
- 前端与 Repo A 一律**只读**该字段，不得重算或按版本号推断。

## 5. 网站统计数字（禁止伪造）

`website-design.md` 中的统计区（如 `120+` / `2,580+` / `98.7%` / `24/7`）**不得写死**。每个数字必须定义：

| 统计项 | 数据来源 | 计算公式 | 更新频率 |
|--------|---------|---------|---------|
| 已验证应用数 | R2 `verified/*/current.json` 计数 | `count(current.json where status==verified)` | 每次构建 |
| 已验证版本数 | R2 `verified/*/versions/*.json` 计数 | `count(versions/*.json)` | 每次构建 |
| 验证成功率 | GitHub Actions API | `passed_runs / total_runs`（近 30 天） | 每次构建（拉 API） |
| GitHub Stars | GitHub REST API `GET /repos/{owner}/{repo}` | 各上游仓库 `stargazers_count` 求和 / 展示（对应前端 `{{github_stars}}` 占位符） | 每次构建（拉 API，限流降级为 `—`） |
| 自动化测试 | 常量/文案 | 固定文案 "Automated Testing"，非数值 | 静态 |

如果没有真实统计来源：使用明确的 **placeholder**（如 `{{verified_app_count}}` 在构建时替换），**绝不伪造业务数字**。降级：API 失败则用上次缓存或显示 `—`。

### 5.1 统计落点：`data/stats.json`（Repo A 构建期生成）

统计数字**不进 `current.json`**（否则 `current.json` 就不再逐字段等于 Manifest 的 `website` 段，违反 §2）。`fetch-verified.mjs` 构建期另生成一份：

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-08-27T10:05:00Z",
  "verified_app_count": 1,
  "verified_version_count": 3,
  "success_rate": { "value": 96.5, "window_days": 30, "source": "actions_runs", "sampled_at": "2026-08-27T10:05:00Z" },
  "github_stars": { "ghost": 52000, "n8n": 108000 },
  "degraded": ["success_rate"]
}
```

| 字段 | 计算来源 | 缺失时 |
|------|---------|--------|
| `verified_app_count` | `index.json.apps` 中 `status == "verified"` 计数 | 不可能缺（index 为必需） |
| `verified_version_count` | 各 app `versions/*.json` 中九项 checks 全真记录计数 | 同上 |
| `success_rate` | Repo C `application-verify.yml` 近 30 天 run 结论：`completed_success / completed_total` | 记入 `degraded[]`，前端显示 `—` |
| `github_stars` | GitHub REST `GET /repos/{owner}/{repo}` 的 `stargazers_count`（按 app 存，前端自行求和） | 记入 `degraded[]`，前端显示 `—` |

- `success_rate` 必须带 `window_days` 与采样时间，前端展示为"近 30 天"，**不得**展示成累计值。
- `degraded[]` 存在时，前端对应统计卡必须显示 `—` 而非上次数值，避免陈旧数字被误读为实时值。
- 统计文件属**派生产物**：不进 R2 的 verified 命名空间（写 Repo A 构建工作区 `data/stats.json`），不构成第二套验证事实源。


## 6. 反模式

- ❌ 前端根据版本号自己猜 `Security Update` / `New Version`。
- ❌ 前端 hardcode `120+` 等数字。
- ❌ 前端读 `apps/` 或 Git 内 `verified` 当事实源。
- ❌ 网站出现"未经 Manifest 证明"的已验证状态。
- ❌ 版本页渲染契约里不存在的检查项（如 `Docker Build` / `API Test`）——检查项名称以 §3.1 映射表为唯一准绳。
- ❌ 把 `github_stars` / `success_rate` 等构建期派生统计塞进 `current.json`（应落 `data/stats.json`，见 §5.1）。
- ❌ 没有 `index.json` 就靠枚举猜测 app 列表（§2.1）。
- ❌ 深链 templateURL 用站点自身 origin（`window.location.origin + /templates/...`）或 Repo A 自托管模板副本，而非公开 S3 直链（§2.4：站点副本会与验证所用模板漂移）。
- ❌ 渲染两阶段提交中间态的占位版本记录（三项上传 check 为 `false`，见 §2.2）。
- ❌ 前端自行拼接截图对象路径（必须用 `screenshots[].url` 原值）。
- ❌ 未验证的上游新版本在网站上显示为"已验证"或"待验证"占位卡（未 `PUBLISHED` 的应用根本不进 R2）。
