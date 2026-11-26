---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [scripts/golden_scan.py, scripts/golden_baseline.json, .github/workflows/golden-scan.yml]
---
# 黄金原则

少数几条**能机械检查**的规则，目的是让代码库在智能体大量写代码时不慢慢走样。
每条都由 `scripts/golden_scan.py` 用 AST 检查（标准库实现，扫描 `personal_agent/` 和 `evals/`）。

- **CI**（`checks` workflow）：`golden_scan.py --fail-on-new`，出现基线以外的新偏差就失败。
- **每周**（`golden-scan` workflow，周一 01:00 UTC，也可手动触发）：生成完整报告，写进 job summary，
  并新建或更新一个 issue「Golden principles drift report」；没有任何偏差时关闭该 issue。**不会自动改代码、开 PR 或合并。**
- **已知偏差**记在 `scripts/golden_baseline.json`，每条都指向 [tech-debt-tracker](../exec-plans/tech-debt-tracker.md) 里的编号；
  修好后扫描会提示把基线条目删掉。

| 编号 | 原则 | 为什么 | 检查方式 | 怎么改 |
| --- | --- | --- | --- | --- |
| GP1 | **在边界处校验数据形状** | 坏数据越早拒绝越便宜；队列里坏 payload 不该被重试 5 次 | `register(TaskSpec(handler=…))` 的 handler（或它调用的 `_validate`）必须 `raise Fatal`；POST/PUT/PATCH 路由的参数不能是 `dict` / `Request` / `Any` | handler 开头校验 payload，失败抛 `Fatal`（进死信不重试）；请求体用 pydantic 模型 |
| GP2 | **配置只从 providers 进来** | 配置散落在各处时，智能体找不到某个行为由哪个开关控制 | `os.environ` / `os.getenv` 只允许出现在 `personal_agent/config.py`、`personal_agent/obs.py`、`personal_agent/infra/`、`personal_agent/queue/faults.py` | 在 `config.py`（或拥有该设置的适配器）读取，再把值传进去 |
| GP3 | **结构化日志** | JSON 事件能用 `scripts/logq.py` 精确查询；自由文本只能猜 | 运行时包里除 `cli.py` 外不能 `print()`；`log.info(f"…")`、`%` / `+` / `.format` 拼出来的日志消息算偏差 | `personal_agent.obs.log_event(logger, "event", key=value)` |
| GP4 | **优先用共享工具** | 同一件事多份实现会各自走样 | 跨模块导入 `_private` 名字算偏差（说明它其实是共享工具）；运行时包里只有 `core/storage.py` 能直接用 `hashlib` | 把共享函数改成公开名字放到最低需要它的层；哈希用 `core.storage.digest` / `core.context.fingerprint` |
| GP5 | **失败不冒充成功** | 静默吞掉异常会把故障变成“成功” | `except Exception` / `except BaseException` / 裸 `except` 的处理体里既没有调用也没有 `raise` | 重新抛出、转成 `JobError`、或 `log_event`；确实要丢弃时在 except 行写 `# noqa: BLE001 <原因>` |
| GP6 | **外部调用必须有超时** | 一个卡住的依赖不该拖住整个 worker | `subprocess.run` / `check_output` / `check_call`、`httpx.Client` / `AsyncClient` / `httpx.get` 等调用要带 `timeout=` | 显式传 `timeout=` |

## 用法

```bash
make golden-scan                                  # 列出全部偏差（新 / 已知 / 已修复）
.venv/bin/python scripts/golden_scan.py --fail-on-new   # CI 用：有新偏差就失败
```

新增规则时：在脚本里实现并写测试（`tests/test_golden_scan.py`），在上表登记；如果现有代码有偏差，
要么顺手修掉，要么登记到 tech-debt-tracker 再加进基线。
