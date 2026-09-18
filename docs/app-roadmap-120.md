# CoreNova Launch · 120 个新应用分批上架路线图

> 候选快照：2026-09-15。本清单是新增 backlog，不包含 `apps/*.yaml` 已有的 22 个应用。
> 候选不等于已验证；只有 Application Verification 全绿并进入 `PUBLISHED`
> 的记录才能出现在官网。

## 选品与排序规则

1. 上游仓库活跃、有明确的开源许可或可审核的许可文件。
2. 有真实的自托管需求，优先数据主权、团队协作、开发运维和家庭媒体。
3. 前三批优先当前单容器平台可落地的应用；需多容器、GPU、特权或 Docker socket
   的应用放在后续批次，等平台契约扩展后再做。
4. 每个应用上架前必须通过：schema → release 解析 → 镜像 digest 解析 →
   `linux/amd64` 真容器测试 → 截图 → 发布门禁。
5. 仓库归档、版本 tag 与镜像 tag 无法一一对应、许可不清、或无安全的单容器
   形态时，直接从当批退回 backlog，不降低验证标准。

## Batch 1 · 个人生产力与媒体快速批（已发布：2026-09-16，10/10）

| # | 应用 | 上游 | 核心价值 | 当前边界 |
|---:|---|---|---|---|
| 1 | code-server | `coder/code-server` | 浏览器中的 VS Code 开发环境 | 单容器，可立即接入 |
| 2 | Mealie | `mealie-recipes/mealie` | 菜谱、餐食计划与购物清单 | SQLite 单容器，可立即接入 |
| 3 | Linkding | `sissbruecker/linkding` | 轻量多用户书签库 | SQLite 单容器，可立即接入 |
| 4 | Trilium Notes | `TriliumNext/Trilium` | 大型层级化个人知识库 | 单容器，可立即接入 |
| 5 | Node-RED | `node-red/node-red` | 事件流和 IoT 可视化编排 | 单容器，可立即接入 |
| 6 | Gotify | `gotify/server` | 简单可控的应用消息推送 | SQLite 单容器，可立即接入 |
| 7 | Navidrome | `navidrome/navidrome` | 个人音乐流媒体 | 单容器，媒体目录需纳入单卷模型 |
| 8 | Audiobookshelf | `advplyr/audiobookshelf` | 有声书和播客服务器 | 单容器，通过统一数据根目录持久化 |
| 9 | Actual Budget | `actualbudget/actual` | 隐私优先的个人预算与同步 | SQLite 单容器，可立即接入 |
| 10 | SFTPGo | `drakkan/sftpgo` | SFTP/HTTP/WebDAV 文件传输与管理 | 单容器，仅验证 HTTP 管理面 |

## Batch 2 · 首页、工具箱与可视化协作

2026-09-16：Homer `v26.08.3`、IT-Tools `v2024.10.22-7ca5933`、CyberChef `v11.4.0`
和 draw.io `v31.4.5` 已通过本地与 CI 完整验证并发布至公开数据源（4/10）。
验证任务：[Homer](https://github.com/CoreNovaLabs/CoreNovaLaunchVerify/actions/runs/35062601771)、
[IT-Tools](https://github.com/CoreNovaLabs/CoreNovaLaunchVerify/actions/runs/35062605378)、
[CyberChef](https://github.com/CoreNovaLabs/CoreNovaLaunchVerify/actions/runs/35062608111)、
[draw.io](https://github.com/CoreNovaLabs/CoreNovaLaunchVerify/actions/runs/35062611179)。
四项 `verify` job 均成功；中间两项官网通知被并发队列替换，末次通知和官网构建已成功。
本批按实际部署条件拆分，剩余 6 项保留候选资格：

- Homepage：部署域名需要映射至 `HOMEPAGE_ALLOWED_HOSTS`；现有 URL 注入传入完整 URL，
  不能直接用作 host 列表。接入前补充 host 注入与访问控制方案。
- Dashy：`4.6.0` 挂载空 `/app/user-data` 后首页返回 200，但 `/conf.yml` 返回 404；
  需支持初始配置落盘，不能仅凭首页状态码上架。
- Homarr：`v1.77.1` 必需 `SECRET_ENCRYPTION_KEY`，需平台支持生成、注入并持久化私密值。
- Flame：上游启动示例依赖管理凭据，待完成私密凭据注入方案审计。
- Excalidraw：当前官方 `publish-docker.yml` 只发布 `latest`，待提供可追溯的发行版镜像。
- SilverBullet：个人知识空间需要访问认证，待完成每实例凭据注入与首次访问方案。

本次证据：空卷 Docker 实测、容器内精确版本断言、HTTP/必需资源测试、真实浏览器截图。
对应的 `data/runs/` 报告仅为本地证据；是否已发布以公开 `current.json` 为准。

| # | 应用 | 上游 | 核心价值 | 预计门禁 |
|---:|---|---|---|---|
| 11 | Homepage | `gethomepage/homepage` | 自托管服务入口与状态面板 | 不挂 Docker socket 的安全形态 |
| 12 | Dashy | `Lissy93/dashy` | 可定制个人与团队导航页 | 固定版本镜像审计 |
| 13 | Homarr | `homarr-labs/homarr` | 家庭实验室仪表盘 | 确认无 socket 的基础模式 |
| 14 | Homer | `bastienwirtz/homer` | 超轻量静态服务首页 | 单容器 |
| 15 | Flame | `pawelmalak/flame` | 应用快捷入口与书签 | 版本镜像可追溯性 |
| 16 | IT-Tools | `CorentinTh/it-tools` | 开发者常用编码、转换与计算工具 | 无状态单容器 |
| 17 | CyberChef | `gchq/CyberChef` | 浏览器数据解析与编解码 | 官方版本镜像对应审计 |
| 18 | Excalidraw | `excalidraw/excalidraw` | 协作白板与架构草图 | 先上架无状态基础形态 |
| 19 | draw.io | `jgraph/drawio` | 专业流程图与架构图 | 单容器 |
| 20 | SilverBullet | `silverbulletmd/silverbullet` | Markdown 可编程知识空间 | 单容器 |

## Batch 3 · 文件分发与对象存储

2026-09-18：Dufs `v0.46.0` 已接入公开只读形态，禁止匿名上传、删除及 WebDAV 写入。
已补充无 shell 镜像的 argv 版本断言，保留原字符串命令兼容性，238 项回归测试通过。
本机 Docker API 无响应，未重启或影响其他容器；完整验证交由
[CI 验证任务](https://github.com/CoreNovaLabs/CoreNovaLaunchVerify/actions/runs/35349743323)，
CI 的 `verify dufs` 已成功，公开 `current.json` 已确认发布版本 `v0.46.0`，
验证编号 `dufs-v0.46.0-20260918-001`（本批已发布 1/10）。

本批其他候选保持待接入，不计作已上架：PairDrop 需要核实 HTTPS 反代的
WebSocket 与客户端 IP 转发；copyparty 默认匿名可写，需要安全配置初始化；
Gokapi 需要解决上传分块与代理请求体限制；PicoShare 需要私密凭据注入；
FileGator 的数据和配置持久化路径需要适配。对象存储类仍按表中前置条件审计。

| # | 应用 | 上游 | 核心价值 | 预计门禁 |
|---:|---|---|---|---|
| 21 | ownCloud Infinite Scale | `owncloud/ocis` | 现代文件同步与共享 | 单二进制组合模式审计 |
| 22 | RustFS | `rustfs/rustfs` | S3 兼容对象存储 | 管理密钥与存储路径设计 |
| 23 | Garage | `deuxfleurs-org/garage` | 轻量分布式对象存储 | 先做单节点形态 |
| 24 | SeaweedFS | `seaweedfs/seaweedfs` | 高效文件与对象存储 | 单进程 server 形态 |
| 25 | Dufs | `sigoden/dufs` | WebDAV/静态文件服务 | 单容器 |
| 26 | Gokapi | `Forceu/Gokapi` | 可过期的安全文件分享 | 单容器 |
| 27 | PairDrop | `schlagmichdoch/PairDrop` | 局域网/浏览器点对点传输 | 无状态单容器 |
| 28 | copyparty | `9001/copyparty` | 多协议轻量文件服务器 | 单容器 |
| 29 | PicoShare | `mtlynch/picoshare` | 简洁文件上传与分享 | 单容器 |
| 30 | FileGator | `filegator/filegator` | 多用户 Web 文件管理 | 单容器 |

## Batch 4 · 文档、知识库与团队写作

| # | 应用 | 上游 | 核心价值 | 平台前置 |
|---:|---|---|---|---|
| 31 | Nextcloud | `nextcloud/server` | 文件、日历与协作套件 | 多容器 DB/Redis，不做 SQLite 假精简 |
| 32 | Seafile | `haiwen/seafile` | 高性能文件同步 | 多容器 DB/缓存 |
| 33 | Paperless-ngx | `paperless-ngx/paperless-ngx` | OCR 文档归档与检索 | 多容器 Redis/Tika/Gotenberg |
| 34 | Outline | `outline/outline` | 团队 Wiki 与协作文档 | PostgreSQL + Redis |
| 35 | BookStack | `BookStackApp/BookStack` | 面向团队的书架式 Wiki | MySQL/MariaDB |
| 36 | Wiki.js | `requarks/wiki` | 可扩展的现代 Wiki | PostgreSQL |
| 37 | Docmost | `docmost/docmost` | 实时协作 Wiki | PostgreSQL + Redis |
| 38 | HedgeDoc | `hedgedoc/hedgedoc` | 多人 Markdown 协作 | PostgreSQL |
| 39 | Etherpad | `ether/etherpad` | 低门槛实时文本协作 | 需评估内嵌 DB 可靠性 |
| 40 | Apache Answer | `apache/answer` | 问答社区与内部知识库 | DB 持久化形态审计 |

## Batch 5 · 代码托管、CI 与开发平台

| # | 应用 | 上游 | 核心价值 | 平台前置 |
|---:|---|---|---|---|
| 41 | GitBucket | `gitbucket/gitbucket` | 轻量 GitHub 风格代码托管 | 单容器可行，需补 SSH 端口模型 |
| 42 | Gogs | `gogs/gogs` | 低资源 Git 服务 | 单容器，需补 SSH 端口模型 |
| 43 | OneDev | `theonedev/onedev` | Git + CI + 项目管理 | 单容器资源评估 |
| 44 | Woodpecker CI | `woodpecker-ci/woodpecker` | 轻量容器 CI | server/agent 多服务 |
| 45 | Jenkins | `jenkinsci/jenkins` | 通用自动化与 CI/CD | 单容器，agent 能力后续补 |
| 46 | GitLab CE | `gitlabhq/gitlabhq` | 一体化 DevSecOps | 高资源多进程形态 |
| 47 | SonarQube | `SonarSource/sonarqube` | 代码质量与安全扫描 | PostgreSQL，高内存 |
| 48 | NetBox | `netbox-community/netbox` | 网络基础设施事实源 | PostgreSQL + Redis |
| 49 | Dockge | `louislam/dockge` | Compose 项目管理 | 需 Docker socket，必须先有特权隔离方案 |
| 50 | Coder | `coder/coder` | 团队云开发环境 | DB + 工作区供应端 |

## Batch 6 · 自动化与内部工具平台

| # | 应用 | 上游 | 核心价值 | 平台前置 |
|---:|---|---|---|---|
| 51 | ntfy | `binwiederhier/ntfy` | 移动端与桌面端自托管推送 | 需平台支持镜像启动命令 |
| 52 | Activepieces | `activepieces/activepieces` | 低代码业务自动化 | DB/Redis 生产形态 |
| 53 | Windmill | `windmill-labs/windmill` | 脚本、工作流与内部应用 | PostgreSQL + workers |
| 54 | Automatisch | `automatisch/automatisch` | 开源 SaaS 集成自动化 | DB + worker |
| 55 | Huginn | `huginn/huginn` | 个人信息代理与事件流 | DB + worker |
| 56 | Kestra | `kestra-io/kestra` | 数据与业务工作流编排 | 多服务/数据库 |
| 57 | Rundeck | `rundeck/rundeck` | 运维 runbook 自动化 | 单容器基础形态可评估 |
| 58 | Appsmith | `appsmithorg/appsmith` | 内部工具与管理后台 | 多服务打包形态审计 |
| 59 | ToolJet | `ToolJet/ToolJet` | 低代码内部应用 | PostgreSQL |
| 60 | Budibase | `Budibase/budibase` | 数据驱动低代码应用 | 多容器 |

## Batch 7 · 可观测性与基础设施健康

| # | 应用 | 上游 | 核心价值 | 平台前置 |
|---:|---|---|---|---|
| 61 | Prometheus | `prometheus/prometheus` | 时序监控与告警基座 | 单容器 |
| 62 | Loki | `grafana/loki` | 日志聚合与查询 | 单节点形态 |
| 63 | VictoriaMetrics | `VictoriaMetrics/VictoriaMetrics` | 高性能时序数据库 | 单节点形态 |
| 64 | Beszel | `henrygd/beszel` | 轻量服务器监控 | hub 可单容器，agent 后续补 |
| 65 | Gatus | `TwiN/gatus` | 宣告式健康检查与状态页 | 单容器 |
| 66 | Healthchecks | `healthchecks/healthchecks` | Cron/定时任务失联监控 | DB 生产形态 |
| 67 | Glances | `nicolargo/glances` | 主机与容器资源观测 | 需限权主机指标访问 |
| 68 | Scrutiny | `AnalogJ/scrutiny` | 硬盘 SMART 健康分析 | 需设备映射，暂不上架 |
| 69 | NetAlertX | `jokob-sk/NetAlertX` | 局域网设备发现与告警 | host network/特权评估 |
| 70 | Zabbix | `zabbix/zabbix` | 企业级网络与主机监控 | DB + server + frontend |

## Batch 8 · 身份、访问控制与密钥管理

| # | 应用 | 上游 | 核心价值 | 平台前置 |
|---:|---|---|---|---|
| 71 | Authelia | `authelia/authelia` | 反向代理前置 SSO/MFA | 需多服务联动验证 |
| 72 | Authentik | `goauthentik/authentik` | 身份提供商与访问治理 | PostgreSQL + worker |
| 73 | Keycloak | `keycloak/keycloak` | 标准化 IAM/SSO | DB 生产形态 |
| 74 | ZITADEL | `zitadel/zitadel` | 多租户身份平台 | PostgreSQL |
| 75 | Pocket ID | `pocket-id/pocket-id` | Passkey 优先的轻量 OIDC | 单容器 |
| 76 | LLDAP | `lldap/lldap` | 轻量 LDAP 目录 | 需 LDAP + HTTP 多端口模型 |
| 77 | Kanidm | `kanidm/kanidm` | 现代身份和账号管理 | 多端口/TLS 强约束 |
| 78 | CrowdSec | `crowdsecurity/crowdsec` | 协作式入侵检测 | 需日志和主机集成 |
| 79 | Infisical | `Infisical/infisical` | 密钥与配置管理 | DB/Redis + 根密钥注入 |
| 80 | Passbolt | `passbolt/passbolt_api` | 团队凭据管理 | DB + 邮件 + GPG 初始化 |

## Batch 9 · 图片、视频与联邦社交

| # | 应用 | 上游 | 核心价值 | 平台前置 |
|---:|---|---|---|---|
| 81 | Immich | `immich-app/immich` | 高质量私有照片与视频备份 | PostgreSQL + Redis + ML |
| 82 | PhotoPrism | `photoprism/photoprism` | AI 辅助私有照片库 | DB，ML 资源评估 |
| 83 | LibrePhotos | `LibrePhotos/librephotos` | 开源照片管理与识别 | 多容器 + ML |
| 84 | Piwigo | `Piwigo/Piwigo` | 成熟图片库与分享 | DB |
| 85 | Lychee | `LycheeOrg/Lychee` | 轻量相册管理 | DB/SQLite 可靠性评估 |
| 86 | PeerTube | `Chocobozzz/PeerTube` | 联邦视频托管 | PostgreSQL + Redis + 转码 |
| 87 | Owncast | `owncast/owncast` | 单机直播与聊天 | 单容器，需 RTMP + HTTP 多端口 |
| 88 | MediaCMS | `MediaCMS-io/mediacms` | 视频管理与发布 | DB + workers + 转码 |
| 89 | Pixelfed | `pixelfed/pixelfed` | 联邦图片社交 | DB + Redis + worker |
| 90 | Mastodon | `mastodon/mastodon` | 联邦微博社交 | DB + Redis + workers |

## Batch 10 · 阅读、网页归档与下载管理

| # | 应用 | 上游 | 核心价值 | 平台前置 |
|---:|---|---|---|---|
| 91 | Kavita | `Kareadita/Kavita` | 电子书与漫画阅读服务器 | 单容器 |
| 92 | ArchiveBox | `ArchiveBox/ArchiveBox` | 网页及数字资料归档 | 浏览器依赖与存储评估 |
| 93 | Karakeep | `karakeep-app/karakeep` | 书签、稍后读和 AI 标签 | DB + browser + 可选 AI |
| 94 | Glance | `glanceapp/glance` | RSS、天气和信息聚合首页 | 单容器 |
| 95 | Wallabag | `wallabag/wallabag` | 稍后读与网页保存 | DB，可评估 SQLite 形态 |
| 96 | Calibre-Web-Automated | `crocodilestick/Calibre-Web-Automated` | 电子书入库、转换与阅读 | 单容器，媒体路径整合 |
| 97 | qBittorrent | `qbittorrent/qBittorrent` | 成熟 BitTorrent Web 管理 | 上游与镜像不同源，需供应链审计 |
| 98 | Deluge | `deluge-torrent/deluge` | 轻量多端 BitTorrent 客户端 | 上游与镜像不同源，需供应链审计 |
| 99 | MeTube | `alexta69/metube` | yt-dlp Web 下载界面 | 单容器，需合规使用提示 |
| 100 | Tube Archivist | `tubearchivist/tubearchivist` | 视频频道归档与检索 | Redis + Elasticsearch |

## Batch 11 · 项目管理、协作与业务运营

| # | 应用 | 上游 | 核心价值 | 平台前置 |
|---:|---|---|---|---|
| 101 | Plane | `makeplane/plane` | 现代项目与产品管理 | 多容器 |
| 102 | Leantime | `Leantime/leantime` | 面向中小团队的项目管理 | DB |
| 103 | OpenProject | `opf/openproject` | 企业项目组合与协作 | DB，高资源 |
| 104 | Taiga | `taigaio/taiga-back` | 敏捷项目管理 | frontend + backend + DB |
| 105 | Mattermost | `mattermost/mattermost` | 团队消息与工作流 | DB |
| 106 | Rocket.Chat | `RocketChat/Rocket.Chat` | 团队即时通信与客服 | MongoDB |
| 107 | Zulip | `zulip/zulip` | 主题流式团队沟通 | 多服务 |
| 108 | Cal.com | `calcom/cal.diy` | 预约排期与团队日程 | DB + worker |
| 109 | Kimai | `kimai/kimai` | 团队工时跟踪与报表 | DB |
| 110 | InvoiceShelf | `InvoiceShelf/InvoiceShelf` | 发票、客户与收款管理 | DB + 定时任务 |

## Batch 12 · 数据平台、无代码与生成式 AI

| # | 应用 | 上游 | 核心价值 | 平台前置 |
|---:|---|---|---|---|
| 111 | Directus | `directus/directus` | 数据 API 与内容管理 | DB |
| 112 | Strapi | `strapi/strapi` | Headless CMS 与 API | 单容器 SQLite 或外部 DB 取舍 |
| 113 | Baserow | `baserow/baserow` | 无代码数据库与表格 | 多容器 |
| 114 | Grist | `gristlabs/grist-core` | 关系型电子表格 | 单容器形态可评估 |
| 115 | Teable | `teableio/teable` | 高性能无代码数据库 | DB + 多服务 |
| 116 | Ollama | `ollama/ollama` | 本地大模型运行时 | CPU 可启动，有价值验证需 GPU 平台 |
| 117 | Open WebUI | `open-webui/open-webui` | 多模型对话与知识库界面 | 需外部模型或 Ollama |
| 118 | AnythingLLM | `Mintplex-Labs/anything-llm` | 私有文档 RAG 与 agent | 模型/向量库依赖评估 |
| 119 | Langflow | `langflow-ai/langflow` | 可视化 LLM 应用编排 | DB + worker，可选外部模型 |
| 120 | Langfuse | `langfuse/langfuse` | LLM 追踪、评估与观测 | PostgreSQL/ClickHouse/Redis |

## 每批执行规则

- 默认每批 10 个；批内可因镜像、许可或运行时事实失败而替换，但总数保持 120。
- 每个新应用独立运行 `validate_app_schema.py --app` 和
  `run_application_verify.py --no-publish --skip-ami-drift`。
- 本地验证失败的应用不推送；通过后再推送到 `main`，由 GitHub Actions
  在 R2 后端跑正式发布。
- 每批发布后核对 R2 `verified/index.json`、官网应用数、中英文详情页和
  CloudFormation 深链的镜像 digest、AMI、数据卷参数。
