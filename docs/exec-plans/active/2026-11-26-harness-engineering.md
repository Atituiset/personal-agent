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

- [ ] 1. 执行计划约定：PLANS.md、模板、本计划、tech-debt-tracker
- [ ] 2. AGENTS.md 改成约 100 行的目录，补 ARCHITECTURE / RELIABILITY / SECURITY / QUALITY_SCORE、design-docs、references；`scripts/doc_check.py` + CI
- [ ] 3. 分层 lint：`scripts/check_architecture.py` + `tests/test_architecture.py`，报错带修复说明
- [ ] 4. 运行时可读：结构化日志、`personal-agent metrics`、`make dev-up`、`make runtime-check`（预算 + 7 个系统检查）+ CI
- [ ] 5. 评测反馈回路：`evals/followup.py`，门禁失败时生成 JSON + Markdown 跟进项
- [ ] 6. 黄金原则：`docs/design-docs/golden-principles.md`、`scripts/golden_scan.py`、每周 workflow
- [ ] 7. 全量验证（pytest、evals、新检查、actionlint），本计划移到 completed/

## 决策日志

- 2026-11-26：保留 `docs/design.md` 和各测试报告的原位置，只在 `docs/design-docs/index.md` 里索引；原因是 README 和外部链接都指向这些路径，搬家收益小于断链风险。
- 2026-11-26：新检查都用标准库 Python 写（不引入新依赖），和仓库现有的测试、评测保持一种语言。

## 意外与发现

- （进行中补充）

## 验证

- `pytest -q` 全部通过；`evals.run_evals --trials 5 --k 1 3 5` 回归集 150/150，`evals.gate` 通过；
- `scripts/doc_check.py`、`scripts/check_architecture.py`、`scripts/golden_scan.py --fail-on-new` 退出码 0；
- `make runtime-check` 通过；workflow 通过 actionlint。

## 结果

（完成时填写）
