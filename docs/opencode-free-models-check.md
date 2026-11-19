# OpenCode 免费模型实测记录（2026-11-19，北京时间）

结论：本机目前无法用免费模型完成真实调用。所有尝试都返回免费额度已用完（429），所以真实模型下的 3–5 个任务和 kill -9 测试还没有跑成。

| 时间 | 方式 | 模型 | 结果 |
|---|---|---|---|
| ≈23:06 | 不带 key 直接 curl `https://opencode.ai/inference/openai/v1/chat/completions` | big-pickle | HTTP 429 `FreeUsageLimitError`："Free models work in OpenCode…" |
| ≈23:06 | `opencode run`（CLI v2.3.2，未登录，独立 XDG 目录） | deepseek-v4.1-flash-free | `provider.free-limit`，status 429，retryAfterMs 35418000（≈9.8 小时） |
| ≈23:06 | 同上 | big-pickle / longcat-2.5-free / hy4-flash-free | 429，retryAfterMs 分别为 32009000 / 33677000 / 32369000 |
| 23:08 | 本机 worker `PA_LLM=opencode`，通过 API 提交 1 个 demo.summarize | deepseek-v4.1-flash-free | 第 1 次尝试失败：`rate_limited`，retry_after 31882s，任务进入 `retry_wait`（没有进 dead），账本记了 1 次调用；之后手动取消了这个任务 |

换成独立的配置和数据目录后，额度依然是用完的状态。所以限制大概率按出口 IP 或共享额度计算，与本机状态无关（这是推断，官方没有说明计数方式）。我们没有尝试任何绕过：没有注册账号，没有伪造请求头，也没有换代理。

官方依据：
- 免费模型只能在 OpenCode 内使用，其他客户端会收到 403：https://opencode.ai/docs/console/harnesses/
- 模型列表、免费期限和数据条款：https://opencode.ai/docs/console/models/

额度恢复后，或者有了 `OPENCODE_API_KEY` 之后，按 README 里“接入真实模型（OpenCode）”一节重跑即可。
