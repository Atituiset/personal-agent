"""OpenCode CLI 适配器的离线测试。

- free-limit：用 2026-11-19 在本机真实调用 `opencode run` 录下的输出（tests/fixtures/opencode-free-limit.jsonl）。
- 成功路径：用假的 opencode 可执行文件输出 text / tokens 事件。由于录制时免费额度已用完，
  成功事件的字段形状是按 security-agent 的 OpenCode 适配器（type=text, part.text）假设的，尚未用真实输出核对。
"""
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from personal_agent.core.errors import RateLimited
from personal_agent.core.executor import LLMRequest
from personal_agent.infra.opencode_cli import OpenCodeCLI, classify, parse_events

FIXTURE = Path(__file__).with_name("fixtures") / "opencode-free-limit.jsonl"


def test_recorded_free_limit_maps_to_rate_limited():
    _, text, usage, error = parse_events(FIXTURE.read_text())
    assert text == "" and usage is None and error["status"] == 429
    err = classify(error)
    assert isinstance(err, RateLimited) and err.retryable and err.retry_after > 3600


def _fake_binary(tmp_path, events, code=0):
    script = tmp_path / "opencode"
    lines = "\n".join(json.dumps(e) for e in events)
    script.write_text(f"#!{sys.executable}\nimport sys, os\n"
                      f"open(os.environ['ARGS_OUT'], 'w').write(repr(sys.argv[1:]) + '|' + os.getcwd())\n"
                      f"print({lines!r})\nsys.exit({code})\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_success_text_and_usage(tmp_path, monkeypatch):
    events = [{"type": "step_start", "part": {"type": "step-start"}},
              {"type": "text", "part": {"type": "text", "text": "队列把提交和执行分开。"}},
              {"type": "step_finish", "part": {"type": "step-finish", "tokens": {"input": 30, "output": 9}}}]
    binary = _fake_binary(tmp_path, events)
    monkeypatch.setenv("ARGS_OUT", str(tmp_path / "args"))
    monkeypatch.setenv("OPENCODE_SOMETHING", "should-not-leak")
    result = asyncio.run(OpenCodeCLI(str(binary), "opencode/deepseek-v4.1-flash-free").complete(
        LLMRequest(prompt="原文", model="fake-summarizer-v1")))
    assert result.text == "队列把提交和执行分开。"
    assert result.usage == {"input_tokens": 30, "output_tokens": 9}
    assert result.model == "opencode/deepseek-v4.1-flash-free" and result.cost_usd is None
    args, cwd = (tmp_path / "args").read_text().split("|")
    assert "'--model', 'opencode/deepseek-v4.1-flash-free'" in args and "pa-opencode-" in cwd


def test_error_event_raises(tmp_path, monkeypatch):
    binary = _fake_binary(tmp_path, [json.loads(l) for l in FIXTURE.read_text().splitlines()])
    monkeypatch.setenv("ARGS_OUT", str(tmp_path / "args"))
    with pytest.raises(RateLimited):
        asyncio.run(OpenCodeCLI(str(binary)).complete(LLMRequest(prompt="x")))
