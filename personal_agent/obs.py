"""横切关注点：结构化日志（providers 层，见 ARCHITECTURE.md）。

所有运行时事件都用 `log_event(logger, "event_name", **fields)` 记录，一行一个 JSON 对象，字段名稳定，
方便用 `scripts/logq.py` 或 `jq` 直接查询，而不是对自由文本做正则。

PA_LOG_FORMAT：
- text（默认）：`<时间> <logger> {"event": ...}`，和以前的输出一致；
- json：整行就是一个 JSON 对象 `{"ts", "level", "logger", "event", ...}`（`make dev-up` 用这个）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    """记一条结构化事件。fields 里的值要能 JSON 序列化；不能的会被 str()。不要放密钥和 prompt 原文。"""
    logger.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


class JsonLineFormatter(logging.Formatter):
    """把一条日志变成一行 JSON。消息本身是 JSON 对象（log_event 产生的）时，字段合并到顶层。"""

    def format(self, record: logging.LogRecord) -> str:
        base = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname.lower(), "logger": record.name}
        msg = record.getMessage()
        try:
            parsed = json.loads(msg)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            base.update(parsed)
        else:
            base["event"] = "log"
            base["msg"] = msg
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False, default=str)


def setup_logging(fmt: str | None = None, level: int = logging.INFO, stream=None) -> str:
    """配置根 logger。返回实际使用的格式（text / json）。"""
    fmt = (fmt or os.environ.get("PA_LOG_FORMAT") or "text").lower()
    handler = logging.StreamHandler(stream or sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonLineFormatter())
    else:
        fmt = "text"
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    return fmt
