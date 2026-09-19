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
| gitea | [35418998361](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35418998361) | （进行中→待填） | | |
| portainer | [35419000417](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35419000417) | （进行中→待填） | | |
| vaultwarden | [35419002582](https://github.com/CoreNovaLabs/CoreNovaLaunch/actions/runs/35419002582) | （进行中→待填） | | |

说明：首轮 3 个 run（§0 缺口 1）应用层已全过，本次重验主要补平台契约引用
与模板 revision 绑定。

## 3. AWS 真实部署（用户流程逐步留档）

对每个应用：建栈参数（= 深链参数）→ 栈事件时间线 → Outputs →
首次登录 → 核心操作 → 重启实例后数据保留 → 备份/恢复 → 删栈清理。

### 3.1 gitea v1.27.3（B 类，验证公网地址注入 GITEA__server__ROOT_URL）

- 建栈参数：（待填：ImageReference/AmiId/ContainerPort/DataVolumeSize/ROOT_URL 注入）
- 栈：（待填 stack-id / 公网地址 / 耗时）
- 首次登录：（待填：安装向导 → 管理员创建）
- 核心操作：（待填：建仓库 + push 一次提交）
- 重启保留：（待填：stop/start 后登录与数据在）
- 备份恢复：（待填：gitea dump → 清卷 → 恢复 → 数据在）
- 清理：（待填：栈删除确认，保留卷/快照处置）

### 3.2 portainer 2.45.1（B 类，验证 Docker socket 访问面）

（待填，同上结构）

### 3.3 vaultwarden 1.37.3（基线）

（待填，同上结构）

## 4. 结论与跟进

（待填：用户能否独立完成全流程、发现的摩擦点、对官网指引的改进项）
