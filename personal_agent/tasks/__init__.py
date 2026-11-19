"""task_type → 任务规格（pool、超时、handler）。"""
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class TaskSpec:
    name: str
    handler: Callable[..., Awaitable[dict]]
    pool: str = "default"
    timeout_s: float = 120.0
    max_attempts: int = 5


TASKS: dict[str, TaskSpec] = {}


def register(spec: TaskSpec) -> TaskSpec:
    TASKS[spec.name] = spec
    return spec


from . import demo  # noqa: E402,F401  注册内置任务
