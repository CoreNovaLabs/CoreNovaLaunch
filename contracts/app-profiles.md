# Contract · App Profiles（应用推荐配置档）

> 优先级：**最高**（contracts/ 高于普通设计文档；本契约与 app-schema.md 冲突时，本契约的尺寸阶梯作为**缺省基准**，app-schema 显式选择优先——且**向上自由、低于 `min_size` 地板才需声明 reason**）。
> 术语：本文沿用 Repo A / Repo B / Repo C 代号，分别指 `CoreNovaLaunchWebsite`（官网，本地目录 `website/`）、`CoreNovaLaunchAmi`（AMI 构建，引导期未落地）、`CoreNovaLaunch`（验证枢纽）。
> 适用：Repo C `apps/{app}.yaml` 中 `deploy.instance_type` / `deploy.disk_gb` / `deployment` 端口档 / `resources` 的推荐基准。
> 本文回答一个问题："不同应用该用什么配置？"——按 `app_type`（部署类型）给**尺寸阶梯**，既避免每个应用从零拍脑袋，也允许应用**自由上选更大配置**（而非被钉死最低档），仅对跌破安全/资源地板的情况要求说明理由。

## 1. 为什么需要 App Profiles

`app.category`（cms / ai / database / ...）是**业务分类**，用于网站展示与筛选；它**不决定资源档**。
真正决定 instance_type / 磁盘 / 端口暴露 / 是否需要持久卷的是**部署形态**——即 `app_type`。

示例（说明正交性）：

| 应用 | `category`（业务分类） | `app_type`（部署类型） | 原因 |
|------|------------------------|------------------------|------|
| Ghost 博客 | `cms` | `stateless_web` | 状态在外部 DB / 对象存储，本地无持久业务数据 |
| Postgres | `database` | `database` | 本地必须有高 IOPS 持久卷，端口仅 internal |
| n8n（内嵌 SQLite） | `automation` | `stateful_app` | 本地需持久卷 |
| Redis | `database` | `cache` | 内存存储，端口 internal，无状态卷 |

`category` 与 `app_type` **正交、解耦、都必填**（见 app-schema.md §2 / §4）。

## 2. `app_type` 枚举（部署类型）

| 值 | 含义 | 典型应用 |
|----|------|---------|
| `stateless_web` | 无状态 Web 服务，本地不持久业务数据 | Ghost、WordPress、Umami、Plausible、AppSmith |
| `stateful_app` | 有状态应用，本地需持久卷 | n8n(内嵌DB)、Cal.com、Directus、Vikunja |
| `database` | 数据库引擎 | Postgres、MySQL、MongoDB |
| `cache` | 缓存 / 内存存储 | Redis、Memcached、Valkey |
| `queue` | 消息队列 | RabbitMQ、NATS、Redpanda |
| `worker` | 后台 worker，无对外端口 | 各类 sidekiq / celery / bullmq worker |
| `cron` | 定时任务，无长驻端口 | 各类 scheduler |
| `other` | 兜底，需人工标注 resource 意图 | — |

## 3. 配置档：尺寸阶梯（sizing ladder）

每个 `app_type` 提供一组**尺寸档（t-shirt size）**。app 通过 `deployment.size` 选择，缺省取 `default_size`。

- **选更大尺寸 = 自由**，不需要任何理由（这就是"应用可以选不同配置，而非被钉死最低档"）。
- **选低于 `min_size` 的档 = 需 `# override: <安全/功能 reason>`**（§5），因为那是资源/安全地板。
- `default_size` 是该类型大多数应用的推荐起点；`min_size` 是允许下探的底线。两者通常相等（即默认就是底线，但可自由上探）。

| app_type | `size` → `instance_type` / `disk_gb`（可自由上选） | `min_size` | `default_size` | `port_tier` | `stateful_volume` | `startup_timeout_seconds` |
|----------|------------------------------------------------------|-----------|---------------|-------------|-------------------|----------------------------|
| `stateless_web` | small=t3.small/20 · medium=t3.medium/40 · large=t3.large/80 · xlarge=t3.xlarge/160 | small | small | public | false | 180 |
| `stateful_app` | small=t3.small/30 · medium=t3.medium/60 · large=t3.large/120 · xlarge=t3.xlarge/240 | small | small | public | true | 180 |
| `database` | medium=t3.medium/100 · large=t3.large/200 · xlarge=t3.xlarge/400（无 small 档，地板即 medium） | medium | medium | internal | true (gp3, 高 IOPS) | 240 |
| `cache` | small=t3.small/10 · medium=t3.medium/20 · large=t3.large/40 | small | small | internal | false (可 ephemeral) | 120 |
| `queue` | small=t3.small/20 · medium=t3.medium/40 · large=t3.large/80 | small | small | internal | true | 180 |
| `worker` | small=t3.small/10 · medium=t3.medium/20 · large=t3.large/40 | small | small | none | false | 120 |
| `cron` | small=t3.small/10 · medium=t3.medium/20 · large=t3.large/40 | small | small | none | false | 120 |
| `other` | small=t3.small/20 · medium=t3.medium/40 · large=t3.large/80 | small | small | internal | false | 180 |

> 有效尺寸档需同时 ≥ `min_size`。例如 `database` 没有 small 档，若某 app 写 `size: small` 会校验失败（地板为 medium）。
> `architecture` 固定 x86_64（platform-contract.md §7），故 instance_type 均为 t3 家族（x86_64）。若未来支持 ARM，此处同步扩展。

## 4. 端口档（port_tier）定义

与 Security Group 渲染直接对应（见 infra-build-design.md）：

| port_tier | 含义 | SG 入站规则 |
|-----------|------|------------|
| `public` | 经 ALB / NGINX 暴露公网 | 443/80 from 0.0.0.0/0（前置 ALB） |
| `internal` | 仅 VPC 内 / 应用 SG 内可达 | 仅允许来自应用 SG 或 VPC CIDR |
| `none` | 不暴露任何入站端口 | 无入站规则（worker / cron 仅出站） |

`port_tier` **由 app_type 推导**（见 §3），应用不得在 `apps/{app}.yaml` 中自行声明 public/internal/none 来覆盖安全边界——如需例外，必须在 `infra/**` 或安全评审中显式批准（超出 AI 修改范围，见 workflow-state-machine.md §6 白名单）。

## 5. 选择规则（应用如何选配置）

应用**不是被钉死在最低档**——它有两种合法方式选不同配置：

**方式 A（推荐）：用 `deployment.size` 选尺寸档**
```yaml
app:
  app_type: "stateless_web"
deployment:
  size: "medium"        # 自由上选，无需理由；缺省取 app_type 的 default_size
```
- `size` ∈ {small, medium, large, xlarge}，但必须 ≥ 本 `app_type` 的 `min_size`（见 §3）。
- 选 `default_size` 以上的尺寸：**自由**，不写理由。

**方式 B（非对称微调）：显式覆盖某维度**
当应用需要"更大磁盘但同 CPU"这类非对称需求时，直接写 `deploy.instance_type` / `deploy.disk_gb`：
```yaml
deploy:
  instance_type: "t3.large"   # 自由上探，无需理由
  disk_gb: 50                 # 自由上探，无需理由
```
- 显式值 **≥** 由 `size`（或 `default_size`）推导的值 → 自由，无需 `# override:`。
- 显式值 **<** 本 `app_type` 的 `min_size` 地板（如 `database` 想降到 t3.small）→ **必须** `# override: <安全/功能 reason>`，否则 CI 失败。

> 语义总结：**向上自由、向下守地板**。最小档是安全/资源底线，不是"只能给这么点"。应用想多要资源（更大 size 或非对称上探）一律放行。

**`port_tier` 仍由 app_type 推导**（§4），应用不得在 `apps/{app}.yaml` 中自行声明 public/internal/none 覆盖安全边界。

校验细则见 app-schema.md §5（规则 10 / 11）。

## 6. 与 app-schema 的关系

- `app_type` 是 `app-schema.md` 的 **required** 字段（见 app-schema.md §2 / §4）。
- `deployment.size` 是 **optional** 选择器（app-schema.md §1 / §2）；**不写时取本契约 §3 的 `default_size`**，再据本表映射出 `instance_type` / `disk_gb`。
- `deploy.instance_type` / `deploy.disk_gb` 在 app-schema 中仍为 optional；**不写时由 `size` 推导**；显式写则为按维度覆盖（向上自由、低于 `min_size` 地板需 `# override:` 理由）。
- `health_check` 的 `startup_timeout_seconds` 缺省取 §3 同行的默认值；其余 `method` / `timeout_seconds` / `retries` / `interval_seconds` 缺省取 app-schema.md 通用默认值；`endpoint` 仍必须由 app 按应用填写（无法通用）。

## 7. 反模式

- ❌ 所有应用写死同一 `instance_type`（无差异化）。
- ❌ `database` / `cache` / `queue` 暴露 `public` 端口（安全违规）。
- ❌ 把 `min_size` 地板当"只能给这么点"——应用想多要资源应自由上选 `size`，而非被限制。
- ❌ 把 size 降到 `min_size` 以下却不写 `# override: <reason>`（资源/安全不足，AI 乱配）。
- ❌ 用 `category` 推断资源档（`category` 是业务分类，不决定资源）。
- ❌ `worker` / `cron` 配置对外端口。
