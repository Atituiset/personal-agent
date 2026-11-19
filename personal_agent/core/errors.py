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
