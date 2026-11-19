"""入口 API：POST /jobs 只写库并返回 job_id（提交与执行分离）；GET /jobs/{id} 查状态。"""
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from personal_agent.config import Settings
from personal_agent.core.storage import ArtifactStore
from personal_agent.queue.broker import PgBroker
from personal_agent.tasks import TASKS

MAX_INLINE_PAYLOAD = 64 * 1024


class SubmitJob(BaseModel):
    task_type: str
    payload: dict = Field(default_factory=dict, description="小于 64 KiB，写入 Artifact Store，消息里只放引用")
    idempotency_key: str | None = Field(default=None, max_length=512)
    priority: int = Field(default=5, ge=0, le=9)
    user_id: str = "local"
    deadline_at: datetime | None = None
    parent_job_id: uuid.UUID | None = None


def _public(job: dict) -> dict:
    keys = ("job_id", "task_type", "state", "attempt", "max_attempts", "priority", "idempotency_key",
            "trace_id", "payload_ref", "result_ref", "last_error", "enqueued_at", "leased_at", "finished_at",
            "parent_job_id", "deadline_at")
    return {k: (str(job[k]) if isinstance(job[k], uuid.UUID) else job[k]) for k in keys}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    state = {}

    @asynccontextmanager
    async def lifespan(app):
        state["broker"] = await PgBroker(settings.database_url, min_size=2, max_size=10).open()
        state["store"] = ArtifactStore(settings.artifact_dir)
        yield
        await state["broker"].close()

    app = FastAPI(title="personal-agent", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/jobs", status_code=202)
    async def submit(req: SubmitJob):
        spec = TASKS.get(req.task_type)
        if spec is None:
            raise HTTPException(422, f"unknown task_type {req.task_type!r}")
        store: ArtifactStore = state["store"]
        ref = store.put_json(req.payload)
        if ref["size"] > MAX_INLINE_PAYLOAD:
            raise HTTPException(413, "payload too large for inline submit")
        key = req.idempotency_key or f"{req.task_type}:{uuid.uuid4()}"   # 没给就不去重
        job, dedup = await state["broker"].enqueue(
            task_type=req.task_type, payload_ref=ref, idempotency_key=key, pool=spec.pool,
            priority=req.priority, user_id=req.user_id, deadline_at=req.deadline_at,
            parent_job_id=req.parent_job_id, max_attempts=spec.max_attempts)
        if dedup and job["payload_ref"]["sha256"] != ref["sha256"]:
            raise HTTPException(409, {"detail": "idempotency_key reused with a different payload",
                                      "job_id": str(job["job_id"])})
        return {"job_id": str(job["job_id"]), "state": job["state"], "deduplicated": dedup,
                "trace_id": job["trace_id"]}

    @app.get("/jobs/{job_id}")
    async def status(job_id: uuid.UUID, include_result: bool = False):
        job = await state["broker"].get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        body = _public(job)
        if include_result and job["result_ref"]:
            body["result"] = state["store"].get_json(job["result_ref"])
        return body

    return app
