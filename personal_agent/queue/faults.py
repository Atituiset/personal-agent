"""故障注入（仅测试使用）：让 worker 在指定位置停住，方便测试从外部 kill -9 / SIGSTOP。

PA_FAULT_PAUSE_AT=before_persist|after_persist
PA_FAULT_DIR=<目录>：到达时写 <point>.<job_id>.reached（内容是 pid），直到出现 <dir>/release 才继续。
未设置这两个变量时没有任何效果。
"""
import asyncio
import os
from pathlib import Path


async def pause_point(point: str, job_id) -> None:
    if os.environ.get("PA_FAULT_PAUSE_AT") != point:
        return
    directory = Path(os.environ["PA_FAULT_DIR"])
    (directory / f"{point}.{job_id}.reached").write_text(str(os.getpid()))
    while not (directory / "release").exists():
        await asyncio.sleep(0.05)
