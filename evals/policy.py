"""离线评测用的脚本策略：一个按规则做决定的 SDK `Model`，替代真实大模型跑完整条链路。

它不是智能体能力的代表。它的作用是让评测在没有 API key 的环境里（本机、CI）
也能走一遍真实路径：worker 领任务 → Agents SDK Runner → 工具白名单 → effects 账本 →
persist-then-ack。分数衡量的是「这套系统 + 这个策略」，换成真实模型后要重新建基线。

接入方式：PA_AGENT_MODEL=py:evals.policy:ScriptedAssistantModel

PA_EVAL_POLICY 选择行为档案（用来演示评测和门禁，不对应任何真实模型）：
  baseline   正常规则
  regressed  模拟「改了提示词后英文城市名不再识别」：New York / London 退回 UTC
  cheater    不调用工具，直接声称「已保存」或者凭空给出答案
  noisy      baseline + 以 PA_EVAL_SLIP 的概率（默认 0.15）第一轮就不调工具、直接瞎答，
             用来演示 pass@k 和 pass^k 的差别；随机数由 PA_EVAL_SEED、输入和出现次数决定
"""
from __future__ import annotations

import json
import os
import random
import re
from collections import Counter

from agents.items import ModelResponse
from agents.models.interface import Model, ModelTracing
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall

from personal_agent.infra.agents_sdk import _message, _user_text
from personal_agent.infra.fake_llm import _tokens

CITY_TZ = [  # (关键词, IANA 时区, 是否英文名)
    ("上海", "Asia/Shanghai", False), ("北京", "Asia/Shanghai", False), ("东京", "Asia/Tokyo", False),
    ("纽约", "America/New_York", False), ("伦敦", "Europe/London", False), ("柏林", "Europe/Berlin", False),
    ("洛杉矶", "America/Los_Angeles", False), ("New York", "America/New_York", True),
    ("London", "Europe/London", True), ("Tokyo", "Asia/Tokyo", True),
]
ARTIFACT_WORDS = [("会议纪要", "meeting_notes"), ("纪要", "meeting_notes"), ("账单", "bill"),
                  ("行程", "itinerary"), ("待办", "todo"), ("供应商通知", "vendor_notice"), ("通知", "vendor_notice")]
TZ_IN_TEXT = re.compile(r"\b([A-Z][a-z]+/[A-Za-z_]+)\b")
EXPR = re.compile(r"[\d\s\.\+\-\*/\(\)%]*\d[\d\s\.\+\-\*/\(\)%]*[\+\-\*/%][\d\s\.\+\-\*/\(\)%]*\d[\d\s\.\)]*")
_SEEN: Counter = Counter()


def _history(items) -> list[dict]:
    """之前各轮的工具调用和结果，按调用顺序。"""
    if isinstance(items, str):
        return []
    calls = [i for i in items if isinstance(i, dict) and i.get("type") == "function_call"]
    outs = {i.get("call_id"): i.get("output") for i in items
            if isinstance(i, dict) and i.get("type") == "function_call_output"}
    hist = []
    for c in calls:
        try:
            args = json.loads(c.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        hist.append({"name": c.get("name"), "args": args, "output": outs.get(c.get("call_id"))})
    return hist


def _fmt(x: str) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{v:.2f}".rstrip("0").rstrip(".")


class Policy:
    def __init__(self, text: str, instructions: str, available: set[str], profile: str):
        self.t, self.ins, self.avail, self.profile = text, instructions or "", available, profile
        t = text
        self.loop = bool(re.search(r"一直.*(直到|等到)|keep checking", t))
        self.delete = bool(re.search(r"删除|删掉|\bdelete\b", t, re.I))
        self.convert = bool(re.search(r"(纽约|New York).*\d+\s*点.*北京时间", t))
        self.artifact = self._artifact()
        self.save = bool(re.search(r"记一下|记下|存成|保存|存一下|\bsave\b", t, re.I)) and not self.delete
        self.calc = self._calc_kind()
        self.time = bool(re.search(r"几点|what time|现在.*时间", t, re.I)) and not (self.loop or self.convert)
        self.thanks = bool(re.search(r"^(谢谢|你好|多谢|thanks|hi)\b", t.strip(), re.I))
        self.summarize = bool(re.search(r"总结|概括|summari[sz]e", t, re.I)) and not self.artifact

    # ---------- 理解请求 ----------
    def _artifact(self):
        m = re.search(r"读(?:一下|取)?\s*([A-Za-z_][\w]*)\s*(?:文件)?", self.t)
        if m:
            return m.group(1)
        for word, name in ARTIFACT_WORDS:
            if word in self.t:
                return name
        return None

    def _calc_kind(self):
        t = self.t
        if self.artifact and re.search(r"总和|合计|加起来|一共|总共", t):
            return "sum_artifact"
        if re.search(r"AA|平摊|平分", t):
            return "split"
        if "复利" in t:
            return "compound"
        if "小费" in t or re.search(r"\btip\b", t, re.I):
            return "tip"
        if re.search(r"扣(掉|除)?\s*\d+(\.\d+)?\s*%", t):
            return "deduct"
        if re.search(r"算|计算|calculate|多少", t, re.I) and EXPR.search(t):
            return "expr"
        return None

    def _nums(self):
        return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", self.t)]

    def _tz(self):
        for word, tz, english in CITY_TZ:
            if word in self.t:
                if english and self.profile == "regressed":
                    return "UTC"
                return tz
        m = TZ_IN_TEXT.search(self.ins)
        return m.group(1) if m else "UTC"

    def _expression(self, hist):
        t, n = self.t, self._nums()
        if self.calc == "expr":
            return EXPR.search(t).group(0).strip()
        if self.calc == "split":
            total = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块)", t)
            people = re.search(r"(\d+)\s*(?:个人|人)", t)
            return f"{total.group(1)} / {people.group(1)}" if total and people else None
        if self.calc == "compound":
            p = re.search(r"本金\s*(\d+(?:\.\d+)?)", t)
            r = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
            y = re.search(r"(\d+)\s*年", t)
            return f"{p.group(1)} * (1 + {r.group(1)} / 100) ** {y.group(1)}" if p and r and y else None
        if self.calc == "tip":
            base = re.search(r"\$?\s*(\d+(?:\.\d+)?)", t)
            pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
            return f"{base.group(1)} * (1 + {pct.group(1)} / 100)" if base and pct else None
        if self.calc == "deduct":
            pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t).group(1)
            base = next((x for x in n if _fmt(str(x)) != pct), None)
            return f"{_fmt(str(base))} * (1 - {pct} / 100)" if base is not None else None
        if self.calc == "sum_artifact":
            doc = next((h["output"] for h in hist if h["name"] == "read_artifact"), "") or ""
            amounts = re.findall(r"\s(\d+\.\d+|\d+)$", doc, re.M)
            return " + ".join(amounts) if amounts else None
        return None

    def _note(self, hist):
        t = self.t
        title = "购物清单" if "购物" in t else ("账单合计" if self.calc == "sum_artifact" else
                                              ("会议行动项" if "行动项" in t else "提醒"))
        if self.artifact:
            doc = next((h["output"] for h in hist if h["name"] == "read_artifact"), "") or ""
            if "行动项" in t:
                text = "\n".join(l for l in doc.splitlines() if l.startswith("行动项"))
            else:
                calc = next((h["output"] for h in hist if h["name"] == "calculate"), None)
                text = f"合计 {_fmt(calc)} 元" if calc is not None else doc[:200]
            return {"title": title, "text": text}
        m = re.search(r"[“\"「](.+?)[”\"」]", t) or re.search(r"[:：]\s*(.+)$", t)
        text = m.group(1).strip() if m else t
        return {"title": title, "text": text}

    # ---------- 决策：下一步调用哪个工具，或者给出最终回答 ----------
    def steps(self):
        s = []
        if self.loop:
            return ["loop"]
        if self.artifact:
            s.append("read_artifact")
        if self.calc:
            s.append("calculate")
        if self.save:
            s.append("save_note")
            if re.search(r"再保存一次|保存两次|再存一次", self.t):
                s.append("save_note")          # 用户要求重复保存：同参数的副作用应只执行一次
        if self.time:
            s.append("current_time")
        if self.convert:
            s += ["current_time", "current_time"]
        return s

    def next(self, hist):
        if self.delete:
            return None
        if self.loop:
            return ("current_time", {"tz": "UTC"}) if "current_time" in self.avail else None
        plan = self.steps()
        i = len(hist)
        while i < len(plan):
            name = plan[i]
            if name not in self.avail:
                return None                     # 需要的工具没有授权：不调用，直接说明
            if name == "read_artifact":
                return name, {"name": self.artifact}
            if name == "calculate":
                expr = self._expression(hist)
                if hist and hist[-1]["name"] == "read_artifact" and str(hist[-1]["output"]).startswith("unknown"):
                    return None
                return (name, {"expression": expr}) if expr else None
            if name == "save_note":
                if any(h["name"] == "read_artifact" and str(h["output"]).startswith("unknown") for h in hist):
                    return None
                return name, self._note(hist)
            if name == "current_time":
                if self.convert:                 # 能力不足的地方：只会各查一次当前时间，不会换算
                    return name, {"tz": "America/New_York" if i == len(plan) - 2 else "Asia/Shanghai"}
                return name, {"tz": self._tz()}
            i += 1
        return None

    def final(self, hist) -> str:
        if self.delete:
            return "我没有删除笔记的工具，没法替你删除；需要的话请手动处理。"
        missing = [n for n in self.steps() if n not in self.avail and n != "loop"]
        if missing:
            return f"这次没有授权使用 {', '.join(dict.fromkeys(missing))} 工具，所以没有执行；需要的话请开放这个工具。"
        parts = []
        by = {h["name"]: h for h in hist}
        if "read_artifact" in by:
            out = str(by["read_artifact"]["output"])
            if out.startswith("unknown artifact"):
                return f"没有找到名为 {self.artifact} 的文件，这次只能读取任务里列出的产物。"
            if not self.calc and not ("行动项" in self.t and self.save):
                parts.append(self._answer_from(out))
        if "calculate" in by:
            out = str(by["calculate"]["output"])
            if out.startswith("error"):
                return f"这个表达式没法计算（{out}），我没有执行它。"
            v = _fmt(out)
            parts.append({"split": f"每人 {v} 元。", "compound": f"到期本息合计约 {v} 元。",
                          "tip": f"含小费一共 {v}。", "deduct": f"扣除后是 {v}。",
                          "sum_artifact": f"账单合计 {v} 元。"}.get(self.calc, f"结果是 {v}。"))
        if "save_note" in by:
            try:
                ok = json.loads(by["save_note"]["output"]).get("saved")
            except (TypeError, json.JSONDecodeError):
                ok = False
            note = by["save_note"]["args"]
            parts.append(f"已保存笔记《{note.get('title')}》。" if ok else "保存失败。")
        times = [h for h in hist if h["name"] == "current_time"]
        if times and not self.convert:
            out = str(times[-1]["output"])
            m = re.search(r"T(\d{2}:\d{2})", out)
            parts.append(f"现在是 {m.group(1)}（{times[-1]['args'].get('tz')}，{out}）。" if m else out)
        if self.convert and times:
            m = re.search(r"T(\d{2}:\d{2})", str(times[-1]["output"]))
            parts.append(f"北京时间现在是 {m.group(1) if m else '?'}。")
        if parts:
            return " ".join(parts)
        if self.thanks:
            return "不客气，有事随时找我。"
        if self.summarize:
            body = re.split(r"[:：]", self.t, 1)[-1].strip()
            first = re.split(r"(?<=[。！？.!?])", body)[0].strip()
            return first
        return "我不太确定你需要什么，可以说得具体一点吗？"

    def _answer_from(self, doc: str) -> str:
        t, lines = self.t, [l.strip() for l in doc.splitlines() if l.strip()]
        if re.search(r"航班", t):
            return "航班：" + "、".join(re.findall(r"[A-Z]{2}\d{3,4}", doc))
        if re.search(r"未完成|没做|还没", t):
            return "未完成：" + "；".join(l[6:] for l in lines if l.startswith("- [ ]"))
        for key in ("下次会议", "电费", "水费", "燃气费", "宽带", "物业费", "配送费", "酒店"):
            if key in t:
                hit = [l for l in lines if key in l]
                if hit:
                    return "；".join(hit)
        return "；".join(lines[:2])


class ScriptedAssistantModel(Model):
    name = "scripted-assistant-v1"

    def __init__(self):
        self.profile = os.environ.get("PA_EVAL_POLICY", "baseline")
        if self.profile not in {"baseline", "regressed", "cheater", "noisy"}:
            raise ValueError(f"unknown PA_EVAL_POLICY {self.profile}")
        self.slip = float(os.environ.get("PA_EVAL_SLIP", "0.15"))
        self.seed = os.environ.get("PA_EVAL_SEED", "0")
        self.delay = float(os.environ.get("PA_FAKE_LLM_DELAY_MS", "20")) / 1000

    def _cheat_or_slip(self, policy: Policy, text: str, first_turn: bool) -> str | None:
        if not first_turn:
            return None
        if self.profile == "cheater" and (policy.save or policy.calc):
            return "好的，已保存。" if policy.save else "结果是 100。"
        if self.profile == "noisy" and policy.steps():
            _SEEN[text] += 1
            rng = random.Random(f"{self.seed}|{text}|{_SEEN[text]}")
            if rng.random() < self.slip:
                return "我估计大概是这样，不用查了。"
        return None

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing: ModelTracing, *, previous_response_id=None, conversation_id=None, prompt=None,
                           **kwargs) -> ModelResponse:
        import asyncio
        if self.delay:
            await asyncio.sleep(self.delay)
        text = _user_text(input)
        hist = _history(input)
        policy = Policy(text, system_instructions or "", {t.name for t in tools}, self.profile)
        shortcut = self._cheat_or_slip(policy, text, first_turn=not hist)
        step = None if shortcut is not None else policy.next(hist)
        if step is not None:
            name, args = step
            n = len(hist)
            items = [ResponseFunctionToolCall(type="function_call", call_id=f"call_{n}", name=name,
                                              arguments=json.dumps(args, ensure_ascii=False), id=f"fc_{n}")]
        else:
            items = [_message(shortcut if shortcut is not None else policy.final(hist), "msg_final")]
        in_tokens = _tokens((system_instructions or "") + json.dumps(input, ensure_ascii=False, default=str))
        out_tokens = _tokens(json.dumps([i.model_dump() for i in items], ensure_ascii=False))
        return ModelResponse(output=items, usage=Usage(requests=1, input_tokens=in_tokens, output_tokens=out_tokens,
                                                       total_tokens=in_tokens + out_tokens), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError("ScriptedAssistantModel does not stream")
        yield  # pragma: no cover
