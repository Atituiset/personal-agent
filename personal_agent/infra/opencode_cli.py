"""OpenCode CLI 后端：通过 `opencode run --format json` 调用模型，包括 OpenCode 的免费模型。

为什么走 CLI 而不是 HTTP：OpenCode 官方文档写明免费模型只能在 OpenCode 内部使用，
其他客户端（即使带 Console API key）会收到 HTTP 403
（https://opencode.ai/docs/console/harnesses/）。所以接免费模型只能调用官方 CLI。

隔离做法参考 security-agent 的 `security_agent/infra/opencode.py`：每次调用使用一次性的
XDG 目录和工作目录、关闭项目配置、权限全部 deny（摘要任务不需要任何工具），不读写用户自己的
OpenCode 配置。若设置了 OPENCODE_API_KEY 则透传，用于付费模型；免费模型不需要。

环境变量：
  PA_OPENCODE_BIN    opencode 可执行文件（默认 opencode）
  PA_OPENCODE_MODEL  默认 opencode/deepseek-v4.1-flash-free
"""
import asyncio
import json
import os
import signal
import tempfile
import time
from pathlib import Path

from personal_agent.core.errors import Fatal, RateLimited, Retryable
from personal_agent.core.executor import LLMRequest, LLMResult

DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"
CONFIG = {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
          "permission": {"*": "deny"}}


def parse_events(stdout: str) -> tuple[list[dict], str, dict | None, dict | None]:
    """返回 (events, 文本, usage, error)。usage 只在事件里真的带了 token 数时才有值。"""
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    texts = [e["part"]["text"] for e in events
             if e.get("type") == "text" and isinstance(e.get("part"), dict) and e["part"].get("text")]
    error = next((e.get("error") or {} for e in events if e.get("type") == "error"), None)
    usage = None
    for e in events:                    # step_finish 一类事件里的 tokens（字段名以实际输出为准，未知则为 None）
        tokens = (e.get("part") or {}).get("tokens") if isinstance(e.get("part"), dict) else None
        if isinstance(tokens, dict) and ("input" in tokens or "output" in tokens):
            usage = {"input_tokens": (usage or {}).get("input_tokens", 0) + int(tokens.get("input") or 0),
                     "output_tokens": (usage or {}).get("output_tokens", 0) + int(tokens.get("output") or 0)}
    return events, "".join(texts).strip(), usage, error


def classify(error: dict) -> Exception:
    status = error.get("status")
    if status == 429 or error.get("type") == "provider.free-limit":
        after = error.get("retryAfterMs")
        return RateLimited(f"OpenCode {error.get('type')}: {error.get('message', '')[:300]}",
                           retry_after=after / 1000 if isinstance(after, (int, float)) else None)
    if isinstance(status, int) and 400 <= status < 500:
        return Fatal(f"OpenCode HTTP {status} {error.get('type')}: {error.get('message', '')[:300]}")
    return Retryable(f"OpenCode error {error.get('type')}: {error.get('message', '')[:300]}")


class OpenCodeCLI:
    def __init__(self, binary: str | None = None, model: str | None = None):
        self.binary = binary or os.environ.get("PA_OPENCODE_BIN", "opencode")
        self.model = model or os.environ.get("PA_OPENCODE_MODEL", DEFAULT_MODEL)

    async def complete(self, request: LLMRequest) -> LLMResult:
        model = request.model if "/" in request.model else self.model
        prompt = (f"{request.system}\n\n" if request.system else "") + \
                 f"只输出结果本身，不要调用任何工具，不超过 {request.max_output_tokens} 个 token。\n\n{request.prompt}"
        with tempfile.TemporaryDirectory(prefix="pa-opencode-") as temp:
            home = Path(temp)
            cwd = home / "workspace"
            cwd.mkdir()
            env = {k: v for k, v in os.environ.items() if not k.startswith("OPENCODE_")}
            if os.environ.get("OPENCODE_API_KEY"):
                env["OPENCODE_API_KEY"] = os.environ["OPENCODE_API_KEY"]
            env.update(XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
                       XDG_CACHE_HOME=str(home / "cache"), XDG_STATE_HOME=str(home / "state"),
                       OPENCODE_CONFIG_CONTENT=json.dumps(CONFIG), OPENCODE_DISABLE_PROJECT_CONFIG="true")
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                self.binary, "run", "--standalone", "--format", "json", "--model", model, prompt,
                cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), request.timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise Retryable(f"OpenCode timed out after {request.timeout}s")
        _, text, usage, error = parse_events(stdout.decode("utf-8", "replace"))
        if error is not None:
            raise classify(error)
        if proc.returncode:
            raise Retryable(f"OpenCode exited {proc.returncode}: {stderr.decode('utf-8', 'replace')[-300:]}")
        if not text:
            raise Retryable("OpenCode returned no text")
        return LLMResult(text=text, model=model, usage=usage, cost_usd=None,)
