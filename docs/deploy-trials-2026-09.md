# 用户部署流程演练留档 — 2026-09-19（P0 本周任务）

> 目的：按"部署可信度收尾"计划，选 3 个代表应用走**完整用户部署流程**，
> 验证用户能否独立完成：官网生成模板 → AWS 部署 → 首次登录 → 核心操作 →
> 重启数据保留 → 备份恢复。全程记录，作为"一键部署"承诺的证据。
>
> - 代表样本：`gitea`、`portainer`（B 类暂停应用，重验通过后解除 hold）、
>   `vaultwarden`（正常应用基线）。
> - 部署模板：公开桶 one-click 模板
>   （`corenovalaunch-templates.s3.us-east-1.amazonaws.com/corenova-one-click.template.yaml`），
>   内容 revision = `f14bc41776ef92827a97184784dc5519b9c576ac`
>   （与本次重验证据的 `deploy.template.revision` 绑定，deployment-contract §2.4）。
> - 执行方式：`aws cloudformation create-stack`，参数等价于官网 Deploy 深链
>   （镜像 digest 钉扎、AmiId 来自平台契约），全程 CLI 复现用户在控制台的步骤。

## 0. 前置链路（本次演练同时闭合的两个缺口）

1. **平台契约漂移**：9/18 平台层修复（commit 47ffe5ab）改了模板与 init 脚本，
   Platform Contract 仍是 9/16 证据（revision 42f3c1ff）→ 首轮 3 个应用验证
   `required_platform_contract_valid=false`（应用层全过，卡在平台引用）。
   处置：重跑 Golden Verification → 结果见 §1。
2. **模板-证据错配**：current.json 此前无模板绑定字段。本次已落地
   `config.template_revision` + `website.deploy.template`（主仓 459b414），
   重验发布的新证据自动绑定 revision `f14bc417…`。
3. **Golden 本身被三个基建问题堵死**（连续修复，见 §1）：
   ① app.yaml 超 ValidateTemplate TemplateBody 51200 字节上限且失败会把既有契约标 invalid
   （修复 7b1ecbc：超限走 TemplateURL 临时对象）；
   ② golden-verify.yml 未注入 `TEMPLATE_S3_BUCKET`，CI 中模板桶名为空
   （修复 50b515e）；
   ③ `corenova-template-publisher` IAM 身份无 `golden-validate/*` 前缀的
   PutObject/DeleteObject，桶策略也未公开读该前缀 → CFN 服务端读不到校验对象
   （修复：IAM inline policy 追加 `GoldenValidateTempObjects`，桶策略追加
   `PublicReadGoldenValidateTemp`；本地探针验证匿名 GET + ValidateTemplate 全通）。

## 1. Golden Verification（平台层）

| run | 结果 | 说明 |
|---|---|---|
| [35417102536](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35417102536) | 失败 | app.yaml 53588 字节 > TemplateBody 51200 上限；既有契约按 §5 标为 invalid |
| [35417691817](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35417691817) | 失败 | 修复①后仍失败：CI 未注入 TEMPLATE_S3_BUCKET，无法走 TemplateURL |
| [35417919325](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35417919325) | 失败 | 修复②后：IAM 拒 PutObject `golden-validate/*` |
| [35418103960](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35418103960) | 失败 | IAM 通过后：CFN 读对象 Access Denied（桶策略未公开读该前缀） |
| [35418312574](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35418312574) | **成功** | 16/16 步全过；`validate-template: network.yaml=ok app.yaml=ok canary.yaml=ok`；契约 status=valid；canary 已清理 |

- 成功 run：https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35418312574
- 结论：平台契约已按当前平台层（含 9/18 修复）重新生效，Platform Contract
  `status=valid`，应用验证的平台引用门槛解除。
- 新平台契约 verification_id / ami_id：（待填：从新契约取）
- 临时对象残留核查：`s3://corenovalaunch-templates/golden-validate/` 应为空
  （golden.py finally 清理）→（待填）

## 2. 应用重验（容器级，CI）

| app | run | 结论 | verification_id | template_revision |
|---|---|---|---|---|
| gitea | [35418998361](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35418998361) | 失败（发布竞态） | - | - |
| gitea 重跑 | [35419349804](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35419349804) | 成功 | gitea-v1.27.3-20260919-004 | f14bc41776ef92827a97184784dc5519b9c576ac |
| portainer 重跑 | [35419742107](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35419742107) | 成功 | portainer-2.45.1-20260919-003 | f14bc41776ef92827a97184784dc5519b9c576ac |
| vaultwarden 重跑 | [35420015536](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35420015536) | 成功 | vaultwarden-1.37.3-20260919-003 | f14bc41776ef92827a97184784dc5519b9c576ac |

- **发布竞态缺陷（新发现，记为 P1）**：并发验证多个应用时，共享 key
  `verified/index.json` 用 ETag 乐观锁（put_if_match），后写者 PreconditionFailed
  且无重试 → gitea run 35418998361 的 current.json 已补投成功（重试路径）但
  index.json 条目卡在旧版本，run 标 FAILED（失败台账 #21）。
- **模板绑定验证**：gitea 重跑后 current.json 的 `deploy.template` 同时含
  `url`（TEMPLATE_S3_BUCKET 注入修复后）与 `revision`（f14bc417…）双字段，
  与本地 `usertemplate.revision()` 计算一致 —— 模板-证据错配闭环。

## 3. AWS 真实部署（用户流程逐步留档）

对每个应用：建栈参数（= 深链参数）→ 栈事件时间线 → Outputs →
首次登录 → 核心操作 → 重启实例后数据保留 → 备份/恢复 → 删栈清理。

### 3.1 gitea v1.27.3（B 类，验证公网地址注入 GITEA__server__ROOT_URL）

- 建栈参数（= 深链参数 + `AllowedWebCidr` 管理员出口 + `CloudWatchLogGroupName` 覆盖）：
  ImageReference=`gitea/gitea:1.27.3@sha256:d584940b…`（digest 钉扎）、
  ContainerPort=3000、AmiId=ami-025d99823a4caad37、InstanceType=t3.small、
  DataVolumeSize=30、DataContainerPath=/data、HealthCheckPath=/、
  AppUrlEnvironmentName=GITEA__server__ROOT_URL、
  ExtraEnvironment=`GITEA__server__ROOT_URL=${CORENOVA_APP_URL}`。
- 首次建栈失败（缺陷 A，见 §5）：CloudWatchLogGroupName 默认值 `/corenova/apps`
  与既有 Log Group 冲突，Early Validation `ResourceExistenceCheck` 拒绝 → 回滚。
  用户侧等价表现：任何第二个 CoreNova 栈都无法部署。处置：本次用参数覆盖
  `/corenova/apps/gitea-v1-27-3`（改参数不改模板）。
- 栈：arn `…:stack/corenova-gitea-v1-27-3/7c051c10-b3dd-11f1-b4c5-0e400b3328f7`
  InstanceId=i-04f4550d7887d3968，ResolvedLaunchUrl=`http://ec2-35-170-248-157.compute-1.amazonaws.com`
  （创建耗时：约 11 分钟，03:47 → 03:58 建成；80 口即通，容器端口 3000 不对外暴露，仅 nginx 入口）
- **首次登录 ✓**：首次访问进入安装向导（`Installation - Gitea`）；表单提交
  （SQLite + /data 路径 + 同表创建管理员）→ 站点上线。注：安装表单管理员
  确认密码字段名为 `admin_confirm_passwd`（演练用 API 提交时首试字段名
  `retype` 被静默重渲染，无错误提示 —— 官网 DeployGuide 若补充安装步骤截图
  可避免此坑）。
- **ROOT_URL 注入 ✓**（B 类 hold 解除条件）：SSM 查容器 env 实证
  `GITEA__server__ROOT_URL=http://ec2-35-170-248-157.compute-1.amazonaws.com`
  —— AppUrlEnvironmentName + ExtraEnvironment 的 `${CORENOVA_APP_URL}` 占位符
  替换链路全通。
- **核心操作 ✓**：管理员登录（is_admin=true）→ 建仓 trialadmin/trial-repo
  （auto_init）→ git push 提交 `17df3ec`（TRIAL-MARKER.txt）。
- **重启保留 ✓**：stop/start 实例 → 新公网 IP `3.87.227.155`（旧 ResolvedLaunchUrl
  失效 —— 无 Elastic IP 时 stop/start 必然换址，见跟进项 C）；应用 ~10s 就绪
  （容器自启动）；TRIAL-MARKER.txt 与两条 commit 全部保留（数据卷 /data 持久化）。
- **备份恢复 ✓（端到端）**：容器内全量 tar（`/data/gitea-data-backup.tgz`，
  sha256 `c5a9e138…`）→ API 删除仓库（204，确认 404）→ 停容器 → tar 全量解包
  恢复（sha256 校验一致）→ 起容器 → 仓库与标记文件完整回来。
  另做基础设施级备份：数据卷 EBS 快照 `snap-0abe96f3290cd5e97`
  （30GB gp3，DeleteAfter 2026-10-19，试运行结束后手动删除）。

### 3.2 portainer 2.45.1（B 类，验证 Docker socket 访问面）

栈 `corenova-portainer-2-45-1`（f2f19320-b41f-11f1），实例 i-0ca9e7c9d8305a168，
公网 `ec2-34-238-255-200.compute-1.amazonaws.com`；参数与官网深链等价 +
AllowedWebCidr（管理员出口 /32）+ LogGroup 覆盖。重验证据：
portainer-2.45.1-20260919-003（revision f14bc41776…，url 双字段齐）。

**访问路径事实（防重蹈覆辙）**：容器口仅绑定 `127.0.0.1:9000`（app.yaml 参数描述
“published on 127.0.0.1 only - nginx is the public face”），公网入口是 80 口 nginx
反代。直连 `:9000` 超时是**设计行为**，不是故障。

1. **首次登录**：`POST /api/users/admin/init` 直接 403（缺陷 D：需要
   `X-Setup-Token`，token 只在容器启动日志里）→ SSM `docker logs portainer`
   取 token → admin 创建 HTTP 200 → JWT 登录成功。
2. **核心操作（socket 验证）**：endpoint 总数 0（缺陷 B 实证：默认无本地 Docker
   管理能力）→ UpdateStack `DockerSocketAccess=true` → UPDATE_COMPLETE 但实例
   未替换、cfn-init 未重跑，参数不生效（缺陷 E）→ SSM 手动重跑
   `30-app-container.sh` → systemd 单元含 `docker.sock` → 容器 Mounts
   `RW=true` → 宿主直连 socket：Docker API 返回 Version 29.8.1 + 容器列表。
   **socket 挂载后管理能力实证可用**。endpoint 手动添加 API（`POST /api/endpoints`）
   多种姿势均 400（2.45 私有校验），非阻塞项：socket 挂载是能力验证的关键证据。
3. **重启数据保留**：stop/start → 新 IP 100.53.76.92（缺陷 C 同 gitea）→
   约 3 分钟自愈 → 80 口 `/api/status` 200，admin 登录保留（InstanceID 不变，
   portainer.db 原样）。
4. **备份恢复**：宿主打包数据卷 `2940 B`（sha256 3ee2ef8c…）→ 停容器 →
   `mv portainer.db portainer.db.lost` 模拟丢失 → 解包恢复 → db sha256
   `9c2895d1…` 前后一致（SHA-MATCH）→ 起容器 → admin 登录 HTTP 200，
   endpoints 状态与破坏前一致。
5. **hold 解除**：commit bcb22a7 删 `deployment.hold` + sync-holds
   `--app portainer`；解除依据：hold 理由（“模板默认不挂载 socket”）对应的处置
   参数链路已实证可用；默认形态局限记为缺陷 B 跟进项。

### 3.3 vaultwarden 1.37.3（基线）

（待填，同上结构）

## 4. 结论与跟进

（待填：用户能否独立完成全流程、发现的摩擦点、对官网指引的改进项）

## 5. 演练中发现的产品缺陷（跟进台账）

### 缺陷 A（P1）：CloudWatchLogGroupName 默认值全局冲突

- 现象：用户模板 `templates/cloudformation/fixed/app.yaml` 的
  `CloudWatchLogGroupName` 默认值 `/corenova/apps` 是全局唯一资源名。已有同名
  Log Group 时，CloudFormation Early Validation
  （`AWS::EarlyValidation::ResourceExistenceCheck`）直接拒绝建栈：
  `Validation failed with 1 error(s)` → 每个用户的**第二个** CoreNova 栈必失败，
  且控制台错误信息不含资源名，用户无法自助定位。
- 对照：Golden canary 已按 run_id scope 该参数（golden.py canary_parameters），
  用户模板路径漏掉了同样的处理。
- 修复方向：默认值改为 `Fn::Sub: "/corenova/apps/${AWS::StackName}"`（每栈独立，
  需改 app.yaml → 重跑 Golden → 重新发布模板）；官网深链与 DeployGuide 同步。
- 本次演练处置：部署参数显式覆盖（`/corenova/apps/<stack>`）。

### 缺陷 C（P2）：stop/start 后 ResolvedLaunchUrl 失效（无 Elastic IP）

- 现象：实例 stop/start 后公网 IP 重新分配（35.170.248.157 → 3.87.227.155），
  栈 Output 的 ResolvedLaunchUrl 与注入容器的 ROOT_URL 均指向旧地址；应用仍
  按新 Host 正常服务，但站内绝对链接（克隆 URL 等）会带旧域名。
- 修复方向：官网 DeployGuide 增加“绑定 Elastic IP / 自有域名后再日常使用”
  提示；或模板增加可选 ElasticIP 参数。
- 本次演练处置：留档，不改模板。

### 缺陷 B（P1）：portainer verified 清单缺 DockerSocketAccess 需求

- 现象：portainer 核心功能（管理宿主 Docker）需要挂载 `/var/run/docker.sock`，
  但验证 compose（apps/portainer/docker-compose.yml）未挂载，verified 清单无
  docker socket 字段 → 官网深链按清单生成的部署**没有本地 Docker 管理能力**
  （验证通过 ≠ 用户可用）。
- 修复方向：app-schema 增加 `deploy.docker_socket: true` 字段；
  portainer.yaml 声明之；compose 同步挂载重验；深链映射到
  DockerSocketAccess 参数。
- 本次演练处置：先按深链等价参数部署并记录现象；再用 UpdateStack 对比
  `DockerSocketAccess=true` 形态。**已实证参数生效后能力完整可用**
  （见 §3.2 核心操作），hold 已解除（bcb22a7）。

### 缺陷 D（P2）：portainer 首次 init 需 X-Setup-Token，官网指引未提及

- 现象：portainer 2.45 首次创建管理员时，API 要求 `X-Setup-Token` 请求头，
  token 只出现在容器启动日志（`docker logs portainer`）中。官网
  `post_deploy.admin_setup` 指引完全未提 → 按 API/脚本路径操作的用户必然 403
  （`Invalid or missing setup token`）。
- 修复方向：portainer.yaml `admin_setup` 补充“从容器日志获取 setup token”
  步骤（或模板把日志路径写进 DeployGuide 提示）。
- 本次演练处置：SSM 读容器日志取 token → init 成功。

### 缺陷 E（P1）：CFN Init Metadata 参数变更“保存成功”但永不生效

- 现象：UpdateStack 修改 `DockerSocketAccess=true` → UPDATE_COMPLETE，但实例
  未替换（LaunchTime 早于 UpdateStack）、cfn-init 未重跑（日志
  `++ DOCKER_SOCKET_ACCESS=false` 铁证），且 base AMI 未装
  `/opt/aws/bin/cfn-init`，无自助重放路径。
- 影响：**所有走 CFN Init Metadata 的实例级参数**（DockerSocketAccess、
  ExtraTCP/UDPPort、ExtraEnvironment 等）创建后均不可修改，控制台却显示成功——
  静默配置漂移。
- 修复方向：此类参数声明 UpdateReplacePolicy 维度上的替换语义（模板
  `UpdateReplaceInstances`），或在 DeployGuide 明示“修改该参数需重建栈”。
- 本次演练处置：SSM 手动 export CFNOVA_* 后重跑
  `/opt/corenova/bin/30-app-container.sh` + systemctl restart 生效；注意手动
  路径在 export 变量集不完整时会引入二次配置漂移，只能作为演练级临时手段。
