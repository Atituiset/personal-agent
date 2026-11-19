"""配置只从环境变量和 .env 读取；.env 不提交到仓库。"""
import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    artifact_dir: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise SystemExit("DATABASE_URL is not set (see .env.example)")
        return cls(database_url=url, artifact_dir=os.environ.get("PA_ARTIFACT_DIR", "./var/artifacts"))
