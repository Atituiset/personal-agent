"""scripts/doc_check.py：当前仓库的文档必须通过，并且检查能抓到断链、过期、缺 front matter 等问题。"""
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "doc_check.py"
FM = "---\nowner: \"@me\"\nlast_reviewed: {d}\ncode_paths: [{p}]\n---\n"


@pytest.fixture()
def dc():
    spec = importlib.util.spec_from_file_location("doc_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_repository_docs_pass(dc):
    rep = dc.run()
    assert rep.errors == [], "\n" + "\n".join(rep.errors)


def _repo(tmp_path, dc, extra: dict[str, str]):
    files = {"personal_agent/x.py": ""}
    for doc in dc.REQUIRED_IN_AGENTS:
        files[doc] = FM.format(d="2026-11-01", p="personal_agent/x.py") + "# doc\n"
    files["AGENTS.md"] = "\n".join(f"- [{d}]({d})" for d in dc.REQUIRED_IN_AGENTS) + "\n"
    files.update(extra)
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


TODAY = dt.date(2026, 11, 26)
PLAN = "---\nowner: me\nlast_reviewed: 2026-11-20\nstatus: {status}\ncode_paths: []\n---\n# plan\n"


def test_clean_fake_repo_passes(dc, tmp_path):
    rep = dc.run(_repo(tmp_path, dc, {}), today=TODAY)
    assert rep.errors == []


@pytest.mark.parametrize("extra, rule", [
    ({"README.md": "see [x](docs/missing.md)\n"}, "[links]"),
    ({"README.md": "code in `personal_agent/gone.py`\n"}, "[paths]"),
    ({"RELIABILITY.md": "# no front matter\n"}, "missing front matter"),
    ({"RELIABILITY.md": FM.format(d="2026-11-01", p="personal_agent/nope.py")}, "code_paths entry"),
    ({"RELIABILITY.md": FM.format(d="2025-01-01", p="personal_agent/x.py")}, "days ago"),
    ({"RELIABILITY.md": FM.format(d="2027-01-01", p="personal_agent/x.py")}, "in the future"),
    ({"AGENTS.md": "- [ARCHITECTURE.md](ARCHITECTURE.md)\n"}, "does not link"),
    ({"AGENTS.md": "x\n" * 200}, "lines (>"),
    ({"docs/exec-plans/completed/p.md": PLAN.format(status="active")}, "status is 'active'"),
])
def test_problems_are_reported_with_fix(dc, tmp_path, extra, rule):
    rep = dc.run(_repo(tmp_path, dc, extra), today=TODAY)
    hits = [e for e in rep.errors if rule in e]
    assert hits and all("fix:" in e for e in hits), rep.errors


def test_active_plans_may_mention_future_paths(dc, tmp_path):
    text = PLAN.format(status="active") + "will add `scripts/new_tool.py`\n"
    rep = dc.run(_repo(tmp_path, dc, {"docs/exec-plans/active/2026-11-20-x.md": text}), today=TODAY)
    assert rep.errors == []
