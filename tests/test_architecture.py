"""分层约束（ARCHITECTURE.md）：在 pytest 里跑 scripts/check_architecture.py 的同一套规则，并确认它能抓到违规。"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_architecture.py"


@pytest.fixture()
def arch():
    spec = importlib.util.spec_from_file_location("check_architecture", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # dataclasses need the module to be importable by name
    spec.loader.exec_module(mod)
    return mod


def test_current_code_respects_layers(arch):
    violations = arch.run()
    assert violations == [], "\n" + "\n".join(map(str, violations))


def _fake_repo(tmp_path, files: dict[str, str]):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


@pytest.mark.parametrize("rel, src, rule, hint", [
    ("personal_agent/core/bad.py", "from personal_agent.queue.broker import PgBroker\n", "layer-direction", "Protocol"),
    ("personal_agent/tasks/bad.py", "from personal_agent.queue.broker import PgBroker\n", "layer-direction", "JobContext"),
    ("personal_agent/infra/bad.py", "from personal_agent.queue import broker\n", "layer-direction", "siblings"),
    ("personal_agent/queue/worker.py", "from personal_agent.api.app import create_app\n", "layer-direction", "move the shared code"),
    ("personal_agent/tasks/bad.py", "import psycopg\n", "third-party-layer", "wrap"),
    ("personal_agent/core/bad.py", "import yaml\n", "unknown-dependency", "THIRD_PARTY"),
    ("personal_agent/infra/bad.py", "from evals.policy import Policy\n", "no-test-imports", "move the shared helper"),
])
def test_violations_are_reported_with_fix(arch, tmp_path, monkeypatch, rel, src, rule, hint):
    root = _fake_repo(tmp_path, {rel: src})
    monkeypatch.setattr(arch, "ROOT", root)
    [v] = arch.check_file(root / rel)
    assert v.rule == rule and hint in v.fix and v.line == 1


def test_relative_imports_are_resolved(arch, tmp_path, monkeypatch):
    root = _fake_repo(tmp_path, {"personal_agent/infra/x.py": "from ..queue.broker import PgBroker\n"})
    monkeypatch.setattr(arch, "ROOT", root)
    [v] = arch.check_file(root / "personal_agent/infra/x.py")
    assert "personal_agent.queue.broker" in v.message


def test_every_exception_is_tracked(arch):
    tracker = (arch.ROOT / "docs/exec-plans/tech-debt-tracker.md").read_text(encoding="utf-8")
    for td in arch.EXCEPTIONS.values():
        assert f"| {td} |" in tracker
