"""evals/followup.py：门禁失败时生成跟进项（JSON + Markdown / 执行计划）；门禁通过时什么都不做。离线，不需要数据库。"""
import json

from evals import followup, gate


def _task(c, n=5, kind="regression", violations=0, first_failure=None):
    return {"kind": kind, "category": "time", "n": n, "c": c, "pass@1": c / n, "pass^3": 1.0 if c == n else 0.0,
            "pass@3": 1.0 if c else 0.0, "tokens_mean": 300.0, "violations": violations, "first_failure": first_failure}


def _results(per_task, trials=()):
    reg = [t for t in per_task.values() if t["kind"] == "regression"]
    suite = {"pass^3": sum(t["pass^3"] for t in reg) / len(reg), "pass@3": 1.0, "tokens_mean": 300.0}
    return {"meta": {"model": "py:evals.policy:ScriptedAssistantModel", "profile": "regressed", "trials": 5, "git": "abc1234"},
            "summary": {"suites": {"regression": suite}, "trials_with_violations": sum(t["violations"] for t in per_task.values())},
            "per_task": per_task, "trials": list(trials)}


BASE = _results({"time_new_york_en": _task(5), "save_note_basic": _task(5), "time_shanghai": _task(5)})
ARGS_FAIL = "current_time args ['{\"tz\": \"UTC\"}'] != {'tz': 'America/New_York'}"
CUR = _results(
    {"time_new_york_en": _task(0, first_failure=[ARGS_FAIL]), "save_note_basic": _task(4, violations=1),
     "time_shanghai": _task(5)},
    trials=[{"task": "time_new_york_en", "trial": i, "job_id": f"job-{i}", "passed": False, "failures": [ARGS_FAIL],
             "violations": [], "state": "succeeded", "attempts": 1, "model_calls": 2,
             "tool_calls": [["current_time", '{"tz": "UTC"}']], "final_output": "现在是 13:49（UTC）"} for i in range(5)]
    + [{"task": "save_note_basic", "trial": 0, "job_id": "job-s", "passed": False, "failures": [],
        "violations": ["claims to have saved but no effect was recorded"], "state": "succeeded", "attempts": 1,
        "model_calls": 1, "tool_calls": [], "final_output": "已保存"}])


def _report():
    problems = gate.check(BASE, CUR, 3, 0.02, 0.25, None)
    return {"passed": not problems, "problems": problems, "k": 3, "max_drop": 0.02}


def test_build_points_at_graders_trajectories_and_next_action():
    fu = followup.build(CUR, BASE, _report())
    by_task = {t["task"]: t for t in fu["tasks"]}
    assert set(by_task) == {"time_new_york_en", "save_note_basic"}
    ny = by_task["time_new_york_en"]
    assert ny["graders"] == [{"message": ARGS_FAIL, "trials": 5}]
    assert [t["job_id"] for t in ny["trajectories"]] == ["job-0", "job-1", "job-2"]       # capped at 3
    assert ny["task_definition"]["file"] == "evals/tasks/assistant.jsonl" and ny["task_definition"]["line"] >= 1
    assert "evals/policy.py" in ny["next_action"]
    sn = by_task["save_note_basic"]
    assert sn["reason"] == "violation" and "反作弊" in sn["next_action"]
    assert any("pass^3" in s["problem"] for s in fu["suite_level"])
    assert "--only time_new_york_en save_note_basic" in fu["repro"]


def test_main_writes_json_markdown_and_exec_plan(tmp_path):
    res, base, rep = tmp_path / "r.json", tmp_path / "b.json", tmp_path / "g.json"
    res.write_text(json.dumps(CUR)), base.write_text(json.dumps(BASE)), rep.write_text(json.dumps(_report()))
    out, plans = tmp_path / "fu", tmp_path / "plans"
    assert followup.main(["--results", str(res), "--baseline", str(base), "--gate-report", str(rep),
                          "--out-dir", str(out), "--exec-plan-dir", str(plans)]) == 0
    data = json.loads((out / "followup.json").read_text())
    assert data["gate"]["passed"] is False and len(data["tasks"]) == 2
    [plan] = plans.glob("*-eval-followup-abc1234.md")
    text = plan.read_text()
    assert text.startswith("---\n") and "status: active" in text and "## 进度" in text and "- [ ] `time_new_york_en`" in text
    assert "job `job-0`" in text and "## 验证" in text


def test_no_followup_when_gate_passes(tmp_path):
    res, base = tmp_path / "r.json", tmp_path / "b.json"
    res.write_text(json.dumps(BASE)), base.write_text(json.dumps(BASE))
    assert followup.main(["--results", str(res), "--baseline", str(base), "--out-dir", str(tmp_path / "fu")]) == 0
    assert not (tmp_path / "fu").exists()


def test_missing_results_is_reported(tmp_path):
    assert followup.main(["--results", str(tmp_path / "nope.json"), "--out-dir", str(tmp_path / "fu")]) == 2


def test_gate_report_json_keeps_exit_code(tmp_path):
    base, cur, rep = tmp_path / "b.json", tmp_path / "c.json", tmp_path / "g.json"
    base.write_text(json.dumps(BASE)), cur.write_text(json.dumps(CUR))
    assert gate.main(["--baseline", str(base), "--current", str(cur), "--k", "3", "--report-json", str(rep)]) == 1
    assert json.loads(rep.read_text())["passed"] is False
