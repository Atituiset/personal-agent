"""真实模型冒烟：向本机 API 提交 N 个 demo.summarize，等待完成并打印耗时 / tokens / 输出。

前提：worker 以 PA_LLM=opencode（免费模型，需 OpenCode 额度未用完）或 PA_LLM=openai + OPENCODE_API_KEY 启动。
用法：.venv/bin/python scripts/opencode-live.py [N] [API]
"""
import json
import sys
import time
import uuid

import httpx

TEXTS = [
    "任务队列把提交和执行分开。API 只写库并立即返回 job_id，worker 在后台消费任务。",
    "租约加心跳可以发现崩溃的 worker；租约过期后 reaper 会把任务重新放回队列。",
    "先持久化结果再确认（persist-then-ack），崩溃后可以直接复用已保存的回执，不必再次调用模型。",
    "幂等键保证同一请求重复提交只产生一个任务；不同 payload 复用同一键会返回 409。",
    "重试使用带随机抖动的指数退避，超过最大次数的任务进入死信表等待人工处理。",
]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    api = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
    c = httpx.Client(base_url=api, timeout=10)
    jobs = []
    for text in TEXTS[:n]:
        r = c.post("/jobs", json={"task_type": "demo.summarize", "payload": {"text": text},
                                  "idempotency_key": f"live-{uuid.uuid4()}"})
        r.raise_for_status()
        jobs.append((r.json()["job_id"], time.monotonic()))
    for job_id, t0 in jobs:
        while True:
            d = c.get(f"/jobs/{job_id}", params={"include_result": "true"}).json()
            if d["state"] in ("succeeded", "dead", "cancelled") or (d["state"] == "retry_wait" and d.get("last_error")):
                break
            time.sleep(0.5)
        print(json.dumps({"job_id": job_id, "state": d["state"], "seconds": round(time.monotonic() - t0, 2),
                          "attempt": d["attempt"], "error": d.get("last_error"), "result": d.get("result")},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
