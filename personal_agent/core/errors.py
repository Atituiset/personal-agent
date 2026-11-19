"""任务错误分类：决定 worker 在失败后走 retry_wait 还是 dead。"""


class JobError(Exception):
    kind = "error"
    retryable = True

    def to_json(self) -> dict:
        return {"kind": self.kind, "message": str(self)[:2000]}


class Retryable(JobError):
    kind = "retryable"


class Fatal(JobError):
    kind = "fatal"
    retryable = False


class LeaseLost(Exception):
    """fencing：当前 worker 已不是租约持有者，它的写入被拒绝。"""


class RateLimited(Retryable):
    """上游限流或额度用完；`retry_after` 秒后再试（worker 会把它作为最短退避）。"""
    kind = "rate_limited"

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after

    def to_json(self) -> dict:
        return {**super().to_json(), "retry_after": self.retry_after}
