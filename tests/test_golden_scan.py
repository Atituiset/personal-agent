"""scripts/golden_scan.py：当前代码相对基线没有新偏差，并且每条规则都能抓到对应的写法。"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "golden_scan.py"


@pytest.fixture()
def gs():
    spec = importlib.util.spec_from_file_location("golden_scan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_no_new_drift_and_baseline_is_current(gs):
    new, known, fixed = gs.classify(gs.scan(), gs.load_baseline())
    assert new == [], "\n" + "\n".join(f.render() for f in new)
    assert fixed == [], f"baseline entries no longer occur, remove them: {fixed}"


def test_baseline_entries_are_tracked(gs):
    tracker = (gs.ROOT / "docs/exec-plans/tech-debt-tracker.md").read_text(encoding="utf-8")
    for b in gs.load_baseline():
        assert f"| {b['tracker']} |" in tracker


CASES = [
    ("personal_agent/tasks/x.py", "from . import TaskSpec, register\nasync def h(ctx, p):\n    return p\n"
     "register(TaskSpec(name='x', handler=h))\n", "GP1-validate-payload"),
    ("personal_agent/api/x.py", "@app.post('/x')\nasync def submit(body: dict):\n    return body\n", "GP1-typed-body"),
    ("personal_agent/tasks/x.py", "import os\nX = os.environ.get('A')\n", "GP2-config-via-providers"),
    ("personal_agent/queue/x.py", "import os\nX = os.getenv('A')\n", "GP2-config-via-providers"),
    ("personal_agent/queue/x.py", "print('hi')\n", "GP3-no-print"),
    ("personal_agent/queue/x.py", "log.info(f'job {j} done')\n", "GP3-structured-log"),
    ("evals/x.py", "from personal_agent.infra.fake_llm import _tokens\n", "GP4-no-private-import"),
    ("personal_agent/tasks/x.py", "import hashlib\n", "GP4-shared-hashing"),
    ("personal_agent/queue/x.py", "try:\n    f()\nexcept Exception:\n    pass\n", "GP5-no-silent-except"),
    ("personal_agent/infra/x.py", "import httpx\nhttpx.get('http://x')\n", "GP6-timeout"),
    ("evals/x.py", "import subprocess\nsubprocess.run(['git'])\n", "GP6-timeout"),
]


@pytest.mark.parametrize("rel, src, rule", CASES)
def test_each_rule_fires_with_fix(gs, tmp_path, rel, src, rule):
    p = tmp_path / rel
    p.parent.mkdir(parents=True)
    p.write_text(src)
    findings = gs.scan_file(p, tmp_path)
    assert [f.rule for f in findings] == [rule]
    assert gs.FIX[rule]


@pytest.mark.parametrize("rel, src", [
    ("personal_agent/tasks/x.py", "from . import TaskSpec, register\nfrom personal_agent.core.errors import Fatal\n"
     "def _validate(p):\n    if not p:\n        raise Fatal('bad')\nasync def h(ctx, p):\n    _validate(p)\n"
     "register(TaskSpec(name='x', handler=h))\n"),
    ("personal_agent/queue/x.py", "try:\n    f()\nexcept Exception:  # noqa: BLE001 best effort cleanup\n    pass\n"),
    ("personal_agent/queue/x.py", "try:\n    f()\nexcept Exception as exc:\n    log_event(log, 'x', error=repr(exc))\n"),
    ("personal_agent/queue/x.py", "try:\n    f()\nexcept KeyError:\n    pass\n"),
    ("personal_agent/infra/x.py", "import os, httpx\nX = os.environ['A']\nhttpx.AsyncClient(timeout=5)\n"),
    ("personal_agent/cli.py", "print('output')\n"),
])
def test_compliant_code_is_clean(gs, tmp_path, rel, src):
    p = tmp_path / rel
    p.parent.mkdir(parents=True)
    p.write_text(src)
    assert gs.scan_file(p, tmp_path) == []


def test_fail_on_new_exit_code_and_reports(gs, tmp_path):
    md, js = tmp_path / "r.md", tmp_path / "r.json"
    assert gs.main(["--fail-on-new", "--markdown", str(md), "--json", str(js)]) == 0
    assert "Golden principles drift report" in md.read_text() and json.loads(js.read_text())["new"] == []
    empty = tmp_path / "empty.json"
    empty.write_text('{"accepted": []}')
    assert gs.main(["--fail-on-new", "--baseline", str(empty)]) == 1
