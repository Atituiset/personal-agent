"""任务类型 agent.run：用 OpenAI Agents SDK 跑一个带工具白名单的 Agent。

payload:
  instructions  str，系统指令（≤ 8000 字符）
  input         str，用户输入（≤ 32000 字符）
  tools         list[str]，允许使用的工具，只能从 TOOLS 里选；默认不给工具
  artifacts     {name: artifact_ref}，read_artifact 只能读这里列出的产物
  max_turns     int，默认 6，上限 PA_AGENT_MAX_TURNS_CAP（默认 12）
  timeout_s     float，可选，整个 Runner.run 的时间上限

队列语义：
- 整个 Runner.run 是一次 process 步骤；结果交给 worker 走 persist-then-ack。
  persist 之后崩溃，新 worker 直接用 receipt ACK，不会再跑 Runner。
- 有副作用的工具（save_note）通过 ctx.effect 记账：同一个 job 重跑时，相同参数的副作用不会执行第二次。
- max_turns、Runner 超时（同时受任务超时和 deadline_at 约束）、lease 丢失都会中止这次运行。
- 每次模型调用的 usage 记入 ctx.usage，worker 会写进 attempts.usage（失败的 attempt 也写）。
"""
from __future__ import annotations

import ast
import asyncio
import json
import operator
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from personal_agent.infra.agents_sdk import (AgentRunContext, LedgerHooks, build_agent_model, map_error,  # 先配置 tracing
                                             run_config, usage_totals)
from agents import Agent, ModelSettings, RunContextWrapper, Runner, function_tool  # noqa: E402

from personal_agent.core.context import fingerprint
from personal_agent.core.errors import Fatal, LeaseLost, Retryable

from . import TaskSpec, register

TASK_TIMEOUT_S = 300.0
DEFAULT_RUN_TIMEOUT_S = 120.0
MAX_ARTIFACT_CHARS = 16000


# ---------------------------------------------------------------- 工具（只读 + 一个本地副作用）
@function_tool
def current_time(ctx: RunContextWrapper[AgentRunContext], tz: str = "UTC") -> str:
    """返回当前时间（ISO 8601）。

    Args:
        tz: IANA 时区名，例如 UTC 或 Asia/Shanghai。
    """
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return f"unknown timezone: {tz}"
    return datetime.now(timezone.utc).astimezone(zone).isoformat(timespec="seconds")


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def safe_eval(expression: str):
    """只支持数字、括号和 + - * / // % **；不调用任何函数、不访问任何名字。"""
    if len(expression) > 200:
        raise ValueError("expression too long")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 10**6):
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        raise ValueError(f"unsupported syntax: {type(node).__name__}")
    return ev(ast.parse(expression, mode="eval"))


@function_tool
def calculate(ctx: RunContextWrapper[AgentRunContext], expression: str) -> str:
    """计算一个算术表达式，只支持数字、括号和 + - * / // % **。

    Args:
        expression: 例如 (3 + 4) * 12。
    """
    try:
        return str(safe_eval(expression))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
        return f"error: {exc}"


@function_tool
def read_artifact(ctx: RunContextWrapper[AgentRunContext], name: str) -> str:
    """读取本任务 payload.artifacts 里列出的一个文本产物（最多 16000 字符）。

    Args:
        name: payload.artifacts 里的名字。
    """
    rc = ctx.context
    ref = (rc.payload.get("artifacts") or {}).get(name)
    if not isinstance(ref, dict):
        return f"unknown artifact: {name}"
    data = rc.job_ctx.store.get(ref)               # 哈希校验失败会抛 IntegrityError
    return data.decode("utf-8", errors="replace")[:MAX_ARTIFACT_CHARS]


async def _save_note(ctx: RunContextWrapper[AgentRunContext], title: str, text: str) -> str:
    rc = ctx.context
    job = rc.job_ctx
    key = f"agent.save_note:{job.job_id}:{fingerprint({'title': title, 'text': text})}"

    async def do():
        ref = job.store.put_json({"job_id": str(job.job_id), "title": title[:200], "text": text[:8000]})
        return {"note_ref": ref, "attempt": job.job["attempt"], "pid": os.getpid(), "at": time.time()}
    receipt = await job.effect(key, do)          # 同一 job 相同参数只执行一次
    return json.dumps({"saved": True, "sha256": receipt["note_ref"]["sha256"], "attempt": receipt["attempt"]})


# failure_error_function=None：副作用工具出错（例如 effect 需要人工确认）直接中止 run，不交给模型
save_note = function_tool(_save_note, name_override="save_note", failure_error_function=None,
                          description_override="把一条笔记保存到本地产物库（副作用，按参数去重，只执行一次）。")

TOOLS = {t.name: t for t in (current_time, calculate, read_artifact, save_note)}


# ---------------------------------------------------------------- 任务
def _validate(payload: dict) -> tuple[str, str, list, int, float | None]:
    instructions, user_input = payload.get("instructions", ""), payload.get("input")
    if not isinstance(instructions, str) or len(instructions) > 8000:
        raise Fatal("payload.instructions must be a string (≤ 8000 chars)")
    if not isinstance(user_input, str) or not user_input.strip() or len(user_input) > 32000:
        raise Fatal("payload.input must be a non-empty string (≤ 32000 chars)")
    names = payload.get("tools", [])
    if not isinstance(names, list) or any(n not in TOOLS for n in names):
        raise Fatal(f"payload.tools must be a subset of {sorted(TOOLS)}")
    cap = int(os.environ.get("PA_AGENT_MAX_TURNS_CAP", "12"))
    max_turns = payload.get("max_turns", 6)
    if not isinstance(max_turns, int) or not 1 <= max_turns <= cap:
        raise Fatal(f"payload.max_turns must be an int in [1, {cap}]")
    timeout = payload.get("timeout_s")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise Fatal("payload.timeout_s must be a positive number")
    return instructions, user_input, [TOOLS[n] for n in dict.fromkeys(names)], max_turns, timeout


def run_budget(job: dict, requested: float | None) -> float:
    """Runner.run 的时间预算：不超过任务超时（留 5 秒给 persist/ACK）、payload.timeout_s 和 deadline_at。"""
    budget = min(requested or DEFAULT_RUN_TIMEOUT_S, TASK_TIMEOUT_S - 5)
    deadline = job.get("deadline_at")
    if deadline is not None:
        left = (deadline - datetime.now(timezone.utc)).total_seconds()
        if left <= 0:
            raise Fatal("deadline_at has passed")
        budget = min(budget, left)
    return budget


async def run_agent(ctx, payload: dict) -> dict:
    instructions, user_input, tools, max_turns, timeout = _validate(payload)
    budget = run_budget(ctx.job, timeout)
    model, model_name = build_agent_model()
    meta = {"job_id": str(ctx.job_id), "attempt": ctx.job["attempt"], "trace_id": ctx.job["trace_id"],
            "task_type": ctx.job["task_type"]}
    rc = AgentRunContext(job_ctx=ctx, payload=payload, meta=meta, model_name=model_name)
    agent = Agent(name="personal-agent", instructions=instructions or None, model=model, tools=tools,
                  model_settings=ModelSettings(timeout=min(budget, float(os.environ.get("PA_AGENT_MODEL_TIMEOUT", "60")))))
    run = asyncio.ensure_future(Runner.run(agent, user_input, context=rc, max_turns=max_turns, hooks=LedgerHooks(),
                                           run_config=run_config("personal-agent.agent.run", meta)))
    lost = asyncio.ensure_future(ctx.lost.wait())
    try:
        done, _ = await asyncio.wait({run, lost}, timeout=budget, return_when=asyncio.FIRST_COMPLETED)
        if run not in done:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            if lost in done:
                raise LeaseLost(f"lease lost during agent run for {ctx.job_id}")
            raise Retryable(f"agent run exceeded {budget:.1f}s budget")
        try:
            result = run.result()
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc) from exc
    finally:
        lost.cancel()

    tool_calls, outputs = [], []
    for item in result.new_items:
        if item.type == "tool_call_item":
            raw = item.raw_item
            tool_calls.append({"name": getattr(raw, "name", None), "arguments": getattr(raw, "arguments", None)})
        elif item.type == "tool_call_output_item":
            outputs.append(item.output)
    for call, out in zip(tool_calls, outputs):
        call["output"] = out
    return {"final_output": str(result.final_output), "model": model_name, "max_turns": max_turns,
            "model_calls": len(rc.calls), "tool_calls": tool_calls,
            "usage": usage_totals(result.context_wrapper.usage)}


register(TaskSpec(name="agent.run", handler=run_agent, pool="default", timeout_s=TASK_TIMEOUT_S))
