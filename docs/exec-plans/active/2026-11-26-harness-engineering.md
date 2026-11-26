---
owner: "@Atituiset"
last_reviewed: 2026-11-26
status: active
code_paths: [AGENTS.md, ARCHITECTURE.md, PLANS.md, scripts, evals, .github/workflows]
---
# 把 Harness Engineering 的做法落到 personal-agent

## 目的

参考 OpenAI《Harness engineering》（Ryan Lopopolo，2026-02-11）：让仓库本身成为智能体能读懂、能自己验证的环境。
具体是六件事：AGENTS.md 只当目录、执行计划进仓库、架构不变量机械化、运行时对智能体可读、评测门禁变成反馈回路、黄金原则定期扫描。

## 范围

- 包括：文档结构和文档检查、执行计划约定、分层 lint、本地隔离实例 + 指标 + 预算检查、评测失败跟进、黄金原则扫描和每周 workflow。
- 不包括：部署（不上 Railway）；改动队列语义；接真实模型。

## 进度

- [x] 1. 执行计划约定：PLANS.md、模板、本计划、tech-debt-tracker（提交 `docs: exec plans as first-class artifacts`）
- [x] 2. AGENTS.md 改成约 100 行的目录，补 ARCHITECTURE / RELIABILITY / SECURITY / QUALITY_SCORE、design-docs、references；`scripts/doc_check.py` + CI
- [x] 3. 分层 lint：`scripts/check_architecture.py` + `tests/test_architecture.py`，报错带修复说明；现有代码只有一处例外，登记为 TD-1
- [x] 4. 运行时可读：结构化日志、`personal-agent metrics`、`make dev-up`、`make runtime-check`（预算 + 7 个系统检查）+ CI
- [x] 5. 评测反馈回路：`evals/followup.py`，门禁失败时生成 JSON + Markdown 跟进项
- [ ] 6. 黄金原则：`docs/design-docs/golden-principles.md`、`scripts/golden_scan.py`、每周 workflow
- [ ] 7. 全量验证（pytest、evals、新检查、actionlint），本计划移到 completed/

## 决策日志

- 2026-11-26：保留 `docs/design.md` 和各测试报告的原位置，只在 `docs/design-docs/index.md` 里索引；原因是 README 和外部链接都指向这些路径，搬家收益小于断链风险。
- 2026-11-26：分层检查先于文档检查提交，因为 AGENTS.md 和 ARCHITECTURE.md 要引用检查脚本。
- 2026-11-26：隔离实例按顺序尝试三种数据库来源：`PA_DEV_ADMIN_URL`（CI 用 postgres:17 服务容器）、本机 initdb 私有集群、Docker；每个 worktree 的 `var/` 各自独立，库名带仓库路径哈希，不会互相踩。
- 2026-11-26：系统检查跑在实例的第二个库（`…_eval`）上，因为评测 runner 会 TRUNCATE 并自己起 worker，和实例的常驻 worker 共用一个库会抢任务。
- 2026-11-26：跟进项只在门禁失败时生成，门禁的判定逻辑和 `evals/baseline.json` 不动；本地写进 `docs/exec-plans/active/`（成为下一轮的执行计划），CI 写 job summary 和 artifact（CI 不能往仓库里提交）。
- 2026-11-26：新检查都用标准库 Python 写（不引入新依赖），和仓库现有的测试、评测保持一种语言。

## 意外与发现

- 本机 `make runtime-check` 第一次跑（2026-11-26，私有 initdb 集群，50 个探针任务）：提交 p95 4.8 ms，入队→开始 p95 283.7 ms，入队→ACK p95 310.9 ms，7 个系统检查各 3/3。入队→开始的 p50 在 175 ms 左右，主要是 worker 空闲时 0.2 s 的轮询间隔。
- `pytest` 结束时会改写已提交的 `test-results/*.json`（conftest 的 sessionfinish），验证后要 `git checkout test-results/`，不要把本机数字混进提交。

- 仓库原来没有 AGENTS.md，所以第 2 点是新建而不是拆分。
- 现有代码基本已经按层组织（`core` 不依赖驱动的测试早就有）；唯一越界的是 `tasks/agent.py` 直接用 Agents SDK（TD-1）。

- 用 `--profile regressed` 演示：门禁按预期失败（`time_new_york_en`、`time_london_en` pass@1 1.000→0.000，回归 pass^3 1.000→0.933），`evals/followup.py` 生成 2 个任务项 + 1 个套件级问题，下一步指向 `evals/policy.py`，轨迹里能看到 `current_time({"tz": "UTC"})`。

## 验证

- `pytest -q` 全部通过；`evals.run_evals --trials 5 --k 1 3 5` 回归集 150/150，`evals.gate` 通过；
- `scripts/doc_check.py`、`scripts/check_architecture.py`、`scripts/golden_scan.py --fail-on-new` 退出码 0；
- `make runtime-check` 通过；workflow 通过 actionlint。

## 结果

（完成时填写）
