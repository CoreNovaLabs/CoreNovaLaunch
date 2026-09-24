# Contract · App Schema（应用注册 Schema）

> 优先级：**最高**（contracts/ 高于普通设计文档）。
> 术语：本文沿用 Repo A / Repo B / Repo C 代号，分别指 `CoreNovaLaunchWebsite`（官网，本地目录 `website/`）、`CoreNovaLaunchAmi`（AMI 构建，引导期未落地）、`CoreNovaLaunch`（验证枢纽）。
> 适用：Repo C `CoreNovaLaunch` 中的应用注册文件 `apps/{app}.yaml`。
> 本契约定义应用元数据的**唯一事实源**。任何设计文档、脚本、工作流与本文冲突时，以本文为准。

## 0. 核心原则：单一事实源

CoreNova Launch 把"应用该怎么跑"的所有静态定义集中在 `apps/{app}.yaml` 一个文件里。

- **`deploy.docker_image`、`deploy.image_tag_template`、`deploy.container_port`、`deploy.compose_file` 是容器定义的唯一事实源。**
- 对应的 `apps/{app}/docker-compose.yml` **不得硬编码** image、port 与应用可见 URL；必须用变量替换引用 app schema 的值：
  - `image: ${CORENOVA_APP_IMAGE}`
  - `ports: - "${CORENOVA_HOST_PORT}:${CORENOVA_CONTAINER_PORT}"`
- 工作流在执行验证前注入以下变量（**完整清单，compose 只允许引用这些**）：

| 变量 | 值 | 用途 |
|------|----|------|
| `CORENOVA_APP_IMAGE` | `<精确 tag>@<container.digest>` | 供 `image:` 使用，保证起的就是被解析的那个内容 |
| `CORENOVA_APP_IMAGE_REF` | 精确 tag（如 `ghost:5.75.0-alpine`） | 供报告/日志展示 |
| `CORENOVA_CONTAINER_PORT` | `deploy.container_port` | 容器内监听端口 |
| `CORENOVA_HOST_PORT` | 本机实际发布端口（默认等于 container_port，被占用时可覆盖） | 避免多应用同机验证端口冲突 |
| `CORENOVA_APP_URL` | `http://localhost:${CORENOVA_HOST_PORT}` | 应用自引用 URL（Ghost `url`、Nextcloud `TRUSTED_SERVER` 等），**禁止在 compose 里写死端口** |
| `CORENOVA_DATA_DIR` | 本次验证独占的临时数据目录 | bind mount，跑完即弃，不用命名卷（保证每轮从零启动） |

- 禁止在 compose 文件里再写一份 `image:` / `ports:` / 带端口的 URL 字面量，那会制造第二个事实源，导致"改了 yaml 没改 compose"的漂移。

## 1. 完整字段定义

```yaml
app:
  name: "ghost"                       # required, string, [a-z0-9-]+，全局唯一，等于文件名（不含 .yaml）
  category: "cms"                     # required, string, 业务分类，见 §4 enum
  app_type: "stateless_web"           # required, enum, 部署类型，见 app-profiles.md §2（与 category 解耦）
  icon: "/icons/ghost.svg"           # required, string, 指向 Repo A 静态资源
  i18n:                               # required
    en:
      display_name: "Ghost Blog"     # required, string
      description: "Open-source professional publishing platform"  # required, string
    zh:
      display_name: "Ghost 博客"       # required, string
      description: "开源专业发布平台"     # required, string

source:                               # required, 版本来源
  repo: "TryGhost/Ghost"             # required, string, 上游 GitHub 仓库 owner/name
  version_strategy: "release_tag"     # required, enum，见 §4
  release_filter:                     # optional
    prerelease: false                 # default false
    draft: false                      # default false

deploy:                               # required, 容器部署唯一事实源
  docker_image: "ghost"               # required, string, **镜像基名（不含 tag）**，作为 image_tag_template 的仓库基名
  image_tag_template: "ghost:{version_no_v}-alpine"  # required, string, 精确 tag 模板（占位符见 §3.1）；被验证镜像由此渲染，禁止移动 tag
  container_port: 2368                # required, integer, 容器内监听端口
  compose_file: "apps/ghost/docker-compose.yml"  # required, string, 相对仓库根路径
  # instance_type / disk_gb 见下方注释；资源档优先用 deployment.size 选择
  instance_type: "t3.small"          # optional, string；缺省由 deployment.size 推导（app-profiles.md §3）；显式写=按维度覆盖，向上自由、低于 min_size 地板需 `# override: <reason>`
  disk_gb: 20                         # optional, integer；缺省由 deployment.size 推导；同上
  extra_environment:                  # optional, string[]（KEY=VALUE）；应用私有、**非敏感**环境变量，
    - "database__client=sqlite3"      #   one-click 模板经 ExtraEnvironment 参数注入（env-file）。
    - "database__connection__filename=/var/lib/ghost/content/data/ghost.db"  # 密钥禁止放这里（走 SSM/Secrets）。
    - "database__useNullAsDefault=true"

resources:                            # optional（legacy）；优先用 deployment.size + deploy.instance_type/disk_gb；与 deploy.* 二选一，若都写必须相等（校验报错）
  instance_type: "t3.small"          # 缺省由 deployment.size 推导；显式写=向上自由、低于 min_size 地板需 `# override: <reason>`
  disk_gb: 20

health_check:                        # required
  endpoint: "/ghost/api/admin/site/" # required, string, 相对路径（验证打 localhost）
  expected_status: 200               # required, integer
  method: "GET"                      # optional, enum，default GET
  timeout_seconds: 5                 # optional, number，default 5
  retries: 10                         # optional, integer，default 10，就绪探测重试次数
  interval_seconds: 3                 # optional, number，default 3
  startup_timeout_seconds: 180       # optional, number，default 180，容器启动硬超时
  body: null                          # optional, string，仅 method=POST 时提供请求体
  content_type: "application/json"    # optional, string，body 存在时的 Content-Type
  expected_body_contains: null        # optional, string，响应体需包含的 substring（可选断言）
  version_assertion:                  # optional, 运行期版本断言：证明"容器里跑的就是 app_version"（见 §3.1/§3.2）
    kind: "exec_command"              # required(enum)：env | label | api_json_path | header | exec_command
    command: "node -p \"require('/usr/src/ghost/node_modules/ghost/package.json').version\""  # kind=exec_command 必填
    expected: "{version_no_v}"        # required, string，支持 {version} / {version_no_v} 占位
    match: "exact"                    # optional(enum)，exact | prefix，default exact；版本带 build 后缀时用 prefix
  # 语义：container running ≠ application ready。
  # startup_timeout 内完成 readiness 才算应用就绪；否则验证失败。
  # body / expected_body_contains 仅在需要时用于非 GET 就绪探针。

tests:                                # required
  predefined_dir: "apps/ghost/tests" # required, string，预写测试目录（优先于 AI 生成）
  scenarios:                          # optional, 截图场景清单（与 Manifest 截图一一对应，是 §5 规则 8 的 gating 依据）
    - slug: "home"                    # required, string, ^[a-z0-9][a-z0-9-]*$；同时用作 R2 对象文件名与 screenshots_order 值（**禁止中文/空格**）
      url: "/"                        # required, string, 相对验证 baseURL 的路径
      caption:                        # required, 双语显示名（网站 figcaption 用）
        en: "Home"
        zh: "首页"
    - slug: "admin"
      url: "/ghost/"
      caption: { en: "Admin", zh: "管理后台" }
  # 耦合约束：tests.scenarios[].slug ≡ Manifest.artifacts.screenshots[].scenario ≡ website.screenshots_order 条目。
  # 三者是同一组截图场景的三种引用，禁止各自独立命名导致漂移。

deployment:                           # required, 网站展示用的静态部署信息
  size: "small"                       # optional, enum；资源尺寸档，缺省取 app_type 的 default_size（app-profiles.md §3）；向上自由选，无需理由
  documentation_url: "https://docs.ghost.org"  # optional, string
  regions: ["us-east-1"]              # required, string[]，支持部署的区域列表
  post_deploy:                        # optional, 部署后指引（网站详情页「部署速览/部署后指引」区块的唯一事实源）
    admin_path: "/ghost/"             # optional, string；后台入口路径，必须以 / 开头；无后台概念的应用不写
    admin_setup:                      # admin_path 存在时必填；如何获得后台权限的**静态指引**（双语）
      en: "First visit opens the setup wizard to create the owner account."
      zh: "首次访问会进入初始化向导，按步骤创建管理员账户。"
    notes:                            # optional, Localized[]；部署后注意事项（双语）
      - en: "Data lives on the instance's EBS volume; back it up before terminating."
        zh: "数据保存在实例的 EBS 卷上，终止实例前请先备份。"
  cost_estimate:                      # optional, 月成本估算（网站部署区成本卡的事实源，§5 规则18）
    monthly_usd: 23                   # required(number)；按需价格估算（实例 730h/月 + EBS + 公网 IPv4），人工核对的数字
    note:                             # optional, Localized；估算口径（实例档/磁盘/区域），建议注明"以实际账单为准"
      en: "Verified default: ~$15.2 t3.small + $4 for 50 GB gp3 + $3.65 public IPv4 per month."
      zh: "已验证默认配置：t3.small 约 $15.2 + 50GB gp3 约 $4 + 公网 IPv4 约 $3.65/月。"
  persistence: volume                # optional, volume | none；none 显式声明无应用数据盘（规则19）
  data_path: "/var/lib/ghost/content" # optional, string；容器内数据挂载目录（与 compose 文件的容器目标一致），
                                      # 是 CFN DataContainerPath 参数的真相源；必须以 / 开头（§5 规则19）
  app_url_env_name: "url"             # optional, string；接收应用访问 URL 的原生环境变量名
                                      # （Ghost 为 url）；默认注入私有 LaunchUrl=http://localhost:8080（规则20）
  production_contract:                # optional, L1.5 生产核对声明（规则22，deployment-contract.md §2.6）：
                                      # 先候选，真实 AWS 精确核对及清理确认后才晋级；不自动解除人工 hold
    checks: ["admin_auth", "data_dir_write"]  # 非空、去重，值 ∈ {admin_auth, data_dir_write, url_injection, host_metrics}
  hold:                               # optional, 运维性部署暂停（规则21）：独立于历史验证结果，
                                      # “已验证”≠“当前可部署”；官网拦截所有部署入口并展示原因。
                                      # 人工重新核对后移除，再跑 sync_holds；自动验证/晋级永不清除
    reason:                           # required, Localized；暂停原因（双语），官网原样渲染
      en: "Deployment paused pending production-contract re-verification."
      zh: "暂停部署：待完成生产契约重新验证。"

release_type_override: null           # optional, enum(initial|new_version|security_update|bug_fix)
                                      # 仅在上游 release notes 无法被 deployment-contract.md §4.1 规则可靠判定时人工覆盖；非空必须带 `# reason:` 注释

website:                              # required
  featured: true                      # required, boolean
  screenshots_order: ["home", "admin"]  # optional, string[]，值必须 ∈ tests.scenarios[].slug
  tags: ["blog", "cms", "publishing"] # required, string[]
  features:                           # optional, 双语能力要点（网站详情页）；每项必须同时含 en 与 zh
    - en: "Automated testing before each release"
      zh: "每次发布前自动化测试"
    - en: "One-click CloudFormation deploy"
      zh: "CloudFormation 一键部署"
```

## 2. 必填 / 可选 / 类型 / 默认值

| 字段 | 必填 | 类型 | 默认 | 约束 |
|------|------|------|------|------|
| `app.name` | ✅ | string | — | `^[a-z0-9-]+$`，等于文件名 |
| `app.category` | ✅ | enum | — | 业务分类，见 §4 |
| `app.app_type` | ✅ | enum | — | 部署类型，见 app-profiles.md §2，与 category 解耦 |
| `app.icon` | ✅ | string | — | 以 `/` 开头 |
| `app.i18n.{en,zh}.display_name` | ✅ | string | — | 双语都必须 |
| `app.i18n.{en,zh}.description` | ✅ | string | — | 双语都必须 |
| `source.repo` | ✅ | string | — | `owner/name` |
| `source.version_strategy` | ✅ | enum | — | 见 §4 |
| `source.release_filter.prerelease` | ❌ | bool | `false` | — |
| `source.release_filter.draft` | ❌ | bool | `false` | — |
| `deploy.docker_image` | ✅ | string | — | **镜像基名，不含 `:` tag**（tag 由 `image_tag_template` 渲染） |
| `deploy.image_tag_template` | ✅ | string | — | 含且仅含 `{version}` / `{version_no_v}` 占位（§3.1）；渲染结果必须带版本号，禁止 `latest` / `5-alpine` 这类移动 tag |
| `deploy.container_port` | ✅ | int | — | 1–65535 |
| `deploy.compose_file` | ✅ | string | — | 文件必须存在且用变量替换 |
| `deploy.instance_type` | ❌ | string | — | AWS 实例类型；缺省由 `deployment.size` 推导 |
| `deploy.disk_gb` | ❌ | int | — | ≥ 8；缺省由 `deployment.size` 推导 |
| `deploy.extra_environment` | ❌ | string[] | `[]` | `KEY=VALUE` 形式的应用私有**非敏感** env；one-click 模板经 `ExtraEnvironment` 注入；值含 secret/password/token 等敏感词即校验失败 |
| `deployment.size` | ❌ | enum | app_type 的 `default_size` | small/medium/large/xlarge；必须 ≥ 本 app_type 的 `min_size` |
| `health_check.endpoint` | ✅ | string | — | 以 `/` 开头 |
| `health_check.expected_status` | ✅ | int | — | 2xx/3xx；非此范围需显式注释 |
| `health_check.method` | ❌ | enum | `GET` | GET/HEAD/POST（body 仅 POST 用） |
| `health_check.*_seconds` | ❌ | number | 见上 | > 0 |
| `health_check.body` | ❌ | string | `null` | 仅 method=POST 时非空 |
| `health_check.content_type` | ❌ | string | `application/json` | body 存在时生效 |
| `health_check.expected_body_contains` | ❌ | string | `null` | 可选响应体断言 |
| `health_check.version_assertion.kind` | ❌ | enum | `null` | env/label/api_json_path/header/exec_command；配置后 VERIFYING 阶段必须实测通过 |
| `health_check.version_assertion.expected` | 条件必填 | string | — | 配置了 assertion 时必填；仅允许 `{version}` / `{version_no_v}` 占位 |
| `tests.predefined_dir` | ✅ | string | — | 目录应存在 |
| `tests.scenarios[].slug` | ❌ | string | — | `^[a-z0-9][a-z0-9-]*$`，同 app 内唯一；作为对象键与 `screenshots_order` 值 |
| `tests.scenarios[].caption.{en,zh}` | 条件必填 | string | — | 写了 scenario 就必须双语齐全 |
| `deployment.regions` | ✅ | string[] | — | 非空；v1 必须等于 `[<Platform Contract.region>]` |
| `deployment.documentation_url` | ❌ | string | — | URL |
| `deployment.post_deploy.admin_path` | ❌ | string | `null` | 以 `/` 开头的后台入口路径；无后台概念的应用不写 |
| `deployment.post_deploy.admin_setup` | 条件必填 | Localized | — | `admin_path` 存在时 `en`/`zh` 均非空；只写"如何获取权限"，**禁止出现任何凭据**（规则16 红线同理） |
| `deployment.post_deploy.notes[]` | ❌ | Localized[] | `[]` | 每项 `en` 与 `zh` 都必须非空 |
| `deployment.cost_estimate.monthly_usd` | 条件必填 | number | — | `cost_estimate` 存在时必填且 > 0；按需价格估算至少覆盖实例 + 全部 EBS + 公网 IPv4 |
| `deployment.cost_estimate.note` | ❌ | Localized | `null` | 估算口径说明；若存在 `en`/`zh` 均非空；含敏感词即校验失败 |
| `deployment.persistence` | ❌ | `volume` / `none` | 未声明 | `none` 显式声明不创建应用数据盘、不挂载应用数据目录；只允许无挂载的 `stateless_web`，缺少 `data_path` 本身不代表 `none` |
| `deployment.data_path` | stateful_app 或显式 volume 必填 | string | `null` | 容器内数据挂载目录，必须以 `/` 开头；与 compose 文件容器目标一致，是 CFN `DataContainerPath` 的真相源；`none` 时禁止声明 |
| `deployment.app_url_env_name` | ❌ | string | `null` | 接收公开访问 URL 的应用原生环境变量名；必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$` |
| `deployment.production_contract.checks` | ❌ | string[] | `null` | L1.5 生产核对声明；非空、去重，每项 ∈ {`admin_auth`,`data_dir_write`,`url_injection`,`host_metrics`}（规则22） |
| `release_type_override` | ❌ | enum | `null` | 非空时必须带 `# reason:`（deployment-contract.md §4.1） |
| `website.featured` | ✅ | bool | — | — |
| `website.tags` | ✅ | string[] | — | 非空 |
| `website.features[]` | ❌ | Localized[] | `[]` | 每项 `en` 与 `zh` 都必须非空 |

## 3. 版本 ↔ 镜像 ↔ Digest 四者对应（强制规则）

应用版本与容器镜像之间存在明确映射，禁止出现无法证明版本关系的写法：

| 概念 | 含义 | 可变性 | 落点 |
|------|------|--------|------|
| **App Version** | CoreNova 展示的应用版本，来自 `source` 解析（如 `v5.75.0`） | 每次发布变 | 写入 Verification Manifest `app_version` |
| **GitHub Release** | 上游 `source.repo` 的 release tag / revision | 每次发布变 | 写入 Manifest `release.source_revision` |
| **Docker Tag** | `deploy.image_tag_template` 用 app_version 渲染出的**精确** tag | **不可变**（版本号写死在 tag 里） | 写入 Manifest `container.image` |
| **Docker Digest** | 镜像内容寻址的 `sha256:...` | **不可变** | 写入 Manifest `container.digest` |

**禁止写法（校验直接失败）：**

```yaml
source:
  version_strategy: release_tag
deploy:
  docker_image: "ghost:5-alpine"       # ❌ 移动 tag：今天解析到 5.75.0，下月解析到 5.80.x，
                                       #    而 app_version 仍写成 v5.75.0 —— 版本断言纯属未经证明的声明
```

**正确写法（三者可追溯）：**

```yaml
source:
  version_strategy: release_tag
deploy:
  docker_image: "ghost"                          # 基名
  image_tag_template: "ghost:{version_no_v}-alpine"   # → ghost:5.75.0-alpine（精确 tag）
health_check:
  version_assertion:                             # 运行期自证：容器里跑的确实是 5.75.0
    kind: "exec_command"
    command: "node -p \"require('/usr/src/ghost/node_modules/ghost/package.json').version\""
    expected: "{version_no_v}"
    match: "exact"
```

### 3.1 `image_tag_template` 校验规则

1. 占位符**只允许** `{version}`（`app_version` 原样，如 `v5.75.0`）与 `{version_no_v}`（去掉前导 `v`/`V`，如 `5.75.0`）；出现其他占位符 → 校验失败。
2. 渲染结果必须包含至少一个**点分数字版本号**（正则 `\d+\.\d+`）；渲染成 `ghost:latest`、`ghost:5-alpine`、`ghost:alpine` 一律失败。
3. 渲染结果的 tag 部分不得等于 `latest` / `stable` / `main` 等移动语义标签，也不得只含 major 号（如 `5`）。
4. 模板的仓库基名部分（去掉 tag）必须与 `deploy.docker_image` 一致，否则说明写模板时改了仓库而忘了改基名 → 失败。
5. `source.version_strategy == release_tag` 或 `semver_latest` 时，模板**必须**含 `{version}` 或 `{version_no_v}`；`git_branch` / `pinned` 可不含（版本本身即镜像引用），但此时 Manifest 的 `app_version` 必须来自镜像引用本身，不得来自 release tag。
6. `source.version_strategy == release_tag` ⇒ `docker_image` 与渲染结果都不得以 `:latest` 结尾（兼容旧规则）。

### 3.2 `version_assertion` 语义（运行期自证）

| kind | 取值方式 | 典型场景 |
|------|---------|---------|
| `env` | `docker inspect` 容器环境变量 | 镜像内 `GHOST_VERSION` 之类 |
| `label` | 镜像 OCI label（`org.opencontainers.image.version`） | 上游规范打包 |
| `api_json_path` | `GET {baseURL}{path}` 后按 JSON Pointer 取值 | 应用暴露 `/version` 类端点 |
| `header` | 健康探测响应的 HTTP 头 | Ghost `x-ghost-version`（用 `match: prefix`） |
| `exec_command` | `docker exec` 内跑命令取 stdout；`command` 为字符串时通过 `sh -c` 执行，为非空字符串列表时直接按 argv 执行（不展开 shell） | 读 `package.json` / `--version`；scratch 镜像可用 `command: ["/bin/dufs", "--version"]` |

- `expected` 支持 §3.1 同一组占位符；`match: prefix` 仅允许用于"上游自报版本粒度更粗"（如只报 `5.75`）的场景，且必须在 yaml 注释说明粒度来源。
- 断言失败 → 分类 `APPLICATION`（镜像与版本不符）**不得自动重试、不得发布**；若确认是断言方式过时（上游改了路径），走 `FIX_PR` 改 `apps/{app}.yaml`（属 AI 白名单外，需人工 review）。
- 未配置断言的 app 必须在 yaml 注明"上游无版本可观测性"，并在报告里显示 `version_assertion: not_configured`，让读者知道版本关系仅由 tag 命名证明。

## 4. Enum 定义

- `app.category`（业务分类）：`cms` | `ai` | `media` | `devops` | `productivity` | `database` | `auth` | `automation` | `other`
- `app.app_type`（部署类型，与 category 正交）：`stateless_web` | `stateful_app` | `database` | `cache` | `queue` | `worker` | `cron` | `other`（推荐档见 app-profiles.md §3）
- `source.version_strategy`：
  - `release_tag` — 跟踪上游最新 release tag（最常用）
  - `semver_latest` — 从 tag 中取满足 semver 的最新稳定版
  - `git_branch` — 跟踪某分支 HEAD（不稳定，仅调试）
  - `pinned` — 锁定到 `deploy.docker_image` 指定版本，不做自动升级
- `health_check.method`：`GET` | `HEAD` | `POST`
- `release.type`（运行时生成，见 deployment-contract）：`initial` | `new_version` | `security_update` | `bug_fix`

## 5. 校验规则（工具/CI 强制）

1. `app.name` == 文件名（去掉 `.yaml`）。
2. `i18n.en` 与 `i18n.zh` 字段齐全。
3. `deploy.compose_file` 指向的文件存在，且：
   - `image:` 只允许 `${CORENOVA_APP_IMAGE}`，**不得**出现镜像字面量；
   - `ports:` 只允许 `${CORENOVA_HOST_PORT}:${CORENOVA_CONTAINER_PORT}` 形式；
   - 全文**不得**出现硬编码端口或含端口的 URL 字面量（如 `http://localhost:2368`）——应用自引用 URL 必须用 `${CORENOVA_APP_URL}`（§0）；
   - compose 引用的变量必须 ∈ §0 注入清单，未声明变量视为校验失败。
4. `deploy.image_tag_template` 满足 §3.1 全部六条（移动 tag / 无版本号 / 基名不符一律失败）。
5. `deploy.instance_type` 与 `resources.instance_type` 同时出现时必须相等。
6. `health_check.expected_status` 必须为 2xx（200/201/202/204）或 3xx 重定向；非上述范围需显式注释原因；`method=POST` 时 `body` 必须非空。
7. `deployment.regions` 非空，且每个 region 必须已被某有效 Platform Contract 覆盖（否则 Application Verification 因 `required_platform_contract_valid=false` 而失败）。
8. `tests.scenarios[].slug` 集合必须与 `website.screenshots_order` 完全一致（且等于生成 Manifest 时 `artifacts.screenshots[].scenario`）；slug 必须匹配 `^[a-z0-9][a-z0-9-]*$`；三者命名不一致校验失败，禁止各自独立命名导致漂移。
9. `app.app_type` 必填，且与 `app.category` 解耦（两者可任意组合，如 `category: cms` + `app_type: stateless_web`）。
10. **尺寸阶梯选择（app-profiles.md §3 / §5）**：`deployment.size` 取值必须 ∈ {small, medium, large, xlarge} 且 ≥ 本 `app_type` 的 `min_size`（如 `database` 无 small 档，写 `size: small` 校验失败）。`deploy.instance_type` / `deploy.disk_gb` 不写时由 `size`（或 `default_size`）推导；显式写时视为按维度覆盖——**≥ 推导值自由放行（无需理由）**，**< 本 `app_type` 的 `min_size` 地板必须 `# override: <reason>`**，否则校验失败。`port_tier` 由 app_type 推导，禁止 app 自行覆盖。
11. `deployment.size`（若存在）与 `deploy.instance_type` / `deploy.disk_gb`（若同时存在）按维度合并：某维度显式值优先于 size 推导值；合并后任一维度低于 `min_size` 地板即触发规则 10 的 `# override:` 要求。
12. `health_check.version_assertion` 若存在：`kind` ∈ §3.2 五枚举；`kind` 对应的取值字段必须齐备（`exec_command`→`command`、`env`→`name`、`label`→`name`、`api_json_path`→`path`+`json_pointer`、`header`→`name`）；`expected` 只能含 §3.1 允许的两个占位符；`match` ∈ {exact, prefix}。
13. `website.features[]` 若存在，每项 `en` 与 `zh` 均非空（缺一即失败，禁止网站一侧显示另一语言）。
14. v1 单区域约束：`deployment.regions` 必须恰好等于 `[<所引用 Platform Contract.region>]`；写多区域即校验失败（防止前端显示"已支持"而平台契约不存在，见 platform-contract.md §7）。
15. `release_type_override` 非空时，同一逻辑行或其紧邻上一行必须含 `# reason:` 注释，且值 ∈ 四枚举（deployment-contract.md §4.1）。
16. `deploy.extra_environment` 每项必须匹配 `^[A-Za-z_][A-Za-z0-9_]*=.+$`（KEY=VALUE）；KEY 或 VALUE 含 `secret`/`password`/`token`/`private_key`（不区分大小写）即校验失败——敏感值走 SSM Parameter Store / Secrets，不进 app schema（§7 单容器边界同理）。
17. `deployment.post_deploy` 若存在：必须是映射；`admin_path` 若存在必须以 `/` 开头；`admin_path` 存在时 `admin_setup` 的 `en`/`zh` 均必须非空（有后台入口就必须说明怎么进去）；`notes[]` 每项 `en`/`zh` 均非空；所有文案含 `secret`/`password`/`token`/`private_key`（不区分大小写）即校验失败——凭据不进契约，只允许写"去哪获取"。
18. `deployment.cost_estimate` 若存在：必须是映射；`monthly_usd` 必须为正数（写了 `cost_estimate` 就必须给出可信数字）；`note` 若存在 `en`/`zh` 均非空，且含 `secret`/`password`/`token`/`private_key`（不区分大小写）即校验失败——价格与口径是注册时人工核对的事实，前端只展示、绝不按实例规格自行计算。
19. `stateful_app` 或显式 `deployment.persistence: volume` 必须声明 `deployment.data_path`；其他类型若声明也必须为以 `/` 开头的字符串；含空格或 shell 元字符（`;&|`$`）则校验失败。该值必须与应用 compose 文件的容器挂载目标一致——它是 CFN `DataContainerPath` 参数的唯一真相源，compose / CFN / extra_environment 三处不得各自硬编码导致漂移。`deployment.persistence` 若声明只允许 `volume` 或 `none`；`none` 只允许 `stateless_web`，禁止声明 `data_path`、Compose 卷挂载和 `data_dir_write` 核对项。缺少数据路径不得推断为 `none`。`none` 投影为 `deploy.persistence: none`、`deploy.data_volume_gb: 0`，省略 `deploy.data_path`；不创建应用数据 EBS、不挂载数据目录，但实例系统盘及其他平台资源仍存在并计费。
20. `deployment.app_url_env_name` 若存在，必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`。它只声明应用原生变量名；变量值由 CloudFormation 在启动时根据显式 `LaunchUrl` 或实例 `PublicDnsName` 生成，禁止 app schema、前端或模板写死部署地址。
21. `deployment.hold` 若存在：必须是映射且仅含 `reason` 键；`reason` 的 `en`/`zh` 均非空，且含 `secret`/`password`/`token`/`private_key`（不区分大小写）即校验失败。hold 是**运维性部署暂停**，独立于验证结果（`status: verified` 的应用也可能被 hold）；声明后投影进 `current.json` 的 `deploy.hold`，官网以此拦截全部部署入口。发布与解除走 `scripts/verify/sync_holds.py`（条件写，不动 versions/、index 与任何验证字段），不等下一次验证——否则“hold 拦住验证 → hold 字段永远进不了发布数据”死锁。
22. `deployment.production_contract` 若存在：必须是映射且仅含 `checks` 键；`checks` 为非空、无重复的 string[]，每项 ∈ {`admin_auth`, `data_dir_write`, `url_injection`, `host_metrics`}。它声明 L1.5 **发布前**生产门禁（deployment-contract.md §2.6）：先把 Manifest、报告、截图隔离到 `candidates/{app}/{run}/{attempt}/{verification_id}/`，`CANDIDATE_READY` 不是发布；`production-verify.yml` 核对同 Manifest SHA、digest、模板 revision/CFN 实际模板、私有 SSM 参数及清理确认后，才 CAS current 并更新版本与索引。24 小时新鲜度、latest 候选意图/run/attempt、app config hash 与 current etag 均须通过；任一失败不晋级。**人工 hold 永不自动清除**，须人工重新核对后移除 yaml 再 sync_holds。核对项同时决定深链模板参数（`admin_auth`→`AdminAuthEnabled`、`host_metrics`→`HostMetricsAccess`），投影为 `deploy.production_contract.checks`；默认 `LaunchUrl=http://localhost:8080`、`AllowedWebCidr=127.0.0.1/32`、`SelfSignedTls=false`。无该声明仍走旧 P1–P5，不能宣称有生产验收。`checks` 含 `data_dir_write` 时必须声明 `deployment.data_path`（规则19）；含 `url_injection` 时须有 `deployment.app_url_env_name`（规则20）或含 `${CORENOVA_APP_URL}` 的 `deploy.extra_environment`，否则校验失败。

## 6. 反模式

- ❌ 在 compose 文件里再写一份 `image:` / `ports:`（第二个事实源）。
- ❌ `docker_image: latest` + `version_strategy: release_tag`（无法证明版本）。
- ❌ 用移动 tag（`ghost:5-alpine` / `:latest` / `n8n:latest`）充当被验证镜像，再声称 `app_version` 为某个具体版本（§3.1）。
- ❌ compose 里写死 `url: http://localhost:2368` 这类带端口的字面量（与 `${CORENOVA_CONTAINER_PORT}` 漂移，规则 3）。
- ❌ `tests.scenarios[].slug` 使用中文或空格（对象键非 ASCII、URL 编码不确定，规则 8）。
- ❌ 把 `features` / `docker_image` 等前端展示字段直接写进 Repo A 的 mock 而不进 app schema → Manifest 投影链（会造成第二事实源）。
- ❌ `deployment.regions` 写多个区域而平台契约仅覆盖 `us-east-1`（规则 14；网站会把"规划中"显示成"已支持"）。
- ❌ 把 digest 静态写死在 app schema（digest 是运行时解析结果，归 Verification Manifest）。
- ❌ 在 `deployment.post_deploy` 文案里写初始密码/账号（凭据面进契约即泄漏面；只写"去哪获取"，规则 17）。
- ❌ 前端按实例规格/磁盘自行计算成本（价格是事实，必须来自 `deployment.cost_estimate` 注册数据，规则 18）。
- ❌ compose 文件的容器挂载目标、`extra_environment` 中的路径引用、CFN `DataContainerPath` 三处各自硬编码不同路径（必须以 `deployment.data_path` 为唯一真相源，规则 19）。
- ❌ 前端猜测应用接收公开 URL 的变量名，或在 `extra_environment` 写死主机名（必须来自 `deployment.app_url_env_name`，规则 20）。
- ❌ 把 `ami_id` / `region` 写进 app schema（那是平台层契约，归 Platform Contract）。
- ❌ 用版本重新验证代替暂停解除：`deployment.hold` 的解除条件是生产契约重新验证通过，验证流水线也不负责写入 hold（那是 sync_holds 的运维路径，规则 21）。
- 禁止把生产核对全绿当成自动删除人工 hold 的授权，或用旧 current 的证据晋级新候选；生产门禁与人工暂停是独立控制（规则 21/22）。

## 7. 部署模型边界：当前为单容器（2026-08-30 明确）

**现状（v1）**：一个应用 = 一个被验证镜像（`deploy.docker_image` + `image_tag_template`）+ 一个对外端口
（`deploy.container_port`）。compose 里只允许这一个 `${CORENOVA_APP_IMAGE}` 服务对外暴露。

**因此暂不可注册**：需要 sidecar 的应用，如 `n8n + Postgres`、`Ghost + MySQL`、`Nextcloud + Redis + DB`。
这类应用若强行按单容器接（内嵌 SQLite）可以跑，但属降级形态，须在 yaml 注释标明。

**多容器扩展方向（本期不实现，仅记录设计意图）**：
- `deploy.services[]`：每项 `{ name, image_template, container_port, internal: bool, depends_on[] }`；
  对外端口仍唯一（主服务），sidecar 一律 `internal`（不暴露；port_tier 由 app_type 推导不变）。
- 依赖序：compose 由 `depends_on` 拓扑排序生成；验证按序等就绪。
- 健康检查聚合：`health_check` 拆为主服务探针 + 各 sidecar 就绪探针；`checks.container_healthy`
  = 全部服务就绪；Manifest 的 `container.digest` 升格为 `services[].digest` 逐服务钉住。
- 版本语义：`app_version` 仍指主应用版本；sidecar 版本各自钉 digest 进 Manifest 供复跑。

在上述扩展落地并同步更新 §0/§3/§5 校验规则之前，校验器对 compose 中出现第二个对外 `image:` 直接判失败。
