# CoreNovaLaunchVerify

[English](README.md) | 简体中文

**CoreNova Launch** 的应用验证与发布组件：注册自托管应用，验证以 digest 钉扎的容器镜像，并发布网站消费的已验证数据。

```text
应用注册 → 版本与镜像解析 → 容器验证 → 发布门禁 → 网站数据
                              ↑
                           平台契约
```

- **Application Verification（应用验证）**：执行 Docker Compose、就绪检查、版本断言、预写测试与 Playwright 截图，不创建 AWS 资源。
- **Publish Gate（发布门禁）**：通过两阶段提交发布 Manifest、报告、截图与索引；网站读取已发布数据，不自行推断验证结果。
- **Golden Verification（平台验证）**：独立验证 AWS 平台并生成 Platform Contract，会创建计费资源。

本仓库不负责构建 AMI 或实现网站。生产发布链路仍采用单容器 v1；实验性 stack v2 不替换已有部署。

[快速开始](#快速开始) · [配置说明](#配置与产物) · [接入应用](#接入新应用) · [部署安全](#部署安全) · [维护操作](#维护操作) · [参考文档](#参考文档)

## 快速开始

以下命令均在 `CoreNovaLaunchVerify` 目录下执行。

### 1. 安装依赖

需要 Python **3.10+**，推荐 3.12。运行应用验证还需要可用的 Docker daemon、Compose v2、GitHub 与镜像仓库的网络访问，以及 Playwright Chromium。示例通过已登录的 GitHub CLI（`gh auth login`）提供 `GITHUB_TOKEN`。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

### 2. 执行离线检查

依赖安装完成后，以下检查不需要 Docker、AWS 凭据或网络访问：

```bash
.venv/bin/python scripts/verify/validate_app_schema.py --all
.venv/bin/python scripts/verify/golden_verify.py --check
.venv/bin/python -m pytest tests -q
```

### 3. 验证应用，不发布

将 `ghost` 替换为 [apps/](apps/) 中的应用名。

```bash
GITHUB_TOKEN=$(gh auth token) .venv/bin/python \
  scripts/verify/run_application_verify.py --app ghost --no-publish --skip-ami-drift
```

- `--no-publish` 在发布前停止，不更新 `current.json` 或已发布索引。
- `--skip-ami-drift` 跳过 AWS 公开 AMI 漂移查询，便于没有 AWS 凭据时进行本地测试。它**不会**跳过其他平台契约检查，也不能产生新的平台验证证据。
- 当前后端必须已有有效且匹配的平台契约。新检出的仓库或修改过的平台资产可能需要维护者提供或重新生成契约；离线检查本身不会生成契约。

正常验证时去掉 `--skip-ami-drift`，并提供漂移查询所需的 AWS 只读凭据。发布属于独立、需明确执行的操作，见[维护操作](#维护操作)。

## 配置与产物

[config/verify.yaml](config/verify.yaml) 控制验证与发布，[config/platform.yaml](config/platform.yaml) 定义平台身份。环境变量优先于配置文件。

| 配置项 | 用途 |
| --- | --- |
| `GITHUB_TOKEN` | 上游 release 与仓库查询的身份认证。 |
| `VERIFIED_BACKEND` | 选择 `dir` 或 `r2`；仓库默认值为 `dir`，两种后端之间不会自动回退。 |
| `VERIFIED_OUTPUT_DIR` | 本地产物目录，默认为 `data/`。 |
| `CORENOVA_REGISTRY_MIRROR` | 可选镜像仓库前缀，只改变拉取路径，不改变 Manifest 记录的镜像身份。 |
| `CORENOVA_PROBE_HOST` | Docker 与验证器不在同机时，填写验证器可访问的主机地址；环境支持时可使用 `host.docker.internal`。 |
| `R2_*` | 使用 R2 后端时的端点、桶、公开 URL 与访问凭据。 |
| `SITE_REPO` / `REPO_A_PAT` | 网站重建通知的目标仓库与访问凭据。 |

不要将凭据写入应用注册文件或提交到 Git。配置覆盖规则见 [corenova/config.py](corenova/config.py)，CI 环境变量接线见[工作流定义](.github/workflows/)。

本地产物位于 `data/`，包括 `runs/{verification_id}/state.json`、报告、截图，以及发布到 `dir` 后端时的 `verified/` 记录。选择 R2 后，本地产物不构成另一套网站数据源。`data/` 不进入 Git。

## 接入新应用

先阅读 [App Schema](contracts/app-schema.md) 与[应用规格](contracts/app-profiles.md)。

1. 使用 `scripts/dev/new_app.py` 生成骨架，必填参数见 `--help`。需要人工核实的事实会保留为 TODO。
2. 完成 `apps/{name}.yaml`：健康端点、版本断言、持久化数据路径与中英文文案。镜像模板必须解析为精确版本 tag，禁止使用 `latest` 等移动 tag。
3. 完成 `apps/{name}/docker-compose.yml`，通过 `CORENOVA_APP_IMAGE`、`CORENOVA_HOST_PORT`、`CORENOVA_CONTAINER_PORT`、`CORENOVA_APP_URL`、`CORENOVA_DATA_DIR` 注入镜像、端口、公开 URL 与数据目录。
4. 在 `apps/{name}/tests/` 中补充 pytest 与 Playwright 覆盖，只断言实际观测到的行为，并说明尚未验证的能力。场景 slug 必须为 ASCII，且与 `website.screenshots_order` 一致。
5. 先通过 schema 校验，再用 `--no-publish` 运行应用验证，门禁通过后才发布。网站图标 `{name}.svg` 另行放入网站仓库的 `public/icons/`。

Compose 行为、部署参数与能力说明必须一致。模板没有提供的宿主机挂载、公开端口或认证机制，不得描述为已经具备。

## 部署安全

以下设置适用于当前[单容器 CloudFormation 模板](templates/cloudformation/fixed/app.yaml)，不会自动应用到已有实例。

| 能力 | 默认值与启用方式 |
| --- | --- |
| Web 访问 | `AllowedWebCidr=127.0.0.1/32`：HTTP、HTTPS 与健康路径默认仅允许本机访问，首次初始化使用 SSM 转发。 |
| 宿主机 Docker 控制 | `DockerSocketAccess=false`。启用相当于授予宿主机 root 控制权，需先限制访问并配置认证。 |
| 额外业务端口 | `ExtraTcpPort=0` 与 `ExtraUdpPort=0` 表示关闭；启用后，Docker 映射与安全组规则使用指定端口及 `ExtraPortIngressCidr`。 |
| 上传与 WebSocket | HTTP/HTTPS 共用配置；`MaxUploadSizeMb=100` MiB，可调至 10240 MiB，应用自身限制仍有效。支持流式传输与 WebSocket 升级，代理读写超时为 3600 秒。 |
| 持久化数据 | 镜像使用非 root 用户时，空目录按镜像用户设置属主，已有非空目录属主不符则启动失败。不递归改属主或重设目录权限。 |

远程直连时，仅放行管理员或 VPN 出口 CIDR，并同步收窄 `HttpIngressCidr`。CIDR 白名单不是应用认证；确需公开服务时，先配置认证与 HTTPS，再显式开放。

<details>
<summary>SSM 访问与应用注意事项</summary>

准备 AWS CLI、Session Manager 插件及相应 IAM 权限，替换实例 ID 后建立转发：

```bash
aws ssm start-session --target i-xxxxxxxxxxxxxxxxx \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["80"],"localPortNumber":["8080"]}'
```

打开 `http://localhost:8080`。如果应用仅通过此转发访问，将 `LaunchUrl` 设为同一地址。

- **code-server / 数据权限**：数字 UID 不依赖镜像内存在 shell 或 `id`；允许新 ext4 卷自带的 `lost+found`。环境文件使用 `corenova-app` 组。
- **地址注入**：`AppUrlEnvironmentName` 优先。`ExtraEnvironment` 仅展开 `${CORENOVA_APP_URL}`；必需的 URL 无法解析或出现未知占位符时会失败，不将配置作为 shell 代码执行。
- **Portainer**：默认仅验证服务启动，不证明可以管理宿主机 Docker。本机管理需要显式启用 socket 访问；远程环境需要单独接入。
- **Netdata**：仅覆盖容器 Agent，不挂宿主机 `/proc`、`/sys`、cgroup 或 Docker socket，不授予额外能力。完整主机监控需要单独评估；接入 Netdata Cloud 不会保护本地 Agent 入口。
- **Syncthing**：直连同步需设置 `ExtraTcpPort=22000`、`ExtraUdpPort=22000`，并在 `ExtraPortIngressCidr` 填写对端网段。GUI 仍仅绑定本机，不公开 UDP 21027 局域网发现。规则添加到传入的 `SecurityGroupId`；使用已有网络时，应采用实例独占安全组。
- **Vikunja**：新部署通过 `VIKUNJA_FILES_BASEPATH=/db` 将 SQLite 与附件放在 `/db`。旧容器可能仍在 `/app/vikunja/files` 保存附件，更换容器前必须导出并验证恢复。

</details>

**已有部署必须安排维护。** 修改模板或 CloudFormation 参数不会自动重放 cfn-init。应先备份并验证恢复，再显式应用配置。更换数据库引擎需要单独设计备份、迁移、恢复、切换与回滚流程，不能只换模板重启。实验性 v2 不经过 v1 发布门禁。

## 维护操作

<details>
<summary>仅解析版本与镜像，不运行验证</summary>

以下命令访问 GitHub 与镜像仓库，不验证或发布应用。

```bash
GITHUB_TOKEN=$(gh auth token) .venv/bin/python scripts/verify/resolve_version.py --app ghost
GITHUB_TOKEN=$(gh auth token) .venv/bin/python scripts/verify/resolve_image.py --app ghost
```

</details>

<details>
<summary>修改模板与运行本地回归</summary>

修改 `templates/cloudformation/fixed/init/*.sh` 后，同步 `app.yaml` 中的内嵌脚本并重新生成 `canary.yaml`：

```bash
.venv/bin/python scripts/verify/golden_verify.py --sync-init
.venv/bin/python scripts/verify/golden_verify.py --check
.venv/bin/python -m ruff check corenova scripts tests apps
.venv/bin/python -m pytest tests -q
```

可选 Docker 集成测试使用本机预加载的镜像，并清理临时容器和数据卷。镜像缺失时测试失败，不会自动拉取。

```bash
CORENOVA_TEST_DOCKER=1 .venv/bin/python -m pytest tests/test_user_template.py -q
```

本地测试不能替代 AWS Golden Verification。

</details>

<details>
<summary>运行 AWS Golden Verification：会创建计费资源</summary>

先预览计划，不发起 AWS 调用：

```bash
.venv/bin/python scripts/verify/golden_verify.py --dry-run
```

仅在具备 AWS 凭据且获准创建资源时，执行真实平台验证：

```bash
.venv/bin/python scripts/verify/golden_verify.py
```

流程会创建 canary、执行平台探针、写入平台契约并尝试清理资源；结束后需确认清理完成。公开 AMI 模式通过 cfn-init 安装 Docker/Nginx，平台复验间隔不得超过 30 天。

</details>

<details>
<summary>发布已验证数据：会写入当前后端</summary>

执行前确认后端与凭据。不加 `--no-publish` 时，验证成功后将继续发布；配置完整时还会通知网站仓库重建：

```bash
GITHUB_TOKEN=$(gh auth token) .venv/bin/python \
  scripts/verify/run_application_verify.py --app ghost
```

发布门禁要求九项检查全部通过后才提交 `current.json`。R2 网站数据与公开 S3 一键部署模板属于不同发布渠道；模板分发通过 `scripts/verify/build_user_template.py` 与 `publish-template` 工作流完成。

</details>

### CI 工作流

| 工作流 | 职责 |
| --- | --- |
| `pr-checks` | Lint、应用 schema 校验与仓库测试。 |
| `monitor-versions` | 每六小时检查上游版本。 |
| `application-verify` | 按应用控制并发，执行验证与发布。 |
| `golden-verify` | 收到 dispatch 或在每月 1 日、16 日执行 AWS 平台验证。 |
| `publish-template` | 发布一键部署模板，并检查匿名访问是否可读。 |
| `publish-site` | 通知网站仓库重建。 |
| `reverify-failed` | 重试分类为 `TRANSIENT` 的失败。 |

## 参考文档

| 路径 | 内容 |
| --- | --- |
| [apps/](apps/) | 应用注册、Compose 定义与应用测试。 |
| [corenova/](corenova/) | 验证、Manifest 生成与发布逻辑。 |
| [scripts/](scripts/) | 验证、监控与开发入口。 |
| [templates/](templates/) | CloudFormation 模板与初始化资产。 |
| [tests/](tests/) | 仓库回归测试。 |

契约：[App Schema](contracts/app-schema.md) · [Platform Contract](contracts/platform-contract.md) · [Verification Manifest](contracts/verification-manifest.md) · [Deployment Contract](contracts/deployment-contract.md) · [Workflow State Machine](contracts/workflow-state-machine.md)。

应用规划：[应用路线图](docs/app-roadmap-120.md)。

在 umbrella 工作区中，架构与跨仓配置说明位于 `../docs/`，CI 凭据要求见 `../docs/repo-structure.md` §6。其中 `docs/contracts/` 是权威契约，本仓库 `contracts/` 为镜像副本。修改命令或操作边界时，请同步更新中英文 README。
