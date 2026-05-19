from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AUTH_PATH = Path.home() / ".config" / "gopro-upload" / "gopro_auth.json"


@dataclass
class GoProAuth:
    access_token: str
    user_id: str


def load_gopro_auth(
    *,
    config_token: str = "",
    config_user_id: str = "",
    auth_path: Path | None = None,
) -> GoProAuth | None:
    token = os.environ.get("GOPRO_ACCESS_TOKEN") or config_token
    user_id = os.environ.get("GOPRO_USER_ID") or config_user_id
    if token and user_id:
        return GoProAuth(access_token=token, user_id=user_id)
    path = auth_path or DEFAULT_AUTH_PATH
    if path.exists():
        data = json.loads(path.read_text())
        if data.get("access_token") and data.get("user_id"):
            return GoProAuth(
                access_token=data["access_token"],
                user_id=data["user_id"],
            )
    return None


def save_gopro_auth(auth: GoProAuth, auth_path: Path | None = None) -> Path:
    path = auth_path or DEFAULT_AUTH_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"access_token": auth.access_token, "user_id": auth.user_id},
            indent=2,
        )
    )
    path.chmod(0o600)
    return path
