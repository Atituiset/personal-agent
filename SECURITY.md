---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [scripts/dev_instance.py, personal_agent/config.py, personal_agent/infra/agents_sdk.py, personal_agent/tasks/agent.py, personal_agent/api/app.py, .env.example, docker-compose.yml]
---
# 安全约定

这是一个单用户、本机运行的项目；以下是代码里已经落实的约定，改动时不能削弱。

## 密钥

- 密钥只从环境变量或本地 `.env` 读取（`personal_agent/config.py`）；`.env` 在 `.gitignore` 里，绝不提交。示例值见 `.env.example`。
- 真实模型的 key 按 `PA_LLM_API_KEY`、`OPENAI_API_KEY`、`OPENCODE_API_KEY` 的顺序取；不写进日志、结果产物和 trace。
- worker 启动日志只打印库名，不打印连接串里的密码（`PgBroker.database`）。

## Tracing 和数据外流

- `PA_AGENTS_TRACING` 默认 `off`：在导入 SDK 之前就关掉 tracing，任何模式下都不注册上传到 OpenAI 的处理器。
- `local` 模式只写本地 JSONL，默认不记录模型输入输出（`PA_AGENTS_TRACE_SENSITIVE=1` 才记录）。

## 智能体的权限

- 工具白名单：`agent.run` 只能用 payload 里 `tools` 列出的工具，列表外的名字直接判为不可重试错误（`Fatal`）。
- `read_artifact` 只能读 payload 里 `artifacts` 列出的产物；评测里有一个不在白名单里的 canary 产物，内容出现在回答或笔记里即算违规。
- `calculate` 用 AST 白名单求值（`safe_eval`），拒绝属性访问、函数调用和过大的幂运算。
- 产物里夹带的指令（提示注入）不应被执行；评测有对应任务（`evals/fixtures/vendor_notice.txt`）。
- 有副作用的工具必须经过 `JobContext.effect` 记账；对外副作用（邮件、发帖、推代码）默认需要人工确认，不在无人值守路径上自动执行。

## 网络暴露

- API 默认只监听 `127.0.0.1`；`docker-compose.yml` 里的 Postgres 端口也只绑定 `127.0.0.1`。
- `POST /jobs` 的内联 payload 上限 64 KiB。

## 本地隔离实例

- `make dev-up` 为实例生成随机数据库密码，只写在 `var/dev/<name>/instance.json`（权限 600，`var/` 不提交）；私有集群只监听 `127.0.0.1`。
- `scripts/dev_instance.py status` 和 `up` 打印的信息去掉了密码。

## 测试用的故障注入

- `PA_FAULT_PAUSE_AT` / `PA_FAULT_DIR`（`personal_agent/queue/faults.py`）只用于测试和评测里的 kill -9 场景，生产环境不要设置。

## 报告问题

发现安全问题请不要在公开 issue 里贴细节，直接联系仓库所有者。
