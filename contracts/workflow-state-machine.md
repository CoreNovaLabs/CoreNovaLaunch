# Contract · Workflow State Machine（工作流状态机）

> 优先级：**最高**。
> 术语：本文沿用 Repo A / Repo B / Repo C 代号，分别指 `CoreNovaLaunchWebsite`（官网，本地目录 `website/`）、`CoreNovaLaunchAmi`（AMI 构建，引导期未落地）、`CoreNovaLaunch`（验证枢纽）。
> 适用：Repo C 的所有验证/部署工作流（Application Verification 的容器阶段、L1.5 生产核对、Platform Verification）。
> 本文保证流程确定性：同一时刻每个 app 的状态唯一、可追溯。任何设计文档与之冲突，以本文为准。

## 1. 状态总表

```
                          ┌── 无 production_contract（旧路径）──────────────────────────────────┐
                          │                                                                    ▼
DISCOVERED ─▶ RESOLVED ─▶ VERIFYING ─▶ VERIFIED ─┤                                              PUBLISHING ─▶ PUBLISHED
  ▲   ▲                                           │                                                  ▲
  │   │                                           └─▶ CANDIDATE_READY ─▶ DEPLOYING ─▶ DEPLOYED ─┘
  │   │                                                            （仅 L1.5 生产核对，见 §3）
  │   └─ RETRY（TRANSIENT，最多 3 次指数退避）

任何 VERIFYING/PUBLISHING/DEPLOYING 失败 ─▶ FAILED
FAILED ─┬─ TRANSIENT        ─▶ RETRY（唯一可自动重试，最多 3 次指数退避）
        ├─ APPLICATION      ─▶ FIX_PR 或 MANUAL_REQUIRED
        ├─ TEST             ─▶ FIX_PR
        ├─ INFRASTRUCTURE   ─▶ MANUAL_REQUIRED（基础设施不动）
        └─ MANUAL_REQUIRED  ─▶ MANUAL_REQUIRED
```

> 五个子分类与 `corenova/failure.py` 的 `ALL_CLASSIFICATIONS` 及本文 §4 表格一一对应，无第六种。
> `FIX_PR` 不等于"自动修复"：修复 PR 由人工在流水线外发起（§6）。

## 2. 各状态定义

| 状态 | 含义 | 数据落点 |
|------|------|---------|
| `DISCOVERED` | 版本监控发现新版本/新应用/手动触发 | workflow run 开始 |
| `RESOLVED` | 已解析 app_version、docker_image、docker_digest、platform_contract | 写入 run 上下文 |
| `DEPLOYING` | **Platform Verification 与 L1.5 生产核对**：CFN 栈（canary / 一次性核对栈）创建或更新中 | stack 状态 |
| `DEPLOYED` | **Platform Verification 与 L1.5 生产核对**：EC2 已起、cfn-init 完成、cfn-signal 收到 | — |
| `VERIFYING` | 跑验证（Application：compose+Playwright；Platform：AWS 资源探针；生产核对：真实栈上的外部探针） | — |
| `VERIFIED` | 验证通过，但尚未发布 | 生成 Manifest（未上传 current） |
| `CANDIDATE_READY` | **仅声明 `deployment.production_contract` 的应用**：容器阶段全绿，产物已隔离暂存，等待生产核对 | `candidates/{app}/{run}/{attempt}/{vid}/manifest.json` |
| `PUBLISHING` | 上传 R2 + 发 repository_dispatch；候选应用为**生产核对通过后的 CAS 晋级**（`promote`） | R2 写入中 |
| `PUBLISHED` | 已发布，网站事实源更新 | `current.json` 已更新 |
| `FAILED` | 任一阶段失败，进入子分类 | issue / PR |
| `RETRY` | 瞬时失败自动重试 | 重新进入 `VERIFYING` |
| `FIX_PR` | 等待修复 PR（由人工在流水线外发起，见 §6；流水线本身不连 AI） | PR |
| `MANUAL_REQUIRED` | 需人工介入 | 标注 issue |

## 3. 三条路径的差异

### 3.1 Application Verification · 容器阶段（每次版本更新）
```
DISCOVERED → RESOLVED → VERIFYING → VERIFIED → (PUBLISHING → PUBLISHED | CANDIDATE_READY)
                                  ↘ FAILED → (RETRY | FIX_PR | MANUAL_REQUIRED)
```
- 本阶段**不创建 AWS 资源、不部署 CloudFormation**，因此在 GitHub Actions 上零 AWS 费用。
- 分支由 app 是否声明 `deployment.production_contract.checks`（规则 22）决定：
  - **未声明**：`VERIFIED` 后直接走 `PUBLISHING → PUBLISHED`（旧路径，verification-manifest.md §6.2 的 P1–P5）。
  - **已声明**：`VERIFIED` 后进入 `CANDIDATE_READY`——产物写入隔离前缀 `candidates/{app}/{run}/{attempt}/{vid}/`，
    **不写** `current.json`、版本记录与索引。**`CANDIDATE_READY` 不是 `PUBLISHED`**，即使九项 checks 全绿也不代表已发布。
- `RESOLVED` 阶段复用既有有效 Platform Contract（`verification.platform = referenced`）。

### 3.2 L1.5 生产核对 + 晋级（仅候选应用，deployment-contract.md §2.6）
```
CANDIDATE_READY → DEPLOYING → DEPLOYED → VERIFYING(prodcheck) → PUBLISHING(promote, CAS) → PUBLISHED
       │               │           │              │
       │               │           │              └─ 核对/清理未确认 → FAILED（旧稳定发布保持）
       │               └───────────┴─ 真实 AWS 一次性用户栈：会产生费用，跑完必须删干净
       └─ 候选过期、身份不符、CAS 冲突 → FAILED（不晋级）
```
- 由 `application-verify.yml` 在 `CANDIDATE_READY` 后 dispatch `production-verify.yml`，输入是**精确候选引用 JSON**，
  不允许按应用名回落到 current/latest。链条断掉时候选保持不动，旧稳定发布不受影响。
- 本阶段与容器阶段共用 `verify-<app>` 并发组（§5），晋级前重读 main 的策略：新增 hold 或 config 变更会使旧候选失效。
- **晋级必须原样保留已登记的 `deploy.hold`**：核对全绿不是解除暂停的授权，人工 hold 永不自动清除。
- 清理未确认（`cleanup_confirmed`）即判 `FAILED`，不得晋级。

### 3.3 Platform Verification（AWS Golden，低频）
```
DISCOVERED → RESOLVED → DEPLOYING → DEPLOYED → VERIFYING → VERIFIED → PUBLISHING → PUBLISHED
                                                                  ↘ FAILED → (RETRY | FIX_PR | MANUAL_REQUIRED)
```
- `DEPLOYING`/`DEPLOYED` 在本路径与 §3.2 使用，容器阶段（§3.1）不进入。
- `PUBLISHED` 含义 = 标记 Platform Contract `status=valid`（见 platform-contract.md），并生成 `platform_verification_id` 供后续 Application Verification 引用。

## 4. `FAILED` 子分类与 Retry 规则

| 分类 | 示例 | 自动重试? | 动作 |
|------|------|----------|------|
| `TRANSIENT` | 网络超时、Docker pull 超时、GitHub API 失败、AWS 限流 | ✅（最多 3 次，指数退避） | `RETRY` |
| `APPLICATION` | 容器启动失败、迁移失败、无效 app 配置 | ❌ | `FIX_PR` 或 `MANUAL_REQUIRED` |
| `TEST` | Playwright 断言失败、selector 变化 | ❌ | `FIX_PR` |
| `INFRASTRUCTURE` | CFN 失败、AMI 失败、cfn-init 失败、Nginx 失败 | ❌（基础设施不动） | `MANUAL_REQUIRED` |
| `MANUAL_REQUIRED` | 未知失败、安全敏感变更、AI 置信度不足 | ❌ | `MANUAL_REQUIRED` |

**禁止对所有失败统一重试。** 只有 `TRANSIENT` 可自动 `RETRY`；其余进入 `FIX_PR`（应用/测试层，人工在流水线外修改白名单内文件，默认 `apps/{app}/tests/**`，见 §6）或 `MANUAL_REQUIRED`（基础设施/安全层，必须人工）。

## 5. Concurrency（并发）

### App 级并发
同一 app 不允许多个 publish verification 同时竞争：

```yaml
concurrency:
  group: verify-${{ inputs.app_name }}
  cancel-in-progress: false
```

`cancel-in-progress: false` 保证正在发布的验证跑完，新触发排队，避免 `current.json` 撕裂。

**`app_name` 必须是必填输入（空值防护）：**

- `monitor-versions` / 手动 `workflow_dispatch` / `workflow_call` 三种入口都必须显式带 `app_name`；`application-verify.yml` 的**第一个 step** 校验其为空即 `exit 1`。
- 原因：`group: verify-` 在空值下会塌成**同一个分组**，导致所有 app 的验证互相排队串行（每 app 约 5–15 分钟，多 app 后队列不可用）。
- **禁止**用 `group: verify-${{ inputs.app_name || github.run_id }}` 之类的兜底——那等于关掉互斥保护，同 app 两个 run 可同时进入 PUBLISHING，撕裂 `current.json`。宁可失败并重触发，也不放开同 app 互斥。
- 需要并行处理多 app 时，由**调用方**（`monitor-versions`）按 app 分别 dispatch，而不是在一个 run 里跑多 app。

### 版本覆盖保护
- **较旧验证结果不得覆盖较新已发布版本。**
- 例：`v5.76` 已 `PUBLISHED`（current=v5.76）；稍后 `v5.75` 的迟到的重试验证 `PASS` → 写 `versions/v5.75.0.json`，但**绝不更新** `current.json`（current 仍为 v5.76）。
- 实现：发布前比较待发布 `app_version` 与现有 `current.json` 的 `app_version`：
  - **可语义化比较**（`release_tag` / `semver_latest` 产出的 semver，或带 `v`/`V` 前缀的版本——比较前统一去除前缀后按 semver 比较）：仅当待发布版本 ≥ 当前版本时才更新 `current.json`。
  - **不可语义化比较**（`git_branch` / `pinned` 产出的 commit SHA、日期标签等）：不得用版本号裁决，统一以 `verification_run_id` 较新者为准（或要求显式 `force` 输入）；默认**不覆盖**已有 `current.json`，避免把迟到的旧提交误判为新版本。

## 6. AI 辅助修复（人工发起、离线、走 PR）

**模型（2026-08-30 定稿）**：测试脚本**预先写好**（`apps/{app}/tests/**`）；Actions 只负责跑它们。
失败时流水线**不连接 AI**——只产出"交接包"（失败台账 + HTML 报告 + `analyze_failure.py` 规则式诊断）。
随后由**人工在流水线外**把交接包喂给自己的 AI/编辑器，修改白名单内文件，再走 PR + review + 重验。
即：AI 是人的工具，不是流水线里的自动 actor。

```
FAILED(APPLICATION|TEST)
  → 流水线写台账 + 报告 + 规则诊断（交接包），状态 FIX_PR（等待修复 PR）
  → [离线] 人 + AI 依据交接包修改白名单内文件
  → Create PR → Run CI（重跑预写测试）→ Human review → Merge → Re-verify(RESOLVED)
```

- 流水线**绝不调用任何外部 AI API**（密钥面 + 不可复现）；`analyze_failure.py` 为规则式诊断，
  将来若接入 AI 生成，只替换其 `_diagnose()` 实现，白名单校验与退出码契约不变。
- 默认允许修改：`apps/{app}/tests/**`
- 视设计允许：`apps/{app}/*.yaml`（仅应用配置，非基础设施）
- **禁止**修改：`.github/workflows/**`、`templates/cloudformation/**`、`packer/**`、`infra/**`、IAM、Security Group、networking、production 部署逻辑。
- AI/人 **不得直接改 main**；必须走 PR + review。

**反模式（防止"为过关而修"）**：
- ❌ 修复只靠删除/放宽断言让测试变绿——那是 TEST 回归，不是修复；review 必须核对断言仍覆盖原意图。
- ❌ 把 `expected` 改成"当前实际值"而不验证该值是否正确（reward hacking）。
- ❌ 在 PR 里夹带白名单外改动（CI 白名单校验与 review 均应拒绝）。

## 7. 失败台账（`FAILED` 的持久化载体）

`RETRY` / `FIX_PR` / `MANUAL_REQUIRED` 都是**跨 run 的状态**，而 GitHub Actions run 本身不可靠地承载"待办"。台账唯一载体 = **Repo C 的 GitHub issue**（不引入数据库，避免第二个状态存储）。

| 项 | 规范 |
|----|------|
| 标题 | `verify(<app>): <app_version> FAILED (<classification>)` |
| Label | `verify-failed` + `classification:<TRANSIENT\|APPLICATION\|TEST\|INFRASTRUCTURE\|MANUAL_REQUIRED>` + `app:<app>` |
| 幂等键 | 正文 fenced `corenova-failure` JSON 块的 `verification_id` 完整匹配；同 ID 的开放 issue 再次失败 → **更新同一 issue**（追加 attempt，同步标题），不重新打开已关闭 issue |
| Assign / 状态 | `MANUAL_REQUIRED` 打 `needs-human`；`FIX_PR` 由 AI 分支引用该 issue（`Fixes #N`） |

早期镜像解析失败必须保留已解析的 `app_version`，使用 `pre-verification-{app}-{清洗后的版本}` 隔离不同版本；版本尚未解析才写 `unknown`，并按应用和失败检查隔离 ID。进入验证后保留已分配的 ID。Actions 中即使早期失败也须写 `run_url`，不得把 CLI 指定版本当作已解析结果。历史 `unknown` 记录不得按猜测自动关闭，需人工核对成功证据。

正文 metadata 块（机器可读，`reverify-failed` 据此筛选）：

````markdown
```corenova-failure
app: ghost
app_version: v5.75.0
verification_id: ghost-v5.75.0-20260827-001
classification: TRANSIENT
failed_stage: VERIFYING            # RESOLVED | VERIFYING | PUBLISHING
failed_check: health_check_passed  # 九项 checks 之一，或 resolve_digest / compose_up
attempts: 2
run_url: https://github.com/<org>/CoreNovaLaunch/actions/runs/123456
platform_verification_id: plat-us-east-1-x86_64-20260827-001
```
````

**`reverify-failed.yml` 消费规则（严格）：**

1. 只取 `classification:TRANSIENT` 且 `attempts < 3` 且 issue 处于 open 的记录 → 按 app 重新 dispatch `application-verify`。
2. 重试后**沿用同一 `verification_id`**（§2 定义：同一验证的重试不改号），成功则关闭 issue 并写最终态 Manifest；失败则 `attempts += 1` 更新台账。
3. `attempts` 达 3 → 改 label 为 `classification:MANUAL_REQUIRED` + `needs-human`，不再自动重试。
4. 人工**关闭** issue = 显式放弃该次重试（脚本不得自动重开已关闭 issue）。
5. `APPLICATION` / `TEST` / `INFRASTRUCTURE` / `MANUAL_REQUIRED` 四类**永不**被 `reverify-failed` 自动触发——只有 `TRANSIENT` 有自动重试资格（§4）。

**覆盖范围**：台账只记录 §3.1 容器阶段的失败。§3.2 的 L1.5 生产核对失败**不写 issue**，其载体是 run 结论与
始终上传的 `prodcheck-{run}-{attempt}` 证据 artifact（`production-verify.yml`），因为候选本身已带精确身份且不会污染稳定发布。

## 8. 反模式

- ❌ 容器阶段（§3.1）进入 `DEPLOYING`/`DEPLOYED`：AWS 资源只在生产核对（§3.2）与 Golden Verification（§3.3）里创建。
- ❌ 把 `CANDIDATE_READY` 当作已发布，或让候选产物写进公开 `current.json` / 版本 / 索引。
- ❌ 生产核对未全绿、清理未确认、候选已过期或 CAS 冲突时仍然晋级；旧稳定发布必须保持原样。
- ❌ 晋级时顺手清除 `deploy.hold`（人工 hold 永不自动清除，见 deployment-contract.md §2.5/§2.6）。
- ❌ 对所有 `FAILED` 统一 `RETRY`。
- ❌ AI 修复越权改基础设施/安全工作流。
- ❌ 旧版本覆盖新版本 `current.json`。
- ❌ 并发下 `cancel-in-progress: true` 导致发布撕裂。
- ❌ `app_name` 允许为空（并发组塌缩成 `verify-`，所有 app 互相串行排队）。
- ❌ 把跨 run 的待重试状态存在 run 上下文、actions artifact 或本地文件里（必须落 §7 的 issue 台账）。
