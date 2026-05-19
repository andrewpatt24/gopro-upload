from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("config.yaml")


def normalize_drive_folder_id(folder_id: str) -> str:
    """Strip URL junk (?lfhs=2) or paths accidentally pasted from Drive URLs."""
    fid = folder_id.strip().strip('"').strip("'")
    if "drive.google.com" in fid:
        if "/folders/" in fid:
            fid = fid.split("/folders/", 1)[1]
        elif "id=" in fid:
            fid = fid.split("id=", 1)[1]
    fid = fid.split("?")[0].split("&")[0].strip("/")
    return fid


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOPRO_UPLOAD_", extra="ignore")

    drive_folder_id: str = ""
    chunk_size_mb: int = 16
    db_path: str = "data/migration.db"
    reports_dir: str = "reports"

    gopro_access_token: str = Field(default="", validation_alias="GOPRO_ACCESS_TOKEN")
    gopro_user_id: str = Field(default="", validation_alias="GOPRO_USER_ID")

    google_credentials_path: str = "credentials.json"
    google_token_path: str = "~/.config/gopro-upload/google_token.json"
    google_scopes: list[str] = Field(
        default_factory=lambda: ["https://www.googleapis.com/auth/drive.file"]
    )

    max_retries: int = 5
    stale_transfer_minutes: int = 30
    per_page: int = 30

    @property
    def chunk_size_bytes(self) -> int:
        # Round down to multiple of 256 KB (Drive requirement).
        raw = self.chunk_size_mb * 1024 * 1024
        unit = 256 * 1024
        return (raw // unit) * unit or unit

    @property
    def google_token_path_expanded(self) -> Path:
        return Path(self.google_token_path).expanduser()

    @property
    def db_path_expanded(self) -> Path:
        return Path(self.db_path)

    @property
    def reports_dir_expanded(self) -> Path:
        return Path(self.reports_dir)


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        with path.open() as f:
            data = yaml.safe_load(f) or {}
    filtered = {k: v for k, v in data.items() if v is not None and v != ""}
    settings = Settings(**filtered)
    # Explicit env overrides (no GOPRO_UPLOAD_ prefix required for GoPro tokens).
    if token := os.environ.get("GOPRO_ACCESS_TOKEN"):
        settings.gopro_access_token = token
    if uid := os.environ.get("GOPRO_USER_ID"):
        settings.gopro_user_id = uid
    if settings.drive_folder_id:
        settings.drive_folder_id = normalize_drive_folder_id(settings.drive_folder_id)
    return settings
