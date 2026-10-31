"""Runtime configuration loaded from env and ~/.claudewebstream."""

from __future__ import annotations

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CWS_", env_file=".env", extra="ignore")

    home: Path = Path.home() / ".claudewebstream"
    host: str = "127.0.0.1"
    port: int = 8787
    secret_key: str | None = None  # server-level key; auto-generated if missing
    max_concurrent_streams: int = 4
    db_name: str = "claudewebstream.db"
    storage_dirname: str = "storage"
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.home / self.db_name

    @property
    def storage_path(self) -> Path:
        return self.home / self.storage_dirname

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.storage_path.mkdir(parents=True, exist_ok=True)


settings = Settings()