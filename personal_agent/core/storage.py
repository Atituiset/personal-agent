"""内容寻址的 Artifact Store 与持久化原语。

出处：`atomic_json`、`digest`、`seal`、`verify` 改编自同一作者的
https://github.com/Atituiset/security-agent 中的 `security_agent/core/storage.py`
（快照 b5bc339）。该仓库未附许可证文件，由原作者在本仓库以 MIT 许可证重新发布。
改动：增加 `atomic_write`（任意字节）、`ArtifactStore`（sha256 内容寻址 + 读取时校验）。
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data: bytes) -> None:
    """写临时文件 + fsync + os.replace + fsync 目录（同 security-agent atomic_json 的做法）。"""
    path = Path(path)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _fsync_dir(path.parent)


def atomic_json(path, value) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode())


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def seal(root, relative) -> dict:
    return {"path": relative, "sha256": digest((Path(root) / relative).read_bytes())}


def verify(root, record) -> None:
    path = Path(root) / record["path"]
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("artifact path escapes root")
    if digest(path.read_bytes()) != record["sha256"]:
        raise ValueError(f"artifact changed: {record['path']}")


class IntegrityError(ValueError):
    """产物缺失或哈希不匹配；对任务来说是不可重试错误。"""


class ArtifactStore:
    """sha256 内容寻址：同样的内容总是同一个引用，重复写入天然幂等。

    引用格式 `{"uri": "artifact://sha256/<hex>", "sha256": "<hex>", "size": n}`，
    即 security-agent `seal()` 的 `{path, sha256}` 推广成可跨进程传递的 payload_ref。
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha: str) -> Path:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise IntegrityError("invalid sha256")
        return self.root / "sha256" / sha[:2] / sha[2:]

    def put(self, data: bytes) -> dict:
        sha = digest(data)
        path = self._path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, data)
        return {"uri": f"artifact://sha256/{sha}", "sha256": sha, "size": len(data)}

    def put_json(self, value) -> dict:
        return self.put(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())

    def get(self, ref: dict) -> bytes:
        sha = ref.get("sha256", "")
        if ref.get("uri") != f"artifact://sha256/{sha}":
            raise IntegrityError("unsupported artifact uri")
        path = self._path(sha)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise IntegrityError(f"artifact missing: {sha}") from exc
        if digest(data) != sha:
            raise IntegrityError(f"artifact changed: {sha}")
        return data

    def get_json(self, ref: dict):
        return json.loads(self.get(ref))

    def exists(self, ref: dict) -> bool:
        try:
            self.get(ref)
            return True
        except IntegrityError:
            return False
