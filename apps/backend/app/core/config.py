"""Application settings loaded from environment variables."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# apps/backend. 상대 경로가 실행 위치에 따라 달라지지 않도록 기본값을 여기 기준으로 잡습니다.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_env: str
    log_level: str
    workflows_db_path: str


def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "alpha-backend"),
        app_env=os.getenv("APP_ENV", "local"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        workflows_db_path=os.getenv("WORKFLOWS_DB_PATH")
        or str(_BACKEND_ROOT / ".data" / "workflows.sqlite3"),
    )


def data_dir() -> Path:
    """Where the desktop app keeps its files on this PC. Created with owner-only access."""
    configured = os.getenv("ALPHA_DATA_DIR")
    if configured:
        path = Path(configured)
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "Alpha"
    elif sys.platform == "win32":
        path = Path(os.environ["APPDATA"]) / "Alpha"
    else:
        path = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "alpha"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path