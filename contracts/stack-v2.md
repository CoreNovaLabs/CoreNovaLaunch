# 多容器 v2：实验性离线编译协议

状态：阶段 1 底座 + 阶段 2 本地实验运行时。**不可发布，不代表已支持生产多容器部署。**
现有 apps/*.yaml、v1 pipeline、官网、CloudFormation 均不迁移。

## 已实现

- `corenova.stack.validate_stack`：严格字段白名单、1–8 个服务、依赖 DAG、唯一入口。
- `compile_stack`：生成 Compose，全部镜像要求 sha256 digest，固定 linux/amd64。
- 每个长期服务必须提供 argv 健康检查，依赖等待 service_healthy。
- 唯一宿主机端口绑定 127.0.0.1；其他服务仅使用 Compose 项目网络。
  未发布端口不等于禁止出站，也不等于同一项目内服务隔离。
- 持久化目录只能引用声明的逻辑卷，不能指定任意宿主路径。
- 凭据仅支持文件引用，映射至 /run/secrets；不读取、生成或发布真实凭据。
- 生成 deterministic Compose 和含 SHA-256 的 stack-lock.json，状态固定 UNVERIFIED。
- v1 验证器拒绝 v2 标记和 deploy.services，防止未完整验证的栈进入旧发布链路。

## 输入形状

顶层：`schema_version: 2`、`name`、`entrypoint: {service, port}`、
`services` 映射，以及可选 `volumes` / `secrets` 名称列表。

服务必填：`image: repository@sha256:<64 位摘要>`、`healthcheck: [可执行文件, 参数...]`。
可选：`command` argv、`environment` 非敏感字符串映射、`depends_on` 名称列表、
`volumes: {逻辑卷名: 容器绝对路径}`、`secret_files: {XXX_FILE: 凭据名}`。
凭据关键字检查只是防误填，不是完整的秘密检测器；定义与命令仍必须人工审查。
镜像摘要格式校验不等于镜像存在、架构或来源验证，后续 resolver 必须补齐。

```
.venv/bin/python scripts/dev/compile_stack.py reviewed-stack.yaml --out /tmp/new-stack-bundle
```

输出目录必须不存在。命令不启动容器、不联网、不接触 AWS、不生成凭据。
运行时变量预留：CORENOVA_STACK_DATA_DIR、CORENOVA_STACK_SECRETS_DIR、
CORENOVA_STACK_HOST_PORT。目录须是调用方专属绝对路径；数据子目录应预创建并设置
符合应用 UID/GID 的权限。编译器不做 chmod 777 或自动创建宿主挂载路径。
Docker Compose 文件型 secret 的实际可读性受宿主文件权限影响，不假设 uid/mode
字段能自动修正绑定文件权限。

## 尚未实现的门禁（不能省略）

### 本地运行时新增能力

`corenova.stack_runtime` 提供 `resolve_images` 和 `LocalStack`：
精确数值 tag/digest 拉取、linux/amd64 检查、记录 registry digest 与 image ID；
新建专属工作目录、600 权限随机凭据、重复初始化复用且拒绝不安全文件；
唯一随机 Compose 项目、等待全部服务健康、逐服务状态检查、限定项目清理。
清理不删除镜像、不 prune、不删除数据卷。

显式 opt-in 实测命令：

```
CORENOVA_STACK_INTEGRATION=1 .venv/bin/python -m pytest tests/test_stack_runtime.py -q
```

测试使用两套真实 Python HTTP 服务验证认证、转发、后端不映射宿主机端口、
依赖停止返回 503、整栈健康失败、容器重建后的修改数据及凭据保持不变。
这不是数据库应用试点，也不等价于生产验证。当前文件凭据仅面向容器 root
消费者；非 root UID/GID、安全恢复既有工作目录仍待实现。临时测试数据由
pytest 管理；测试结束只清理自身 Compose 容器和网络，不影响其他容器。

本次验证记录：离线全量 269 passed、1 个 Docker opt-in 用例默认跳过；
显式开启 Docker 后，运行时测试 4 passed（83.69 秒；之后新增两项 resolver 单元测试）。
真实重建读取了启动后修改的标记值并确认原凭据不变；故障阶段确认 HTTP 503
和整栈不健康。测试结束确认 cn-v2 容器全部清理，原有 open-webui 仍健康。

### 剩余工作

1. 接入逐应用 release 来源证据（现有运行时仅做精确镜像引用解析）。
2. 各服务 UID/GID 与卷权限初始化、非 root 凭据读取及跨进程恢复。
3. 将本地运行时接入完整验证报告及实际数据库应用试点。
4. 在真实应用上验证数据恢复与业务行为，不能只依赖通用 HTTP 夹具。
5. 初始化任务完成语义，超时与失败诊断脱敏。
6. 公开 Manifest v2、不可变部署包发布、生产引导校验及官网 v2 入口。
7. 真实 AWS 验证（事先确认费用）与数据恢复演练。

阶段 2 需以上前五项实测通过，不能仅以单元测试通过宣称完成。
参考：[Compose 服务规范](https://docs.docker.com/reference/compose-file/services/)、
[Compose 文件凭据](https://docs.docker.com/compose/how-tos/use-secrets/)。
